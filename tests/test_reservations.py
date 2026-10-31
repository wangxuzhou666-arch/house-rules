"""The book of the daily cap (house_rules/reservations.py): how a line is read and how damage is met, the 24 hours the reservations count
over, the reservation a `wrapup --auto` may use, how the book is written again, and the lock that makes processes take turns.

Nothing here reaches git or claude. The times are fixed instants a test chooses (`NOON` and its neighbours); only the processes run on
the real clock, and they are told apart by what they do and not by when. What the CLI and the Stop hook do with the book is tested in
test_daily_cap.py and test_auto_stop.py.
"""
import errno
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import REPO_ROOT, book_entries, book_lines, book_of, local_zone

from house_rules import reservations
from house_rules.errors import HouseRulesError
from house_rules.reservations import (
    BOOK_NAME, BY_HOOK, BY_WRAPUP, DAILY_WINDOW, EMPTY_BOOK, HOLD_TTL, LOCK_NAME, Reservation, consume_line, parse_book, read_book,
    reservation_lock, reserve_line, reusable, window_times, write_book,
)

SECOND, MINUTE, HOUR, DAY = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
NOON = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)  # mid-January is away from every clock change
WORKTREE = "/work/tree"
HEAD = "0123456789abcdef0123456789abcdef01234567"


class RootCase(unittest.TestCase):
    """A directory that stands for the main worktree of a repo, with processes that are killed when the test ends."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))

    def spawn(self, script: str, *arguments: str) -> subprocess.Popen:
        """A python process that runs `script` with the repo's root on sys.path, the directory of this test, and `arguments`."""
        proc = subprocess.Popen([sys.executable, "-c", script, str(REPO_ROOT), str(self.root), *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        return proc

    def book_text(self) -> list[str]:
        return (self.root / BOOK_NAME).read_text(encoding="utf-8").splitlines()


class FoldTest(unittest.TestCase):
    """`parse_book`: the text of the file as a Book, and what it makes of a line it cannot use."""

    def test_a_reservation_is_one_compact_json_line_and_is_read_back(self) -> None:
        line = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        (item,) = parse_book(line).reservations
        self.assertRegex(item.id, r"^[0-9a-f]{16}$")
        self.assertEqual(line, f'{{"v":1,"kind":"reserve","id":"{item.id}","ts":"2026-01-15T12:00:00+00:00",'
                               f'"worktree":"{WORKTREE}","head":"{HEAD}","by":"hook"}}')
        self.assertEqual(item, Reservation(item.id, NOON, WORKTREE, HEAD, BY_HOOK))
        self.assertNotEqual(item.id, parse_book(reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)).reservations[0].id)

    def test_a_consumption_names_the_reservation_it_used(self) -> None:
        reserve = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        (item,) = parse_book(reserve).reservations
        consume = consume_line(item, NOON + MINUTE)
        self.assertEqual(consume, f'{{"v":1,"kind":"consume","id":"{item.id}","ts":"2026-01-15T12:01:00+00:00"}}')
        book = parse_book(f"{reserve}\n{consume}\n")
        self.assertEqual((book.reservations, book.consumed, book.damaged), ((item,), frozenset({item.id}), ()))
        self.assertEqual(book.kept, ((reserve, item.id, NOON, True), (consume, item.id, NOON + MINUTE, False)))  # as written, in file order

    def test_a_line_that_cannot_be_used_is_passed_over_and_named_whatever_is_wrong_with_it(self) -> None:
        good = json.loads(reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON))
        consumption = json.loads(consume_line(parse_book(json.dumps(good)).reservations[0], NOON))
        without = lambda item, key: {name: value for name, value in item.items() if name != key}  # noqa: E731
        wrong = {"not JSON": "{not json", "a JSON list": "[1, 2]", "a JSON string": '"text"', "a JSON number": "7", "null": "null",
                 "no version": without(good, "v"), "another version": {**good, "v": 2}, "a version that is a word": {**good, "v": "1"},
                 "no id": without(good, "id"), "an empty id": {**good, "id": ""}, "an id that is no string": {**good, "id": 7},
                 "no time": without(good, "ts"), "a time that is no string": {**good, "ts": 1768478400},
                 "a time that is no time": {**good, "ts": "yesterday"}, "a time without a zone": {**good, "ts": "2026-01-15T12:00:00"},
                 "a time at the edge of the calendar": {**good, "ts": "0001-01-01T00:00:00+05:00"},
                 "no kind": without(good, "kind"), "a kind nobody knows": {**good, "kind": "cancel"},
                 "no by": without(good, "by"), "a by nobody knows": {**good, "by": "person"},
                 "no worktree": without(good, "worktree"), "an empty worktree": {**good, "worktree": ""},
                 "a worktree that is no string": {**good, "worktree": ["a"]},
                 "no head": without(good, "head"), "an empty head": {**good, "head": ""},
                 "a consumption without an id": without(consumption, "id"), "a consumption without a time": without(consumption, "ts"),
                 "a consumption with a time without a zone": {**consumption, "ts": "2026-01-15T12:00:00"}}
        intact = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        for name, damage in wrong.items():
            with self.subTest(name):
                book = parse_book(f"{intact}\n{damage if isinstance(damage, str) else json.dumps(damage)}\n{intact}\n")
                self.assertEqual(len(book.reservations), 2)  # the lines around it go on counting
                self.assertEqual(len(book.kept), 2)
                (named,) = book.damaged
                self.assertTrue(named.startswith("line 2: "), named)

    def test_damage_is_named_by_the_line_it_is_on_and_by_what_is_wrong(self) -> None:
        intact = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        book = parse_book(f"{intact}\n\n{{not json\n{intact}\n[]\n")
        self.assertEqual([phrase.split(":")[0] for phrase in book.damaged], ["line 3", "line 5"])
        self.assertIn("not valid JSON", book.damaged[0])
        self.assertEqual(book.damaged[1], "line 5: not a version 1 object")

    def test_blank_lines_and_a_missing_newline_are_no_damage(self) -> None:
        intact = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        for text in (intact, f"{intact}\n", f"\n\n{intact}\n  \n\t\n", f"{intact}\n\n\n"):
            with self.subTest(text=text):
                book = parse_book(text)
                self.assertEqual((len(book.reservations), book.damaged), (1, ()))
        self.assertEqual(parse_book(""), EMPTY_BOOK)
        self.assertEqual(parse_book("\n  \n"), EMPTY_BOOK)

    def test_a_time_in_another_zone_is_read_as_the_same_instant_in_utc(self) -> None:
        line = '{"v":1,"kind":"reserve","id":"a1","ts":"2026-01-15T17:30:00+05:30","worktree":"/w","head":"none","by":"hook"}'
        (item,) = parse_book(line).reservations
        self.assertEqual(item.ts, NOON)
        self.assertEqual(item.ts.utcoffset(), timedelta(0))

    def test_a_character_that_splitlines_splits_on_does_not_split_a_line(self) -> None:
        worktree = "/tmp/a b\u0085c\x0cd"
        written = reserve_line(worktree, HEAD, BY_HOOK, NOON)
        self.assertTrue(written.isascii())  # written escaped, so the line stays whole whoever reads it
        raw = written.replace("\\u2028", " ")  # and a book edited by hand may hold it as it is
        for line in (written, raw):
            (item,) = parse_book(f"{line}\n").reservations
            self.assertEqual(item.worktree, worktree)

    def test_a_kind_of_consume_line_that_names_no_reservation_still_folds(self) -> None:
        orphan = '{"v":1,"kind":"consume","id":"gone","ts":"2026-01-15T12:00:00+00:00"}'
        book = parse_book(orphan)
        self.assertEqual((book.reservations, book.consumed, book.damaged), ((), frozenset({"gone"}), ()))


class WindowTest(unittest.TestCase):
    """`window_times`: which reservations the cap counts, over any 24 hours and not over a calendar day."""

    def times(self, *planted: tuple[timedelta, str], now: datetime = NOON, **options: object) -> list[datetime]:
        return window_times(book_of(*planted, now=NOON, worktree=WORKTREE, **options), now)

    def test_the_window_is_24_hours(self) -> None:
        self.assertEqual(DAILY_WINDOW, DAY)

    def test_a_reservation_inside_the_last_24_hours_is_counted_and_one_outside_is_not(self) -> None:
        self.assertEqual(self.times((DAY - MINUTE, BY_WRAPUP)), [NOON - DAY + MINUTE])
        self.assertEqual(self.times((DAY + MINUTE, BY_WRAPUP)), [])

    def test_a_reservation_exactly_24_hours_old_is_out_and_one_second_younger_is_in(self) -> None:
        self.assertEqual(self.times((DAY, BY_WRAPUP)), [])  # the next one may be made at that very moment
        self.assertEqual(len(self.times((DAY - SECOND, BY_WRAPUP))), 1)

    def test_the_day_is_the_last_24_hours_and_not_the_calendar_date(self) -> None:
        (moment,) = self.times((23 * HOUR, BY_WRAPUP))  # 13:00 of the day before
        self.assertEqual(moment.date(), (NOON - DAY).date())

    def test_a_reservation_dated_after_now_is_still_counted(self) -> None:
        self.assertEqual(len(self.times((-HOUR, BY_WRAPUP))), 1)  # a clock set back must not let slots slip

    def test_the_reservations_of_the_hook_and_of_a_run_count_alike_whatever_the_worktree(self) -> None:
        mixed = parse_book("\n".join([*book_lines((HOUR, BY_HOOK), now=NOON, worktree="/one"),
                                      *book_lines((2 * HOUR, BY_WRAPUP), now=NOON, worktree="/another")]))
        self.assertEqual(len(window_times(mixed, NOON)), 2)

    def test_a_consumed_reservation_still_counts_and_its_consumption_is_not_a_second_one(self) -> None:
        self.assertEqual(self.times((HOUR, BY_HOOK), consumed=(0,)), [NOON - HOUR])
        self.assertEqual(len(self.times((HOUR, BY_HOOK), (2 * HOUR, BY_HOOK), consumed=(0, 1))), 2)

    def test_they_come_oldest_first_whatever_the_order_of_the_file(self) -> None:
        moments = self.times((3 * HOUR, BY_HOOK), (5 * HOUR, BY_WRAPUP), (HOUR, BY_HOOK))
        self.assertEqual([NOON - moment for moment in moments], [5 * HOUR, 3 * HOUR, HOUR])

    def test_a_time_written_in_another_zone_counts_as_the_instant_it_is(self) -> None:
        def window_of(ts: str) -> int:
            line = json.dumps({"v": 1, "kind": "reserve", "id": "a1", "ts": ts, "worktree": "/w", "head": "none", "by": "hook"})
            return len(window_times(parse_book(line), NOON))

        self.assertEqual(window_of("2026-01-14T17:30:00+05:30"), 0)  # 12:00 UTC on the 14th: exactly 24 hours before NOON
        self.assertEqual(window_of("2026-01-14T17:30:01+05:30"), 1)
        self.assertEqual(window_of("2026-01-14T07:00:01-05:00"), 1)

    def test_the_answer_is_the_same_in_every_zone_and_when_the_clocks_change_in_between(self) -> None:
        spring = datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc)  # 00:30 in New York; at 02:00 that morning the clocks went forward
        autumn = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)  # 00:30 in New York; at 02:00 that morning the clocks went back
        cases = [(spring, DAY - SECOND, 1), (spring, DAY, 0), (autumn, DAY - SECOND, 1), (autumn, DAY, 0)]
        for zone in ("EST5EDT,M3.2.0,M11.1.0", "UTC", "IST-5:30"):
            with self.subTest(zone=zone), local_zone(zone):
                if zone.startswith("EST5EDT"):  # the zone must really move the wall clock, or this test would prove nothing
                    wall = lambda moment: moment.astimezone().replace(tzinfo=None)  # noqa: E731
                    self.assertEqual(wall(spring + DAY) - wall(spring), DAY + HOUR)
                    self.assertEqual(wall(autumn + DAY) - wall(autumn), DAY - HOUR)
                for made, age, counted in cases:
                    book = parse_book(book_lines((timedelta(0), BY_HOOK), now=made)[0])
                    self.assertEqual(len(window_times(book, made + age)), counted, (made, age))


class ReusableTest(unittest.TestCase):
    """`reusable`: the reservation of the Stop hook that a `wrapup --auto` run may use as its slot."""

    def find(self, *planted: tuple[timedelta, str], **options: object) -> Reservation | None:
        worktree = options.pop("asking", WORKTREE)
        return reusable(book_of(*planted, now=NOON, worktree=WORKTREE, **options), worktree, NOON)

    def test_the_reservation_of_the_hook_for_this_worktree_is_found(self) -> None:
        found = self.find((5 * MINUTE, BY_HOOK))
        self.assertEqual((found.by, found.worktree, found.ts), (BY_HOOK, WORKTREE, NOON - 5 * MINUTE))

    def test_nothing_in_an_empty_book(self) -> None:
        self.assertIsNone(reusable(EMPTY_BOOK, WORKTREE, NOON))

    def test_the_reservation_of_a_run_is_never_used_by_another(self) -> None:
        self.assertIsNone(self.find((5 * MINUTE, BY_WRAPUP)))

    def test_only_the_worktree_the_hook_asked_for_may_use_it(self) -> None:
        self.assertIsNone(self.find((5 * MINUTE, BY_HOOK), asking="/work/other"))
        self.assertIsNone(self.find((5 * MINUTE, BY_HOOK), asking="/work/tree/"))  # compared as the strings they are

    def test_one_that_was_used_is_not_used_again(self) -> None:
        self.assertIsNone(self.find((5 * MINUTE, BY_HOOK), consumed=(0,)))

    def test_it_is_used_for_less_than_an_hour_and_not_for_an_hour(self) -> None:
        self.assertEqual(HOLD_TTL, HOUR)
        self.assertIsNotNone(self.find((HOUR - SECOND, BY_HOOK)))  # 59 minutes 59 seconds old
        self.assertIsNone(self.find((HOUR, BY_HOOK)))
        self.assertIsNone(self.find((2 * HOUR, BY_HOOK)))

    def test_the_oldest_of_several_is_the_one_found_and_the_next_is_found_once_it_is_used(self) -> None:
        planted = ((10 * MINUTE, BY_HOOK), (30 * MINUTE, BY_HOOK), (20 * MINUTE, BY_HOOK))
        self.assertEqual(self.find(*planted).ts, NOON - 30 * MINUTE)
        self.assertEqual(self.find(*planted, consumed=(1,)).ts, NOON - 20 * MINUTE)

    def test_a_reservation_dated_after_now_may_be_used(self) -> None:
        self.assertIsNotNone(self.find((-MINUTE, BY_HOOK)))  # a clock set back must not strand the review the hook asked for

    def test_one_that_is_too_old_does_not_hide_a_younger_one(self) -> None:
        self.assertEqual(self.find((2 * HOUR, BY_HOOK), (10 * MINUTE, BY_HOOK)).ts, NOON - 10 * MINUTE)


class WriteBookTest(RootCase):
    """`write_book`: the whole book again, into place, without what has left the window."""

    def write(self, lines: list[str], add: str, now: datetime = NOON) -> None:
        write_book(self.root, parse_book("\n".join(lines)), now, add)

    def test_the_new_line_goes_on_the_end_of_the_lines_that_stay(self) -> None:
        first, second = book_lines((3 * HOUR, BY_HOOK), (2 * HOUR, BY_WRAPUP), now=NOON)
        add = reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON)
        self.write([first, second], add)
        self.assertEqual(self.book_text(), [first, second, add])

    def test_a_book_that_is_not_there_is_made_with_the_one_line(self) -> None:
        add = reserve_line(WORKTREE, HEAD, BY_HOOK, NOON)
        write_book(self.root, EMPTY_BOOK, NOON, add)
        self.assertEqual(self.book_text(), [add])

    def test_what_left_the_window_goes_with_its_consumption_and_the_rest_stays_in_order(self) -> None:
        old, young = book_lines((DAY + HOUR, BY_HOOK), (2 * HOUR, BY_HOOK), now=NOON)
        old_item, young_item = parse_book(f"{old}\n{young}").reservations
        consumed_old, consumed_young = consume_line(old_item, NOON - DAY), consume_line(young_item, NOON - HOUR)
        add = reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON)
        self.write([old, young, consumed_old, consumed_young], add)
        self.assertEqual(self.book_text(), [young, consumed_young, add])  # the consumption is young, but its reservation is not

    def test_a_reservation_exactly_24_hours_old_is_dropped_and_one_second_younger_is_kept(self) -> None:
        edge, inside = book_lines((DAY, BY_HOOK), (DAY - SECOND, BY_HOOK), now=NOON)
        add = reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON)
        self.write([edge, inside], add)
        self.assertEqual(self.book_text(), [inside, add])

    def test_a_consumption_of_a_reservation_the_book_does_not_hold_is_dropped(self) -> None:
        (young,) = book_lines((HOUR, BY_HOOK), now=NOON)
        stranger = consume_line(Reservation("gone", NOON, WORKTREE, HEAD, BY_HOOK), NOON)
        add = reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON)
        self.write([stranger, young], add)
        self.assertEqual(self.book_text(), [young, add])

    def test_a_line_that_was_passed_over_is_not_written_again(self) -> None:
        (young,) = book_lines((HOUR, BY_HOOK), now=NOON)
        add = reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON)
        self.write(["{not json", young, '{"v":2}'], add)
        self.assertEqual(self.book_text(), [young, add])

    def test_the_book_is_the_owners_alone_and_no_temporary_file_is_left(self) -> None:
        self.write([], reserve_line(WORKTREE, HEAD, BY_HOOK, NOON))
        self.assertEqual(stat.S_IMODE((self.root / BOOK_NAME).stat().st_mode), 0o600)
        self.assertEqual([path.name for path in (self.root / ".house-rules").iterdir()], [Path(BOOK_NAME).name])

    def test_a_path_with_characters_that_split_lines_or_need_escaping_round_trips(self) -> None:
        worktree = "/tmp/a b é \"quoted\" \\ back\ttab"
        add = reserve_line(worktree, HEAD, BY_HOOK, NOON)
        write_book(self.root, EMPTY_BOOK, NOON, add)
        self.assertEqual(len(self.book_text()), 1)
        (item,) = read_book(self.root).reservations
        self.assertEqual(item.worktree, worktree)

    def test_the_book_that_was_there_stays_when_the_new_one_cannot_be_put_in_place(self) -> None:
        (young,) = book_lines((HOUR, BY_HOOK), now=NOON)
        self.write([], young)
        before = (self.root / BOOK_NAME).read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(HouseRulesError) as caught:
            self.write([young], reserve_line(WORKTREE, HEAD, BY_WRAPUP, NOON))
        self.assertIn(f"cannot write {self.root / BOOK_NAME}: disk full", str(caught.exception))
        self.assertEqual((self.root / BOOK_NAME).read_bytes(), before)
        self.assertEqual([path.name for path in (self.root / ".house-rules").iterdir()], [Path(BOOK_NAME).name])

    def test_a_directory_that_cannot_be_made_is_a_named_error_and_not_a_traceback(self) -> None:
        (self.root / ".house-rules").write_text("a file where the directory should be", encoding="utf-8")
        with self.assertRaises(HouseRulesError) as caught:
            self.write([], reserve_line(WORKTREE, HEAD, BY_HOOK, NOON))
        self.assertIn(f"cannot write {self.root / BOOK_NAME}", str(caught.exception))


class ReadBookTest(RootCase):
    """`read_book`: the book of a repo, read without a lock and without creating anything."""

    def test_a_repo_without_a_book_has_an_empty_one_and_reading_creates_nothing(self) -> None:
        self.assertIs(read_book(self.root), EMPTY_BOOK)
        self.assertEqual(list(self.root.iterdir()), [])
        (self.root / ".house-rules").mkdir()
        self.assertIs(read_book(self.root), EMPTY_BOOK)
        self.assertEqual(list((self.root / ".house-rules").iterdir()), [])

    def test_a_book_is_read_as_it_is_written(self) -> None:
        lines = book_lines((HOUR, BY_HOOK), (2 * HOUR, BY_WRAPUP), now=NOON)
        (self.root / ".house-rules").mkdir()
        (self.root / BOOK_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(read_book(self.root), parse_book("\n".join(lines)))

    def test_bytes_that_are_not_utf8_do_not_stop_the_read_and_the_line_they_are_in_is_named(self) -> None:
        (good,) = book_lines((HOUR, BY_HOOK), now=NOON)
        (self.root / ".house-rules").mkdir()
        (self.root / BOOK_NAME).write_bytes(b"\xff\xfe not text\n" + good.encode() + b"\n")
        book = read_book(self.root)
        self.assertEqual((len(book.reservations), len(book.damaged)), (1, 1))

    def test_a_symlink_or_a_directory_where_the_book_should_be_is_a_named_error(self) -> None:
        path = self.root / BOOK_NAME
        path.parent.mkdir()
        path.symlink_to(self.root / "elsewhere")
        with self.assertRaises(HouseRulesError) as caught:
            read_book(self.root)
        self.assertIn(BOOK_NAME, str(caught.exception))
        path.unlink()
        path.mkdir()
        with self.assertRaises(HouseRulesError) as caught:
            read_book(self.root)
        self.assertIn(BOOK_NAME, str(caught.exception))

    def test_a_state_directory_that_cannot_be_read_is_a_named_error_too(self) -> None:
        (self.root / ".house-rules").write_text("a file where the directory should be", encoding="utf-8")
        with self.assertRaises(HouseRulesError) as caught:
            read_book(self.root)
        self.assertIn(f"cannot read {self.root / BOOK_NAME}", str(caught.exception))


WAITER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from house_rules.reservations import reservation_lock
print("ready", flush=True)
with reservation_lock(Path(sys.argv[2]), wait=30):
    print("got", flush=True)
"""
HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from house_rules.reservations import reservation_lock
with reservation_lock(Path(sys.argv[2])):
    print("held", flush=True)
    time.sleep(60)
"""


class LockTest(RootCase):
    """`reservation_lock`: processes take turns, a wait has an end, and a lock never outlives the process that holds it."""

    def test_a_second_process_waits_while_the_lock_is_held_and_goes_on_when_it_is_let_go(self) -> None:
        with reservation_lock(self.root):
            waiter = self.spawn(WAITER)
            self.assertEqual(waiter.stdout.readline(), "ready\n")  # it has started, so what it does next is the wait for the lock
            time.sleep(0.5)
            self.assertIsNone(waiter.poll())
        out, _ = waiter.communicate(timeout=30)
        self.assertEqual((waiter.returncode, out), (0, "got\n"))

    def test_the_wait_ends_with_an_error_that_names_the_lock_file_and_the_seconds(self) -> None:
        with reservation_lock(self.root):
            started = time.monotonic()
            with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root, wait=0.2):
                self.fail("the lock was held by this very test")
            waited = time.monotonic() - started
        self.assertIn(f"{self.root / LOCK_NAME} stayed locked for 0.2s", str(caught.exception))
        self.assertTrue(0.2 <= waited < 3, waited)

    def test_the_wait_of_a_lock_asked_for_without_one_is_the_constant_as_it_is_then(self) -> None:
        self.assertEqual(reservations.LOCK_WAIT_SECONDS, 5.0)
        with reservation_lock(self.root), mock.patch.object(reservations, "LOCK_WAIT_SECONDS", 0.2):
            started = time.monotonic()
            with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
                self.fail("the lock was held by this very test")
        self.assertIn("stayed locked for 0.2s", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3)

    def test_the_lock_is_let_go_at_the_end_of_the_block_and_when_the_block_fails(self) -> None:
        with reservation_lock(self.root):
            pass
        with self.assertRaises(KeyError), reservation_lock(self.root, wait=0.5):
            raise KeyError("a bug in the block")
        with reservation_lock(self.root, wait=0.5):
            pass

    def test_a_lock_the_file_system_refuses_is_a_named_error_and_not_a_plain_os_error(self) -> None:
        refused = OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))  # as a network file system answers when it cannot take the lock
        with mock.patch("fcntl.flock", side_effect=refused):
            with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
                self.fail("the lock was refused")
        self.assertEqual(str(caught.exception), f"cannot lock {self.root / LOCK_NAME}: [Errno {errno.ENOLCK}] {os.strerror(errno.ENOLCK)}")
        self.assertIs(caught.exception.__cause__, refused)
        with reservation_lock(self.root, wait=0.5):  # the file was closed on the way out, so nothing is left locked
            pass

    def test_an_error_when_the_lock_is_let_go_does_not_replace_the_result_of_the_block(self) -> None:
        real_flock = fcntl.flock

        def flock_that_cannot_unlock(handle: object, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            real_flock(handle, operation)

        with mock.patch("fcntl.flock", side_effect=flock_that_cannot_unlock):
            with reservation_lock(self.root):
                pass
            with self.assertRaises(KeyError), reservation_lock(self.root, wait=0.5):
                raise KeyError("a bug in the block")
        with reservation_lock(self.root, wait=0.5):  # closing the file let go of it
            pass

    def test_a_lock_held_by_a_process_that_is_killed_is_free_at_once(self) -> None:
        holder = self.spawn(HOLDER)
        self.assertEqual(holder.stdout.readline(), "held\n")
        with self.assertRaises(HouseRulesError), reservation_lock(self.root, wait=0.2):
            self.fail("another process holds it")
        holder.send_signal(signal.SIGKILL)  # no chance to clean up, as in a crash
        holder.wait(timeout=30)
        with reservation_lock(self.root, wait=2):
            pass

    def test_the_lock_file_is_never_replaced(self) -> None:
        with reservation_lock(self.root):
            pass
        lock = self.root / LOCK_NAME
        before = lock.stat().st_ino
        write_book(self.root, EMPTY_BOOK, NOON, reserve_line(WORKTREE, HEAD, BY_HOOK, NOON))  # the book is replaced every time
        with reservation_lock(self.root):
            pass
        self.assertEqual(lock.stat().st_ino, before)

    def test_a_symlink_or_a_directory_where_the_lock_should_be_is_a_named_error(self) -> None:
        path = self.root / LOCK_NAME
        path.parent.mkdir()
        path.symlink_to(self.root / "elsewhere")
        with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
            self.fail("a lock behind a symlink")
        self.assertIn(LOCK_NAME, str(caught.exception))
        path.unlink()
        path.mkdir()
        with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
            self.fail("a lock that is a directory")
        self.assertIn(LOCK_NAME, str(caught.exception))

    def test_a_state_directory_that_cannot_be_made_is_a_named_error(self) -> None:
        (self.root / ".house-rules").write_text("a file where the directory should be", encoding="utf-8")
        with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
            self.fail("no directory for the lock")
        self.assertIn(f"cannot create {self.root / '.house-rules'}", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root opens a file whatever its mode")
    def test_a_lock_file_that_cannot_be_opened_is_a_named_error(self) -> None:
        path = self.root / LOCK_NAME
        path.parent.mkdir()
        path.touch()
        path.chmod(0)
        with self.assertRaises(HouseRulesError) as caught, reservation_lock(self.root):
            self.fail("no lock file to hold")
        self.assertIn(f"cannot open {path}", str(caught.exception))


SLOT_TAKER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from types import SimpleNamespace
from house_rules.review import take_automatic_slot
root = Path(sys.argv[2])
config = SimpleNamespace(root=root, worktree=root / "worktree", wrapup_auto_max_per_day=3)
(root / f"ready-{sys.argv[3]}").write_text("")
while not (root / "go").exists():
    time.sleep(0.001)
print("REFUSED" if take_automatic_slot(config, by="wrapup", head="none", may_consume=True) else "TAKEN")
"""


class ConcurrencyTest(RootCase):
    """Processes that ask for a slot at the same moment take turns: the count is decided under the lock, so no more slots are taken
    than the limit allows."""

    def wait_until_ready(self, count: int) -> None:
        deadline = time.monotonic() + 60
        while len(list(self.root.glob("ready-*"))) < count:
            self.assertLess(time.monotonic(), deadline, "the processes did not start")
            time.sleep(0.02)

    def test_twelve_processes_asking_at_once_take_three_slots_of_three_and_nine_are_refused(self) -> None:
        taken = [self.spawn(SLOT_TAKER, str(number)) for number in range(12)]
        self.wait_until_ready(12)
        (self.root / "go").write_text("")  # they all stand ready: now
        answers = [proc.communicate(timeout=60)[0].strip() for proc in taken]
        self.assertEqual(sorted(answers), ["REFUSED"] * 9 + ["TAKEN"] * 3, answers)
        entries = book_entries(self.root)
        self.assertEqual([(entry["kind"], entry["by"]) for entry in entries], [("reserve", "wrapup")] * 3)
        self.assertEqual(sorted(path.name for path in (self.root / ".house-rules").iterdir()),
                         sorted([Path(BOOK_NAME).name, Path(LOCK_NAME).name]))  # no temporary file is left behind

    def test_while_the_lock_is_held_elsewhere_a_process_neither_reads_nor_writes_the_book_and_it_goes_on_after(self) -> None:
        (self.root / "go").write_text("")
        with reservation_lock(self.root):
            taker = self.spawn(SLOT_TAKER, "0")
            self.wait_until_ready(1)
            time.sleep(0.5)
            self.assertIsNone(taker.poll())
            self.assertFalse((self.root / BOOK_NAME).exists())
        out, _ = taker.communicate(timeout=30)
        self.assertEqual((taker.returncode, out.strip()), (0, "TAKEN"))
        self.assertEqual(len(book_entries(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
