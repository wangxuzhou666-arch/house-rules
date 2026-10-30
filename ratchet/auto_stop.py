"""`ratchet auto stop`: the plugin's Stop hook. When a turn ends it judges the commits the git hook noted during the session, and when
the work since the last review has crossed the wrap-up threshold it asks Claude to run the automatic wrap-up.

It runs at the end of every turn of every session, in repos that never heard of ratchet too. So the cheap checks come first: the
environment, then the input, then one `stat` of the session's pending file, which `auto note` makes only after a commit. Only a
session with something noted pays for git and the ledger. The plugin's hooks/run.sh makes the environment and `stat` checks first, in
shell, so that a session with nothing noted does not start Python at all.

The answer is one JSON object on stdout, or nothing:
- `hookSpecificOutput.additionalContext` when the work is due and its review would start: Claude takes the turn on and runs the
  wrap-up skill with the text it is given;
- `systemMessage` when the work is due but its review would be refused: the person is told, once for each distinct refusal
  (`announced.json`), and Claude is not asked to do anything.

Every other outcome is silence with exit status 0, and so is every failure, told in hook.log: a Stop hook that failed at the end
of every turn would be switched off, and exit status 2 would keep Claude from stopping.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from .auto import (
    CLAIMED_NAME, is_session_id, log_failure, make_state_dir, pending_path, raised_at, read_pending, session_id, state_dir,
    utc_timestamp,
)
from .config import load_config
from .review import REVIEW_CHILD_ENV, Refusal

if TYPE_CHECKING:
    from .wrapup import WrapupPlan

# Claude Code sets this to 0 in a session no one is at. It is not documented: it was seen as 0 under `claude -p` (2.1.312) and as 1
# in an attended VS Code session. So only a 0 counts as headless, and anything else, or nothing, is attended.
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
HEADLESS_OK_ENV = "RATCHET_AUTO_HEADLESS"  # 1: judge in a headless session too (pipelines, the smoke test)
ANNOUNCED_NAME = "announced.json"
ANNOUNCED_KEEP = timedelta(days=30)

# Background tasks that may commit or review once the turn is over, so the judgement waits for them: a wrap-up started beside one
# would measure work that is still changing, or review beside a review. Other tasks (a dev server, a monitor) may never end, so
# they must not hold the judgement back.
_WORKING_TASK_TYPES = frozenset({"subagent", "workflow", "teammate"})
_ENDED_STATUSES = frozenset({"completed", "complete", "done", "finished", "succeeded", "failed", "error", "killed", "stopped",
                             "cancelled", "canceled", "aborted", "exited", "terminated"})  # any other status, or none, is running
_RATCHET_REVIEW_RE = re.compile(r"(?:^|[\s/])ratchet\s+(?:wrapup|review)\b")


@dataclass(frozen=True)
class Due:
    """A worktree whose work reached the wrap-up threshold."""

    worktree: Path
    root: Path  # the main worktree of its repo: it names the repo, whichever of its worktrees the commit was made in
    plan: WrapupPlan


# ---- what the input says ------------------------------------------------------------------------------------------

def _read_input(stdin: TextIO) -> dict | None:
    """The hook input: one JSON object on stdin. None for empty input, and for input that is not an object (told in hook.log)."""
    text = stdin.read()
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        log_failure(f"auto stop: the input is not JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        log_failure(f"auto stop: the input is a JSON {type(payload).__name__}, not an object")
        return None
    return payload


def _is_headless() -> bool:
    return os.environ.get(ATTENDED_ENV) == "0" and os.environ.get(HEADLESS_OK_ENV) != "1"


def _keeps_waiting(task: dict) -> bool:
    """Whether a background task of the input holds the judgement back: it is not over, and it is one that may commit or review."""
    if str(task.get("status", "")).lower() in _ENDED_STATUSES:
        return False
    if task.get("type") in _WORKING_TASK_TYPES:
        return True
    return task.get("type") == "shell" and _RATCHET_REVIEW_RE.search(str(task.get("command", ""))) is not None


def _session_of(payload: dict) -> str | None:
    """The session id of the input, else the one in the environment (a hook has both); None when neither could be a file name."""
    session = payload.get("session_id")
    if isinstance(session, str) and is_session_id(session):
        return session
    return session_id()


# ---- judging ------------------------------------------------------------------------------------------------------

def _claim(pending: Path) -> Path | None:
    """Move the pending file out of `pending/`, so that one Stop judges it: `rename` is atomic, and a Stop that finds the file gone
    (another window of the session took it first) gets None. The new name has the process id in it, as a second claim of the same
    session may start while this one is still judging."""
    claimed = make_state_dir(CLAIMED_NAME) / f"{pending.stem}.{os.getpid()}.jsonl"
    try:
        os.rename(pending, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _due_in(worktree: Path) -> Due | None:
    """Judge one worktree a commit was noted in. None when there is nothing to judge or the work is too little: the path is gone, its
    repo does not ask for the automatic wrap-up (any more), or it has not reached a threshold."""
    from .ledger import read_events
    from .wrapup import plan_wrapup  # the heavy part: a session with nothing noted never gets here

    if not worktree.is_dir():
        return None
    config = load_config(worktree, None, None, worktree)  # as `--repo <worktree>` loads it
    if not config.wrapup_auto:
        return None
    plan = plan_wrapup(config, read_events(config.ledger_path), foresee_refusal=True)
    return Due(worktree, config.root, plan) if plan.due else None


def _judge(claimed: Path) -> list[Due]:
    """The worktrees the claimed commits were made in whose work is due. A worktree counts once, however many commits were made in
    it: the measurement is of everything since its last review."""
    due = []
    for worktree in dict.fromkeys(commit.worktree for commit in read_pending(claimed)):
        try:
            judged = _due_in(worktree)
        except Exception as exc:  # batch isolation: a worktree that cannot be judged (a damaged config, a deleted branch) must not hide the rest
            log_failure(f"auto stop: {worktree}: {type(exc).__name__}: {exc}{raised_at(exc)}")
            continue
        if judged is not None:
            due.append(judged)
    return due


# ---- what was told before -----------------------------------------------------------------------------------------

def _read_announced(path: Path) -> dict[str, str]:
    """{key: when it was told} of the refusals told before, without the ones from before ANNOUNCED_KEEP. A file that is damaged
    counts as empty (told in hook.log): the worst case is a refusal told once more."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        told = json.loads(raw)
    except ValueError as exc:
        log_failure(f"auto stop: {path} is damaged, so its refusals count as new: {exc}")
        return {}
    cutoff = datetime.now(timezone.utc) - ANNOUNCED_KEEP
    return {key: when for key, when in told.items() if datetime.fromisoformat(when) >= cutoff}


def _announced_key(due: Due, refusal: Refusal) -> str:
    """One refusal of one worktree of one repo. The change it was counted on (its round key) says which refusal it is; a refusal that
    came before there was one (the rules, the round key itself) is told by its message."""
    return json.dumps([str(due.root), str(due.worktree), refusal.kind, refusal.round_key or refusal.message])


def _new_refusals(refused: list[tuple[Due, Refusal]]) -> list[tuple[Due, Refusal]]:
    """The refusals among `refused` that were not told before, and from now on they are. Remembered before they are told: a hook killed
    in between then misses one message, where the other order would tell it again at the end of every turn."""
    if not refused:
        return []
    path = state_dir() / ANNOUNCED_NAME
    told = _read_announced(path)
    fresh = [(due, refusal) for due, refusal in refused if _announced_key(due, refusal) not in told]
    if fresh:
        now = utc_timestamp()
        told.update({_announced_key(due, refusal): now for due, refusal in fresh})
        scratch = path.with_name(f"{path.name}.{os.getpid()}")
        scratch.write_text(json.dumps(told, indent=2) + "\n", encoding="utf-8")
        scratch.replace(path)  # a reader never sees half a file
    return fresh


# ---- the answer ---------------------------------------------------------------------------------------------------

def _due_line(due: Due) -> str:
    plan, target = due.plan, shlex.quote(str(due.worktree))
    return (f"review-ratchet: {target} has {plan.changes.lines} changed line(s) in {plan.changes.files} file(s) since its last review, "
            f"past the wrap-up threshold ({plan.thresholds.min_changed_lines} lines or {plan.thresholds.min_files} files). "
            f"Run the automatic wrap-up now: /review-ratchet:wrapup auto {target} "
            f"(Skill tool: review-ratchet:wrapup, args: auto {target}; equivalent: ratchet wrapup --repo {target}).")


def _refusal_line(due: Due, refusal: Refusal) -> str:
    message = " ".join(refusal.message.split())  # one line, whatever the message holds
    return f"review-ratchet: the wrap-up review of {due.worktree} is due but would be refused: {message}"


def _say(due: list[Due]) -> dict | None:
    """What the hook answers for the due worktrees: Claude is asked for the ones whose review would start, and the person is told
    of the refusals that are new."""
    ready = [item for item in due if item.plan.would_refuse is None]
    refused = [(item, item.plan.would_refuse) for item in due if item.plan.would_refuse is not None]
    answer: dict = {}
    if ready:
        answer["hookSpecificOutput"] = {"hookEventName": "Stop", "additionalContext": "\n".join(_due_line(item) for item in ready)}
    told = [_refusal_line(item, refusal) for item, refusal in _new_refusals(refused)]
    if told:
        answer["systemMessage"] = "\n".join(told)
    return answer or None


def _answer(stdin: TextIO) -> dict | None:
    if os.environ.get(REVIEW_CHILD_ENV) == "1" or _is_headless():
        return None
    payload = _read_input(stdin)
    if payload is None or payload.get("stop_hook_active") is True:
        return None
    session = _session_of(payload)
    if session is None or not pending_path(session).exists():
        return None
    if any(_keeps_waiting(task) for task in payload.get("background_tasks", [])):
        return None  # the pending file stays for the Stop after the task
    claimed = _claim(pending_path(session))
    if claimed is None:
        return None
    try:
        due = _judge(claimed)
    finally:
        claimed.unlink()  # a hook killed before this leaves the file, which costs one missed trigger: the next review measures it all again
    return _say(due)


def stop_hook(stdin: TextIO) -> int:
    """Run the hook on `stdin` and print its answer, if any. Always 0."""
    try:
        answer = _answer(stdin)
    except Exception as exc:  # side channel: the hook ends every turn, so its failure is a line in hook.log and never the turn's problem
        log_failure(f"auto stop: {type(exc).__name__}: {exc}{raised_at(exc)}")
        return 0
    if answer is not None:
        print(json.dumps(answer))
    return 0
