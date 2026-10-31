"""The book of the daily cap: one reservation for each automatic review, written when the review is asked for and not when it ends.

The book is `.ratchet/auto-reservations.jsonl` in the main worktree, shared by every worktree like the ledger and git-ignored with
the rest of `.ratchet/`. A line is one JSON object, `{"v": 1, "kind": "reserve", "id", "ts", "worktree", "head", "by"}` for a
reservation or `{"v": 1, "kind": "consume", "id", "ts"}` for the reservation of the Stop hook that a `ratchet wrapup --auto` then
used. `ts` is UTC with its zone, so the 24 hours of the cap do not move when the clocks change or the machine changes zone.

Every write holds `reservation_lock`: it reads the book, decides, and writes the whole book again into place (`replace_local`), so
a reader without the lock sees the old book or the new one, a crash leaves no half line, and the lines that left the window are
dropped by the same write. The pure part (the fold, the window, the reservation that can be used) is here; the limit and the words of
a refusal are in review.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Iterator

from .errors import RatchetError
from .proposal_store import local_path, replace_local

BOOK_NAME = ".ratchet/auto-reservations.jsonl"
LOCK_NAME = ".ratchet/auto-reservations.lock"
DAILY_WINDOW = timedelta(hours=24)  # the "day" of the daily cap: any 24 hours, not a calendar day
HOLD_TTL = timedelta(hours=1)  # how long the reservation of the Stop hook waits for the `wrapup --auto` it asked for
LOCK_WAIT_SECONDS = 5.0  # the lock is held for a read and a write of a file of a few lines; the Stop hook is cut off at 30
LOCK_POLL_SECONDS = 0.02
BY_HOOK, BY_WRAPUP = "hook", "wrapup"  # who made a reservation: the Stop hook, or a `ratchet wrapup --auto` run
_VERSION = 1


@dataclass(frozen=True)
class Reservation:
    id: str
    ts: datetime  # with a time zone (UTC)
    worktree: str
    head: str
    by: str


@dataclass(frozen=True)
class Book:
    """What the file holds, line by line. `kept` are the lines that were understood, as written, for the next write."""

    reservations: tuple[Reservation, ...]
    consumed: frozenset[str]  # the ids of the reservations a `wrapup --auto` used
    damaged: tuple[str, ...]  # one phrase for each line that was passed over, such as "line 3: not valid JSON (Expecting value)"
    kept: tuple[tuple[str, str, datetime, bool], ...]  # (line, id, ts, is a reservation) of each line understood, in file order


EMPTY_BOOK = Book((), frozenset(), (), ())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: object) -> datetime:
    """A time as `reserve_line` writes it, in UTC. ValueError for anything else: a naive time cannot be compared with now, so it is a
    wrong shape and not a time that is just old."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone")
    return moment.astimezone(timezone.utc)


def _nonempty(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"no {what}")
    return value


def _entry(line: str) -> Reservation | tuple[str, datetime]:
    """A reservation, or (id, ts) for a consumption. ValueError for a line that is not JSON and for one of the wrong shape alike."""
    try:
        item = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from exc
    if not isinstance(item, dict) or item.get("v") != _VERSION:
        raise ValueError("not a version 1 object")
    ident, moment = _nonempty(item.get("id"), "id"), _aware(item.get("ts"))
    if item.get("kind") == "consume":
        return ident, moment
    if item.get("kind") != "reserve":
        raise ValueError(f"kind {item.get('kind')!r}")
    by = item.get("by")
    if by not in (BY_HOOK, BY_WRAPUP):
        raise ValueError(f"by {by!r}")
    return Reservation(ident, moment, _nonempty(item.get("worktree"), "worktree"), _nonempty(item.get("head"), "head"), by)


def parse_book(text: str) -> Book:
    """Fold the file's text. A line that is not JSON and a line of the wrong shape are met the same way: passed over, named in
    `damaged`, and gone from the book at the next write. The cap is a limit on cost, so a damaged line must not switch it off (the
    whole file counted as empty) and must not stop every automatic review either (an error): the intact lines go on counting."""
    reserved, consumed, damaged, kept = [], set(), [], []
    for number, line in enumerate(text.split("\n"), start=1):  # not splitlines(): a path may hold a character that it splits on
        if not line.strip():
            continue
        try:
            entry = _entry(line)
        except (ValueError, OverflowError) as exc:  # OverflowError: a time at the edge of the calendar cannot be turned to UTC
            damaged.append(f"line {number}: {exc}")
            continue
        if isinstance(entry, Reservation):
            reserved.append(entry)
            kept.append((line, entry.id, entry.ts, True))
        else:
            consumed.add(entry[0])
            kept.append((line, entry[0], entry[1], False))
    return Book(tuple(reserved), frozenset(consumed), tuple(damaged), tuple(kept))


def read_book(root: Path) -> Book:
    """The book of the repo whose main worktree is `root`; empty when there is none. Reads only: no lock, and nothing is created."""
    path = local_path(root, BOOK_NAME)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return EMPTY_BOOK
    except OSError as exc:
        raise RatchetError(f"cannot read {path}: {exc}") from exc
    return parse_book(raw.decode("utf-8", errors="replace"))


def window_times(book: Book, now: datetime) -> list[datetime]:
    """When the reservations of the last 24 hours were made, oldest first. A reservation exactly 24 hours old is out of the window:
    the next one may be made at that moment. One dated after `now` (the clock was set back) is still in it."""
    started = [item.ts for item in book.reservations]
    return sorted(moment for moment in started if now - moment < DAILY_WINDOW)


def reusable(book: Book, worktree: str, now: datetime) -> Reservation | None:
    """The oldest reservation of the Stop hook for `worktree` that is younger than HOLD_TTL and that no `wrapup --auto` has used."""
    waiting = [item for item in book.reservations
               if item.by == BY_HOOK and item.worktree == worktree and item.id not in book.consumed and now - item.ts < HOLD_TTL]
    return min(waiting, key=lambda item: item.ts, default=None)


def _line(**fields: object) -> str:
    return json.dumps({"v": _VERSION, **fields}, separators=(",", ":"))  # ASCII only: a path with U+2028 in it stays on its line


def reserve_line(worktree: str, head: str, by: str, now: datetime) -> str:
    return _line(kind="reserve", id=os.urandom(8).hex(), ts=now.isoformat(timespec="seconds"), worktree=worktree, head=head, by=by)


def consume_line(reservation: Reservation, now: datetime) -> str:
    return _line(kind="consume", id=reservation.id, ts=now.isoformat(timespec="seconds"))


def write_book(root: Path, book: Book, now: datetime, add: str) -> None:
    """Write the book again with the line `add` on its end, without the lines that left the window and the consumptions of those.
    The caller holds the lock."""
    alive = {ident for _, ident, moment, reserved in book.kept if reserved and now - moment < DAILY_WINDOW}
    lines = [line for line, ident, _, _ in book.kept if ident in alive]
    try:
        replace_local(root, BOOK_NAME, "".join(f"{line}\n" for line in [*lines, add]))
    except OSError as exc:
        raise RatchetError(f"cannot write {root / BOOK_NAME}: {exc}") from exc


def _open_lock_file(path: Path) -> IO[str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RatchetError(f"cannot create {path.parent}: {exc}") from exc
    try:
        return path.open("a")
    except OSError as exc:
        raise RatchetError(f"cannot open {path}: {exc}") from exc


@contextlib.contextmanager
def reservation_lock(root: Path, wait: float | None = None) -> Iterator[None]:
    """Hold the lock of the book for the block, waiting up to `wait` seconds (None: LOCK_WAIT_SECONDS, as it is when the lock is asked
    for) for another process to let go of it. A flock is released when the process ends, so a crash leaves nothing behind (as
    proposal_store.mutation_lock; that one gives up at once, which would refuse a review whenever a Stop hook and a wrap-up meet)."""
    try:
        import fcntl
    except ImportError as exc:
        raise RatchetError("the daily cap needs Unix file locks") from exc
    path = local_path(root, LOCK_NAME)
    waited = LOCK_WAIT_SECONDS if wait is None else wait
    with _open_lock_file(path) as handle:
        deadline = time.monotonic() + waited
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RatchetError(f"another process is counting automatic reviews ({path} stayed locked for {waited:g}s); "
                                       "run it again") from None
                time.sleep(LOCK_POLL_SECONDS)
            else:
                break
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
