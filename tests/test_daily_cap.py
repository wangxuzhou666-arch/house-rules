"""The daily cap on automatic reviews: the config key, the 24 hours it counts over, the refusal, and `ratchet wrapup --auto`.

Nothing here runs the real claude: the review is an executable fake. A test that goes through the CLI plants reviews that are hours
old or more than a day old, so that the real clock cannot move one across the edge while the test runs; the exact edge of the 24
hours is tested on the pure functions, with a `now` the test chooses.
"""
import contextlib
import json
import os
import re
import time
import unittest
from collections.abc import Iterator
from datetime import datetime, timedelta

import support  # noqa: F401  (puts the repo root on sys.path)
import test_review_refusal
from support import RepoCase, git, make_config, review_finding, review_round
from test_review_refusal import dump_env_claude
from test_wrapup import numbered

from ratchet.config import DEFAULT_WRAPUP_AUTO_MAX_PER_DAY, load_config
from ratchet.ledger import read_events
from ratchet.review import (
    DAILY_WINDOW, REFUSE_CLAUDE, REFUSE_DAILY_CAP, REFUSE_ROUND_CAP, REFUSE_ROUND_KEY, REFUSE_RULES, TRIGGER_AUTO, Refusal,
    automatic_review_times, daily_cap_refusal, review_run_event,
)
from ratchet.wrapup import WrapupPlan, plan_wrapup

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
# Noon of a January day, local time as a ledger spells it. Mid-January is away from every clock change, so the 24 hours before it
# are the same on the wall clock and on a stopwatch.
LOCAL_NOON = datetime(2026, 1, 15, 12, 0, 0)
OTHER_CHANGE = "other@0123456789ab"  # not the change a test reviews, so these rounds do not count toward the round cap there
# What a refusal says when two automatic reviews, at 10:00 and 11:30 on the day of LOCAL_NOON, used up a limit of 2.
TWO_AT_NOON = ("daily cap reached: 2 automatic review(s) ran in the last 24 hours and wrapup_auto_max_per_day is 2. "
               "The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves that window; "
               "the work stays counted until then, and the first automatic wrap-up after that reviews it. "
               "/review-ratchet:wrapup (`ratchet wrapup` without --auto) reviews it now.")
# The keys of every `wrapup --json` object, whatever its status.
JSON_KEYS = {"status", "base", "base_reason", "changed_lines", "changed_files", "thresholds", "run", "new_unmapped",
             "repeated_unmapped", "repeat_of", "open_unmapped", "reason", "would_refuse"}


def auto_reviews(*ages: timedelta, now: datetime | None = None, key: str = OTHER_CHANGE) -> list[dict]:
    """The `review_run` of an automatic review for each age: it ran that long before `now` (naive local time; the clock when None)."""
    moment = now or datetime.now()
    return [review_round(key, run=f"auto{number}", ts=(moment - age).isoformat(timespec="seconds"), trigger=TRIGGER_AUTO)
            for number, age in enumerate(ages, start=1)]


@contextlib.contextmanager
def local_zone(posix_tz: str) -> Iterator[None]:
    """The time zone of this process, as the POSIX `TZ` string says, for the length of the block. A fixed offset such as `IST-5:30`
    (UTC+5:30) keeps a test from depending on where it runs: reading a local time as UTC is then a wrong answer, not a lucky one."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = posix_tz
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


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


class AutomaticReviewTimesTest(unittest.TestCase):
    """Which reviews the cap counts: the automatic ones of the 24 hours before now, in the whole ledger."""

    def times(self, *events: dict) -> list[datetime]:
        return automatic_review_times(list(events), LOCAL_NOON.astimezone())

    def test_the_window_is_24_hours(self) -> None:
        self.assertEqual(DAILY_WINDOW, DAY)

    def test_a_review_inside_the_last_24_hours_is_counted_and_one_outside_is_not(self) -> None:
        self.assertEqual(len(self.times(*auto_reviews(DAY - MINUTE, now=LOCAL_NOON))), 1)
        self.assertEqual(self.times(*auto_reviews(DAY + MINUTE, now=LOCAL_NOON)), [])

    def test_a_review_exactly_24_hours_old_is_out_and_one_second_younger_is_in(self) -> None:
        self.assertEqual(self.times(*auto_reviews(DAY, now=LOCAL_NOON)), [])  # the next one may run at that very moment
        self.assertEqual(len(self.times(*auto_reviews(DAY - timedelta(seconds=1), now=LOCAL_NOON))), 1)

    def test_the_day_is_the_last_24_hours_and_not_the_calendar_date(self) -> None:
        (moment,) = self.times(*auto_reviews(23 * HOUR, now=LOCAL_NOON))  # 13:00 of the day before
        self.assertEqual(moment.date(), (LOCAL_NOON - DAY).date())

    def test_a_review_dated_after_now_is_still_counted(self) -> None:
        self.assertEqual(len(self.times(*auto_reviews(-HOUR, now=LOCAL_NOON))), 1)  # a clock set back must not let reviews slip

    def test_a_review_a_person_started_is_not_counted(self) -> None:
        (auto,) = auto_reviews(HOUR, now=LOCAL_NOON)
        manual = {key: value for key, value in auto.items() if key != "trigger"}
        self.assertEqual(self.times(manual), [])
        self.assertEqual(self.times({**auto, "trigger": "a-trigger-a-later-version-adds"}), [])
        self.assertEqual(len(self.times(auto)), 1)

    def test_only_review_runs_are_read(self) -> None:
        recent = (LOCAL_NOON - HOUR).isoformat(timespec="seconds")
        self.assertEqual(self.times(review_finding(ts=recent)), [])

    def test_the_whole_ledger_counts_whatever_the_change_they_reviewed(self) -> None:
        events = [review_round(key, run=f"auto{number}", ts=(LOCAL_NOON - HOUR).isoformat(timespec="seconds"), trigger=TRIGGER_AUTO)
                  for number, key in enumerate(("main@0a1b2c3d4e5f", "feature@0a1b2c3d4e5f", "target:42"), start=1)]
        self.assertEqual(len(self.times(*events)), 3)

    def test_they_come_oldest_first_whatever_the_order_of_the_ledger(self) -> None:
        events = auto_reviews(3 * HOUR, 5 * HOUR, HOUR, now=LOCAL_NOON)
        moments = self.times(*events)
        self.assertEqual(moments, sorted(moments))
        self.assertEqual([LOCAL_NOON - moment.replace(tzinfo=None) for moment in moments], [5 * HOUR, 3 * HOUR, HOUR])

    def test_a_ledger_time_is_local_time_and_not_utc(self) -> None:
        for zone in ("IST-5:30", "EST5"):  # east and west of UTC: reading local time as UTC is wrong in both
            with self.subTest(zone=zone), local_zone(zone):
                now = LOCAL_NOON.astimezone()
                self.assertEqual(len(automatic_review_times(auto_reviews(22 * HOUR, now=LOCAL_NOON), now)), 1)
                self.assertEqual(automatic_review_times(auto_reviews(26 * HOUR, now=LOCAL_NOON), now), [])


class DailyCapRefusalTest(unittest.TestCase):
    """The cap itself: when it holds, which review frees the next one, and the words that say so."""

    def refusal(self, events: list[dict], *, limit: int = 2, now: datetime = LOCAL_NOON) -> Refusal | None:
        return daily_cap_refusal(make_config(wrapup_auto_max_per_day=limit), events, now.astimezone())

    def test_below_the_limit_there_is_no_refusal(self) -> None:
        self.assertIsNone(self.refusal([]))
        self.assertIsNone(self.refusal(auto_reviews(2 * HOUR, now=LOCAL_NOON)))

    def test_at_the_limit_it_is_refused_in_these_words_with_no_round_key(self) -> None:
        events = auto_reviews(2 * HOUR, 30 * MINUTE, now=LOCAL_NOON)
        self.assertEqual(self.refusal(events), Refusal(REFUSE_DAILY_CAP, None, TWO_AT_NOON))

    def test_the_next_review_can_run_exactly_when_the_message_says(self) -> None:
        events = auto_reviews(2 * HOUR, 30 * MINUTE, now=LOCAL_NOON)  # the one from 10:00 frees the slot at 10:00 the next day
        free = datetime(2026, 1, 16, 10, 0, 0)
        self.assertIsNone(self.refusal(events, now=free))
        self.assertIsNotNone(self.refusal(events, now=free - timedelta(seconds=1)))

    def test_a_limit_of_one_waits_for_the_one_review(self) -> None:
        refusal = self.refusal(auto_reviews(2 * HOUR, now=LOCAL_NOON), limit=1)
        self.assertEqual(refusal.kind, REFUSE_DAILY_CAP)
        self.assertTrue(refusal.message.startswith(
            "daily cap reached: 1 automatic review(s) ran in the last 24 hours and wrapup_auto_max_per_day is 1. "
            "The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves that window;"))

    def test_a_limit_of_three_is_reached_by_three_and_waits_for_the_oldest(self) -> None:
        self.assertIsNone(self.refusal(auto_reviews(5 * HOUR, 3 * HOUR, now=LOCAL_NOON), limit=3))
        refusal = self.refusal(auto_reviews(5 * HOUR, 3 * HOUR, HOUR, now=LOCAL_NOON), limit=3)
        self.assertTrue(refusal.message.startswith("daily cap reached: 3 automatic review(s)"))
        self.assertIn("The next one can run after 2026-01-16T07:00:00, when the one from 2026-01-15T07:00:00 leaves", refusal.message)

    def test_a_lowered_limit_waits_until_fewer_than_the_limit_are_left(self) -> None:
        events = auto_reviews(4 * HOUR, 3 * HOUR, 2 * HOUR, HOUR, now=LOCAL_NOON)  # at 08:00, 09:00, 10:00 and 11:00
        limit_two = self.refusal(events, limit=2)
        self.assertTrue(limit_two.message.startswith("daily cap reached: 4 automatic review(s)"))
        self.assertIn("The next one can run after 2026-01-16T10:00:00, when the one from 2026-01-15T10:00:00 leaves", limit_two.message)
        free = datetime(2026, 1, 16, 10, 0, 0)  # 08:00, 09:00 and 10:00 are out then and only 11:00 is left: one is below two
        self.assertIsNone(self.refusal(events, limit=2, now=free))
        self.assertIsNotNone(self.refusal(events, limit=2, now=free - timedelta(seconds=1)))
        limit_one = self.refusal(events, limit=1)
        self.assertIn("The next one can run after 2026-01-16T11:00:00, when the one from 2026-01-15T11:00:00 leaves", limit_one.message)
        self.assertIsNone(self.refusal(events, limit=1, now=datetime(2026, 1, 16, 11, 0, 0)))

    def test_reviews_a_person_started_never_count(self) -> None:
        manual = [review_round(OTHER_CHANGE, run=f"manual{number}", ts=(LOCAL_NOON - HOUR).isoformat(timespec="seconds"))
                  for number in range(1, 6)]
        self.assertIsNone(self.refusal(manual, limit=1))

    def test_an_automatic_review_more_than_a_day_old_does_not_count(self) -> None:
        self.assertIsNone(self.refusal(auto_reviews(DAY + HOUR, 2 * HOUR, now=LOCAL_NOON)))

    def test_the_message_holds_still_while_the_cap_does_and_changes_in_the_next_episode(self) -> None:
        events = auto_reviews(2 * HOUR, 30 * MINUTE, now=LOCAL_NOON)
        later = LOCAL_NOON + 5 * HOUR  # nothing new ran and the cap still holds: the Stop hook must see the same words and say them once
        self.assertEqual(self.refusal(events, now=later).message, self.refusal(events).message)
        next_day = datetime(2026, 1, 16, 12, 0, 0)  # both are out of the window; two new ones use the limit up again
        again = events + auto_reviews(2 * HOUR, 30 * MINUTE, now=next_day)
        self.assertNotEqual(self.refusal(again, now=next_day).message, self.refusal(events).message)

    def test_the_times_are_the_ones_the_ledger_spells_and_have_no_zone(self) -> None:
        events = auto_reviews(2 * HOUR, 30 * MINUTE, now=LOCAL_NOON)
        for zone in ("IST-5:30", "EST5"):
            with self.subTest(zone=zone), local_zone(zone):
                message = self.refusal(events).message
                self.assertEqual(message, TWO_AT_NOON)
                self.assertIsNone(re.search(r"\d\d:\d\d:\d\d[Z+-]", message))

    def test_the_time_the_next_review_can_run_is_24_hours_on_even_when_the_clocks_change_in_between(self) -> None:
        with local_zone("EST5EDT,M3.2.0,M11.1.0"):  # US Eastern: at 02:00 on 2026-03-08 the clocks go forward an hour
            now = datetime(2026, 3, 7, 18, 0, 0)
            message = self.refusal(auto_reviews(3 * HOUR, 2 * HOUR, now=now), now=now).message
        # 15:00 on the 7th is 20:00 UTC; 24 hours later is 20:00 UTC on the 8th, which the clocks then call 16:00 (not 15:00)
        self.assertIn("The next one can run after 2026-03-08T16:00:00, when the one from 2026-03-07T15:00:00 leaves", message)


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


class CappedCase(test_review_refusal.RefusalCase):
    """A repo on `main` with rules, one commit and 250 lines of new work; two automatic reviews of another change ran today, which
    uses up the default limit of two."""

    def setUp(self) -> None:
        super().setUp()
        self.put("big.py", numbered(250))
        self.append_raw(*auto_reviews(2 * HOUR, HOUR))

    def set_limit(self, limit: int, **config: object) -> None:
        self.write_config(rules_files=["rules.md"], wrapup_auto_max_per_day=limit, **config)

    def refused_message(self) -> str:
        refusal = self.refusal(automatic=True)
        self.assertEqual(refusal.kind, REFUSE_DAILY_CAP)
        return refusal.message


class ReviewRefusalTest(CappedCase):
    """`review_refusal` meets the daily cap only for an automatic review, and in its place among the other refusals."""

    def test_an_automatic_review_is_refused_and_one_a_person_starts_is_not(self) -> None:
        refusal = self.refusal(automatic=True)
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_DAILY_CAP, None))
        self.assertTrue(refusal.message.startswith("daily cap reached: 2 automatic review(s) ran in the last 24 hours and "))
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

    def test_the_daily_cap_comes_before_a_missing_claude(self) -> None:
        self.missing_claude()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_DAILY_CAP)
        self.assertEqual(self.refusal().kind, REFUSE_CLAUDE)  # without it the review would meet the missing claude

    def test_a_missing_claude_is_still_refused_for_an_automatic_review_below_the_limit(self) -> None:
        self.set_limit(3)
        self.missing_claude()
        self.assertEqual(self.refusal(automatic=True).kind, REFUSE_CLAUDE)


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


class AutoWrapupTest(CappedCase):
    """`ratchet wrapup --auto`: the review it runs is marked, and past the cap it is refused before claude runs."""

    def test_an_automatic_review_below_the_limit_runs_and_is_recorded_as_automatic(self) -> None:
        self.set_limit(3)
        reviewed = self.wrap("--auto")
        self.assertEqual((reviewed["status"], reviewed["run"]), ("reviewed", "r1"))
        self.assertEqual(self.claude_calls(), [["-p", "/code-review high"]])
        (own,) = [run for run in self.review_runs() if run["run"] == "r1"]
        self.assertEqual(own["trigger"], "auto")

    def test_a_wrapup_and_a_review_without_auto_record_no_trigger(self) -> None:
        self.assertEqual(self.wrap()["status"], "reviewed")
        self.more_work()
        self.cli_ok("review", "--run", "R2")
        self.more_work("evenmore.py")
        self.assertEqual(self.wrap()["status"], "reviewed")
        own = [run for run in self.review_runs() if run["run"] in ("r1", "R2", "r3")]
        self.assertEqual(len(own), 3)
        self.assertTrue(all("trigger" not in run for run in own))

    def test_at_the_limit_it_is_refused_with_exit_4_and_claude_is_not_run_at_all(self) -> None:
        dump = self.tmp / "claude-everything.jsonl"
        self.use(dump_env_claude(self.tmp, dump))  # records even `--version`, which a fake that logs only reviews does not
        before = self.ledger_bytes()
        code, out, err = self.cli("wrapup", "--auto")
        message = self.refused_message()
        self.assertEqual((code, err), (4, f"ratchet: error: {message}\n"))
        self.assertIn("wrapup: changed lines 250 >= wrapup_min_changed_lines 200; running a review", out)
        self.assertNotIn("review round", out)  # the refusal comes before the round line, as the round cap's does
        self.assertNotIn("--over-cap", message)  # no way past this one on the command line
        self.assertFalse(dump.exists())
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".ratchet").exists())

    def test_with_json_it_is_a_daily_capped_object_and_the_exit_code_is_still_4(self) -> None:
        before = self.ledger_bytes()
        capped = self.wrap("--auto", expect=4)
        message = self.refused_message()
        self.assertEqual(set(capped), JSON_KEYS)
        self.assertEqual((capped["status"], capped["reason"], capped["run"], capped["would_refuse"]), ("daily_capped", message, None, None))
        self.assertEqual((capped["new_unmapped"], capped["open_unmapped"], capped["changed_lines"], capped["changed_files"]), ([], 0, 250, 1))
        self.assertIn(f"wrapup: {message}", self.last_err)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.ledger_bytes(), before)

    def test_a_lower_limit_in_the_config_is_the_cap(self) -> None:
        self.set_limit(1)
        self.assertIn("wrapup_auto_max_per_day is 1.", self.wrap("--auto", expect=4)["reason"])

    def test_a_review_more_than_a_day_old_is_not_counted(self) -> None:
        self.ledger.unlink()
        self.append_raw(*auto_reviews(DAY + HOUR, 2 * HOUR))  # one is left of two
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")

    def test_a_review_a_person_started_is_not_counted(self) -> None:
        self.ledger.unlink()
        self.append_raw(*[review_round(OTHER_CHANGE, run=f"manual{number}", ts=datetime.now().isoformat(timespec="seconds"))
                          for number in range(1, 6)])
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")

    def test_the_count_is_the_whole_repo_and_not_the_branch_that_is_reviewed(self) -> None:
        git(self.repo, "switch", "-q", "-c", "feature")
        self.assertTrue(self.key().startswith("feature@"))  # the planted reviews are of another change, and still count
        self.assertEqual(self.wrap("--auto", expect=4)["status"], "daily_capped")

    def test_a_manual_wrapup_at_the_limit_still_reviews_and_is_not_counted_either(self) -> None:
        manual = self.wrap()
        self.assertEqual((manual["status"], manual["run"]), ("reviewed", "r1"))
        self.assertEqual(len(self.claude_calls()), 1)
        self.more_work()
        refused = self.wrap("--auto", expect=4)
        self.assertTrue(refused["reason"].startswith("daily cap reached: 2 automatic review(s)"))  # two, not three
        self.assertEqual(len(self.claude_calls()), 1)

    def test_the_round_cap_is_met_before_the_daily_one(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.record_rounds(1)
        capped = self.wrap("--auto", expect=3)
        self.assertEqual(capped["status"], "capped")
        self.assertNotIn("daily cap", capped["reason"])

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
        before = self.ledger_bytes()
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

    def test_the_real_run_is_refused_with_the_words_the_dry_run_gave(self) -> None:
        foreseen = self.measure("--auto")["would_refuse"]
        real = self.wrap("--auto", expect=4)
        self.assertEqual((real["status"], real["reason"]), ("daily_capped", foreseen["message"]))

    def test_a_dry_run_below_the_limit_foresees_nothing(self) -> None:
        self.set_limit(3)
        self.assertEqual(self.measure("--auto")["would_refuse"], None)

    def test_the_third_automatic_wrapup_of_two_real_ones_is_refused(self) -> None:
        self.ledger.unlink()  # no planted reviews: the cap is used up by wrap-ups that really ran here
        self.more_work("first.py")
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work("second.py")
        self.assertEqual(self.wrap("--auto")["status"], "reviewed")
        self.more_work("third.py")
        before = self.ledger_bytes()
        refused = self.wrap("--auto", expect=4)
        self.assertEqual(refused["status"], "daily_capped")
        self.assertTrue(refused["reason"].startswith("daily cap reached: 2 automatic review(s)"))
        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual([run.get("trigger") for run in self.review_runs()], ["auto", "auto"])


class LinkedWorktreeCapTest(CappedCase):
    """The ledger is the main worktree's, so the cap counts the automatic reviews of every worktree together."""

    def test_a_review_in_a_linked_worktree_meets_the_reviews_of_the_main_one(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        self.put("linked_big.py", numbered(250), linked)
        code, out, err = self.cli("wrapup", "--json", "--auto", "--repo", str(linked), with_repo=False)
        self.assertEqual(code, 4, err)
        self.assertEqual(json.loads(out)["status"], "daily_capped")
        self.assertEqual(self.claude_calls(), [])


if __name__ == "__main__":
    unittest.main()
