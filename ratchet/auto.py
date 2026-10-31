"""The automatic wrap-up's record keeping: which commits were made in a Claude Code session, and where the Stop hook finds them.

The git post-commit hook runs `ratchet auto note` after a commit. When the commit was made inside a Claude Code session (the
session id is in the environment) and the repo asked for the automatic wrap-up (`wrapup_auto` in its config), `note_commit`
appends one line to `pending/<session id>.jsonl`. When the turn ends, the plugin's Stop hook (`ratchet auto stop`) takes that file
and judges the work; the commits of a worktree it could not judge it puts back (`append_pending`).

The files live in a per-machine state directory, never in a repo: a line names a worktree path, a commit and a session, which are
private to this machine and must not reach a file a repo tracks. The note runs after every commit in a session, so it checks the
environment first and asks git only once it knows a session is there.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_NAME, read_config_file
from .git import commit_sha, main_root, worktree_root
from .review import REVIEW_CHILD_ENV

STATE_DIR_ENV = "RATCHET_STATE_DIR"
XDG_STATE_ENV = "XDG_STATE_HOME"
# Claude Code puts this in the environment of its Bash tool, its hooks and its subagents; a subagent has the main session's id.
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
STATE_NAME = "review-ratchet"
PENDING_NAME = "pending"
CLAIMED_NAME = "claimed"
HOOK_LOG_NAME = "hook.log"
KEEP_SECONDS = 7 * 24 * 60 * 60  # a file nothing has written to for this long belongs to a session that is over

# The id becomes a file name, so it is matched whole (`fullmatch`, as `$` alone also holds before a final newline) against letters,
# digits, `-` and `_`: no `/`, no `..`, no length that a file system refuses.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


# ---- where things are ---------------------------------------------------------------------------------------------

def state_dir() -> Path:
    """Where this machine keeps what the automatic wrap-up records: $RATCHET_STATE_DIR, else $XDG_STATE_HOME/review-ratchet, else
    ~/.local/state/review-ratchet. A variable that is set but empty counts as unset (as the XDG rules say): an empty path would put
    the files in the current directory, which in a git hook is the repo. hooks/run.sh finds the pending file by this same order, in shell,
    before it starts Python: change one and the other (RunShTest pins them together)."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    xdg_base = os.environ.get(XDG_STATE_ENV)
    return (Path(xdg_base) if xdg_base else Path.home() / ".local" / "state") / STATE_NAME


def pending_dir() -> Path:
    return state_dir() / PENDING_NAME


def claimed_dir() -> Path:
    """Where a pending file goes while the Stop hook judges it, so a second Stop does not read it again."""
    return state_dir() / CLAIMED_NAME


def is_session_id(value: str) -> bool:
    return _SESSION_ID_RE.fullmatch(value) is not None


def session_id() -> str | None:
    """The id of the Claude Code session this process runs in; None outside one, or when the id could not be a file name."""
    value = os.environ.get(SESSION_ID_ENV, "")
    return value if is_session_id(value) else None


def pending_path(session: str) -> Path:
    assert is_session_id(session), f"{session!r} is not a session id; check it with is_session_id before it names a file"
    return pending_dir() / f"{session}.jsonl"


# ---- the record ---------------------------------------------------------------------------------------------------

def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PendingCommit:
    """One line of a pending file: a commit made in a Claude Code session."""

    worktree: Path  # the absolute top of the worktree the commit was made in; a linked worktree's own, not the main one's
    head: str  # the full sha of the commit
    ts: str  # when it was noted: UTC, ISO 8601, to the second

    def to_line(self) -> str:
        return json.dumps({"worktree": str(self.worktree), "head": self.head, "ts": self.ts}) + "\n"

    @classmethod
    def from_line(cls, line: str) -> PendingCommit:
        """Raises ValueError for a line that is not a record (a JSON object with those three fields, all strings)."""
        fields = json.loads(line)
        if not isinstance(fields, dict) or not all(isinstance(fields.get(key), str) for key in ("worktree", "head", "ts")):
            raise ValueError("not a pending record: expected a JSON object with the strings worktree, head and ts")
        return cls(Path(fields["worktree"]), fields["head"], fields["ts"])


def read_pending(path: Path) -> list[PendingCommit]:
    """The commits a pending file records, in the order they were made. A line that is not a record is skipped and told in hook.log:
    one damaged line must not hide the commits around it."""
    commits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            commits.append(PendingCommit.from_line(line))
        except ValueError as exc:
            log_failure(f"{path}:{number}: skipped a line: {exc}")
    return commits


# ---- writing ------------------------------------------------------------------------------------------------------

def make_state_dir(subdir: str | None = None) -> Path:
    """The state directory, or `subdir` in it, made with mode 0700 if it is missing. The state directory's own missing parents get the
    default mode: `mkdir(parents=True)` would give the state directory that mode too."""
    state = state_dir()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.mkdir(mode=0o700, exist_ok=True)
    if subdir is None:
        return state
    directory = state / subdir
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _append(path: Path, text: str) -> None:
    """Add `text` to the end of the file with one write on a descriptor opened for appending. The system puts such a write at the end of
    the file whole, so notes made at the same moment (the main agent and a subagent committing together) never mix their lines."""
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)


def append_pending(session: str, commits: list[PendingCommit]) -> None:
    """Add `commits` to the end of the session's pending file in one write, as a note is, so a note made at the same moment stays whole.
    The Stop hook puts back the commits of a worktree it could not judge this way. A note made since it claimed the file is in the new
    one already and these follow it, so the file is then not in the order the commits were made. `commits` is never empty: the write
    would still make the file, and a pending file with nothing in it starts the Stop hook for nothing."""
    assert commits, "nothing to add: the write would still make the pending file"
    _append(pending_path(session), "".join(commit.to_line() for commit in commits))


def log_failure(message: str) -> None:
    """Add one line to hook.log in the state directory. The hooks print nothing, so this is the only place a failure is told."""
    line = f"{utc_timestamp()} {' '.join(message.split())}\n"  # one line, whatever the message holds
    with contextlib.suppress(OSError, RuntimeError):  # hook.log is the last place to tell; RuntimeError: no home directory to put it in
        _append(make_state_dir() / HOOK_LOG_NAME, line)


def prune() -> None:
    """Delete the pending and claimed files nothing has written to for a week. A session that ends without a Stop (a crash, a /clear
    that gives the next turn a new id) leaves its file behind, and nothing else would ever remove it."""
    cutoff = time.time() - KEEP_SECONDS
    for path in (*pending_dir().glob("*"), *claimed_dir().glob("*")):
        with contextlib.suppress(FileNotFoundError):  # another note pruned it between the listing and now
            if path.stat().st_mtime < cutoff:
                path.unlink()


# ---- `ratchet auto note` ------------------------------------------------------------------------------------------

def _wrapup_auto_on(root: Path) -> bool:
    """Whether the config in the main worktree says `"wrapup_auto": true`. No config, or any other value, is off."""
    path = root / CONFIG_NAME
    return path.exists() and read_config_file(path).get("wrapup_auto") is True


def _note(repo: Path | None) -> None:
    session = session_id()
    if session is None or os.environ.get(REVIEW_CHILD_ENV) == "1":
        return
    start = repo if repo is not None else Path.cwd()
    if not _wrapup_auto_on(main_root(start)):
        return
    top = worktree_root(start)
    head = commit_sha(top, "HEAD")
    if head is None:
        return
    make_state_dir(PENDING_NAME)
    _append(pending_path(session), PendingCommit(top, head, utc_timestamp()).to_line())
    prune()  # last: the write above made this session's own file fresh, so a session quiet for a week keeps its old lines


def raised_at(exc: BaseException) -> str:
    """` (at <file>:<line>)` for where `exc` was raised: the innermost frame of its traceback, not the one that caught it."""
    innermost = exc.__traceback__
    while innermost.tb_next is not None:
        innermost = innermost.tb_next
    return f" (at {innermost.tb_frame.f_code.co_filename}:{innermost.tb_lineno})"


def note_commit(repo: Path | None) -> None:
    """Record the commit just made in `repo` (default: the current directory), if it belongs to a Claude Code session whose repo asked for
    the automatic wrap-up. Prints nothing and cannot fail: a hook that broke or cluttered every commit would be switched off, so a
    failure is one line in hook.log, with where it was raised."""
    try:
        _note(repo)
    except Exception as exc:  # side channel: bookkeeping beside a commit that already exists must neither fail it nor print into it
        log_failure(f"auto note: {type(exc).__name__}: {exc}{raised_at(exc)}")
