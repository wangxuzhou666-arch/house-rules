import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import FIXTURES, REPO_ROOT, STYLE_RULES, RepoCase, gate_finding, git, review_finding, review_item, review_round

from ratchet.errors import RatchetError, RoundKeyUnavailable
from ratchet.review import check_run_id, claude_command, default_run_id, findings_table, review_rounds_used, round_key


def review_text(*items: dict, preface: str = "Reviewed the diff.") -> str:
    """What the built-in code review prints: prose, then the findings in a json fence."""
    return f"{preface}\n\n```json\n{json.dumps(list(items), indent=2)}\n```\n"


FINDING_A = review_item(file="src/a.py", line=10, summary="Broad try swallows the timeout", category="p0e-1")
FINDING_B = review_item(file="src/b.py", line=7, summary="Docstring repeats the name", category="p3-1", severity="nice")


class ReviewCase(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        self.record = self.tmp / "claude-call.json"
        # No test may reach the real `claude`: anything that does not set its own command gets one that cannot run.
        patcher = mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": "/nonexistent/claude"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_claude(self, stdout: str = "", exit_code: int = 0, stderr: str = "", sleep: float = 0) -> Path:
        """An executable that records how it was called and prints canned output."""
        script = self.tmp / "fake-claude"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            f"open({str(self.record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}}))\n"
            f"time.sleep({sleep})\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def call(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def review(self, claude: Path | str, *argv: str) -> tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(claude)}):
            return self.cli("review", *argv)

    def findings(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "finding"]

    def review_runs(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "review_run"]

    def reviews_dir(self) -> Path:
        return self.repo / ".ratchet" / "reviews"

    def saved(self) -> list[str]:
        return sorted(p.name for p in self.reviews_dir().glob("*.txt")) if self.reviews_dir().exists() else []


class RunningClaudeTest(ReviewCase):
    def test_it_runs_the_builtin_review_headless_from_the_repo_root(self) -> None:
        code, _, err = self.review(self.fake_claude(review_text(FINDING_A)), "--run", "R1")
        self.assertEqual(code, 0, err)
        call = self.call()
        self.assertEqual(call["argv"], ["-p", "/code-review high"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(call["stdin"], "")

    def test_stdin_is_closed_so_a_headless_run_never_waits_for_input(self) -> None:
        script = self.fake_claude(review_text())
        with mock.patch("ratchet.review.subprocess.run", wraps=subprocess.run) as run:  # the module is shared: git calls land here too
            self.review(script, "--run", "R1")
        (claude_call,) = [call for call in run.call_args_list if call.args[0][0] == str(script)]
        self.assertIs(claude_call.kwargs["stdin"], subprocess.DEVNULL)

    def test_cwd_is_the_repo_root_even_when_started_from_a_subdirectory(self) -> None:
        (self.repo / "sub").mkdir()
        cwd = os.getcwd()
        os.chdir(self.repo / "sub")
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(self.fake_claude(review_text()))}):
            self.assertEqual(self.cli("review", "--run", "R1", with_repo=False)[0], 0)
        self.assertEqual(Path(self.call()["cwd"]).resolve(), self.repo.resolve())

    def test_level_and_target_go_into_the_prompt(self) -> None:
        self.review(self.fake_claude(review_text()), "--run", "R1", "--level", "max", "--target", "feature/x")
        self.assertEqual(self.call()["argv"], ["-p", "/code-review max feature/x"])

    def test_every_level_is_accepted_and_others_are_not(self) -> None:
        for level in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(level=level):
                self.assertEqual(claude_command(level, None)[-1], f"/code-review {level}")
        with self.assertRaises(SystemExit):
            self.cli("review", "--level", "ultra")

    def test_the_command_can_be_replaced_and_may_carry_arguments(self) -> None:
        script = self.fake_claude(review_text())
        self.review(f"{sys.executable} {script} --extra", "--run", "R1")
        self.assertEqual(self.call()["argv"], ["--extra", "-p", "/code-review high"])

    def test_a_target_must_be_one_line(self) -> None:
        for target in ("a\nb", "   "):
            with self.subTest(target=target):
                self.assertEqual(self.review(self.fake_claude(review_text()), "--target", target)[0], 1)

    def test_a_missing_claude_suggests_from_file_and_writes_nothing(self) -> None:
        code, _, err = self.review(self.tmp / "no-such-claude", "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("not found on PATH", err)
        self.assertIn("--from-file", err)
        self.assertEqual(self.saved(), [])
        self.assertFalse(self.ledger.exists())

    def test_a_failing_claude_reports_its_exit_code_and_stderr_and_writes_nothing(self) -> None:
        code, _, err = self.review(self.fake_claude(stdout="partial", exit_code=3, stderr="quota exceeded\n"), "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("exited with 3", err)
        self.assertIn("quota exceeded", err)
        self.assertEqual(self.saved(), [])
        self.assertFalse(self.ledger.exists())

    def test_a_stuck_claude_times_out(self) -> None:
        with mock.patch("ratchet.review.TIMEOUT_SECONDS", 0.3):
            code, _, err = self.review(self.fake_claude(sleep=5), "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("did not finish within", err)

    def test_the_real_executable_with_the_replacement_command_end_to_end(self) -> None:
        script = self.fake_claude(review_text(FINDING_A))
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "ratchet"), "review", "--run", "R1", "--repo", str(self.repo)],
                              capture_output=True, text=True, env={**os.environ, "RATCHET_CLAUDE_CMD": str(script)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("R1-1", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


class RecordingTest(ReviewCase):
    def test_the_raw_output_is_saved_and_the_findings_are_ingested_as_l1(self) -> None:
        raw = review_text(FINDING_A, FINDING_B)
        code, out, err = self.review(self.fake_claude(raw), "--run", "R7")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.reviews_dir() / "R7.txt").read_text(encoding="utf-8"), raw)
        self.assertIn("saved raw review output to .ratchet/reviews/R7.txt", out)
        findings = [e for e in self.events() if e["kind"] == "finding"]
        self.assertEqual([(e["display_id"], e["source"], e["run_id"], e["rule_id"], e["severity"]) for e in findings],
                         [("R7-1", "l1", "R7", "P0E.1", "blocker"), ("R7-2", "l1", "R7", "P3.1", "nice")])

    def test_it_prints_a_table_of_what_was_recorded(self) -> None:
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R7")
        lines = out.splitlines()
        header = next(line for line in lines if line.startswith("id "))
        self.assertEqual(header.split(), ["id", "rule_id", "severity", "location", "summary"])
        row = next(line for line in lines if line.startswith("R7-1"))
        self.assertEqual(row.split(None, 4), ["R7-1", "P0E.1", "blocker", "src/a.py:10", "Broad try swallows the timeout"])
        self.assertIn("review: +2 finding(s)", out)

    def test_the_default_run_id_counts_up_from_r1_and_the_findings_are_short_to_type(self) -> None:
        self.assertEqual(default_run_id(set()), "r1")
        self.assertEqual(default_run_id({"R12", "r1", "x"}), "r4")
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)))
        self.assertEqual([(e["run_id"], e["display_id"]) for e in self.findings()], [("r1", "r1-1"), ("r1", "r1-2")])
        self.assertEqual(self.saved(), ["r1.txt"])
        snapshot = json.loads((self.reviews_dir() / "r1.evidence.json").read_text())
        self.assertEqual(snapshot["run_id"], "r1")
        self.assertEqual(snapshot["basis"], "local_pre_review")
        self.assertIn("r1-2", out)
        self.review(self.fake_claude(review_text(review_item(file="src/c.py", category="p1-1"))))
        self.assertEqual([e["display_id"] for e in self.findings()], ["r1-1", "r1-2", "r2-1"])

    def test_every_review_run_in_the_ledger_counts_whatever_made_it_but_a_gate_does_not(self) -> None:
        self.append_raw(gate_finding("a.py"), review_finding("A-1", run="A"), review_finding("B-1", run="B", source="l2", line=2),
                        review_finding("B-2", run="B", source="l2", line=3))
        self.review(self.fake_claude(review_text(FINDING_A)))
        self.assertEqual(self.findings()[-1]["run_id"], "r3")

    def test_a_run_that_saved_its_output_but_recorded_nothing_still_uses_up_its_number(self) -> None:
        self.assertEqual(self.review(self.fake_claude(review_text()))[0], 0)  # a clean review: r1.txt, nothing in the ledger
        self.assertEqual(self.review(self.fake_claude("The model printed no JSON.\n"))[0], 1)  # an unparsable one: r2.txt
        self.assertEqual(self.review(self.fake_claude(review_text(FINDING_A)))[0], 0)
        self.assertEqual(self.findings()[0]["run_id"], "r3")
        self.assertEqual(self.saved(), ["r1.txt", "r2.txt", "r3.txt"])
        self.assertEqual((self.reviews_dir() / "r1.txt").read_text(encoding="utf-8"), review_text())

    def test_a_taken_default_id_fails_loudly_and_changes_nothing(self) -> None:
        self.append_raw(review_finding("r2-1", run="r2", summary="an earlier review's finding"))  # one run in the ledger, so the next is r2
        before = self.ledger_bytes()
        code, _, err = self.review(self.fake_claude(review_text(FINDING_A)))
        self.assertEqual(code, 1)
        self.assertIn("different file/line/summary", err)
        self.assertIn("use a new --run", err)
        self.assertEqual(self.ledger_bytes(), before)

    def test_an_explicit_run_is_used_as_given_and_counts_toward_the_next_default(self) -> None:
        self.review(self.fake_claude(review_text(FINDING_A)), "--run", "R12")
        self.assertEqual(self.findings()[0]["display_id"], "R12-1")
        self.review(self.fake_claude(review_text(FINDING_B)))
        self.assertEqual(self.findings()[-1]["display_id"], "r2-1")

    def test_dry_run_names_the_run_it_would_use(self) -> None:
        self.append_raw(review_finding("A-1", run="A"))
        _, out, _ = self.review(self.fake_claude(review_text()), "--dry-run")
        self.assertIn("(run r2)", out)

    def test_a_long_summary_is_truncated_in_the_table_but_not_in_the_ledger(self) -> None:
        long_summary = "word " * 40
        _, out, _ = self.review(self.fake_claude(review_text(review_item(summary=long_summary, category="p1-1"))), "--run", "R1")
        row = next(line for line in out.splitlines() if line.startswith("R1-1"))
        shown = row.split(None, 4)[4]
        self.assertLessEqual(len(shown), 60)
        self.assertTrue(shown.endswith("…"))
        self.assertEqual(self.findings()[0]["summary"], long_summary)

    def test_an_unknown_category_is_recorded_as_unmapped(self) -> None:
        _, out, _ = self.review(self.fake_claude(review_text(review_item(category="made-up"))), "--run", "R1")
        self.assertIn("unmapped", out)

    def test_the_same_review_twice_adds_no_findings(self) -> None:
        raw = review_text(FINDING_A)
        self.review(self.fake_claude(raw), "--run", "R1")
        before = self.findings()
        code, out, _ = self.cli("review", "--from-file", str(self.reviews_dir() / "R1.txt"), "--run", "R1")
        self.assertEqual(code, 0)
        self.assertEqual(self.findings(), before)
        self.assertEqual(len(self.review_runs()), 1)  # re-ingesting a recorded run is not another round
        self.assertIn("no new findings", out)
        self.assertIn("1 already recorded", out)

    def test_a_different_review_under_a_used_run_id_is_refused(self) -> None:
        self.review(self.fake_claude(review_text(FINDING_A)), "--run", "R1")
        before = self.ledger_bytes()
        other = self.tmp / "other.txt"
        other.write_text(review_text(FINDING_B), encoding="utf-8")
        code, _, err = self.cli("review", "--from-file", str(other), "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("already holds a different review", err)
        self.assertIn("--run", err)
        self.assertEqual(self.ledger_bytes(), before)

    def test_a_review_with_no_findings_says_so(self) -> None:
        code, out, _ = self.review(self.fake_claude(review_text()), "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn("no new findings", out)
        self.assertEqual(self.findings(), [])
        self.assertEqual([(e["run"], e["finding_count"]) for e in self.review_runs()], [("R1", 0)])

    def test_output_without_a_findings_block_keeps_the_raw_text_and_fails(self) -> None:
        code, out, err = self.review(self.fake_claude("The model rambled and printed no JSON.\n"), "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("neither a JSON array nor text containing", err)
        self.assertIn("saved raw review output to .ratchet/reviews/R1.txt", out)
        self.assertEqual(self.saved(), ["R1.txt"])
        self.assertFalse(self.ledger.exists())

    def test_run_ids_that_are_not_safe_file_names_are_refused(self) -> None:
        for bad in ("../x", "a/b", ".hidden", "", "has space", "-x"):
            with self.subTest(run=bad):
                with self.assertRaises(RatchetError):
                    check_run_id(bad)
        code, _, err = self.review(self.fake_claude(review_text()), "--run", "../escape")
        self.assertEqual(code, 1)
        self.assertIn("must match", err)
        self.assertFalse((self.repo / ".ratchet" / "escape.txt").exists())

    def test_the_open_count_and_the_closing_hint_are_printed(self) -> None:
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R1")
        self.assertIn("open: 2.", out)
        self.assertIn("Fixes-Finding", out)
        self.assertNotIn("label-page", out)

    def test_a_pile_of_open_findings_suggests_the_label_page(self) -> None:
        self.write_config(rules_files=["rules.md"], label_page_threshold=1)
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R1")
        self.assertIn("open > 1: run `ratchet label-page`", out)

    def test_the_open_count_includes_findings_from_earlier_reviews(self) -> None:
        self.review(self.fake_claude(review_text(FINDING_A)), "--run", "R1")
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_B)), "--run", "R2")
        self.assertIn("open: 2.", out)


class FromFileTest(ReviewCase):
    def test_a_saved_review_is_ingested_without_calling_claude(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        code, out, err = self.review(self.tmp / "no-such-claude", "--from-file", str(saved), "--run", "R3")
        self.assertEqual(code, 0, err)
        self.assertIn("R3-1", out)
        self.assertFalse(self.record.exists())
        self.assertEqual((self.reviews_dir() / "R3.txt").read_text(encoding="utf-8"), saved.read_text(encoding="utf-8"))

    def test_level_and_target_do_nothing_with_from_file_and_are_refused(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(), encoding="utf-8")
        for extra in (("--level", "low"), ("--target", "main")):
            with self.subTest(extra=extra):
                code, _, err = self.cli("review", "--from-file", str(saved), *extra)
                self.assertEqual(code, 1)
                self.assertIn("do nothing with --from-file", err)

    def test_a_missing_file_is_an_error(self) -> None:
        code, _, err = self.cli("review", "--from-file", str(self.tmp / "absent.txt"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read input", err)

    def test_a_bare_json_array_file_works_too(self) -> None:
        saved = self.tmp / "array.json"
        saved.write_text(json.dumps([FINDING_A]), encoding="utf-8")
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "R4")[0], 0)
        self.assertEqual(len(self.findings()), 1)


class DryRunTest(ReviewCase):
    def test_dry_run_prints_the_command_and_neither_runs_it_nor_writes(self) -> None:
        code, out, _ = self.review(self.fake_claude(review_text(FINDING_A)), "--dry-run", "--level", "low", "--target", "main", "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn("-p '/code-review low main'", out)
        self.assertIn("nothing run or written", out)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.saved(), [])
        self.assertFalse(self.ledger.exists())

    def test_dry_run_with_from_file_shows_the_table_and_writes_nothing(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        code, out, _ = self.cli("review", "--dry-run", "--from-file", str(saved), "--run", "R3")
        self.assertEqual(code, 0)
        self.assertIn("R3-1", out)
        self.assertIn("[dry-run]", out)
        self.assertEqual(self.saved(), [])
        self.assertFalse(self.ledger.exists())


class RoundKeyTest(RepoCase):
    """What names the change under review, so that its review rounds can be counted."""

    def key(self, target: str | None = None, default: str | None = None) -> str:
        return round_key(self.repo, target, default)

    def switch(self, branch: str, create: bool = False) -> None:
        git(self.repo, "switch", "-q", *(["-c"] if create else []), branch)

    def test_a_target_names_the_change_whatever_the_repo_looks_like(self) -> None:
        self.assertEqual(self.key("feature/x"), "target:feature/x")  # unborn
        self.assertEqual(self.key("  42 "), "target:42")
        self.commit("c1")
        git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.key("42"), "target:42")  # detached

    def test_a_feature_branch_is_named_by_its_fork_point_from_the_default_branch(self) -> None:
        base = self.commit("c1")
        self.switch("feature", create=True)
        self.commit("f1")
        key = self.key()
        self.assertEqual(key, f"feature@{base[:12]}")
        self.assertEqual(len(key.split("@")[1]), 12)
        self.commit("f2")
        self.assertEqual(self.key(), key)  # more commits on the branch: same change
        self.switch("main")
        self.commit("m2")
        self.switch("feature")
        self.assertEqual(self.key(), key)  # the default branch moving on does not move the fork point

    def test_each_commit_on_the_default_branch_is_a_change_of_its_own(self) -> None:
        first = self.commit("c1")
        self.assertEqual(self.key(), f"main@{first[:12]}")
        second = self.commit("c2")
        self.assertEqual(self.key(), f"main@{second[:12]}")

    def test_merging_the_default_branch_into_a_feature_branch_starts_a_new_key(self) -> None:
        base = self.commit("c1")
        self.switch("feature", create=True)
        self.commit("f1")
        self.assertEqual(self.key(), f"feature@{base[:12]}")
        self.switch("main")
        tip = self.commit("m2")
        self.switch("feature")
        git(self.repo, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q", "-m", "merge main", "main")
        self.assertEqual(self.key(), f"feature@{tip[:12]}")

    def test_a_detached_head_without_a_target_asks_for_one(self) -> None:
        self.commit("c1")
        git(self.repo, "checkout", "-q", "--detach")
        with self.assertRaises(RoundKeyUnavailable) as caught:
            self.key()
        self.assertIn("--target", str(caught.exception))

    def test_a_branch_before_its_first_commit_is_unborn_and_needs_no_default_branch(self) -> None:
        self.assertEqual(self.key(), "main@unborn")
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/other")
        self.assertEqual(self.key(), "other@unborn")

    def test_the_configured_default_branch_decides_the_fork_point(self) -> None:
        base = self.commit("c1")
        self.switch("develop", create=True)
        develop = self.commit("d1")
        self.switch("feature", create=True)
        self.commit("f1")
        self.assertEqual(self.key(), f"feature@{base[:12]}")  # measured from main by default
        self.assertEqual(self.key(default="develop"), f"feature@{develop[:12]}")

    def test_a_configured_default_branch_that_does_not_exist_is_an_error_not_a_fallback(self) -> None:
        self.commit("c1")
        self.switch("feature", create=True)
        with self.assertRaises(RatchetError) as caught:
            self.key(default="nope")
        self.assertNotIsInstance(caught.exception, RoundKeyUnavailable)
        self.assertIn("'nope'", str(caught.exception))

    def test_origin_head_is_preferred_to_main(self) -> None:
        base = self.commit("c1")
        self.switch("feature", create=True)
        tip = self.commit("f1")
        self.switch("main")
        self.commit("m2")
        git(self.repo, "update-ref", "refs/remotes/origin/trunk", tip)
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.switch("feature")
        self.assertEqual(self.key(), f"feature@{tip[:12]}")  # trunk (origin/HEAD), not main, which would give base
        self.assertNotEqual(base, tip)

    def test_a_local_default_branch_ahead_of_origin_is_still_per_commit(self) -> None:
        pushed = self.commit("c1")
        git(self.repo, "update-ref", "refs/remotes/origin/main", pushed)
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        ahead = self.commit("c2")
        self.assertEqual(self.key(), f"main@{ahead[:12]}")

    def test_master_is_the_fallback_when_there_is_no_main(self) -> None:
        base = self.commit("c1")
        git(self.repo, "branch", "-m", "main", "master")
        self.switch("feature", create=True)
        self.commit("f1")
        self.assertEqual(self.key(), f"feature@{base[:12]}")

    def test_no_default_branch_to_measure_from_is_unavailable_and_says_how_to_fix_it(self) -> None:
        self.commit("c1")
        git(self.repo, "branch", "-m", "main", "trunk")
        with self.assertRaises(RoundKeyUnavailable) as caught:
            self.key()
        self.assertIn("default_branch", str(caught.exception))
        self.assertEqual(self.key(default="trunk"), f"trunk@{git(self.repo, 'rev-parse', 'HEAD')[:12]}")

    def test_histories_that_share_nothing_with_the_default_branch_are_unavailable(self) -> None:
        self.commit("c1")
        git(self.repo, "checkout", "-q", "--orphan", "island")
        self.commit("i1")
        with self.assertRaises(RoundKeyUnavailable) as caught:
            self.key()
        self.assertIn("shares no history", str(caught.exception))

    def test_rounds_are_counted_per_key_and_only_from_review_run_events(self) -> None:
        events = [review_round("a@1"), review_round("a@1", run="r2"), review_round("b@1"),
                  review_finding(), gate_finding("x.py")]
        self.assertEqual(review_rounds_used(events, "a@1"), 2)
        self.assertEqual(review_rounds_used(events, "b@1"), 1)
        self.assertEqual(review_rounds_used(events, "c@1"), 0)
        self.assertEqual(review_rounds_used([], "a@1"), 0)


class RoundCapTest(ReviewCase):
    """`max_review_rounds` reviews per change; the next one is refused before it costs anything."""

    KEY = "main@unborn"

    def run_rounds(self, count: int, *argv: str) -> None:
        for _ in range(count):
            code, _, err = self.review(self.fake_claude(review_text(FINDING_A)), *argv)
            self.assertEqual(code, 0, err)

    def test_a_normal_run_says_which_round_it_is_before_claude_runs(self) -> None:
        code, out, _ = self.review(self.fake_claude(exit_code=1), "--run", "R1")  # claude fails, so only what came first is printed
        self.assertEqual(code, 1)
        self.assertIn(f"review round 1/3 for {self.KEY}\n", out)
        self.run_rounds(1)
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn(f"review round 2/3 for {self.KEY}\n", out)

    def test_the_round_after_the_cap_is_refused_with_exit_code_3_and_claude_is_not_run(self) -> None:
        self.run_rounds(3)
        self.record.unlink()
        before, saved = self.ledger_bytes(), self.saved()
        code, out, err = self.review(self.fake_claude(review_text(FINDING_B)))
        self.assertEqual(code, 3)
        self.assertIn(f"round cap reached for {self.KEY}: 3/3 rounds used; 3 findings still open (ratchet count). "
                      "Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded).", err)
        self.assertFalse(self.record.exists(), "claude must not be invoked for a refused round")
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.saved(), saved)
        self.assertNotIn("review round", out)

    def test_over_cap_runs_the_review_and_records_it_as_over_the_cap(self) -> None:
        self.run_rounds(3)
        code, out, err = self.review(self.fake_claude(review_text(FINDING_B)), "--over-cap")
        self.assertEqual(code, 0, err)
        self.assertIn(f"review round 4/3 for {self.KEY}", out)
        self.assertTrue(self.record.exists())
        self.assertEqual([(e["round"], e["max_rounds"], e["over_cap"]) for e in self.review_runs()],
                         [(1, 3, False), (2, 3, False), (3, 3, False), (4, 3, True)])
        code, _, err = self.review(self.fake_claude(review_text()))  # --over-cap is for one run, not a new allowance
        self.assertEqual(code, 3)
        self.assertIn("4/3 rounds used", err)

    def test_over_cap_on_a_run_within_the_cap_changes_nothing(self) -> None:
        self.assertEqual(self.review(self.fake_claude(review_text()), "--over-cap")[0], 0)
        self.assertEqual([(e["round"], e["over_cap"]) for e in self.review_runs()], [(1, False)])

    def test_a_from_file_review_is_a_round_and_is_refused_at_the_cap(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        for number in (1, 2, 3):
            self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", f"F{number}")[0], 0)
        self.assertEqual([(e["round"], e["source"]) for e in self.review_runs()], [(1, "file"), (2, "file"), (3, "file")])
        before = self.ledger_bytes()
        code, _, err = self.cli("review", "--from-file", str(saved), "--run", "F4")
        self.assertEqual(code, 3)
        self.assertIn("round cap reached", err)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F4", "--over-cap")[0], 0)
        self.assertEqual(self.review_runs()[-1]["over_cap"], True)

    def test_a_replayed_run_is_not_another_round(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        first = self.cli("review", "--from-file", str(saved), "--run", "F1")
        self.assertIn(f"review round 1/3 for {self.KEY}", first[1])
        code, out, _ = self.cli("review", "--from-file", str(saved), "--run", "F1")
        self.assertEqual(code, 0)
        self.assertIn("review: replay of run F1 (already recorded; not another round)", out)
        self.assertNotIn("review round", out)
        self.assertEqual([(e["run"], e["round"]) for e in self.review_runs()], [("F1", 1)])
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F2")[0], 0)  # a new run id is the next round
        self.assertEqual([(e["run"], e["round"]) for e in self.review_runs()], [("F1", 1), ("F2", 2)])
        self.assertEqual(self.cli_ok("count").splitlines()[-1], f"review rounds on {self.KEY}: 2/3")

    def test_a_replayed_run_is_not_refused_at_the_cap(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        for number in (1, 2, 3):
            self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", f"F{number}")[0], 0)
        before = self.ledger_bytes()
        code, out, err = self.cli("review", "--from-file", str(saved), "--run", "F3")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("review: replay of run F3", out)
        self.assertNotIn("review round", out)
        self.assertNotIn("(over the cap)", out)
        self.assertEqual(self.ledger_bytes(), before)  # no finding, no round
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F4")[0], 3)  # a new run id still is one
        code, out, _ = self.cli("review", "--dry-run", "--from-file", str(saved), "--run", "F2")
        self.assertEqual(code, 0)
        self.assertIn("review: replay of run F2", out)
        self.assertNotIn("would be refused", out)

    def test_a_recorded_run_id_is_refused_without_from_file_before_claude_runs(self) -> None:
        for rounds in (1, 2, 3):  # below the cap and at it
            with self.subTest(rounds_used=rounds):
                self.run_rounds(1)
                self.record.unlink()
                before, saved = self.ledger_bytes(), self.saved()
                code, out, err = self.review(self.fake_claude(review_text(FINDING_B)), "--run", "r1")
                self.assertEqual(code, 1)
                self.assertIn("run r1 is already recorded; omit --run for a new round, or pass --from-file", err)
                self.assertNotIn("round cap", err)  # the run id is refused first, whatever the cap says
                self.assertEqual(out, "")
                self.assertFalse(self.record.exists(), "claude must not be invoked for a recorded run id")
                self.assertEqual((self.ledger_bytes(), self.saved()), (before, saved))

    def test_a_recorded_run_id_is_refused_without_from_file_even_with_over_cap_or_dry_run(self) -> None:
        self.run_rounds(3)
        self.record.unlink()
        before = self.ledger_bytes()
        for flag in ("--over-cap", "--dry-run"):
            with self.subTest(flag):
                code, out, err = self.review(self.fake_claude(review_text(FINDING_B)), "--run", "r3", flag)
                self.assertEqual(code, 1)
                self.assertIn("run r3 is already recorded", err)
                self.assertEqual(out, "")
                self.assertFalse(self.record.exists())
                self.assertEqual(self.ledger_bytes(), before)

    def test_the_default_run_id_is_never_a_recorded_one(self) -> None:
        self.run_rounds(2)
        code, out, err = self.review(self.fake_claude(review_text(FINDING_B)))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("review round 3/3", out)
        self.assertEqual([e["run"] for e in self.review_runs()], ["r1", "r2", "r3"])

    def test_only_from_file_replays_a_recorded_run_at_the_cap(self) -> None:
        self.run_rounds(3)
        before = self.ledger_bytes()
        code, out, err = self.cli("review", "--from-file", str(self.reviews_dir() / "r3.txt"), "--run", "r3")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("replay of run r3", out)
        self.assertEqual(self.ledger_bytes(), before)

    def test_claude_and_from_file_rounds_share_one_count(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(), encoding="utf-8")
        self.cli("review", "--from-file", str(saved), "--run", "F1")
        self.run_rounds(2)
        self.assertEqual(self.review(self.fake_claude(review_text()))[0], 3)

    def test_the_recorded_round_has_every_field_and_follows_its_findings(self) -> None:
        self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R5")
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["finding", "finding", "review_run"])
        run = dict(events[-1])
        self.assertRegex(run.pop("ts"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(run, {"v": 1, "kind": "review_run", "repo": "demo-repo", "run": "R5", "round_key": self.KEY, "round": 1,
                               "max_rounds": 3, "over_cap": False, "finding_count": 2, "source": "claude"})

    def test_a_review_that_found_nothing_is_still_a_round(self) -> None:
        self.review(self.fake_claude(review_text()))
        self.assertEqual([(e["round"], e["finding_count"]) for e in self.review_runs()], [(1, 0)])

    def test_a_failed_review_is_not_a_round(self) -> None:
        for claude in (self.fake_claude(exit_code=1), self.fake_claude("The model printed no JSON.\n"), self.tmp / "no-such-claude"):
            self.assertEqual(self.review(claude)[0], 1)
        self.assertEqual(self.review_runs(), [])
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn("review round 1/3", out)

    def test_dry_run_shows_the_key_and_round_and_records_nothing(self) -> None:
        self.run_rounds(2)
        before = self.ledger_bytes()
        self.record.unlink()
        code, out, _ = self.review(self.fake_claude(review_text()), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"review round 3/3 for {self.KEY}", out)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.ledger_bytes(), before)
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_B), encoding="utf-8")
        code, out, _ = self.cli("review", "--dry-run", "--from-file", str(saved), "--run", "F1")
        self.assertEqual(code, 0)
        self.assertIn(f"review round 3/3 for {self.KEY}", out)
        self.assertEqual(self.ledger_bytes(), before)

    def test_dry_run_past_the_cap_previews_and_warns_instead_of_refusing(self) -> None:
        self.run_rounds(3)
        before = self.ledger_bytes()
        code, out, _ = self.review(self.fake_claude(review_text()), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"review round 4/3 for {self.KEY}", out)
        self.assertIn("would be refused", out)
        code, out, _ = self.review(self.fake_claude(review_text()), "--dry-run", "--over-cap")
        self.assertEqual(code, 0)
        self.assertNotIn("would be refused", out)
        self.assertEqual(self.ledger_bytes(), before)

    def test_rounds_of_other_changes_do_not_count(self) -> None:
        self.append_raw(*[review_round("feature@abc", run=f"x{n}") for n in range(5)], review_round("target:42", run="x9"))
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn(f"review round 1/3 for {self.KEY}", out)

    def test_reviews_recorded_before_the_cap_existed_do_not_count(self) -> None:
        self.ledger.write_text((FIXTURES / "legacy_ledger.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        self.append_raw(review_finding("r1-1", run="r1", summary="an old review"), gate_finding("a.py"))
        code, out, err = self.review(self.fake_claude(review_text()))
        self.assertEqual(code, 0, err)
        self.assertIn("review round 1/3", out)

    def test_a_target_is_counted_apart_from_the_branch(self) -> None:
        for _ in range(3):
            self.assertEqual(self.review(self.fake_claude(review_text()), "--target", "feature/x")[0], 0)
        self.assertEqual({e["round_key"] for e in self.review_runs()}, {"target:feature/x"})
        code, _, err = self.review(self.fake_claude(review_text()), "--target", "feature/x")
        self.assertEqual(code, 3)
        self.assertIn("round cap reached for target:feature/x: 3/3", err)
        self.assertEqual(self.review(self.fake_claude(review_text()), "--target", "feature/y")[0], 0)
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn(f"review round 1/3 for {self.KEY}", out)

    def test_max_review_rounds_comes_from_the_config(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.run_rounds(1)
        code, _, err = self.review(self.fake_claude(review_text()))
        self.assertEqual(code, 3)
        self.assertIn("1/1 rounds used", err)
        self.write_config(rules_files=["rules.md"], max_review_rounds=2)
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn("review round 2/2", out)

    def test_each_commit_on_the_default_branch_gets_its_own_rounds(self) -> None:
        first = self.commit("c1")
        self.run_rounds(3)
        self.assertEqual(self.review(self.fake_claude(review_text()))[0], 3)
        second = self.commit("c2")
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn(f"review round 1/3 for main@{second[:12]}", out)
        self.assertEqual({e["round_key"] for e in self.review_runs()}, {f"main@{first[:12]}", f"main@{second[:12]}"})

    def test_a_feature_branch_keeps_its_rounds_across_commits_until_the_default_branch_is_merged_in(self) -> None:
        base = self.commit("c1")
        git(self.repo, "switch", "-q", "-c", "feature")
        self.commit("f1")
        self.run_rounds(2)
        self.commit("f2")
        self.run_rounds(1)
        self.assertEqual({e["round_key"] for e in self.review_runs()}, {f"feature@{base[:12]}"})
        self.assertEqual(self.review(self.fake_claude(review_text()))[0], 3)
        git(self.repo, "switch", "-q", "main")
        tip = self.commit("m2")
        git(self.repo, "switch", "-q", "feature")
        git(self.repo, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q", "-m", "merge main", "main")
        _, out, _ = self.review(self.fake_claude(review_text()))
        self.assertIn(f"review round 1/3 for feature@{tip[:12]}", out)

    def test_a_detached_head_without_a_target_is_an_error_that_asks_for_one_and_runs_nothing(self) -> None:
        self.commit("c1")
        git(self.repo, "checkout", "-q", "--detach")
        code, _, err = self.review(self.fake_claude(review_text()))
        self.assertEqual(code, 1)
        self.assertIn("detached", err)
        self.assertIn("--target", err)
        self.assertFalse(self.record.exists())
        code, out, err = self.review(self.fake_claude(review_text()), "--target", "42")
        self.assertEqual(code, 0, err)
        self.assertIn("review round 1/3 for target:42", out)


class RoundCountLineTest(ReviewCase):
    def test_count_ends_with_the_rounds_used_on_the_current_change(self) -> None:
        self.assertEqual(self.cli_ok("count").splitlines()[-1], "review rounds on main@unborn: 0/3")
        self.review(self.fake_claude(review_text(FINDING_A)))
        self.review(self.fake_claude(review_text()))
        out = self.cli_ok("count", "--by-rule")
        lines = out.splitlines()
        self.assertEqual(lines[-1], "review rounds on main@unborn: 2/3")
        self.assertTrue(lines[0].startswith("findings: 1 total, 1 open"))
        self.assertIn("  P0E.1: 1", lines)

    def test_it_reflects_max_review_rounds_and_other_keys_are_not_counted(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=5)
        self.append_raw(review_round("main@unborn"), review_round("feature@abc"))
        self.assertEqual(self.cli_ok("count").splitlines()[-1], "review rounds on main@unborn: 1/5")

    def test_it_names_the_fork_point_of_a_feature_branch(self) -> None:
        base = self.commit("c1")
        git(self.repo, "switch", "-q", "-c", "feature")
        self.commit("f1")
        self.assertEqual(self.cli_ok("count").splitlines()[-1], f"review rounds on feature@{base[:12]}: 0/3")

    def test_a_detached_head_says_the_rounds_do_not_apply_and_is_no_error(self) -> None:
        self.commit("c1")
        git(self.repo, "checkout", "-q", "--detach")
        code, out, err = self.cli("count")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "review rounds: n/a (detached HEAD)")
        self.assertTrue(out.startswith("findings:"))

    def test_a_repo_with_no_default_branch_still_counts_and_says_why_there_is_no_rounds_line(self) -> None:
        self.commit("c1")
        git(self.repo, "branch", "-m", "main", "trunk")
        git(self.repo, "switch", "-q", "-c", "feature")
        code, out, err = self.cli("count")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "review rounds: n/a (no default branch found (no origin/HEAD, main or master))")

    def test_a_branch_with_no_shared_history_says_so(self) -> None:
        self.commit("c1")
        git(self.repo, "switch", "-q", "--orphan", "island")
        self.commit("i1")
        code, out, err = self.cli("count")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[-1], "review rounds: n/a (island shares no history with main)")

    def test_a_default_branch_that_is_not_a_branch_does_not_fail_count(self) -> None:
        self.commit("c1")
        self.write_config(rules_files=["rules.md"], default_branch="nope")
        code, out, err = self.cli("count")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("findings:"))
        self.assertEqual(out.splitlines()[-1], "review rounds: unavailable (config default_branch 'nope' is not a branch of this repo)")
        self.assertEqual(self.cli("review", "--dry-run")[0], 1)  # review, which needs the rounds, still refuses


class RoundCapConfigTest(ReviewCase):
    def test_max_review_rounds_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, "3", True, 2.5, None, [3]):
            with self.subTest(value=bad):
                self.write_config(rules_files=["rules.md"], max_review_rounds=bad)
                code, _, err = self.cli("count")
                self.assertEqual(code, 1)
                self.assertIn("max_review_rounds", err)

    def test_the_default_is_three_and_one_is_allowed(self) -> None:
        self.assertEqual(self.cli_ok("count").splitlines()[-1].rsplit(": ", 1)[1], "0/3")
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.assertEqual(self.cli_ok("count").splitlines()[-1].rsplit(": ", 1)[1], "0/1")

    def test_default_branch_must_be_a_non_empty_string(self) -> None:
        for bad in ("", 5, ["main"]):
            with self.subTest(value=bad):
                self.write_config(rules_files=["rules.md"], default_branch=bad)
                code, _, err = self.cli("count")
                self.assertEqual(code, 1)
                self.assertIn("default_branch", err)

    def test_a_configured_default_branch_is_used_by_review(self) -> None:
        base = self.commit("c1")
        git(self.repo, "switch", "-q", "-c", "develop")
        develop = self.commit("d1")
        git(self.repo, "switch", "-q", "-c", "feature")
        self.commit("f1")
        self.assertIn(f"feature@{base[:12]}", self.review(self.fake_claude(review_text()))[1])
        self.write_config(rules_files=["rules.md"], default_branch="develop")
        self.assertIn(f"review round 1/3 for feature@{develop[:12]}", self.review(self.fake_claude(review_text()))[1])


class TableTest(unittest.TestCase):
    def event(self, **overrides: object) -> dict:
        event = {"display_id": "R1-1", "rule_id": "P0E.1", "severity": "blocker", "file": "src/a.py", "line": 3, "summary": "s"}
        event.update(overrides)
        return event

    def test_columns_are_aligned_and_the_summary_is_last_on_one_line(self) -> None:
        lines = findings_table([self.event(), self.event(display_id="R1-002", rule_id="unmapped", severity="should-fix",
                                                          file="src/deep/long_name.py", line=120, summary="two  words\nsplit")])
        self.assertEqual(len(lines), 3)
        for column, first, second in (("rule_id", "P0E.1", "unmapped"), ("severity", "blocker", "should-fix"),
                                      ("location", "src/a.py:3", "src/deep/long_name.py:120"), ("summary", "s", "two words split")):
            with self.subTest(column=column):
                offset = lines[0].index(column)
                self.assertEqual(lines[1][offset:offset + len(first)], first)
                self.assertEqual(lines[2][offset:offset + len(second)], second)
        self.assertEqual(lines[2][lines[0].index("summary"):], "two words split")


if __name__ == "__main__":
    unittest.main()
