"""`ratchet auto stop`: the plugin's Stop hook. When a turn ends it judges the commits the git hook noted during the session, and when
the work since the last review has crossed the wrap-up threshold it asks Claude to run the automatic wrap-up.

It runs at the end of every turn of every session, in repos that never heard of ratchet too. So the cheap checks come first: the
environment, then the input, then one `stat` of the session's pending file, which `auto note` makes only after a commit. Only a
session with something noted pays for git and the ledger. The plugin's hooks/run.sh makes the environment and `stat` checks first, in
shell, so that a session with nothing noted does not start Python at all.

The answer is one JSON object on stdout, or nothing:
- `hookSpecificOutput.additionalContext` when the work is due and its review would start: Claude takes the turn on and runs the
  wrap-up skill with the text it is given;
- `systemMessage` when the work is due but its review would be refused (the rules do not load, the round cap, the daily cap on
  automatic reviews, no `claude`): the person is told, once for each distinct refusal (`announced.json`), and Claude is not asked
  to do anything.

Every other outcome is silence with exit status 0, and so is every failure, told in hook.log: a Stop hook that failed at the end
of every turn would be switched off, and exit status 2 would keep Claude from stopping. A worktree that could not be judged keeps
its commits: they go back into the pending file for the next Stop.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from .auto import (
    CLAIMED_NAME, KEEP_SECONDS, PendingCommit, append_pending, is_session_id, log_failure, make_state_dir, pending_path, raised_at,
    read_pending, session_id, state_dir, utc_timestamp,
)
from .config import Config, load_config
from .git import commit_sha, common_ancestor, is_ancestor
from .ledger import Event
from .reservations import BY_HOOK
from .review import NO_COMMIT, REVIEW_CHILD_ENV, Refusal, checked_out_default_branch, take_automatic_slot

if TYPE_CHECKING:
    from .wrapup import Base, WrapupPlan

# Claude Code sets this to 0 in a session no one is at. It is not documented: it was seen as 0 under `claude -p` (2.1.312) and as 1
# in an attended VS Code session. So only a 0 counts as headless, and anything else, or nothing, is attended.
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
HEADLESS_OK_ENV = "RATCHET_AUTO_HEADLESS"  # 1: judge in a headless session too (pipelines, the smoke test)
ANNOUNCED_NAME = "announced.json"
ANNOUNCED_KEEP = timedelta(days=30)
# A commit that could not be judged is put back only while it is younger than this: the week prune() lets a pending file live.
REQUEUE_KEEP = timedelta(seconds=KEEP_SECONDS)

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


# ---- times read back from the state files -------------------------------------------------------------------------

def _aware_time(value: object) -> datetime:
    """A time a state file holds, as `utc_timestamp` writes it. ValueError for anything but a string in ISO 8601 with a time zone: a
    naive time cannot be compared with now, so it is a wrong shape and not a time that is just old."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone")
    return moment


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


def _commit_before_notes(worktree: Path, notes: list[PendingCommit]) -> str | None:
    """The commit the noted work began from: the best common ancestor of the parents of the noted commits that HEAD still descends
    from (one a rebase or a reset dropped is passed over) and that have a parent. On one line of history that is the parent of the
    earliest of them. It goes by ancestry and not by the order of the notes: a Stop puts the commits it could not judge back after
    the notes made since, and a start point that is recorded once never moves.

    A fresh repo's first round often starts with its root commit, which has no parent: it is passed over for the commits after it,
    so the root commit's own work is the one thing the first wrap-up does not count. A session that noted only the root gives
    nothing, and a later Stop with more commits does. Nothing either when the parents share no ancestor (unrelated histories merged)."""
    parents = []
    for note in notes:
        parent = commit_sha(worktree, f"{note.head}^")
        if parent is not None and is_ancestor(worktree, note.head, "HEAD"):
            parents.append(parent)
    return common_ancestor(worktree, parents) if parents else None


def _note_start_point(config: Config, events: list[Event], notes: list[PendingCommit]) -> None:
    """On the default branch, with no review and no start point to measure from, record the commit the noted work began from (the
    parent of the earliest commit noted there) as the start point: without one the fork point is HEAD and the commits made in the
    session would not count. A repo that ran `ratchet init` after its first commit has one already, and the first wrap-up there is
    measured from it."""
    from .start_point import record_start_point, usable_start_point
    from .wrapup import last_reviewed

    default = checked_out_default_branch(config.worktree, config.default_branch)
    if default is None or last_reviewed(config.worktree, events) is not None:
        return
    default_name, default_commit = default
    if usable_start_point(config, default_name, default_commit) is not None:
        return
    start = _commit_before_notes(config.worktree, notes)
    if start is not None:
        record_start_point(config, default_name, start)


def _due_in(worktree: Path, notes: list[PendingCommit]) -> Due | None:
    """Judge one worktree the commits `notes` were made in. None when there is nothing to judge or the work is too little: the path
    is gone, its repo does not ask for the automatic wrap-up (any more), or it has not reached a threshold. The plan is that of
    `ratchet wrapup --auto`, the command Claude is given: the review it asks for counts against the daily cap, so a worktree
    whose day is used up is foreseen as refused instead of being asked for. The cap counts reservations: asking Claude takes a
    slot (`take_automatic_slot`, after every other refusal has been ruled out), whatever then becomes of the review, and the
    `wrapup --auto` that answers uses that slot and takes none of its own. Every ask takes one, so a Stop that asks again for a
    wrap-up nobody ran takes another; the cap bounds those too."""
    from .ledger import read_events
    from .wrapup import plan_wrapup  # the heavy part: a session with nothing noted never gets here

    if not worktree.is_dir():
        return None
    config = load_config(worktree, None, None, worktree)  # as `--repo <worktree>` loads it
    if not config.wrapup_auto:
        return None
    events = read_events(config.ledger_path)
    _note_start_point(config, events, notes)
    plan = plan_wrapup(config, events, automatic=True, foresee_refusal=True)
    if plan.due and plan.would_refuse is None:  # every other refusal has passed, so the cap is the last: it takes the slot, or refuses
        capped = take_automatic_slot(config, by=BY_HOOK, head=commit_sha(worktree, "HEAD") or NO_COMMIT, may_consume=False,
                                     note=lambda damage: log_failure(f"auto stop: {worktree}: {damage}"))
        plan = replace(plan, would_refuse=capped)
    return Due(worktree, config.root, plan) if plan.due else None


def _judge(claimed: Path) -> tuple[list[Due], list[PendingCommit]]:
    """The worktrees the claimed commits were made in whose work is due, and the commits noted in the worktrees that could not be judged.
    A worktree counts once, however many commits were made in it: the measurement is of everything since its last review. The file is
    read once, so a line that is damaged is told once."""
    noted: dict[Path, list[PendingCommit]] = {}
    for commit in read_pending(claimed):
        noted.setdefault(commit.worktree, []).append(commit)
    due, failed = [], []
    for worktree, commits in noted.items():
        try:
            judged = _due_in(worktree, commits)
        except Exception as exc:  # batch isolation: a worktree that cannot be judged (a damaged config, a deleted branch) must not hide the rest
            failed.extend(commits)
            log_failure(f"auto stop: {worktree}: {type(exc).__name__}: {exc}{raised_at(exc)}")
            continue
        if judged is not None:
            due.append(judged)
    return due, failed


def _is_recent(note: PendingCommit, cutoff: datetime) -> bool:
    """Whether the commit was noted at or after `cutoff`. A note whose time cannot be read has no age to go by, so it is not."""
    try:
        return _aware_time(note.ts) >= cutoff
    except ValueError:
        return False


def _requeue(session: str, failed: list[PendingCommit]) -> None:
    """Put the commits of the worktrees that could not be judged back into the session's pending file, for the next Stop: what failed
    may be a git lock or a timeout that is gone by then, and a commit dropped here is not judged again until another is made in that
    worktree. Only a commit noted within REQUEUE_KEEP goes back. prune() cannot bound this: it goes by the age of the file, and each
    Stop that puts a commit back writes it afresh, so a worktree that fails at every Stop would keep its commits while the session lives.
    A put-back that fails (a full disk) drops those commits too, told in hook.log: it must not take with it the answer for the
    worktrees that were judged due in the same Stop."""
    cutoff = datetime.now(timezone.utc) - REQUEUE_KEEP
    recent = [note for note in failed if _is_recent(note, cutoff)]
    if len(recent) < len(failed):
        worktrees = sorted({str(note.worktree) for note in failed if note not in recent})
        log_failure(f"auto stop: dropped {len(failed) - len(recent)} commit(s) noted in {', '.join(worktrees)} that could not be judged: "
                    f"they are older than {REQUEUE_KEEP.days} days, or their time cannot be read")
    if recent:
        try:
            append_pending(session, recent)
        except OSError as exc:
            worktrees = sorted({str(note.worktree) for note in recent})
            log_failure(f"auto stop: dropped {len(recent)} commit(s) noted in {', '.join(worktrees)} that could not be judged: they could "
                        f"not be put back ({type(exc).__name__}: {exc}{raised_at(exc)})")


# ---- what was told before -----------------------------------------------------------------------------------------

def _recent_told(raw: bytes, cutoff: datetime) -> dict[str, str]:
    """The entries of announced.json (`raw`) told at or after `cutoff`. ValueError when `raw` is not a JSON object of timestamps: a syntax
    error and a wrong shape are one kind of damage, and are met the same way."""
    told = json.loads(raw)
    if not isinstance(told, dict):
        raise ValueError(f"a JSON {type(told).__name__}, not an object")
    return {key: when for key, when in told.items() if _aware_time(when) >= cutoff}


def _read_announced(path: Path) -> dict[str, str]:
    """{key: when it was told} of the refusals told before, without the ones from before ANNOUNCED_KEEP. A file that is damaged,
    whether it is not JSON or is not an object of timestamps, counts as empty (told in hook.log): the worst case is a refusal told
    once more, and the next write puts a good file in its place."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    cutoff = datetime.now(timezone.utc) - ANNOUNCED_KEEP
    try:
        return _recent_told(raw, cutoff)
    except ValueError as exc:
        log_failure(f"auto stop: {path} is damaged, so its refusals count as new: {exc}")
        return {}


def _announced_key(due: Due, refusal: Refusal) -> str:
    """One refusal of one worktree of one repo. The change it was counted on (its round key) says which refusal it is; a refusal with
    none is told by its message: the rules and the round key itself fail before there is a key, and the daily cap counts reservations,
    not a change. The message of the daily cap names the moment its window makes room, so it holds still while the cap does and reads
    differently once the window has moved on and the cap is reached again."""
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

def _since(base: Base) -> str:
    """What the count started from, to follow `since`: the note of the base, the words `ratchet wrapup` gives it. The note of a count
    from HEAD is advice for that command (pass --base), and a branch with no commit has no HEAD to name, so those two are said here."""
    from .wrapup import BASE_HEAD  # in the function: a session with nothing noted never gets here, and wrapup is the heavy import

    if base.reason == BASE_HEAD:
        return "the start" if base.sha is None else "HEAD"
    return base.note


def _due_line(due: Due) -> str:
    plan, target = due.plan, shlex.quote(str(due.worktree))
    return (f"review-ratchet: {target} has {plan.changes.lines} changed line(s) in {plan.changes.files} file(s) since {_since(plan.base)}, "
            f"past the wrap-up threshold ({plan.thresholds.min_changed_lines} lines or {plan.thresholds.min_files} files). "
            f"Run the automatic wrap-up now: /review-ratchet:wrapup auto {target} "
            f"(Skill tool: review-ratchet:wrapup, args: auto {target}; equivalent: ratchet wrapup --repo {target} --auto).")


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
        due, failed = _judge(claimed)
        _requeue(session, failed)
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
