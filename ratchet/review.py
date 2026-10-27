"""`ratchet review`: run Claude Code's built-in /code-review, keep its raw output, and hand it to the ingest path."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .errors import RatchetError, RoundKeyUnavailable
from .fsutil import read_text, write_text
from .git import commit_sha, current_branch, merge_base_with_head, origin_head_branch
from .ledger import REVIEW_SOURCES, Event

LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_LEVEL = "high"
# Tests (and users with a wrapper) point this at another executable; the value is split like a shell command line.
CLAUDE_CMD_ENV = "RATCHET_CLAUDE_CMD"
REVIEWS_SUBDIR = Path(".ratchet") / "reviews"
TIMEOUT_SECONDS = 1800  # a max-level review is minutes; half an hour means it is stuck
SUMMARY_WIDTH = 60
FORK_POINT_LENGTH = 12  # hex digits of the fork point kept in a round key

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # also the file name of the saved output


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
    if not _RUN_ID_RE.match(run_id):
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
                              "set default_branch in .ratchet.json, or pass --target")


def round_key(root: Path, target: str | None, configured_default: str | None) -> str:
    """Names the change under review, so its review rounds can be counted.

    `target:<T>` with --target. Otherwise `<branch>@<fork point>`: the first 12 hex of where the branch forked from the
    default branch. On the default branch itself the fork point is HEAD, so every commit there is a change of its own;
    merging the default branch into a feature branch moves the fork point and starts the count over.
    """
    if target is not None:
        return f"target:{target.strip()}"
    branch = current_branch(root)
    if branch is None:
        raise RoundKeyUnavailable("HEAD is detached, so there is no branch to count review rounds on: check out a branch, "
                                  "or pass --target <T> (not allowed with --from-file)")
    head = commit_sha(root, "HEAD")
    if head is None:
        return f"{branch}@unborn"
    default_name, default_commit = default_branch(root, configured_default)
    fork_point = merge_base_with_head(root, default_commit)  # on the default branch itself that is HEAD
    if fork_point is None:
        raise RoundKeyUnavailable(f"{branch} shares no history with {default_name}, so its fork point is unknown: pass --target")
    return f"{branch}@{fork_point[:FORK_POINT_LENGTH]}"


def review_rounds_used(events: list[Event], key: str) -> int:
    """Review rounds already recorded for `key`. Ledgers from before the cap have none, so those reviews do not count."""
    return sum(1 for event in events if event["kind"] == "review_run" and event["round_key"] == key)


def review_run_event(repo: str, ts: str, *, run_id: str, key: str, round_number: int, max_rounds: int, over_cap: bool,
                     finding_count: int, from_file: bool) -> Event:
    return {"v": 1, "kind": "review_run", "repo": repo, "ts": ts, "run": run_id, "round_key": key, "round": round_number,
            "max_rounds": max_rounds, "over_cap": over_cap, "finding_count": finding_count,
            "source": "file" if from_file else "claude"}


def check_target(target: str | None) -> None:
    if target is not None and ("\n" in target or not target.strip()):
        raise RatchetError("--target must be a single non-empty line")


def claude_command(level: str, target: str | None) -> list[str]:
    check_target(target)
    prompt = f"/code-review {level}" + (f" {target.strip()}" if target else "")
    return [*shlex.split(os.environ.get(CLAUDE_CMD_ENV) or "claude"), "-p", prompt]


def run_claude(argv: list[str], cwd: Path) -> str:
    """The command's stdout. stdin is closed so a headless `claude -p` never waits for input."""
    if shutil.which(argv[0]) is None:
        raise RatchetError(f"`{argv[0]}` not found on PATH; install Claude Code, or run the review elsewhere "
                           "and pass its saved output with --from-file FILE")
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RatchetError(f"`{shlex.join(argv)}` did not finish within {TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise RatchetError(f"cannot run `{argv[0]}`: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise RatchetError(f"`{shlex.join(argv)}` exited with {proc.returncode}" + (f":\n{tail}" if tail else ""))
    return proc.stdout


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
