"""`ratchet review`: run Claude Code's built-in /code-review, keep its raw output, and hand it to the ingest path."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import RatchetError, RoundCapReached, RoundKeyUnavailable
from .fsutil import read_text, write_text
from .git import commit_sha, current_branch, merge_base_with_head, origin_head_branch
from .ledger import REVIEW_SOURCES, Event, FindingState, open_findings
from .rules import load_rules

LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_LEVEL = "high"
# Tests (and users with a wrapper) point this at another executable; the value is split like a shell command line.
CLAUDE_CMD_ENV = "RATCHET_CLAUDE_CMD"
# Set to "1" for the `claude -p` that `run_claude` starts. A headless session loads user and plugin hooks too, so a hook that
# runs inside this review can see it is ratchet's own and must not start another one.
REVIEW_CHILD_ENV = "RATCHET_REVIEW_CHILD"
REVIEWS_SUBDIR = Path(".ratchet") / "reviews"
TIMEOUT_SECONDS = 1800  # a max-level review is minutes; half an hour means it is stuck
VERSION_TIMEOUT_SECONDS = 10  # `claude --version` answers at once; ten seconds means the command is stuck
SUMMARY_WIDTH = 60
FORK_POINT_LENGTH = 12  # hex digits of the fork point kept in a round key
REVIEWER = "claude-code"  # the `reviewer` of a review_run that ratchet ran itself
NO_COMMIT = "none"  # `head` on a branch with no commit yet: the word a finding's `sha` uses for the same state

# The run id is also the file name of the saved output, so it is checked with fullmatch: `$` alone also holds before a final newline.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def review_run_ids(events: list[Event], root: Path) -> set[str]:
    """Every review run the repo knows: those with findings in the ledger, plus those whose raw output was saved.

    The second kind matters: a review that found nothing (or whose output would not parse) leaves a saved file and no
    ledger row, and its number must not be handed out again.
    """
    in_ledger = {event["run_id"] for event in events if event["kind"] == "finding" and event["source"] in REVIEW_SOURCES}
    saved = {path.stem for path in (root / REVIEWS_SUBDIR).glob("*.txt")}
    return in_ledger | saved


def default_run_id(known_runs: set[str]) -> str:
    """`r<k>`, k = 1 + the number of distinct runs so far: short enough to type in a `Fixes-Finding:` trailer.

    Not made collision-proof on purpose: if `r<k>` is already taken (an explicit `--run r3` earlier), recording it fails
    with the usual refusal to reuse an id for a different review rather than quietly skipping to another number.
    """
    return f"r{len(known_runs) + 1}"


def check_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise RatchetError(f"run id {run_id!r} must match {_RUN_ID_RE.pattern}")
    return run_id


def _branch_commit(root: Path, name: str) -> str | None:
    """The commit a local branch `name` points at, else the one origin's `name` does (a clone that never checked it out)."""
    return commit_sha(root, f"refs/heads/{name}") or commit_sha(root, f"refs/remotes/origin/{name}")


def default_branch(root: Path, configured: str | None) -> tuple[str, str]:
    """(name, commit) of the branch changes are measured from: the configured one, else origin/HEAD's target, else main, else master."""
    if configured is not None:
        commit = _branch_commit(root, configured)
        if commit is None:
            raise RatchetError(f"config default_branch {configured!r} is not a branch of this repo")
        return configured, commit
    for name in (origin_head_branch(root), "main", "master"):
        commit = _branch_commit(root, name) if name else None
        if commit is not None:
            return name, commit
    raise RoundKeyUnavailable("cannot tell which branch this one forked from (no origin/HEAD, main or master): "
                              "set default_branch in .ratchet.json, or pass --target",
                              "no default branch found (no origin/HEAD, main or master)")


def checked_out_default_branch(root: Path, configured: str | None) -> tuple[str, str] | None:
    """`default_branch`'s (name, commit) when HEAD is on that branch, where the fork point is HEAD itself. None on any other
    branch, on a detached HEAD, and when there is no default branch to tell."""
    branch = current_branch(root)
    try:
        name, commit = default_branch(root, configured)
    except RoundKeyUnavailable:  # no origin/HEAD, main or master
        return None
    return (name, commit) if branch == name else None


def round_key(root: Path, target: str | None, configured_default: str | None) -> str:
    """Names the change under review, so its review rounds can be counted.

    `target:<T>` with --target. Otherwise `<branch>@<fork point>`: the first 12 hex of where the branch forked from the
    default branch. On the default branch itself the fork point is HEAD, so every commit there is a change of its own;
    merging the default branch into a feature branch moves the fork point and starts the count over.

    `root` is the worktree under review, not necessarily the main one: a linked worktree has its own HEAD and branch, so
    its own key.
    """
    if target is not None:
        return f"target:{target.strip()}"
    branch = current_branch(root)
    if branch is None:
        raise RoundKeyUnavailable("HEAD is detached, so there is no branch to count review rounds on: check out a branch, "
                                  "or pass --target <T> (not allowed with --from-file)", "detached HEAD")
    head = commit_sha(root, "HEAD")
    if head is None:
        return f"{branch}@unborn"
    default_name, default_commit = default_branch(root, configured_default)
    fork_point = merge_base_with_head(root, default_commit)  # on the default branch itself that is HEAD
    if fork_point is None:
        raise RoundKeyUnavailable(f"{branch} shares no history with {default_name}, so its fork point is unknown: pass --target",
                                  f"{branch} shares no history with {default_name}")
    return f"{branch}@{fork_point[:FORK_POINT_LENGTH]}"


def review_rounds_used(events: list[Event], key: str) -> int:
    """Review rounds already recorded for `key`. Ledgers from before the cap have none, so those reviews do not count."""
    return sum(1 for event in events if event["kind"] == "review_run" and event["round_key"] == key)


def review_run_event(repo: str, ts: str, *, run_id: str, key: str, round_number: int, max_rounds: int, over_cap: bool,
                     finding_count: int, level: str | None, head: str, reviewer_version: str | None,
                     version_changed: bool, from_file: bool, tree: str | None = None) -> Event:
    """One review round. A --from-file review does not know its reviewer, version or level: those are recorded as null.

    `head` is the commit HEAD pointed at before the reviewer started (for --from-file, when the file was read).
    `tree` is the id of the working tree as it was then, uncommitted work included; it is left out when there is none.
    `version_changed` is written only when true.
    """
    event: Event = {"v": 1, "kind": "review_run", "repo": repo, "ts": ts, "run": run_id, "round_key": key,
                    "round": round_number, "max_rounds": max_rounds, "over_cap": over_cap,
                    "finding_count": finding_count, "source": "file" if from_file else "claude",
                    "reviewer": None if from_file else REVIEWER, "reviewer_version": reviewer_version, "level": level,
                    "head": head}
    if tree is not None:
        event["tree"] = tree
    if version_changed:
        event["version_changed"] = True
    return event


def replaced_critic_version(events: list[Event], reviewer_version: str | None) -> str | None:
    """The critic version this review replaces: the latest one a review_run recorded, if `reviewer_version` differs from it.

    None means nothing to announce: this review has no version, none was recorded before, or it is the same one. A
    review_run without a version (read with --from-file, or `--version` failed) is skipped, not taken as "the previous".
    """
    if reviewer_version is None:
        return None
    earlier = next((event["reviewer_version"] for event in reversed(events)
                    if event["kind"] == "review_run" and event.get("reviewer_version") is not None), None)
    return earlier if earlier != reviewer_version else None


def critic_version_counts(events: list[Event], state: dict[str, FindingState]) -> list[tuple[str | None, int, int]]:
    """[(critic version, findings, open)] for the review findings in `state`, versions in the order they first reviewed.

    A finding has the version recorded by the review_run of its run. The row with version None collects review findings
    whose run recorded none (an older ledger, a --from-file review, an unreadable version) and comes last. Empty unless
    findings come from two or more distinct versions: with one there is nothing to tell apart. Gate findings are not the
    critic's and are left out. The rows are a listing, not a comparison: a different critic is a different instrument.
    """
    version_of_run: dict[str, str | None] = {}
    version_order: list[str] = []
    for event in events:
        if event["kind"] != "review_run" or event["run"] in version_of_run:
            continue
        version = event.get("reviewer_version")
        version_of_run[event["run"]] = version
        if version is not None and version not in version_order:
            version_order.append(version)
    tallies: dict[str | None, list[int]] = {}  # version -> [findings, open]
    for item in state.values():
        if item.finding["source"] in REVIEW_SOURCES:
            tally = tallies.setdefault(version_of_run.get(item.finding["run_id"]), [0, 0])
            tally[0] += 1
            tally[1] += 1 if item.outcome is None else 0
    versions = [version for version in version_order if version in tallies]
    if len(versions) < 2:
        return []
    rows: list[tuple[str | None, int, int]] = [(version, *tallies[version]) for version in versions]
    if None in tallies:
        rows.append((None, *tallies[None]))
    return rows


def check_target(target: str | None) -> None:
    if target is not None and ("\n" in target or not target.strip()):
        raise RatchetError("--target must be a single non-empty line")


def claude_executable() -> list[str]:
    """The review command without its arguments: `claude`, or what RATCHET_CLAUDE_CMD names."""
    return shlex.split(os.environ.get(CLAUDE_CMD_ENV) or "claude")


def claude_command(level: str, target: str | None) -> list[str]:
    check_target(target)
    prompt = f"/code-review {level}" + (f" {target.strip()}" if target else "")
    return [*claude_executable(), "-p", prompt]


def require_claude(argv: list[str]) -> None:
    """Refuse a review whose command is not installed, before anything is captured, asked or billed."""
    if shutil.which(argv[0]) is None:
        raise RatchetError(f"`{argv[0]}` not found on PATH; install Claude Code, or run the review elsewhere "
                           "and pass its saved output with --from-file FILE")


# Why a review would not start (the `kind` of a Refusal), in the order they are checked.
REFUSE_RULES, REFUSE_ROUND_KEY, REFUSE_ROUND_CAP, REFUSE_CLAUDE = "rules", "round_key", "round_cap", "claude"


@dataclass(frozen=True)
class Refusal:
    """A review that would not start, found before anything is captured, asked or billed."""
    kind: str  # a REFUSE_* word
    round_key: str | None  # the change the round would be counted on; None when the rules or the key itself failed first
    message: str

    def error(self) -> RatchetError:
        """What `ratchet review` raises for it: exit code 3 for a full round cap, 1 for the rest."""
        return (RoundCapReached if self.kind == REFUSE_ROUND_CAP else RatchetError)(self.message)


def round_cap_message(key: str, used: int, max_rounds: int, open_count: int) -> str:
    return (f"round cap reached for {key}: {used}/{max_rounds} rounds used; {open_count} findings "
            "still open (ratchet count). Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded).")


def review_refusal(config: Config, events: list[Event], *, target: str | None, from_file: bool, over_cap_allowed: bool,
                   replayed: bool) -> Refusal | None:
    """Why `ratchet review` would refuse to start, else None. Reads the rules files, git and the PATH; writes nothing.

    The checks `cmd_review` makes before it calls claude, in its order: the rules load, the change gets a round key, the round
    cap leaves room (a replay and --over-cap are exempt), the claude command exists (a --from-file review runs none). A wrap-up
    asks ahead of time because a refused round records no review_run, so the wrap-up's base never moves and the next one
    would be refused the same way.

    `target` has passed `check_target`. `events` are the ledger's; the change under review is the one in `config.worktree`.
    """
    try:
        load_rules(config)
    except RatchetError as exc:
        return Refusal(REFUSE_RULES, None, str(exc))
    try:
        key = round_key(config.worktree, target, config.default_branch)
    except RatchetError as exc:  # no branch to count on (RoundKeyUnavailable), or a default_branch that is not a branch
        return Refusal(REFUSE_ROUND_KEY, None, str(exc))
    used = review_rounds_used(events, key)
    if used + 1 > config.max_review_rounds and not over_cap_allowed and not replayed:
        return Refusal(REFUSE_ROUND_CAP, key, round_cap_message(key, used, config.max_review_rounds, len(open_findings(events))))
    if from_file:
        return None
    argv = claude_command(DEFAULT_LEVEL, target)  # the level is no part of argv[0], which is all `require_claude` looks at
    try:
        require_claude(argv)
    except RatchetError as exc:
        return Refusal(REFUSE_CLAUDE, key, str(exc))
    return None


def _run_command(argv: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> str:
    """The command's stdout. stdin is closed so a headless `claude -p` never waits for input. `env` None: the caller's own."""
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RatchetError(f"`{shlex.join(argv)}` did not finish within {timeout}s") from exc
    except OSError as exc:
        raise RatchetError(f"cannot run `{argv[0]}`: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise RatchetError(f"`{shlex.join(argv)}` exited with {proc.returncode}" + (f":\n{tail}" if tail else ""))
    return proc.stdout


def run_claude(argv: list[str], cwd: Path) -> str:
    return _run_command(argv, cwd, TIMEOUT_SECONDS, {**os.environ, REVIEW_CHILD_ENV: "1"})


def critic_version(cwd: Path) -> str:
    """What the reviewer reports as its version: the stripped stdout of `<claude command> --version`.

    Asked of the very command the review runs (a RATCHET_CLAUDE_CMD wrapper answers for itself), and before the review
    starts: an installation that updates itself while a long review runs would otherwise be recorded under a version
    that did not run it.
    """
    argv = [*claude_executable(), "--version"]
    version = _run_command(argv, cwd, VERSION_TIMEOUT_SECONDS).strip()
    if not version:
        raise RatchetError(f"`{shlex.join(argv)}` printed nothing")
    return version


def review_path(root: Path, run_id: str) -> Path:
    return root / REVIEWS_SUBDIR / f"{run_id}.txt"


def save_review(root: Path, run_id: str, text: str) -> Path:
    """Write the raw output under .ratchet/reviews/. The same text again is a no-op; a different text under a used run id is refused."""
    path = review_path(root, run_id)
    existing = read_text(path)
    if existing is None:
        write_text(path, text)
    elif existing != text:
        raise RatchetError(f"{path} already holds a different review; pass another --run")
    return path


def _truncate(text: str, width: int) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= width else one_line[:width - 1].rstrip() + "…"


def findings_table(events: list[Event]) -> list[str]:
    """Aligned lines: id, rule_id, severity, file:line, summary."""
    rows = [("id", "rule_id", "severity", "location", "summary")]
    rows += [(event["display_id"], event["rule_id"], event["severity"], f"{event['file']}:{event['line']}",
              _truncate(event["summary"], SUMMARY_WIDTH)) for event in events]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4] for row in rows]
