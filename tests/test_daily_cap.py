"""The daily cap on automatic reviews: the config key, the refusal, and the slot that the Stop hook or `ratchet wrapup --auto` takes.

The cap counts the reservations in the book (ratchet/reservations.py; its own tests are in test_reservations.py): one for each
automatic review that the Stop hook asked for or that `wrapup --auto` started, whatever became of the review. Nothing here runs the
real claude: the review is an executable fake. A test that goes through the CLI plants reservations that are hours old or more than a
day old, so that the real clock cannot move one across the edge while the test runs; the exact edge of the 24 hours is tested on the
pure functions, with a `now` the test chooses.
"""
import contextlib
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
import test_review_refusal
from support import RepoCase, book_entries, book_lines, book_of, git, local_zone, make_config, plant_book, review_round
from test_review_refusal import dump_env_claude
from test_wrapup import numbered

from ratchet import reservations
from ratchet.config import DEFAULT_WRAPUP_AUTO_MAX_PER_DAY, load_config
from ratchet.errors import RatchetError
from ratchet.ledger import read_events
from ratchet.reservations import BOOK_NAME, BY_HOOK, BY_WRAPUP, LOCK_NAME, reservation_lock
from ratchet.review import (
    REFUSE_CLAUDE, REFUSE_DAILY_CAP, REFUSE_ROUND_CAP, REFUSE_ROUND_KEY, REFUSE_RULES, TRIGGER_AUTO, Refusal, daily_cap_refusal,
    review_run_event, take_automatic_slot,
)
from ratchet.wrapup import WrapupPlan, plan_wrapup

SECOND, MINUTE, HOUR, DAY = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
# Noon UTC of a January day. Mid-January is away from every clock change, so the 24 hours before it are the same on the wall clock
# and on a stopwatch.
NOON = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
WORKTREE = Path("/work/tree")
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_CHANGE = "other@0123456789ab"  # not the change a test reviews, so these rounds do not count toward the round cap there
# What a refusal says, in the local time of UTC, when two reservations, at 10:00 and 11:30 on the day of NOON, used up a limit of 2.
TWO_AT_NOON = ("daily cap reached: 2 automatic review(s) were started or asked for in the last 24 hours "
               "and wrapup_auto_max_per_day is 2. "
               "The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves that window; "
               "the work stays counted until then, and the first automatic wrap-up after that reviews it. "
               "/review-ratchet:wrapup (`ratchet wrapup` without --auto) reviews it now.")
# The keys of every `wrapup --json` object, whatever its status.
JSON_KEYS = {"status", "base", "base_reason", "changed_lines", "changed_files", "thresholds", "run", "new_unmapped",
             "repeated_unmapped", "repeat_of", "open_unmapped", "reason", "would_refuse"}


class ConfigKeyTest(RepoCase):
    def limit(self) -> int:
        return load_config(self.repo, None, None, self.repo).wrapup_auto_max_per_day

    def test_it_is_two_when_the_config_does_not_say(self) -> None:
        self.assertEqual(self.limit(), 2)  # no config file at all
        self.write_config(rules_files=["rules.md"])
        self.assertEqual(self.limit(), 2)
        self.assertEqual(DEFAULT_WRAPUP_AUTO_MAX_PER_DAY, 2)
        self.assertEqual(make_config().wrapup_auto_max_per_day, 2)

    def test_a_positive_integer_is_read_as_it_is(self) -> None:
        for value in (1, 2, 3, 24, 1000):
            with self.subTest(value=value):
                self.write_config(wrapup_auto_max_per_day=value)
                self.assertEqual(self.limit(), value)

    def test_it_is_a_known_key_and_a_misspelling_of_it_is_not(self) -> None:
        self.write_config(wrapup_auto_max_per_day=3)
        self.assertEqual(self.cli("count")[0], 0)
        self.write_config(wrapup_auto_max_a_day=3)
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("unknown config key", err)

    def test_anything_but_a_positive_integer_is_refused_and_the_message_names_the_key(self) -> None:
        for bad in (True, False, 0, -1, "2", 1.0, 2.5, None, [], [2], {}):
            with self.subTest(value=bad):
                self.write_config(wrapup_auto_max_per_day=bad)
                code, _, err = self.cli("count")
                self.assertEqual(code, 1)
                self.assertIn("config 'wrapup_auto_max_per_day' must be a positive integer", err)

    def test_setting_it_does_not_switch_the_automatic_wrap_up_on(self) -> None:
        self.write_config(wrapup_auto_max_per_day=5)
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.wrapup_auto, config.wrapup_auto_max_per_day), (False, 5))


class DailyCapRefusalTest(unittest.TestCase):
    """The cap itself: when it holds, which reservation frees the next slot, and the words that say so. The book holds UTC and the
    message speaks local time, so these tests set the zone of the process."""

    def setUp(self) -> None:
        zone = contextlib.ExitStack()
        zone.enter_context(local_zone("UTC"))
        self.addCleanup(zone.close)

    def refusal(self, *planted: tuple[timedelta, str], limit: int = 2, now: datetime = NOON, may_consume: bool = True,
                **options: object) -> Refusal | None:
        book = book_of(*planted, now=NOON, **{"worktree": WORKTREE, **options})
        return daily_cap_refusal(make_config(wrapup_auto_max_per_day=limit, worktree=WORKTREE), book, now, may_consume=may_consume)

    def test_below_the_limit_there_is_no_refusal(self) -> None:
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal((2 * HOUR, BY_WRAPUP)))

    def test_at_the_limit_it_is_refused_in_these_words_with_no_round_key(self) -> None:
        self.assertEqual(self.refusal((2 * HOUR, BY_WRAPUP), (30 * MINUTE, BY_WRAPUP)), Refusal(REFUSE_DAILY_CAP, None, TWO_AT_NOON))

    def test_the_next_slot_is_free_exactly_when_the_message_says(self) -> None:
        planted = ((2 * HOUR, BY_WRAPUP), (30 * MINUTE, BY_WRAPUP))  # the one from 10:00 frees the slot at 10:00 the next day
        free = datetime(2026, 1, 16, 10, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(self.refusal(*planted, now=free))
        self.assertIsNotNone(self.refusal(*planted, now=free - SECOND))

    def test_a_limit_of_one_waits_for_the_one_reservation(self) -> None:
        refusal = self.refusal((2 * HOUR, BY_WRAPUP), limit=1)
        self.assertEqual(refusal.kind, REFUSE_DAILY_CAP)
        self.assertTrue(refusal.message.startswith(
            "daily cap reached: 1 automatic review(s) were started or asked for in the last 24 hours and wrapup_auto_max_per_day is 1. "
            "The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves that window;"))

    def test_a_limit_of_three_is_reached_by_three_and_waits_for_the_oldest(self) -> None:
        self.assertIsNone(self.refusal((5 * HOUR, BY_WRAPUP), (3 * HOUR, BY_WRAPUP), limit=3))
        refusal = self.refusal((5 * HOUR, BY_WRAPUP), (3 * HOUR, BY_WRAPUP), (HOUR, BY_WRAPUP), limit=3)
        self.assertTrue(refusal.message.startswith("daily cap reached: 3 automatic review(s)"))
        self.assertIn("The next one can run after 2026-01-16T07:00:00, when the one from 2026-01-15T07:00:00 leaves", refusal.message)

    def test_a_lowered_limit_waits_until_fewer_than_the_limit_are_left(self) -> None:
        planted = tuple((age, BY_WRAPUP) for age in (4 * HOUR, 3 * HOUR, 2 * HOUR, HOUR))  # at 08:00, 09:00, 10:00 and 11:00
        limit_two = self.refusal(*planted, limit=2)
        self.assertTrue(limit_two.message.startswith("daily cap reached: 4 automatic review(s)"))
        self.assertIn("The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves", limit_two.message)
        free = datetime(2026, 1, 16, 10, 0, 0, tzinfo=timezone.utc)  # 08:00, 09:00 and 10:00 are out then and only 11:00 is left
        self.assertIsNone(self.refusal(*planted, limit=2, now=free))
        self.assertIsNotNone(self.refusal(*planted, limit=2, now=free - SECOND))
        limit_one = self.refusal(*planted, limit=1)
        self.assertIn("The next one can run after 2026-01-16T11:00:00, when the one from 2026-01-15T11:00:00 leaves", limit_one.message)
        self.assertIsNone(self.refusal(*planted, limit=1, now=datetime(2026, 1, 16, 11, 0, 0, tzinfo=timezone.utc)))

    def test_a_reservation_of_the_hook_and_one_of_a_run_count_alike_and_a_consumed_one_counts_once(self) -> None:
        self.assertIsNotNone(self.refusal((2 * HOUR, BY_HOOK), (HOUR, BY_WRAPUP)))
        self.assertIsNotNone(self.refusal((2 * HOUR, BY_HOOK), (HOUR, BY_HOOK)))
        self.assertIsNone(self.refusal((2 * HOUR, BY_HOOK), consumed=(0,)))  # the consumption is no slot of its own

    def test_a_reservation_more_than_a_day_old_does_not_count(self) -> None:
        self.assertIsNone(self.refusal((DAY + HOUR, BY_WRAPUP), (2 * HOUR, BY_WRAPUP)))

    def test_the_message_holds_still_while_the_cap_does_and_changes_in_the_next_episode(self) -> None:
        planted = ((2 * HOUR, BY_WRAPUP), (30 * MINUTE, BY_WRAPUP))
        later = NOON + 5 * HOUR  # nothing new was asked and the cap still holds: the Stop hook must see the same words and say them once
        self.assertEqual(self.refusal(*planted, now=later).message, self.refusal(*planted).message)
        next_day = datetime(2026, 1, 16, 12, 0, 0, tzinfo=timezone.utc)  # both are out of the window; two new ones use the limit up again
        # the two from the first day are 26 and 24.5 hours old then, and two new ones are 2 hours and 30 minutes old
        again = parse_planted(((26 * HOUR, BY_WRAPUP), (24 * HOUR + 30 * MINUTE, BY_WRAPUP), *planted), next_day)
        self.assertNotEqual(daily_cap_refusal(make_config(worktree=WORKTREE), again, next_day).message, self.refusal(*planted).message)

    def test_the_times_are_local_and_have_no_zone(self) -> None:
        planted = ((2 * HOUR, BY_WRAPUP), (30 * MINUTE, BY_WRAPUP))
        for zone, shift in (("IST-5:30", "15:30:00"), ("EST5", "05:00:00")):  # 10:00 UTC where it is UTC+5:30, and where it is UTC-5
            with self.subTest(zone=zone), local_zone(zone):
                message = self.refusal(*planted).message
                self.assertEqual(message, TWO_AT_NOON.replace("T10:00:00", f"T{shift}"))
                self.assertIsNone(re.search(r"\d\d:\d\d:\d\d[Z+-]", message))

    def test_the_time_the_next_slot_is_free_is_24_hours_on_even_when_the_clocks_change_in_between(self) -> None:
        now = datetime(2026, 3, 7, 23, 0, 0, tzinfo=timezone.utc)  # 18:00 in New York
        with local_zone("EST5EDT,M3.2.0,M11.1.0"):  # at 02:00 on 2026-03-08 the clocks there go forward an hour
            book = parse_planted(((3 * HOUR, BY_WRAPUP), (2 * HOUR, BY_WRAPUP)), now)
            message = daily_cap_refusal(make_config(worktree=WORKTREE), book, now).message
        # 15:00 on the 7th is 20:00 UTC; 24 hours later is 20:00 UTC on the 8th, which the clocks there then call 16:00 (not 15:00)
        self.assertIn("The next one can run after 2026-03-08T16:00:00, when the one from 2026-03-07T15:00:00 leaves", message)

    def test_a_waiting_reservation_of_the_hook_for_this_worktree_is_the_slot_of_the_wrapup_that_uses_it(self) -> None:
        planted = ((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK))  # the limit is reached, by the very reservation that is waiting
        self.assertIsNone(self.refusal(*planted))
        self.assertEqual(self.refusal(*planted, may_consume=False).kind, REFUSE_DAILY_CAP)  # the hook asks for a slot of its own

    def test_no_other_reservation_is_a_slot_to_be_used(self) -> None:
        for name, planted, options in (("one of a run", ((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_WRAPUP)), {}),
                                       ("one of the hook for another worktree", ((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK)),
                                        {"worktree": "/work/another"}),
                                       ("one that waited too long", ((2 * HOUR, BY_WRAPUP), (2 * HOUR, BY_HOOK)), {}),
                                       ("one that was used", ((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK)), {"consumed": (1,)})):
            with self.subTest(name):
                self.assertEqual(self.refusal(*planted, **options).kind, REFUSE_DAILY_CAP)


def parse_planted(planted: tuple[tuple[timedelta, str], ...], now: datetime):
    """The book of reservations made `age` before `now` (for the worktree of these tests), without the clock of the machine."""
    return book_of(*planted, now=now, worktree=WORKTREE)


class ReviewRunEventTest(unittest.TestCase):
    """The trigger is written only when it is given."""

    def event(self, **options: object) -> dict:
        return review_run_event("demo", "2026-01-15T12:00:00", run_id="r1", key="main@0a1b2c3d4e5f", round_number=1, max_rounds=3,
                                over_cap=False, finding_count=0, level="high", head="none", reviewer_version=None,
                                version_changed=False, from_file=False, **options)

    def test_an_automatic_review_records_its_trigger(self) -> None:
        self.assertEqual(self.event(trigger=TRIGGER_AUTO)["trigger"], "auto")

    def test_a_review_without_one_records_none_at_all(self) -> None:
        self.assertNotIn("trigger", self.event())
        self.assertNotIn("trigger", self.event(trigger=None))


class TakeSlotTest(unittest.TestCase):
    """`take_automatic_slot`: the count and the write under the lock, for a repo that is a bare directory. The slot is taken in the
    main worktree's book, `root`, whichever worktree asks."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.worktree = self.root / "linked"  # not made: nothing here reads it

    def take(self, *, limit: int = 2, by: str = BY_WRAPUP, may_consume: bool = True, **options: object) -> Refusal | None:
        config = make_config(root=self.root, worktree=self.worktree, wrapup_auto_max_per_day=limit)
        return take_automatic_slot(config, by=by, head=HEAD, may_consume=may_consume, **options)

    def plant(self, *planted: tuple[timedelta, str], **options: object) -> None:
        plant_book(self.root, *planted, now=NOON, worktree=options.pop("worktree", self.worktree))

    def book_bytes(self) -> bytes:
        return (self.root / BOOK_NAME).read_bytes()

    def test_it_writes_a_reservation_of_its_own_and_says_nothing(self) -> None:
        self.assertIsNone(self.take(now=NOON))
        [entry] = book_entries(self.root)
        self.assertRegex(entry.pop("id"), r"^[0-9a-f]{16}$")
        self.assertEqual(entry, {"v": 1, "kind": "reserve", "ts": "2026-01-15T12:00:00+00:00", "worktree": str(self.worktree),
                                 "head": HEAD, "by": "wrapup"})
        self.assertFalse(self.worktree.exists())  # the book is the root's

    def test_who_asked_is_what_the_caller_says(self) -> None:
        self.assertIsNone(self.take(by=BY_HOOK, may_consume=False, now=NOON))
        [entry] = book_entries(self.root)
        self.assertEqual((entry["kind"], entry["by"]), ("reserve", "hook"))

    def test_at_the_limit_it_is_refused_in_the_words_of_the_cap_and_the_book_is_left_as_it_was(self) -> None:
        self.plant((2 * HOUR, BY_WRAPUP), (HOUR, BY_WRAPUP))
        before = self.book_bytes()
        refusal = self.take(now=NOON)
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_DAILY_CAP, None))
        self.assertTrue(refusal.message.startswith("daily cap reached: 2 automatic review(s) were started or asked for in the last 24 "))
        self.assertEqual(self.book_bytes(), before)

    def test_each_slot_taken_is_counted_by_the_next_ask(self) -> None:
        self.assertIsNone(self.take(now=NOON))
        self.assertIsNone(self.take(now=NOON + MINUTE))
        self.assertIsNotNone(self.take(now=NOON + 2 * MINUTE))
        self.assertEqual(len(book_entries(self.root)), 2)

    def test_the_reservation_of_the_hook_for_this_worktree_is_used_and_no_new_one_is_made_even_at_the_limit(self) -> None:
        [_, waiting] = plant_book(self.root, (2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK), now=NOON, worktree=self.worktree)
        self.assertIsNone(self.take(now=NOON))
        *kept, consumption = book_entries(self.root)
        self.assertEqual([entry["kind"] for entry in kept], ["reserve", "reserve"])
        self.assertEqual(consumption, {"v": 1, "kind": "consume", "id": waiting.id, "ts": "2026-01-15T12:00:00+00:00"})

    def test_a_reservation_is_used_once_so_the_next_run_counts_for_itself(self) -> None:
        self.plant((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK))
        self.assertIsNone(self.take(now=NOON))
        self.assertIsNotNone(self.take(now=NOON + MINUTE))  # two counted already, and nothing waits any more
        self.assertEqual([entry["kind"] for entry in book_entries(self.root)], ["reserve", "reserve", "consume"])

    def test_the_oldest_of_the_waiting_reservations_is_used_first(self) -> None:
        [older, younger] = plant_book(self.root, (30 * MINUTE, BY_HOOK), (10 * MINUTE, BY_HOOK), now=NOON, worktree=self.worktree)
        self.assertIsNone(self.take(limit=5, now=NOON))
        self.assertIsNone(self.take(limit=5, now=NOON))
        self.assertEqual([entry["id"] for entry in book_entries(self.root)[2:]], [older.id, younger.id])
        self.assertIsNone(self.take(limit=5, now=NOON))  # nothing waits: a reservation of its own, below the limit
        self.assertEqual(book_entries(self.root)[-1]["kind"], "reserve")

    def test_the_hook_never_uses_a_waiting_reservation_every_ask_takes_a_slot_of_its_own(self) -> None:
        self.plant((5 * MINUTE, BY_HOOK))
        self.assertIsNone(self.take(by=BY_HOOK, may_consume=False, now=NOON))
        self.assertEqual([entry["kind"] for entry in book_entries(self.root)], ["reserve", "reserve"])
        self.assertIsNotNone(self.take(by=BY_HOOK, may_consume=False, now=NOON))  # and at the limit the waiting one is no help
        self.assertEqual(len(book_entries(self.root)), 2)

    def test_a_reservation_for_another_worktree_is_not_used(self) -> None:
        self.plant((2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK), worktree=self.root / "elsewhere")
        self.assertIsNotNone(self.take(now=NOON))

    def test_the_write_that_takes_a_slot_drops_what_left_the_window(self) -> None:
        self.plant((DAY + HOUR, BY_WRAPUP), (2 * HOUR, BY_WRAPUP))
        self.assertIsNone(self.take(now=NOON))
        self.assertEqual(len(book_entries(self.root)), 2)  # the old one is gone, the new one is in

    def test_a_refusal_is_no_write_so_it_drops_nothing(self) -> None:
        self.plant((DAY + HOUR, BY_WRAPUP), (2 * HOUR, BY_WRAPUP), (HOUR, BY_WRAPUP))
        before = self.book_bytes()
        self.assertIsNotNone(self.take(now=NOON))
        self.assertEqual(self.book_bytes(), before)

    def damaged_book(self, *lines: str) -> None:
        (self.root / BOOK_NAME).parent.mkdir(parents=True, exist_ok=True)
        (self.root / BOOK_NAME).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def test_a_line_that_cannot_be_used_is_told_to_note_with_the_lock_let_go_and_is_gone_after_the_write(self) -> None:
        (young,) = book_lines((2 * HOUR, BY_WRAPUP), now=NOON, worktree=self.worktree)
        self.damaged_book("{not json", young, '{"v": 2}')
        told = []

        def note(text: str) -> None:
            with reservation_lock(self.root, wait=0.2):  # the lock is free again by the time anything is told
                told.append(text)

        self.assertIsNone(self.take(note=note, now=NOON))
        self.assertEqual(len(told), 2)
        self.assertTrue(told[0].startswith(f"{self.root / BOOK_NAME}: line 1: not valid JSON"), told[0])
        self.assertTrue(told[1].startswith(f"{self.root / BOOK_NAME}: line 3: not a version 1 object"), told[1])
        self.assertTrue(all(text.endswith("left out of the count, and dropped when the book is next written") for text in told))
        self.assertEqual([entry["kind"] for entry in book_entries(self.root)], ["reserve", "reserve"])  # the intact one and the new one

    def test_a_refusal_tells_of_the_damage_too_and_leaves_it_in_the_file(self) -> None:
        young = book_lines((2 * HOUR, BY_WRAPUP), (HOUR, BY_WRAPUP), now=NOON, worktree=self.worktree)
        self.damaged_book(young[0], "garbage", young[1])
        before, told = self.book_bytes(), []
        self.assertIsNotNone(self.take(note=told.append, now=NOON))  # the lines around the damage go on counting
        self.assertEqual(len(told), 1)
        self.assertEqual(self.book_bytes(), before)

    def test_without_a_note_the_damage_is_passed_over_and_nothing_is_told_when_there_is_none(self) -> None:
        self.damaged_book("garbage")
        self.assertIsNone(self.take(now=NOON))
        told = []
        self.assertIsNone(self.take(note=told.append, now=NOON + MINUTE))
        self.assertEqual(told, [])

    def test_the_clock_is_read_inside_the_lock(self) -> None:
        def clock_while_the_lock_is_held() -> datetime:
            with self.assertRaises(RatchetError), reservation_lock(self.root, wait=0.05):
                self.fail("the lock was free when the clock was read")
            return NOON

        with mock.patch("ratchet.review.utc_now", clock_while_the_lock_is_held):
            self.assertIsNone(self.take())
        self.assertEqual(book_entries(self.root)[0]["ts"], "2026-01-15T12:00:00+00:00")

    def test_a_lock_that_stays_held_is_a_named_error_and_nothing_is_written(self) -> None:
        with reservation_lock(self.root), mock.patch.object(reservations, "LOCK_WAIT_SECONDS", 0.2):
            with self.assertRaises(RatchetError) as caught:
                self.take()
        self.assertIn("another process is counting automatic reviews", str(caught.exception))
        self.assertFalse((self.root / BOOK_NAME).exists())

    def test_a_book_that_cannot_be_read_is_a_named_error_and_the_lock_is_let_go(self) -> None:
        (self.root / BOOK_NAME).mkdir(parents=True)
        with self.assertRaises(RatchetError) as caught:
            self.take()
        self.assertIn(BOOK_NAME, str(caught.exception))
        with reservation_lock(self.root, wait=0.2):
            pass


class CappedCase(test_review_refusal.RefusalCase):
    """A repo on `main` with rules, one commit and 250 lines of new work; two automatic reviews were started today, hours ago, which
    uses up the default limit of two. The book holds them: the ledger holds nothing of the cap."""

    def setUp(self) -> None:
        super().setUp()
        self.put("big.py", numbered(250))
        self.planted = plant_book(self.repo, (2 * HOUR, BY_WRAPUP), (HOUR, BY_WRAPUP))

    def set_limit(self, limit: int, **config: object) -> None:
        self.write_config(rules_files=["rules.md"], wrapup_auto_max_per_day=limit, **config)

    def refused_message(self) -> str:
        refusal = self.refusal(automatic=True)
        self.assertEqual(refusal.kind, REFUSE_DAILY_CAP)
        return refusal.message

    def book_bytes(self) -> bytes | None:
        path = self.repo / BOOK_NAME
        return path.read_bytes() if path.exists() else None

    def claude_that(self, body: str, name: str = "other-claude") -> Path:
        """An executable standing in for claude that answers `--version` and then runs the shell `body`."""
        script = self.tmp / name
        script.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo 2.1.0; exit 0; fi\n{body}", encoding="utf-8")
        script.chmod(0o755)
        return script


class ReviewRefusalTest(CappedCase):
    """`review_refusal` meets the daily cap only for an automatic review, and in its place among the other refusals: the last."""

    def test_an_automatic_review_is_refused_and_one_a_person_starts_is_not(self) -> None:
        refusal = self.refusal(automatic=True)
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_DAILY_CAP, None))
        self.assertTrue(refusal.message.startswith(
            "daily cap reached: 2 automatic review(s) were started or asked for in the last 24 hours and "))
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal(automatic=False))

    def test_a_saved_review_runs_no_claude_so_it_is_never_refused_for_the_cap(self) -> None:
        self.assertIsNone(self.refusal(automatic=True, from_file=True))

    def test_over_cap_gets_past_the_round_cap_and_not_the_daily_one(self) -> None:
        self.assertEqual(self.refusal(automatic=True, over_cap_allowed=True).kind, REFUSE_DAILY_CAP)

    def test_below_the_limit_an_automatic_review_is_not_refused(self) -> None:
        self.set_limit(3)
        self.assertIsNone(self.refusal(automatic=True))

    def test_broken_rules_come_before_the_daily_cap(self) -> None:
        self.rules_message()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_RULES)

    def test_a_detached_head_comes_before_the_daily_cap(self) -> None:
        self.detach()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_ROUND_KEY)

    def test_a_full_round_cap_comes_before_the_daily_cap(self) -> None:
        key = self.record_rounds(3)
        refusal = self.refusal(automatic=True)
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_ROUND_CAP, key))

    def test_a_missing_claude_comes_before_the_daily_cap(self) -> None:
        self.missing_claude()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_CLAUDE)  # a review that cannot run takes no slot, so it meets no cap
        self.assertEqual(self.refusal().kind, REFUSE_CLAUDE)

    def test_a_missing_claude_is_still_refused_for_an_automatic_review_below_the_limit(self) -> None:
        self.set_limit(3)
        self.missing_claude()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_CLAUDE)

    def test_asking_ahead_takes_no_slot_and_writes_nothing(self) -> None:
        for limit in (2, 3):
            with self.subTest(limit=limit):
                self.set_limit(limit)
                before = self.book_bytes()
                self.assertEqual(self.refusal(automatic=True) is None, limit == 3)
                self.assertEqual(self.book_bytes(), before)
                self.assertFalse((self.repo / LOCK_NAME).exists())

    def test_a_waiting_reservation_of_the_hook_for_this_worktree_is_room_when_asking_ahead(self) -> None:
        plant_book(self.repo, (2 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK))
        self.assertIsNone(self.refusal(automatic=True))  # the limit is reached, by the very reservation that waits for this review

    def test_the_ledger_counts_for_nothing_whatever_the_trigger_of_its_review_runs(self) -> None:
        (self.repo / BOOK_NAME).unlink()
        recent = datetime.now().isoformat(timespec="seconds")
        self.append_raw(*[review_round(OTHER_CHANGE, run=f"manual{number}", ts=recent) for number in range(1, 4)],
                        *[review_round(OTHER_CHANGE, run=f"auto{number}", ts=recent, trigger=TRIGGER_AUTO) for number in range(1, 4)])
        self.assertIsNone(self.refusal(automatic=True))

    def test_a_book_that_cannot_be_read_is_a_named_error_for_an_automatic_review_only(self) -> None:
        (self.repo / BOOK_NAME).unlink()
        (self.repo / BOOK_NAME).mkdir()
        with self.assertRaises(RatchetError) as caught:
            self.refusal(automatic=True)
        self.assertIn(BOOK_NAME, str(caught.exception))
        self.assertIsNone(self.refusal())  # a review a person starts reads no book


class PlanTest(CappedCase):
    """`plan_wrapup`, which the Stop hook and `wrapup --dry-run` ask whether an automatic review would be refused."""

    def plan(self, **options: object) -> WrapupPlan:
        config = load_config(self.repo, None, None, self.repo)
        return plan_wrapup(config, read_events(config.ledger_path), **options)

    def test_an_automatic_plan_foresees_the_cap_when_asked_to(self) -> None:
        plan = self.plan(automatic=True, foresee_refusal=True)
        self.assertEqual((plan.due, plan.would_refuse.kind, plan.would_refuse.round_key), (True, REFUSE_DAILY_CAP, None))
        self.assertEqual(plan.would_refuse.message, self.refused_message())

    def test_it_is_looked_for_only_when_asked_for(self) -> None:
        self.assertIsNone(self.plan(automatic=True).would_refuse)

    def test_a_plan_that_is_not_automatic_does_not_meet_the_cap(self) -> None:
        self.assertIsNone(self.plan(foresee_refusal=True).would_refuse)

    def test_a_saved_review_is_not_refused_for_the_cap(self) -> None:
        self.assertIsNone(self.plan(automatic=True, foresee_refusal=True, from_file=True).would_refuse)

    def test_less_work_than_the_thresholds_is_not_due_and_nothing_is_foreseen(self) -> None:
        (self.repo / "big.py").unlink()
        plan = self.plan(automatic=True, foresee_refusal=True)
        self.assertEqual((plan.due, plan.would_refuse), (False, None))

    def test_a_plan_takes_no_slot(self) -> None:
        self.set_limit(3)
        before = self.book_bytes()
        self.assertIsNone(self.plan(automatic=True, foresee_refusal=True).would_refuse)
        self.assertEqual(self.book_bytes(), before)


class AutoWrapupTest(CappedCase):
    """`ratchet wrapup --auto`: the review it runs is marked and takes a slot as it starts, and past the cap it is refused before claude runs."""

    def test_an_automatic_review_below_the_limit_runs_and_is_recorded_as_automatic(self) -> None:
        self.set_limit(3)
        reviewed = self.wrap("--auto")
        self.assertEqual((reviewed["status"], reviewed["run"]), ("reviewed", "r1"))
        self.assertEqual(self.claude_calls(), [["-p", "/code-review high"]])
        (own,) = [run for run in self.review_runs() if run["run"] == "r1"]
        self.assertEqual(own["trigger"], "auto")

    def test_it_takes_a_slot_as_a_reservation_of_its_own_in_the_book_of_the_main_worktree(self) -> None:
        self.set_limit(3)
        before = book_entries(self.repo)
        self.wrap("--auto")
        *old, new = book_entries(self.repo)
        self.assertEqual(old, before)
        self.assertRegex(new.pop("id"), r"^[0-9a-f]{16}$")
        self.assertEqual({key: new[key] for key in ("v", "kind", "worktree", "head", "by")},
                         {"v": 1, "kind": "reserve", "worktree": str(self.repo), "head": git(self.repo, "rev-parse", "HEAD"), "by": "wrapup"})
        self.assertLess(abs(datetime.fromisoformat(new["ts"]) - datetime.now(timezone.utc)), 5 * MINUTE)

    def test_a_book_it_creates_is_private_to_the_user(self) -> None:
        self.set_limit(3)
        (self.repo / BOOK_NAME).unlink()
        self.wrap("--auto")
        self.assertEqual(stat.S_IMODE((self.repo / BOOK_NAME).stat().st_mode), 0o600)

    def test_a_wrapup_and_a_review_without_auto_record_no_trigger_and_take_no_slot(self) -> None:
        before = self.book_bytes()
        self.assertEqual(self.wrap()["status"], "reviewed")
        self.more_work()
        self.cli_ok("review", "--run", "R2")
        self.more_work("evenmore.py")
        self.assertEqual(self.wrap()["status"], "reviewed")
        own = [run for run in self.review_runs() if run["run"] in ("r1", "R2", "r3")]
        self.assertEqual(len(own), 3)
        self.assertTrue(all("trigger" not in run for run in own))
        self.assertEqual(self.book_bytes(), before)

    def test_at_the_limit_it_is_refused_with_exit_4_and_claude_is_not_run_at_all(self) -> None:
        dump = self.tmp / "claude-everything.jsonl"
        self.use(dump_env_claude(self.tmp, dump))  # records even `--version`, which a fake that logs only reviews does not
        before, book = self.ledger_bytes(), self.book_bytes()
        code, out, err = self.cli("wrapup", "--auto")
        message = self.refused_message()
        self.assertEqual((code, err), (4, f"ratchet: error: {message}\n"))
        self.assertIn("wrapup: changed lines 250 >= wrapup_min_changed_lines 200; running a review", out)
        self.assertNotIn("review round", out)  # the refusal comes before the round line, as the round cap's does
        self.assertNotIn("--over-cap", message)  # no way past this one on the command line
        self.assertFalse(dump.exists())
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.book_bytes(), book)
        self.assertEqual(sorted(path.name for path in (self.repo / ".ratchet").iterdir()), [Path(BOOK_NAME).name])  # no lock, no review saved

    def test_the_count_under_the_lock_decides_when_the_check_ahead_saw_room_that_another_run_then_took(self) -> None:
        dump = self.tmp / "claude-everything.jsonl"
        self.use(dump_env_claude(self.tmp, dump))
        before, book = self.ledger_bytes(), self.book_bytes()
        with mock.patch("ratchet.cli.review_refusal", return_value=None):  # the check without the lock found room; the count with it finds none
            code, out, err = self.cli("wrapup", "--auto")
        self.assertEqual((code, err), (4, f"ratchet: error: {self.refused_message()}\n"))
        self.assertNotIn("review round", out)
        self.assertFalse(dump.exists())
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.book_bytes(), book)

    def test_with_json_it_is_a_daily_capped_object_and_the_exit_code_is_still_4(self) -> None:
        before, book = self.ledger_bytes(), self.book_bytes()
        capped = self.wrap("--auto", expect=4)
        message = self.refused_message()
        self.assertEqual(set(capped), JSON_KEYS)
        self.assertEqual((capped["status"], capped["reason"], capped["run"], capped["would_refuse"]), ("daily_capped", message, None, None))
        self.assertEqual((capped["new_unmapped"], capped["open_unmapped"], capped["changed_lines"], capped["changed_files"]), ([], 0, 250, 1))
        self.assertIn(f"wrapup: {message}", self.last_err)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.book_bytes(), book)

    def test_a_lower_limit_in_the_config_is_the_cap(self) -> None:
        self.set_limit(1)
        self.assertIn("wrapup_auto_max_per_day is 1.", self.wrap("--auto", expect=4)["reason"])

    def test_a_reservation_more_than_a_day_old_is_not_counted_and_the_write_drops_it(self) -> None:
        plant_book(self.repo, (DAY + HOUR, BY_WRAPUP), (2 * HOUR, BY_WRAPUP))  # one is left of two
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.assertEqual([entry["kind"] for entry in book_entries(self.repo)], ["reserve", "reserve"])  # the day-old one is gone

    def test_review_runs_in_the_ledger_count_for_nothing_whatever_their_trigger(self) -> None:
        (self.repo / BOOK_NAME).unlink()
        recent = datetime.now().isoformat(timespec="seconds")
        self.append_raw(*[review_round(OTHER_CHANGE, run=f"manual{number}", ts=recent) for number in range(1, 6)],
                        *[review_round(OTHER_CHANGE, run=f"auto{number}", ts=recent, trigger=TRIGGER_AUTO) for number in range(1, 6)])
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")

    def test_the_count_is_the_whole_repo_and_not_the_branch_that_is_reviewed(self) -> None:
        git(self.repo, "switch", "-q", "-c", "feature")
        self.assertTrue(self.key().startswith("feature@"))  # the planted reservations were made on another branch, and still count
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")

    def test_a_manual_wrapup_at_the_limit_still_reviews_and_is_not_counted_either(self) -> None:
        before = self.book_bytes()
        manual = self.wrap()
        self.assertEqual((manual["status"], manual["run"]), ("reviewed", "r1"))
        self.assertEqual(len(self.claude_calls()), 1)
        self.more_work()
        refused = self.wrap("--auto", expect=4)
        self.assertTrue(refused["reason"].startswith("daily cap reached: 2 automatic review(s)"))  # two, not three
        self.assertEqual(len(self.claude_calls()), 1)
        self.assertEqual(self.book_bytes(), before)

    def test_the_round_cap_is_met_before_the_daily_one(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.record_rounds(1)
        before = self.book_bytes()
        capped = self.wrap("--auto", expect=3)
        self.assertEqual(capped["status"], "capped")
        self.assertNotIn("daily cap", capped["reason"])
        self.assertEqual(self.book_bytes(), before)

    def test_a_saved_review_cannot_be_called_automatic(self) -> None:
        saved = self.write_review()
        for extra in ([], ["--dry-run"]):
            with self.subTest(extra=extra):
                code, _, err = self.cli("wrapup", "--auto", "--from-file", str(saved), *extra)
                self.assertEqual(code, 1)
                self.assertIn("--auto marks a review that runs claude; it does nothing with --from-file", err)

    def test_it_takes_no_part_in_a_report(self) -> None:
        code, _, err = self.cli("wrapup", "--report-only", "--auto")
        self.assertEqual(code, 1)
        self.assertIn("--report-only measures and reviews nothing; it takes none of --base, --level, --from-file, --dry-run or --auto", err)

    def test_a_dry_run_foresees_the_cap_only_for_an_automatic_wrapup(self) -> None:
        before, book = self.ledger_bytes(), self.book_bytes()
        dry = self.measure("--auto")
        message = self.refused_message()
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual(dry["would_refuse"], {"kind": "daily_cap", "round_key": None, "message": message})
        code, out, err = self.cli("wrapup", "--dry-run", "--auto")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"[dry-run] wrapup: changed lines 250 >= wrapup_min_changed_lines 200; a real run would be refused: {message}; "
                      "nothing run or written", out)
        plain = self.measure()
        self.assertEqual((plain["status"], plain["would_refuse"]), ("dry-run", None))
        self.assertIn("would run", self.cli("wrapup", "--dry-run")[1])
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.book_bytes(), book)

    def test_the_real_run_is_refused_with_the_words_the_dry_run_gave(self) -> None:
        foreseen = self.measure("--auto")["would_refuse"]
        real = self.wrap("--auto", expect=4)
        self.assertEqual((real["status"], real["reason"]), ("daily_capped", foreseen["message"]))

    def test_a_dry_run_below_the_limit_foresees_nothing_and_takes_no_slot(self) -> None:
        self.set_limit(3)
        before = self.book_bytes()
        self.assertEqual(self.measure("--auto")["would_refuse"], None)
        self.assertEqual(self.book_bytes(), before)

    def test_the_third_automatic_wrapup_of_two_real_ones_is_refused(self) -> None:
        (self.repo / BOOK_NAME).unlink()  # no planted reservations: the cap is used up by wrap-ups that really ran here
        self.more_work("first.py")
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work("second.py")
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work("third.py")
        before, book = self.ledger_bytes(), self.book_bytes()
        refused = self.wrap("--auto", expect=4)
        self.assertEqual(refused["status"], "daily_capped")
        self.assertTrue(refused["reason"].startswith("daily cap reached: 2 automatic review(s)"))
        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.book_bytes(), book)
        self.assertEqual([run.get("trigger") for run in self.review_runs()], ["auto", "auto"])
        self.assertEqual([(entry["kind"], entry["by"]) for entry in book_entries(self.repo)], [("reserve", "wrapup")] * 2)

    def test_a_damaged_line_in_the_book_is_warned_about_and_the_lines_around_it_go_on_counting(self) -> None:
        path = self.repo / BOOK_NAME
        first, second = path.read_text(encoding="utf-8").splitlines()
        path.write_text(f"{first}\nnot a reservation\n{second}\n", encoding="utf-8")
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")  # two good lines: the limit is reached all the same
        self.set_limit(3)
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.assertIn(f"warning: {path}: line 2: not valid JSON", self.last_err)
        self.assertEqual([entry["kind"] for entry in book_entries(self.repo)], ["reserve"] * 3)  # the damaged line is gone

    def test_a_lock_that_stays_held_stops_the_review_with_a_named_error_before_claude_runs(self) -> None:
        self.set_limit(3)
        before = self.book_bytes()
        with reservation_lock(self.repo), mock.patch.object(reservations, "LOCK_WAIT_SECONDS", 0.2):
            code, _, err = self.cli("wrapup", "--auto", "--json")
        self.assertEqual(code, 1)
        self.assertIn("ratchet: error: another process is counting automatic reviews", err)
        self.assertEqual((self.claude_calls(), self.book_bytes()), ([], before))


class FailedReviewTest(CappedCase):
    """The slot is taken as the review starts, so a review that fails, times out or prints no findings still counts, and the retry is
    refused at the limit. The count is of requests, which is what the daily cap is there to bound."""

    def setUp(self) -> None:
        super().setUp()
        self.set_limit(3)  # one slot is left

    def fails(self, script: Path, *, expect: int = 1) -> str:
        """`wrapup --auto --json` with `script` for claude; what it said on stderr."""
        self.use(script)
        code, _, err = self.cli("wrapup", "--auto", "--json")
        self.assertEqual(code, expect, err)
        return err

    def retry_is_refused(self) -> None:
        self.use(self.claude)
        self.set_limit(3)
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")
        self.assertEqual(self.claude_calls(), [])

    def test_a_review_whose_claude_exits_with_an_error_counts_and_the_retry_is_refused(self) -> None:
        self.assertIn("exited with 7", self.fails(self.claude_that("exit 7\n")))
        self.assertEqual(self.review_runs(), [])
        self.assertEqual([entry["by"] for entry in book_entries(self.repo)], ["wrapup"] * 3)
        self.retry_is_refused()

    def test_a_review_that_prints_no_findings_counts_and_the_retry_is_refused(self) -> None:
        self.assertIn("neither a JSON array nor text containing", self.fails(self.claude_that("echo 'nothing to report here'\n")))
        self.assertEqual(self.review_runs(), [])
        self.assertEqual(len(book_entries(self.repo)), 3)
        self.retry_is_refused()

    def test_a_review_that_times_out_counts_and_the_retry_is_refused(self) -> None:
        with mock.patch("ratchet.review.TIMEOUT_SECONDS", 0.5):
            err = self.fails(self.claude_that("exec sleep 30\n"))
        self.assertIn("did not finish within 0.5s", err)
        self.assertEqual(len(book_entries(self.repo)), 3)
        self.retry_is_refused()

    def test_the_slot_is_already_in_the_book_when_claude_is_asked(self) -> None:
        seen = self.tmp / "book-when-asked.jsonl"
        self.fails(self.claude_that(f"cp {self.repo / BOOK_NAME} {seen}\nexit 7\n"))
        self.assertEqual(len(seen.read_text(encoding="utf-8").splitlines()), 3)  # the two planted and the review's own


class HookReservationTest(CappedCase):
    """The reservation the Stop hook made for a worktree is the slot of the `wrapup --auto` it asked for: it is used once, and only
    while it is young, and a run that does not say `--auto` does not use it."""

    def hook_asked(self, age: timedelta, *, worktree: Path | None = None) -> list:
        """The book as the hook leaves it after an ask `age` ago: a run's reservation and the hook's, which reach the limit of two."""
        return plant_book(self.repo, (3 * HOUR, BY_WRAPUP), (age, BY_HOOK), worktree=worktree)

    def test_a_young_reservation_of_the_hook_is_used_and_not_counted_even_at_the_limit(self) -> None:
        [_, waiting] = self.hook_asked(5 * MINUTE)
        reviewed = self.wrap("--auto")
        self.assertEqual((reviewed["status"], len(self.claude_calls())), ("reviewed", 1))
        self.assertEqual([run.get("trigger") for run in self.review_runs() if run["run"] == "r1"], ["auto"])
        *kept, consumption = book_entries(self.repo)
        self.assertEqual([entry["kind"] for entry in kept], ["reserve", "reserve"])
        self.assertEqual((consumption["kind"], consumption["id"]), ("consume", waiting.id))

    def test_it_is_used_once_so_the_next_run_counts_and_is_refused_at_the_limit(self) -> None:
        self.hook_asked(5 * MINUTE)
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work()
        before = self.book_bytes()
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")
        self.assertEqual((len(self.claude_calls()), self.book_bytes()), (1, before))

    def test_it_is_used_for_less_than_an_hour_and_the_reservation_counts_for_ever_after(self) -> None:
        for age, used in ((50 * MINUTE, True), (70 * MINUTE, False)):  # margins of ten minutes on both sides of the hour
            with self.subTest(age=age):
                self.hook_asked(age)
                self.assertEqual(self.wrap("--auto", expect=0 if used else 4)["status"], "reviewed" if used else "daily_capped")
                self.more_work(f"work-{int(age.total_seconds())}.py")

    def test_it_is_for_the_worktree_the_hook_asked_about_and_no_other(self) -> None:
        self.hook_asked(5 * MINUTE, worktree=self.tmp / "another-worktree")
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")

    def test_a_run_that_does_not_say_auto_does_not_use_it_and_the_reservation_stays_counted_and_waiting(self) -> None:
        [_, waiting] = self.hook_asked(5 * MINUTE)  # the model that ran the wrap-up dropped `--auto`
        before = self.book_bytes()
        self.assertEqual(self.wrap()["status"], "reviewed")
        self.assertEqual(self.book_bytes(), before)
        self.assertEqual(self.refusal(automatic=True), None)  # still waiting for its own wrap-up
        self.more_work()
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.assertEqual(book_entries(self.repo)[-1]["id"], waiting.id)

    def test_a_dry_run_takes_a_waiting_reservation_for_room_and_uses_none(self) -> None:
        self.hook_asked(5 * MINUTE)
        before = self.book_bytes()
        self.assertIsNone(self.measure("--auto")["would_refuse"])
        self.assertEqual(self.book_bytes(), before)

    def test_a_reservation_is_used_by_the_oldest_waiting_first(self) -> None:
        self.set_limit(5)
        older, younger = plant_book(self.repo, (30 * MINUTE, BY_HOOK), (10 * MINUTE, BY_HOOK))
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work()
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.assertEqual([entry["id"] for entry in book_entries(self.repo)[2:]], [older.id, younger.id])


class NoSlotTest(test_review_refusal.RefusalCase):
    """A run that is refused or cannot run takes no slot: it writes nothing, not even the lock. The repo has no book to begin with."""

    def setUp(self) -> None:
        super().setUp()
        self.put("big.py", numbered(250))

    def assert_no_book_and_no_lock(self) -> None:
        self.assertEqual([(self.repo / name).exists() for name in (BOOK_NAME, LOCK_NAME)], [False, False])

    def test_the_first_slot_makes_the_book_and_the_lock_in_the_main_worktree(self) -> None:
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.assertEqual([(self.repo / name).exists() for name in (BOOK_NAME, LOCK_NAME)], [True, True])
        self.assertEqual([entry["by"] for entry in book_entries(self.repo)], ["wrapup"])

    def test_broken_rules_take_no_slot(self) -> None:
        self.rules_message()
        code, _, err = self.cli("wrapup", "--auto", "--json")
        self.assertEqual(code, 1, err)
        self.assert_no_book_and_no_lock()

    def test_a_full_round_cap_takes_no_slot(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.record_rounds(1)
        self.assertEqual(self.wrap("--auto", expect=3)["status"], "capped")
        self.assert_no_book_and_no_lock()

    def test_a_missing_claude_takes_no_slot(self) -> None:
        self.missing_claude()
        code, _, err = self.cli("wrapup", "--auto", "--json")
        self.assertEqual(code, 1, err)
        self.assertIn("not found on PATH", err)
        self.assert_no_book_and_no_lock()

    def test_work_below_the_thresholds_takes_no_slot(self) -> None:
        (self.repo / "big.py").unlink()
        self.assertEqual(self.wrap("--auto")["status"], "skipped")
        self.assert_no_book_and_no_lock()

    def test_a_dry_run_takes_no_slot_and_makes_no_state_directory(self) -> None:
        self.assertIsNone(self.measure("--auto")["would_refuse"])
        self.assertFalse((self.repo / ".ratchet").exists())

    def test_a_wrapup_and_a_review_without_auto_take_no_slot(self) -> None:
        self.assertEqual(self.wrap()["status"], "reviewed")
        self.more_work()
        self.cli_ok("review", "--run", "R2")
        self.assert_no_book_and_no_lock()


class LinkedWorktreeCapTest(CappedCase):
    """The book is the main worktree's, so the cap counts the slots of every worktree together."""

    def linked_worktree(self) -> Path:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        self.put("linked_big.py", numbered(250), linked)
        return linked

    def wrap_in(self, linked: Path) -> tuple[int, str, str]:
        return self.cli("wrapup", "--json", "--auto", "--repo", str(linked), with_repo=False)

    def test_a_review_in_a_linked_worktree_meets_the_reservations_of_the_main_one(self) -> None:
        code, out, err = self.wrap_in(self.linked_worktree())
        self.assertEqual(code, 4, err)
        self.assertEqual(json.loads(out)["status"], "daily_capped")
        self.assertEqual(self.claude_calls(), [])

    def test_a_dry_run_in_a_linked_worktree_foresees_the_cap_from_the_main_worktrees_book(self) -> None:
        linked = self.linked_worktree()
        code, out, err = self.cli("wrapup", "--json", "--auto", "--dry-run", "--repo", str(linked), with_repo=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["would_refuse"]["kind"], REFUSE_DAILY_CAP)
        self.assertFalse((linked / ".ratchet").exists())

    def test_a_slot_taken_in_a_linked_worktree_is_in_the_main_worktrees_book_for_that_worktree(self) -> None:
        self.set_limit(3)
        linked = self.linked_worktree()
        code, _, err = self.wrap_in(linked)
        self.assertEqual(code, 0, err)
        last = book_entries(self.repo)[-1]
        self.assertEqual((last["kind"], last["by"], last["worktree"]), ("reserve", "wrapup", str(linked)))
        self.assertFalse((linked / ".ratchet").exists())

    def test_a_reservation_of_the_hook_for_a_linked_worktree_is_used_by_a_wrapup_there_and_not_by_the_main_one(self) -> None:
        linked = self.linked_worktree()
        [_, waiting] = plant_book(self.repo, (3 * HOUR, BY_WRAPUP), (5 * MINUTE, BY_HOOK), worktree=linked)
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")
        code, _, err = self.wrap_in(linked)
        self.assertEqual(code, 0, err)
        self.assertEqual(book_entries(self.repo)[-1]["id"], waiting.id)


if __name__ == "__main__":
    unittest.main()
