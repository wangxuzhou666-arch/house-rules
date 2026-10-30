"""`ratchet wrapup`: count what changed since the last review, and review only when enough did.

Everything here is deterministic (git and the ledger, no model). Running the review itself is `cmd_wrapup`'s job.
"""
from __future__ import annotations

import os
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .digest import display_path
from .errors import RatchetError, RoundKeyUnavailable
from .git import commit_sha, current_branch, merge_base_with_head, run_git
from .ledger import SEVERITIES, UNMAPPED, Event, effective_rule_id, fold
from .proposals import CHANGELOG
from .review import NO_COMMIT, default_branch
from .state_tree import numstat_since, tree_exists

# Why a base was chosen: the first rule in docs/WRAPUP.md that held.
BASE_FLAG = "--base"
BASE_LAST_REVIEW_TREE = "last-review-tree"
BASE_LAST_REVIEW = "last-review"
BASE_MERGE_BASE = "merge-base"
BASE_HEAD = "head"
SHA_WIDTH = 12  # hex digits of a sha shown to a person, as in a round key
BINARY_PROBE_BYTES = 8000  # git calls a file binary when a NUL byte is in its first 8000 bytes
READ_CHUNK_BYTES = 1 << 20
SUMMARY_WIDTH = 72
CODE_SNIPPET_SOURCES = ("review", "worktree")  # the snippet_sources whose snippet `snippet_around` cut; `add` records any text at all

# Statuses a wrap-up ends in. "dry-run" and "reported" are the two that review nothing.
SKIPPED, REVIEWED, CAPPED, DRY_RUN, REPORTED = "skipped", "reviewed", "capped", "dry-run", "reported"


@dataclass(frozen=True)
class Base:
    sha: str | None  # a commit, or for BASE_LAST_REVIEW_TREE a tree; None on a branch with no commit yet: every file counts as new
    reason: str  # one of the BASE_* words
    note: str  # the rule that chose it, in a sentence part
    caveat: str = ""  # what that rule cannot see, said as a line of its own; empty when it misses nothing


@dataclass(frozen=True)
class Reviewed:
    head: str  # HEAD when the review started
    run: str
    tree: str | None  # the working tree when it started; None for a review recorded without one


@dataclass(frozen=True)
class Changes:
    lines: int  # lines added plus lines deleted
    files: int  # distinct paths


@dataclass(frozen=True)
class Thresholds:
    min_changed_lines: int
    min_files: int


# ---- the base -------------------------------------------------------------------------------------------------

def choose_base(worktree: Path, events: list[Event], explicit: str | None, configured_default: str | None) -> Base:
    """What the work is measured from: `--base`, else the working tree of the last review of this branch (its HEAD when it kept
    none), else the fork point, else HEAD."""
    if explicit is not None:
        explicit_sha = commit_sha(worktree, explicit)
        if explicit_sha is None:
            raise RatchetError(f"--base {explicit!r} is not a commit of this repo")
        return Base(explicit_sha, BASE_FLAG, f"--base {explicit}")
    head = commit_sha(worktree, "HEAD")
    if head is None:
        return Base(None, BASE_HEAD, "no commit yet, so every file counts as new")
    reviewed = _last_reviewed(worktree, events)
    if reviewed is not None:
        return _review_base(worktree, reviewed)
    fork = _fork_point(worktree, configured_default)
    if fork is not None:
        fork_sha, default_name = fork
        return Base(fork_sha, BASE_MERGE_BASE, f"where this branch forked from {default_name}")
    return Base(head, BASE_HEAD, "HEAD: no earlier review and no fork point found, so only uncommitted changes count; "
                                 "pass --base REF to count committed work too")


def round_key_branch(key: str) -> str | None:
    """The branch a round key names, `feature` for `feature@0a1b2c3d4e5f` (see `review.round_key`). None for `target:<T>`,
    which names a change and no branch.

    What follows the last `@` is a fork point (12 hex digits, or `unborn`) and has none in it, so the name ends there even
    when it has an `@` of its own. A branch name cannot hold `:`, so the `target:` form is never mistaken for one.
    """
    if key.startswith("target:"):
        return None
    return key.rpartition("@")[0] or None  # no `@` leaves nothing before it either


def _last_reviewed(worktree: Path, events: list[Event]) -> Reviewed | None:
    """The newest review of the branch HEAD is on whose starting HEAD this HEAD descends from.

    The ledger is shared by every worktree of the repo, and a review's tree holds the uncommitted work of the worktree it
    ran in. Another branch's review, the main worktree's say, would count that work here as change. Two worktrees never
    stand on one branch, so the branch tells their reviews apart. On a detached HEAD there is no branch, so no review is
    the last one; neither is a `--target` review, whose round key names no branch.

    A review without a usable head (`none` on a branch with no commit, or an event from before the field existed) says
    nothing about which commits it saw. One whose head is not an ancestor (history a rebase rewrote) is passed over for an
    older one that is. The newest such review decides, whether or not it recorded a tree: an older review's tree would
    count again everything the newer one read.
    """
    branch = current_branch(worktree)
    if branch is None:
        return None
    for event in reversed(events):
        if event["kind"] != "review_run" or round_key_branch(event["round_key"]) != branch:
            continue
        head = event.get("head")
        if head not in (None, NO_COMMIT) and _is_ancestor(worktree, head):
            return Reviewed(head, event["run"], event.get("tree"))
    return None


def _review_base(worktree: Path, reviewed: Reviewed) -> Base:
    """The working tree as the review found it, which counts only what happened after it; else the review's HEAD, and why.

    The HEAD is a commit, which holds none of the work the review read while it was uncommitted: that counts again.
    """
    from_head = f"HEAD when review {reviewed.run} started"
    if reviewed.tree is None:
        return Base(reviewed.head, BASE_LAST_REVIEW, from_head,
                    f"review {reviewed.run} recorded no tree, so work it left uncommitted counts again")
    if not tree_exists(worktree, reviewed.tree):
        return Base(reviewed.head, BASE_LAST_REVIEW, from_head,
                    f"review {reviewed.run}'s tree {reviewed.tree[:SHA_WIDTH]} is no longer in this repo (git gc removes "
                    "objects nothing refers to), so work it left uncommitted counts again")
    return Base(reviewed.tree, BASE_LAST_REVIEW_TREE, f"the working tree as review {reviewed.run} found it")


def _is_ancestor(worktree: Path, commit: str) -> bool:
    """Whether HEAD descends from `commit`. A commit this clone no longer has (pruned after a rebase) is not an ancestor."""
    if commit_sha(worktree, commit) is None:
        return False
    return run_git(["merge-base", "--is-ancestor", commit, "HEAD"], worktree, allowed_returncodes=(0, 1)).returncode == 0


def _fork_point(worktree: Path, configured_default: str | None) -> tuple[str, str] | None:
    """(commit, default branch name) where HEAD forked from the default branch, found the way a round key finds it."""
    try:
        default_name, default_commit = default_branch(worktree, configured_default)
    except RoundKeyUnavailable:  # no origin/HEAD, main or master: the case `review` also reports as "no default branch"
        return None
    fork = merge_base_with_head(worktree, default_commit)
    return None if fork is None else (fork, default_name)


# ---- how much changed -----------------------------------------------------------------------------------------

def count_changes(worktree: Path, base: Base, state_paths: Sequence[str]) -> Changes:
    """Lines and files that differ from `base`. A base that is a recorded tree is compared with the tree of the working tree
    as it is now, made the same way (`numstat_since`, which leaves no object in the repo), so untracked files are on both
    sides and a file that was read while uncommitted and committed since is the same file. Any other base is a commit:
    tracked files as `git diff` sees them (committed, staged and unstaged), plus the untracked files git does not ignore,
    whose lines are counted by reading them.

    `state_paths` are the files ratchet itself writes, left out of both counts by the one test `_is_state`: the runtime files
    (`onboard.ignore_entries`) and the files a rule change writes (`rule_change_entries`). Left to `.gitignore` alone, a repo
    that does not ignore the first would count every review's own output as work and trigger the next wrap-up by itself; the
    second are committed, so no `.gitignore` could help, and adopting one rule would count its text three times (rules
    file, digest, changelog).
    """
    if base.reason == BASE_LAST_REVIEW_TREE:
        changed = _numstat_lines(numstat_since(worktree, base.sha))
        files = {path: lines for path, lines in changed.items() if not _is_state(path, state_paths)}
        return Changes(sum(files.values()), len(files))
    since = base.sha or _empty_tree(worktree)
    files = {path: lines for path, lines in _numstat_lines(run_git(["diff", "--numstat", "-z", since], worktree).stdout).items()
             if not _is_state(path, state_paths)}
    for name in run_git(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout.split("\0"):
        if name and not _is_state(name, state_paths):
            files[name] = _text_lines(worktree / name)
    return Changes(sum(files.values()), len(files))


def rule_change_entries(config: Config) -> list[str]:
    """The files a rule change writes, as paths from the repo root: the rules files, the digest compiled from them, and the changelog.

    Not work for the next review to read: the digest is generated, and the changelog restates the clause the rules file
    now holds. Inject targets are not here: they are the user's own files that carry a generated block. The changelog is the
    one at the repo root that `apply-proposals` writes, not a file of that name elsewhere. A file outside the repo gets a
    path that starts with `..`, which no git path does, so it never matches.
    """
    written = [*config.rules_files, config.digest_path, config.root / CHANGELOG]
    return [display_path(path, config.root) for path in written]


def _is_state(path: str, state_paths: Sequence[str]) -> bool:
    """Whether `path` is one of `state_paths` or lies under one that names a directory (`.ratchet/`)."""
    return any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in state_paths)


def _empty_tree(worktree: Path) -> str:
    """The empty tree in this repo's hash format: what a branch with no commit yet is measured from."""
    return run_git(["hash-object", "-t", "tree", os.devnull], worktree).stdout.strip()


def _numstat_lines(raw: str) -> dict[str, int]:
    """{path: lines added + deleted} from `git diff --numstat -z`. A binary file shows `-` for both counts: no lines, one path."""
    per_path: dict[str, int] = {}
    records = iter(raw.split("\0"))
    for record in records:
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        if not path:  # a rename or copy: the old and the new name follow as two more NUL-separated fields
            next(records)
            path = next(records)
        per_path[path] = sum(0 if count == "-" else int(count) for count in (added, deleted))
    return per_path


def _text_lines(path: Path) -> int:
    """Lines of a regular file git would not call binary. A binary, a symlink or an embedded repo's directory has none."""
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        return _count_newlines(path)
    except OSError as exc:
        raise RatchetError(f"cannot read {path} to count its lines: {exc}") from exc


def _count_newlines(path: Path) -> int:
    """Newlines in the file, plus one for a last line that has none; 0 when a NUL byte shows in its first 8000 bytes."""
    with path.open("rb") as handle:
        first = handle.read(BINARY_PROBE_BYTES)
        if b"\0" in first:
            return 0
        count, last = first.count(b"\n"), first[-1:]
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (0 if last in (b"", b"\n") else 1)


# ---- the decision ---------------------------------------------------------------------------------------------

def thresholds_reached(changes: Changes, thresholds: Thresholds) -> list[str]:
    """The thresholds the work has reached, each in words. Either one is enough; empty means too little changed to review."""
    hits = []
    if changes.lines >= thresholds.min_changed_lines:
        hits.append(f"changed lines {changes.lines} >= wrapup_min_changed_lines {thresholds.min_changed_lines}")
    if changes.files >= thresholds.min_files:
        hits.append(f"changed files {changes.files} >= wrapup_min_files {thresholds.min_files}")
    return hits


def skip_reason(changes: Changes, thresholds: Thresholds) -> str:
    return (f"changed lines {changes.lines} < wrapup_min_changed_lines {thresholds.min_changed_lines} "
            f"and changed files {changes.files} < wrapup_min_files {thresholds.min_files}")


# ---- blind spots ----------------------------------------------------------------------------------------------

def unmapped_findings(events: list[Event], legacy_unmapped_alias: frozenset[str], run: str | None = None) -> list[Event]:
    """Open findings no rule covers (the `unmapped` of `count --by-rule`), most severe first, ledger order within a severity.

    With `run`, only that review's own findings: the blind spots a review found, not the ones it inherited.
    """
    blind_spots = [item.finding for item in fold(events).values()
                   if item.outcome is None and effective_rule_id(item.finding, legacy_unmapped_alias) == UNMAPPED
                   and (run is None or item.finding["run_id"] == run)]
    return sorted(blind_spots, key=lambda finding: SEVERITIES.index(finding["severity"]))


@dataclass(frozen=True)
class BlindSpots:
    """The unmapped findings of one review run, split by whether they are worth asking about."""
    new: Sequence[Event]  # most severe first
    repeated: Sequence[Event]  # a blind spot an earlier run reported and that is still open there or was turned down
    repeat_of: Mapping[str, Event] = field(default_factory=dict)  # each repeated finding's id -> the earlier finding it repeats


NO_BLIND_SPOTS = BlindSpots((), ())

# The outcomes that turn a finding down. `fixed` is not one: the same code reported again may not be fixed after all.
REJECTED_OUTCOMES = ("false_positive", "dismissed")


def run_blind_spots(events: list[Event], legacy_unmapped_alias: frozenset[str], run: str) -> BlindSpots:
    """`run`'s open unmapped findings: the new blind spots, and the repeats of one already known from an earlier run.

    A review reads the whole branch, so a blind spot nobody fixed comes back under every new run id. A finding is a repeat
    when an earlier run's unmapped finding flags the same code (`_same_blind_spot`) and is known: still open (the question
    was asked about it, or will be), or turned down with one of REJECTED_OUTCOMES (it was answered). Only the report
    changes: the ledger keeps the repeat, which is still an open finding, and no outcome is written for it. `repeat_of` says
    which earlier finding each repeat repeats, so the report can show the pairs instead of asking to be taken on trust.
    """
    own = unmapped_findings(events, legacy_unmapped_alias, run)
    state = fold(events)
    position = {finding_id: index for index, finding_id in enumerate(state)}  # the order the ledger first saw each finding in
    known = [item.finding for item in state.values()
             if item.finding["run_id"] != run and effective_rule_id(item.finding, legacy_unmapped_alias) == UNMAPPED
             and (item.outcome is None or item.outcome["outcome"] in REJECTED_OUTCOMES)]
    originals = {finding["id"]: _original_of(finding, known, position) for finding in own}
    repeat_of = {finding_id: original for finding_id, original in originals.items() if original is not None}
    return BlindSpots([finding for finding in own if finding["id"] not in repeat_of],
                      [finding for finding in own if finding["id"] in repeat_of], repeat_of)


def _original_of(finding: Event, known: Sequence[Event], position: Mapping[str, int]) -> Event | None:
    """The earliest known blind spot, in ledger order, that `finding` repeats; None when it repeats none.

    The earliest is the one the question was first asked about, which a later repeat of it was not.
    """
    return next((other for other in known
                 if position[other["id"]] < position[finding["id"]] and _same_blind_spot(finding, other)), None)


def _same_blind_spot(finding: Event, other: Event) -> bool:
    """Same file, and the same code around the flagged line: the two snippets equal line for line, whitespace collapsed,
    wherever in the file the code has moved to.

    A snippet is the flagged line and up to three lines each side, as the review saw them (`code_window`). The flagged line
    alone is not enough: `return None` stands on many lines of one file, and a finding on the second says nothing about
    the first. Code edited near the flagged line is other code, and is asked about again.

    With no window on either side the same line number stands in; with one on one side only they are not the same code.
    The summaries are the reviewer's words and differ from run to run, so they are not compared.
    """
    if finding["file"] != other["file"]:
        return False
    window, other_window = code_window(finding), code_window(other)
    if window is None and other_window is None:
        return finding["line"] == other["line"]
    return window == other_window


def code_window(finding: Event) -> tuple[str, ...] | None:
    """The code the review saw around the flagged line: the lines of the finding's snippet, each with its whitespace
    collapsed. A snippet that was cut to fit its budget keeps its `[snippet truncated]` last line, so it equals only a
    snippet cut the same way.

    None when there is no code to compare: no snippet, a snippet `ratchet review` did not cut (`add` records any text), or
    lines that are all blank, which say nothing about the code.
    """
    snippet = finding.get("snippet")
    if snippet is None or finding.get("snippet_source") not in CODE_SNIPPET_SOURCES:
        return None
    window = tuple(" ".join(line.split()) for line in snippet.split("\n"))
    return window if any(window) else None


def review_run_events(events: list[Event]) -> list[Event]:
    return [event for event in events if event["kind"] == "review_run"]


# ---- output ---------------------------------------------------------------------------------------------------

def report(status: str, *, base: Base | None, changes: Changes | None, thresholds: Thresholds, open_unmapped: int, reason: str,
           run: str | None = None, blind_spots: BlindSpots = NO_BLIND_SPOTS) -> dict:
    """The `--json` object. base and the counts are null when nothing was measured (`--report-only`)."""
    return {
        "status": status,
        "base": None if base is None else base.sha,
        "base_reason": None if base is None else base.reason,
        "changed_lines": None if changes is None else changes.lines,
        "changed_files": None if changes is None else changes.files,
        "thresholds": {"min_changed_lines": thresholds.min_changed_lines, "min_files": thresholds.min_files},
        "run": run,
        "new_unmapped": [_blind_spot(finding) for finding in blind_spots.new],
        "repeated_unmapped": [finding["id"] for finding in blind_spots.repeated],
        "repeat_of": {finding_id: original["id"] for finding_id, original in blind_spots.repeat_of.items()},
        "open_unmapped": open_unmapped,
        "reason": reason,
    }


def _blind_spot(finding: Event) -> dict:
    return {key: finding[key] for key in ("id", "display_id", "file", "line", "severity", "summary", "failure_scenario")}


def measure_lines(base: Base, changes: Changes) -> list[str]:
    """The measurement, then what the base could not see (if anything) as a line of its own."""
    since = "the start" if base.sha is None else base.sha[:SHA_WIDTH]
    measured = f"wrapup: {changes.lines} changed line(s) in {changes.files} file(s) since {since} ({base.note})"
    return [measured, f"wrapup: note: {base.caveat}"] if base.caveat else [measured]


def open_unmapped_line(open_unmapped: int) -> str:
    return f"wrapup: open unmapped findings: {open_unmapped}"


def blind_spot_lines(run: str, blind_spots: BlindSpots, open_unmapped: int) -> list[str]:
    """What a wrap-up says about blind spots: the run's new ones, the repeats it will not ask about and which earlier finding
    each repeats, then how many are open in all."""
    repeat_rows = [f"  {finding['display_id']}  {finding['file']}:{finding['line']}  is a repeat of "
                   f"{blind_spots.repeat_of[finding['id']]['display_id']}" for finding in blind_spots.repeated]
    repeats = ([f"wrapup: {len(blind_spots.repeated)} of run {run}'s unmapped finding(s) repeat a blind spot already known from an "
                "earlier run (still open there, or marked false_positive or dismissed; same file, same code around the flagged "
                "line); not asked about again:", *repeat_rows] if blind_spots.repeated else [])
    if not blind_spots.new:
        return [f"wrapup: no new unmapped findings from run {run}", *repeats, open_unmapped_line(open_unmapped)]
    rows = [f"  {finding['display_id']}  {finding['file']}:{finding['line']}  {finding['severity']}  "
            f"{textwrap.shorten(finding['summary'], SUMMARY_WIDTH, placeholder='…')}" for finding in blind_spots.new]
    return [f"wrapup: {len(blind_spots.new)} new unmapped finding(s) from run {run}, which no rule covers:", *rows, *repeats,
            "wrapup: next, /review-ratchet:maintain drafts rule proposals from them (at most 3 per wrap-up)",
            open_unmapped_line(open_unmapped)]
