"""A review that would be refused, known beforehand: `review_refusal`, what `house-rules review` and `wrapup --dry-run` make of it,
and the marker the review's claude process carries.

Nothing here runs the real claude: a command that must exist is an executable fake (some are never run at all), and one that must
be missing is a path to nothing.
"""
import json
import os
import shlex
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
import test_wrapup
from support import RepoCase, git, review_finding, review_round
from test_wrapup import numbered, review_text

from house_rules.config import load_config
from house_rules.errors import DailyCapReached, HouseRulesError, RoundCapReached, RoundKeyUnavailable
from house_rules.ledger import read_events
from house_rules.review import (
    REFUSE_CLAUDE, REFUSE_DAILY_CAP, REFUSE_ROUND_CAP, REFUSE_ROUND_KEY, REFUSE_RULES, REVIEW_CHILD_ENV, Refusal, review_refusal,
    round_key, run_claude,
)
from house_rules.rules import load_rules

REQUIRE_CLAUDE_MESSAGE = ("`{command}` not found on PATH; install Claude Code, or run the review elsewhere "
                          "and pass its saved output with --from-file FILE")
WRAPUP_CAP_NOTE = "house-rules wrapup has no --over-cap: `house-rules review --over-cap` runs one more round, recorded"


def dump_env_claude(directory: Path, dump: Path) -> Path:
    """An executable standing in for claude: it appends its arguments and environment to `dump`, answers `--version`, and
    answers a review with one that found nothing."""
    script = directory / "dump-env-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(dump)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}) + '\\n')\n"
        "if '--version' in sys.argv[1:]:\n"
        "    sys.stdout.write('2.1.0 (Claude Code)')\n"
        "else:\n"
        f"    sys.stdout.write({review_text()!r})\n", encoding="utf-8")
    script.chmod(0o755)
    return script


class RefusalCase(test_wrapup.WrapupCase):
    """A repo on `main` with rules and one commit, and a claude that exists. `config` is what `.house-rules.json` holds besides the rules."""

    config: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(**self.config)
        self.claude = self.fake_claude(review_text())
        self.use(self.claude)

    def use(self, command: object) -> None:
        """HOUSE_RULES_CLAUDE_CMD for the rest of the test."""
        patcher = mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(command)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def missing_claude(self) -> Path:
        path = self.tmp / "no-such-claude"
        self.use(path)
        return path

    def key(self) -> str:
        return round_key(self.repo, None, None)

    def record_rounds(self, count: int, key: str | None = None, prefix: str = "past") -> str:
        """`count` review rounds of a change already in the ledger. Their run ids are not `r<k>`, the id the next `house-rules review`
        picks for itself (no finding or saved review counts a run here): a recorded id stops that review with "already recorded",
        before the cap is looked at."""
        key = key or self.key()
        self.append_raw(*[review_round(key, run=f"{prefix}{number}") for number in range(1, count + 1)])
        return key

    def refusal(self, **overrides: object) -> Refusal | None:
        config = load_config(self.repo, None, None, self.repo)
        arguments = {"target": None, "from_file": False, "over_cap_allowed": False, "replayed": False, "automatic": False, **overrides}
        return review_refusal(config, read_events(config.ledger_path), **arguments)

    def detach(self) -> None:
        git(self.repo, "checkout", "-q", "--detach")

    def rules_message(self) -> str:
        """What `load_rules` says when the rules file is gone (the file is removed here)."""
        (self.repo / "rules.md").unlink()
        with self.assertRaises(HouseRulesError) as caught:
            load_rules(load_config(self.repo, None, None, self.repo))
        return str(caught.exception)


class ForeseenKindsTest(RefusalCase):
    """Each way a review is refused, with the words `review` itself uses for it."""

    def test_a_review_that_can_start_is_not_refused(self) -> None:
        self.assertIsNone(self.refusal())

    def test_rules_that_do_not_load_refuse_it_with_the_message_load_rules_gives_and_no_round_key(self) -> None:
        message = self.rules_message()
        self.assertEqual(self.refusal(), Refusal(REFUSE_RULES, None, message))
        self.assertIn("rules.md", message)

    def test_a_rules_file_without_clauses_refuses_it_too(self) -> None:
        self.put("rules.md", "# nothing here\n")
        refusal = self.refusal()
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_RULES, None))
        self.assertIn("no clause definitions", refusal.message)

    def test_a_detached_head_has_no_round_key_and_the_message_is_the_one_round_key_raises(self) -> None:
        self.detach()
        with self.assertRaises(RoundKeyUnavailable) as caught:
            self.key()
        self.assertEqual(self.refusal(), Refusal(REFUSE_ROUND_KEY, None, str(caught.exception)))
        self.assertIn("HEAD is detached", str(caught.exception))

    def test_a_target_names_the_change_when_the_head_has_no_branch(self) -> None:
        self.detach()
        self.assertIsNone(self.refusal(target="42"))

    def test_a_default_branch_that_is_not_a_branch_is_a_round_key_refusal_too(self) -> None:
        self.write_config(rules_files=["rules.md"], default_branch="nope")
        refusal = self.refusal()
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_ROUND_KEY, None))
        self.assertIn("'nope' is not a branch of this repo", refusal.message)

    def test_a_branch_that_shares_no_history_with_the_default_one_is_a_round_key_refusal(self) -> None:
        git(self.repo, "checkout", "-q", "--orphan", "island")
        self.commit("island")
        refusal = self.refusal()
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_ROUND_KEY, None))
        self.assertIn("shares no history with main", refusal.message)

    def test_the_round_after_the_cap_is_refused_with_the_change_it_is_counted_on_and_reviews_message(self) -> None:
        key = self.record_rounds(3)
        self.append_raw(review_finding("A-1", run="A"), review_finding("A-2", run="A", line=11))
        self.assertEqual(self.refusal(), Refusal(
            REFUSE_ROUND_CAP, key, f"round cap reached for {key}: 3/3 rounds used; 2 findings still open (house-rules count). "
            "Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded)."))

    def test_a_round_below_the_cap_is_not_refused(self) -> None:
        self.record_rounds(2)
        self.assertIsNone(self.refusal())

    def test_the_cap_is_max_review_rounds(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.record_rounds(1)
        self.assertIn("1/1 rounds used", self.refusal().message)
        self.write_config(rules_files=["rules.md"], max_review_rounds=2)
        self.assertIsNone(self.refusal())

    def test_over_cap_allowed_and_a_replay_are_not_refused_at_the_cap(self) -> None:
        self.record_rounds(3)
        self.assertIsNone(self.refusal(over_cap_allowed=True))
        self.assertIsNone(self.refusal(replayed=True))
        self.assertEqual(self.refusal().kind, REFUSE_ROUND_CAP)

    def test_only_the_rounds_of_the_change_itself_count(self) -> None:
        self.record_rounds(5, "feature@0a1b2c3d4e5f", prefix="f")
        self.record_rounds(5, "target:42", prefix="t")
        self.assertIsNone(self.refusal())
        self.assertIsNone(self.refusal(target="43"))
        self.assertEqual(self.refusal(target="42").round_key, "target:42")
        self.assertEqual(self.refusal(target="42"), self.refusal(target=" 42 "))  # a target is stripped, as in the round key

    def test_a_saved_review_is_a_round_and_is_refused_at_the_cap_too(self) -> None:
        self.record_rounds(3)
        self.assertEqual(self.refusal(from_file=True).kind, REFUSE_ROUND_CAP)

    def test_a_missing_claude_is_refused_with_the_change_and_the_message_require_claude_gives(self) -> None:
        missing = self.missing_claude()
        self.assertEqual(self.refusal(), Refusal(REFUSE_CLAUDE, self.key(), REQUIRE_CLAUDE_MESSAGE.format(command=missing)))

    def test_a_file_that_cannot_be_run_is_not_a_claude(self) -> None:
        plain = self.put("plain-file", "not a program\n")
        plain.chmod(0o644)
        self.use(plain)
        self.assertEqual(self.refusal().kind, REFUSE_CLAUDE)
        plain.chmod(plain.stat().st_mode | stat.S_IXUSR)
        self.assertIsNone(self.refusal())

    def test_only_the_first_word_of_the_command_has_to_exist(self) -> None:
        self.use(f"{shlex.quote(str(self.claude))} --extra 'two words'")
        self.assertIsNone(self.refusal())
        missing = self.tmp / "no-such-claude"
        self.use(f"{shlex.quote(str(missing))} --extra")
        self.assertEqual(self.refusal().message, REQUIRE_CLAUDE_MESSAGE.format(command=missing))

    def test_without_the_variable_it_is_claude_on_the_path_that_is_looked_for(self) -> None:
        with mock.patch.dict(os.environ):
            del os.environ["HOUSE_RULES_CLAUDE_CMD"]
            with mock.patch("house_rules.review.shutil.which", return_value=None) as which:
                refusal = self.refusal()
            which.assert_called_once_with("claude")
            with mock.patch("house_rules.review.shutil.which", return_value="/somewhere/claude"):
                self.assertIsNone(self.refusal())
        self.assertEqual(refusal, Refusal(REFUSE_CLAUDE, self.key(), REQUIRE_CLAUDE_MESSAGE.format(command="claude")))

    def test_a_blank_command_is_looked_up_the_way_the_review_would_run_it(self) -> None:
        self.use("   ")  # no words: the review would run `-p <prompt>`, so `-p` is what has to exist
        self.assertEqual(self.refusal().message, REQUIRE_CLAUDE_MESSAGE.format(command="-p"))

    def test_a_saved_review_needs_no_claude(self) -> None:
        self.missing_claude()
        self.assertIsNone(self.refusal(from_file=True))


class ForeseenOrderTest(RefusalCase):
    """The checks run in the order `house-rules review` makes them, so the first thing wrong is the one named."""

    def test_broken_rules_come_before_a_detached_head(self) -> None:
        self.rules_message()
        self.detach()
        self.assertEqual(self.refusal().kind, REFUSE_RULES)

    def test_broken_rules_come_before_everything_else(self) -> None:
        self.record_rounds(3)
        self.missing_claude()
        self.rules_message()
        self.assertEqual(self.refusal().kind, REFUSE_RULES)

    def test_a_detached_head_comes_before_a_missing_claude(self) -> None:
        self.missing_claude()
        self.detach()
        self.assertEqual(self.refusal().kind, REFUSE_ROUND_KEY)

    def test_a_full_round_cap_comes_before_a_missing_claude(self) -> None:
        key = self.record_rounds(3)
        self.missing_claude()
        refusal = self.refusal()
        self.assertEqual((refusal.kind, refusal.round_key), (REFUSE_ROUND_CAP, key))


class RefusalErrorTest(unittest.TestCase):
    def test_a_full_round_cap_is_the_exit_code_3_error_and_the_other_refusals_are_exit_code_1(self) -> None:
        error = Refusal(REFUSE_ROUND_CAP, "main@abc", "the words").error()
        self.assertIsInstance(error, RoundCapReached)
        self.assertEqual((str(error), error.exit_code), ("the words", 3))
        for kind in (REFUSE_RULES, REFUSE_ROUND_KEY, REFUSE_CLAUDE):
            with self.subTest(kind=kind):
                error = Refusal(kind, None, "the words").error()
                self.assertNotIsInstance(error, (RoundCapReached, DailyCapReached))
                self.assertEqual((str(error), error.exit_code), ("the words", 1))

    def test_the_daily_cap_is_the_exit_code_4_error_and_not_a_round_cap(self) -> None:
        error = Refusal(REFUSE_DAILY_CAP, None, "the words").error()
        self.assertIsInstance(error, DailyCapReached)
        self.assertNotIsInstance(error, RoundCapReached)  # a wrap-up re-words a RoundCapReached with "no --over-cap", which is no way past this one
        self.assertEqual((str(error), error.exit_code), ("the words", 4))

    def test_the_kinds_are_these_five_words(self) -> None:
        self.assertEqual((REFUSE_RULES, REFUSE_ROUND_KEY, REFUSE_ROUND_CAP, REFUSE_DAILY_CAP, REFUSE_CLAUDE),
                         ("rules", "round_key", "round_cap", "daily_cap", "claude"))


class ReviewUsesItTest(RefusalCase):
    """`house-rules review` refuses with the very words and exit code `review_refusal` gives, and runs nothing."""

    def assert_refused_as_foreseen(self, expected_code: int) -> Refusal:
        refusal = self.refusal()
        before = self.ledger_bytes()
        code, out, err = self.cli("review")
        self.assertEqual((code, out, err), (expected_code, "", f"house-rules: error: {refusal.message}\n"))  # no "review round N/M" line first
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".house-rules").exists())
        self.assertEqual(self.claude_calls(), [])
        return refusal

    def test_broken_rules(self) -> None:
        self.rules_message()
        self.assert_refused_as_foreseen(1)

    def test_a_detached_head(self) -> None:
        self.detach()
        self.assert_refused_as_foreseen(1)

    def test_a_default_branch_that_is_not_a_branch(self) -> None:
        self.write_config(rules_files=["rules.md"], default_branch="nope")
        self.assert_refused_as_foreseen(1)

    def test_a_full_round_cap(self) -> None:
        self.record_rounds(3)
        refusal = self.assert_refused_as_foreseen(3)
        self.assertIn("round cap reached for", refusal.message)

    def test_a_missing_claude(self) -> None:
        self.missing_claude()
        refusal = self.assert_refused_as_foreseen(1)
        self.assertIn("not found on PATH", refusal.message)

    def test_a_saved_review_is_not_refused_for_a_missing_claude(self) -> None:
        self.missing_claude()
        code, _, err = self.cli("review", "--from-file", str(self.write_review()))
        self.assertEqual((code, err), (0, ""))

    def test_over_cap_and_a_replay_get_past_the_cap(self) -> None:
        saved = str(self.write_review())
        for number in (1, 2, 3):
            self.assertEqual(self.cli("review", "--from-file", saved, "--run", f"F{number}")[0], 0)
        self.assertEqual(self.cli("review", "--from-file", saved, "--run", "F4")[0], 3)
        self.assertEqual(self.cli("review", "--from-file", saved, "--run", "F4", "--over-cap")[0], 0)
        self.assertEqual(self.cli("review", "--from-file", saved, "--run", "F2")[0], 0)  # a replay


class ReviewDryRunTest(RefusalCase):
    def test_a_dry_run_with_a_missing_claude_says_so_and_still_shows_the_plan(self) -> None:
        missing = self.missing_claude()
        before = self.ledger_bytes()
        code, out, err = self.cli("review", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("review round 1/3 for ", out)
        self.assertIn(f"[dry-run] a real run would be refused: {REQUIRE_CLAUDE_MESSAGE.format(command=missing)}", out)
        self.assertIn(f"[dry-run] review: would run `{missing} -p '/code-review high'`", out)
        self.assertEqual(self.ledger_bytes(), before)

    def test_a_dry_run_with_a_working_claude_says_nothing_of_refusal(self) -> None:
        self.assertNotIn("would be refused", self.cli_ok("review", "--dry-run"))

    def test_a_dry_run_of_a_saved_review_does_not_look_for_claude(self) -> None:
        self.missing_claude()
        out = self.cli_ok("review", "--dry-run", "--from-file", str(self.write_review()))
        self.assertNotIn("would be refused", out)

    def test_a_dry_run_past_the_cap_keeps_its_own_words_and_names_only_the_cap(self) -> None:
        self.record_rounds(3)
        self.missing_claude()
        code, out, _ = self.cli("review", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] a real run would be refused: the round cap is reached; --over-cap runs it anyway, recorded", out)
        self.assertNotIn("not found on PATH", out)
        self.assertEqual(out.count("would be refused"), 1)

    def test_a_dry_run_without_a_round_key_is_stopped_as_a_real_run_is(self) -> None:
        self.detach()
        code, out, err = self.cli("review", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("HEAD is detached", err)
        self.assertNotIn("would be refused", out)

    def test_a_dry_run_without_rules_is_stopped_as_a_real_run_is(self) -> None:
        message = self.rules_message()
        code, out, err = self.cli("review", "--dry-run")
        self.assertEqual((code, err), (1, f"house-rules: error: {message}\n"))
        self.assertNotIn("would be refused", out)


class WrapupForeseesTest(RefusalCase):
    """`wrapup --dry-run` reports the refusal a real run would meet, in `would_refuse`, when the work is enough to review."""

    def setUp(self) -> None:
        super().setUp()
        self.put("big.py", numbered(250))  # over wrapup_min_changed_lines

    def reviewed_once(self) -> None:
        """Round 1 recorded by a real review, then fresh work for the next wrap-up to measure."""
        self.assertEqual(self.with_claude(self.claude)["status"], "reviewed")
        self.more_work()

    def test_a_review_that_would_run_has_a_null_would_refuse(self) -> None:
        data = self.measure()
        self.assertEqual((data["status"], data["would_refuse"]), ("dry-run", None))

    def test_a_full_round_cap_is_foreseen_in_the_words_the_real_wrapup_refuses_with(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.reviewed_once()
        before = self.ledger_bytes()
        data = self.measure()
        self.assertEqual((data["status"], data["reason"]), ("dry-run", "changed lines 250 >= wrapup_min_changed_lines 200"))
        refused = data["would_refuse"]
        self.assertEqual(set(refused), {"kind", "round_key", "message"})
        self.assertEqual((refused["kind"], refused["round_key"]), ("round_cap", self.key()))
        self.assertIn(f"round cap reached for {self.key()}: 1/1 rounds used;", refused["message"])
        self.assertTrue(refused["message"].endswith(WRAPUP_CAP_NOTE))
        self.assertEqual(self.ledger_bytes(), before)
        real = self.with_claude(self.claude, expect=3)  # the refusal it foresaw
        self.assertEqual((real["status"], real["reason"], real["would_refuse"]), ("capped", refused["message"], None))
        self.assertEqual(len(self.claude_calls()), 1)

    def test_the_text_report_says_the_review_would_be_refused_and_why(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.reviewed_once()
        message = self.measure()["would_refuse"]["message"]
        code, out, err = self.cli("wrapup", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[dry-run] wrapup: changed lines 250 >= wrapup_min_changed_lines 200; "
                      f"a real run would be refused: {message}; nothing run or written", out)
        self.assertNotIn("would run", out)

    def test_a_missing_claude_is_foreseen_with_the_change_it_would_have_reviewed(self) -> None:
        missing = self.missing_claude()
        message = REQUIRE_CLAUDE_MESSAGE.format(command=missing)
        self.assertEqual(self.measure()["would_refuse"], {"kind": "claude", "round_key": self.key(), "message": message})
        code, out, _ = self.cli("wrapup", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"a real run would be refused: {message}; nothing run or written", out)

    def test_a_detached_head_is_foreseen_without_a_round_key(self) -> None:
        self.detach()
        refused = self.measure()["would_refuse"]
        self.assertEqual((refused["kind"], refused["round_key"]), ("round_key", None))
        self.assertIn("HEAD is detached", refused["message"])

    def test_rules_that_do_not_load_are_foreseen(self) -> None:
        message = self.rules_message()
        self.assertEqual(self.measure()["would_refuse"], {"kind": "rules", "round_key": None, "message": message})

    def test_the_rules_come_before_the_detached_head(self) -> None:
        self.rules_message()
        self.detach()
        self.assertEqual(self.measure()["would_refuse"]["kind"], "rules")

    def test_a_saved_review_is_foreseen_without_a_claude_but_not_without_a_round(self) -> None:
        saved = self.write_review()
        self.missing_claude()
        data = self.measure("--from-file", str(saved))
        self.assertEqual((data["status"], data["would_refuse"]), ("dry-run", None))
        self.assertIn(f"would ingest {saved} as a review", self.cli("wrapup", "--dry-run", "--from-file", str(saved))[1])
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.record_rounds(1)
        self.assertEqual(self.measure("--from-file", str(saved))["would_refuse"]["kind"], "round_cap")

    def test_less_work_than_the_thresholds_is_skipped_and_nothing_is_foreseen(self) -> None:
        (self.repo / "big.py").unlink()
        self.missing_claude()
        data = self.measure()
        self.assertEqual((data["status"], data["would_refuse"]), ("skipped", None))

    def test_every_other_status_carries_the_field_null(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        reviewed = self.with_claude(self.claude)
        self.assertEqual((reviewed["status"], reviewed["would_refuse"]), ("reviewed", None))
        reported = self.wrap("--report-only")
        self.assertEqual((reported["status"], reported["would_refuse"]), ("reported", None))
        skipped = self.wrap()
        self.assertEqual((skipped["status"], skipped["would_refuse"]), ("skipped", None))
        self.more_work()
        capped = self.with_claude(self.claude, expect=3)
        self.assertEqual((capped["status"], capped["would_refuse"]), ("capped", None))

    def test_a_real_wrapup_is_refused_with_the_message_the_dry_run_gave(self) -> None:
        missing = self.missing_claude()
        message = self.measure()["would_refuse"]["message"]
        code, _, err = self.cli("wrapup")
        self.assertEqual((code, err), (1, f"house-rules: error: {message}\n"))
        self.assertIn(str(missing), message)
        self.assertEqual(self.review_runs(), [])

    def test_the_dry_run_writes_nothing(self) -> None:
        self.missing_claude()
        before = self.ledger_bytes()
        self.measure()
        self.cli("wrapup", "--dry-run")
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".house-rules").exists())
        self.assertEqual(self.claude_calls(), [])


class ChildMarkerTest(RepoCase):
    """The claude process of a review is marked, so a hook running inside it knows it is the tool's own review."""

    def setUp(self) -> None:
        super().setUp()
        self.dump = self.tmp / "env-dump.jsonl"
        self.script = dump_env_claude(self.tmp, self.dump)

    def dumped(self) -> list[dict]:
        return [json.loads(line) for line in self.dump.read_text(encoding="utf-8").splitlines()]

    def test_the_process_run_for_a_review_carries_the_marker_set_to_one(self) -> None:
        self.assertEqual(run_claude([str(self.script), "-p", "/code-review high"], self.tmp), review_text())
        (call,) = self.dumped()
        self.assertEqual(call["env"][REVIEW_CHILD_ENV], "1")
        self.assertEqual(REVIEW_CHILD_ENV, "HOUSE_RULES_REVIEW_CHILD")

    def test_the_rest_of_the_environment_is_unchanged(self) -> None:
        with mock.patch.dict(os.environ, {"HOUSE_RULES_TEST_KEPT": "as it was"}):
            path = os.environ["PATH"]
            run_claude([str(self.script), "-p", "x"], self.tmp)
        (call,) = self.dumped()
        self.assertEqual((call["env"]["HOUSE_RULES_TEST_KEPT"], call["env"]["PATH"]), ("as it was", path))

    def test_the_marker_stays_out_of_this_process(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(REVIEW_CHILD_ENV, None)
            run_claude([str(self.script), "-p", "x"], self.tmp)
            self.assertNotIn(REVIEW_CHILD_ENV, os.environ)

    def test_a_marker_set_to_something_else_is_one_for_the_child(self) -> None:
        with mock.patch.dict(os.environ, {REVIEW_CHILD_ENV: "0"}):
            run_claude([str(self.script), "-p", "x"], self.tmp)
            self.assertEqual(os.environ[REVIEW_CHILD_ENV], "0")
        self.assertEqual(self.dumped()[0]["env"][REVIEW_CHILD_ENV], "1")


class RunsMarkedTest(RefusalCase):
    """The claude a review or a wrap-up starts is marked."""

    def setUp(self) -> None:
        super().setUp()
        self.dump = self.tmp / "env-dump.jsonl"
        self.use(dump_env_claude(self.tmp, self.dump))
        outer = mock.patch.dict(os.environ)  # a marker inherited from an outer review would hide a missing one
        outer.start()
        self.addCleanup(outer.stop)
        os.environ.pop(REVIEW_CHILD_ENV, None)

    def reviews(self) -> list[dict]:
        calls = [json.loads(line) for line in self.dump.read_text(encoding="utf-8").splitlines()]
        return [call for call in calls if "-p" in call["argv"]]

    def test_house_rules_review_marks_the_claude_it_runs(self) -> None:
        self.cli_ok("review", "--run", "R1")
        (review,) = self.reviews()
        self.assertEqual((review["argv"], review["env"][REVIEW_CHILD_ENV]), (["-p", "/code-review high"], "1"))

    def test_a_wrapup_marks_the_claude_it_runs(self) -> None:
        self.put("big.py", numbered(250))
        code, _, err = self.cli("wrapup")
        self.assertEqual(code, 0, err)
        (review,) = self.reviews()
        self.assertEqual(review["env"][REVIEW_CHILD_ENV], "1")


if __name__ == "__main__":
    unittest.main()
