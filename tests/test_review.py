import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import (
    FIXTURES, REPO_ROOT, STYLE_RULES, RepoCase, gate_finding, git, make_config, review_finding, review_item, review_round,
)

from house_rules.errors import HouseRulesError, RoundKeyUnavailable
from house_rules.review import (
    check_run_id, claude_command, default_run_id, findings_table, replaced_critic_version, review_rounds_used, round_key,
)
from house_rules.review_evidence import MAX_SNIPPET_CHARS, TRUNCATED_MARK, snapshot_snippets, snippet_around, worktree_snippets
from house_rules.source_evidence import MAX_SOURCE_BYTES
from house_rules.state_tree import state_tree


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
        self.version_record = self.tmp / "claude-version-calls.jsonl"
        # No test may reach the real `claude`: anything that does not set its own command gets one that cannot run.
        patcher = mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": "/nonexistent/claude"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_claude(self, stdout: str = "", exit_code: int = 0, stderr: str = "", sleep: float = 0,
                    version: str = "2.1.0 (Claude Code)", version_exit_code: int = 0, version_sleep: float = 0,
                    during_review: str = "") -> Path:
        """An executable that records how it was called and prints canned output.

        Asked for `--version` it answers with `version` and records the question apart from the review call, so a test can
        count the questions and still read what the review itself was called with. `during_review` is Python run after the
        review call is recorded and before it prints: what happens to the repo while claude is busy.
        """
        script = self.tmp / "fake-claude"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time\n"
            "if '--version' in sys.argv[1:]:\n"
            f"    open({str(self.version_record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"    time.sleep({version_sleep})\n"
            f"    sys.stdout.write({version!r})\n"
            f"    sys.exit({version_exit_code})\n"
            f"open({str(self.record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}}))\n"
            f"{during_review}\n"
            f"time.sleep({sleep})\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"sys.exit({exit_code})\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def call(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def version_asks(self) -> list[list[str]]:
        """The arguments of every `--version` question the fake has been asked, oldest first."""
        if not self.version_record.exists():
            return []
        return [json.loads(line) for line in self.version_record.read_text(encoding="utf-8").splitlines()]

    def review(self, claude: Path | str, *argv: str) -> tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(claude)}):
            return self.cli("review", *argv)

    def findings(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "finding"]

    def review_runs(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "review_run"]

    def reviews_dir(self) -> Path:
        return self.repo / ".house-rules" / "reviews"

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
        with mock.patch("house_rules.review.subprocess.run", wraps=subprocess.run) as run:  # the module is shared: git calls land here too
            self.review(script, "--run", "R1")
        claude_calls = [call for call in run.call_args_list if call.args[0][0] == str(script)]
        self.assertEqual([call.args[0][1:] for call in claude_calls], [["--version"], ["-p", "/code-review high"]])
        for call in claude_calls:
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL, call.args[0])

    def test_cwd_is_the_repo_root_even_when_started_from_a_subdirectory(self) -> None:
        (self.repo / "sub").mkdir()
        cwd = os.getcwd()
        os.chdir(self.repo / "sub")
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(self.fake_claude(review_text()))}):
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
        with mock.patch("house_rules.review.TIMEOUT_SECONDS", 0.3):
            code, _, err = self.review(self.fake_claude(sleep=5), "--run", "R1")
        self.assertEqual(code, 1)
        self.assertIn("did not finish within", err)

    def test_the_real_executable_with_the_replacement_command_end_to_end(self) -> None:
        script = self.fake_claude(review_text(FINDING_A))
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "house-rules"), "review", "--run", "R1", "--repo", str(self.repo)],
                              capture_output=True, text=True, env={**os.environ, "HOUSE_RULES_CLAUDE_CMD": str(script)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("R1-1", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


class RecordingTest(ReviewCase):
    def test_the_raw_output_is_saved_and_the_findings_are_ingested_as_l1(self) -> None:
        raw = review_text(FINDING_A, FINDING_B)
        code, out, err = self.review(self.fake_claude(raw), "--run", "R7")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.reviews_dir() / "R7.txt").read_text(encoding="utf-8"), raw)
        self.assertIn("saved raw review output to .house-rules/reviews/R7.txt", out)
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
        self.assertIn("saved raw review output to .house-rules/reviews/R1.txt", out)
        self.assertEqual(self.saved(), ["R1.txt"])
        self.assertFalse(self.ledger.exists())

    def test_run_ids_that_are_not_safe_file_names_are_refused(self) -> None:
        for bad in ("../x", "a/b", ".hidden", "", "has space", "-x"):
            with self.subTest(run=bad):
                with self.assertRaises(HouseRulesError):
                    check_run_id(bad)
        code, _, err = self.review(self.fake_claude(review_text()), "--run", "../escape")
        self.assertEqual(code, 1)
        self.assertIn("must match", err)
        self.assertFalse((self.repo / ".house-rules" / "escape.txt").exists())

    def test_the_open_count_and_the_closing_hint_are_printed(self) -> None:
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R1")
        self.assertIn("open: 2.", out)
        self.assertIn("Fixes-Finding", out)
        self.assertNotIn("label-page", out)

    def test_a_pile_of_open_findings_suggests_the_label_page(self) -> None:
        self.write_config(rules_files=["rules.md"], label_page_threshold=1)
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A, FINDING_B)), "--run", "R1")
        self.assertIn("open > 1: run `house-rules label-page`", out)

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
        with self.assertRaises(HouseRulesError) as caught:
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
        self.assertIn(f"round cap reached for {self.KEY}: 3/3 rounds used; 3 findings still open (house-rules count). "
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
        self.assertRegex(run.pop("tree"), r"^[0-9a-f]{40}$")  # the working tree as the review found it (rules.md and .house-rules.json, uncommitted)
        self.assertEqual(run, {"v": 1, "kind": "review_run", "repo": "demo-repo", "run": "R5", "round_key": self.KEY, "round": 1,
                               "max_rounds": 3, "over_cap": False, "finding_count": 2, "source": "claude",
                               "reviewer": "claude-code", "reviewer_version": "2.1.0 (Claude Code)", "level": "high",
                               "head": "none"})  # no commit yet; version_changed is written only when it is true

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


class CriticVersionTest(ReviewCase):
    """`reviewer`, `reviewer_version`, `level` and `head` on every review_run, and the line announcing a new critic version."""

    OLD, NEW = "2.1.0 (Claude Code)", "2.2.0 (Claude Code)"

    def setUp(self) -> None:
        super().setUp()
        self.write_config(rules_files=["rules.md"], max_review_rounds=20)  # these tests review one change many times

    def review_with(self, version: str, *argv: str, stdout: str | None = None) -> tuple[int, str, str]:
        return self.review(self.fake_claude(review_text() if stdout is None else stdout, version=version), *argv)

    def saved_review(self) -> Path:
        saved = self.tmp / "saved.txt"
        saved.write_text(review_text(), encoding="utf-8")
        return saved

    def versions(self) -> list[tuple[str | None, bool | None]]:
        """(reviewer_version, version_changed) of each review_run; None when the key is absent."""
        return [(event["reviewer_version"], event.get("version_changed")) for event in self.review_runs()]

    def test_a_review_records_what_ran_it_at_which_level_on_which_commit(self) -> None:
        head = self.commit("c1")
        code, out, err = self.review(self.fake_claude(review_text(FINDING_A), version=self.OLD), "--run", "R1", "--level", "low")
        self.assertEqual((code, err), (0, ""))
        (run,) = self.review_runs()
        self.assertEqual((run["source"], run["reviewer"], run["reviewer_version"], run["level"]),
                         ("claude", "claude-code", self.OLD, "low"))
        self.assertEqual(run["head"], head)
        self.assertRegex(run["head"], r"^[0-9a-f]{40}$")  # the whole sha: it will be handed to git later
        self.assertNotIn("version_changed", run)
        self.assertNotIn("critic version", out)

    def test_the_level_is_the_one_the_review_ran_at(self) -> None:
        self.review_with(self.OLD)
        self.review_with(self.OLD, "--level", "xhigh")
        self.assertEqual([event["level"] for event in self.review_runs()], ["high", "xhigh"])

    def test_head_is_where_the_branch_stood_before_the_reviewer_started(self) -> None:
        before = self.commit("c1")

        def commit_while_claude_is_busy(argv: list[str], cwd: Path) -> str:
            self.commit("c2")
            return review_text(FINDING_A)

        with mock.patch("house_rules.cli.run_claude", side_effect=commit_while_claude_is_busy):
            code, _, err = self.review(self.fake_claude(), "--run", "R1")
        self.assertEqual(code, 0, err)
        after = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertEqual(self.review_runs()[0]["head"], before)
        self.assertEqual(self.findings()[0]["sha"], after[:8])  # a finding's sha is the HEAD at import: the other moment

    def test_head_is_none_before_the_first_commit(self) -> None:
        self.review_with(self.OLD)
        self.assertEqual(self.review_runs()[0]["head"], "none")

    def test_a_from_file_review_does_not_know_its_reviewer_asks_nobody_and_records_the_current_head(self) -> None:
        saved = self.saved_review()
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F1")[0], 0)
        head = self.commit("c1")
        code, out, err = self.cli("review", "--from-file", str(saved), "--run", "F2")
        self.assertEqual((code, err), (0, ""))
        first, second = self.review_runs()
        for run in (first, second):
            self.assertEqual((run["source"], run["reviewer"], run["reviewer_version"], run["level"]), ("file", None, None, None))
            self.assertNotIn("version_changed", run)
        self.assertEqual((first["head"], second["head"]), ("none", head))
        self.assertEqual(self.version_asks(), [])
        self.assertNotIn("critic version", out)

    def test_the_version_is_asked_once_per_review_of_the_command_that_runs_it(self) -> None:
        self.review(self.fake_claude(review_text()), "--run", "R1")
        self.assertEqual(self.version_asks(), [["--version"]])
        self.review(f"{sys.executable} {self.fake_claude(review_text())} --extra", "--run", "R2")
        self.assertEqual(self.version_asks(), [["--version"], ["--extra", "--version"]])
        self.assertEqual(self.call()["argv"], ["--extra", "-p", "/code-review high"])

    def test_a_version_that_cannot_be_read_is_recorded_as_null_and_said_once(self) -> None:
        cases = [("exited with 2", {"version_exit_code": 2}), ("printed nothing", {"version": ""}),
                 ("did not finish within 0.3s", {"version_sleep": 5})]
        for number, (reason, kwargs) in enumerate(cases, start=1):
            # only the case that hangs needs the short limit; a loaded machine must not turn the other two into timeouts
            with self.subTest(reason=reason), mock.patch("house_rules.review.VERSION_TIMEOUT_SECONDS", 0.3 if "version_sleep" in kwargs else 10):
                code, out, err = self.review(self.fake_claude(review_text(FINDING_A), **kwargs), "--run", f"V{number}")
                self.assertEqual(code, 0, err)
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn("warning: critic version not recorded", err)
                self.assertIn(reason, err)
                self.assertIn("reviewer_version is null", err)
                run = self.review_runs()[-1]
                self.assertEqual((run["run"], run["reviewer"], run["reviewer_version"]), (f"V{number}", "claude-code", None))
                self.assertNotIn("critic version changed", out)
        self.assertEqual(len(self.findings()), 3)  # the reviews themselves went ahead

    def test_a_new_critic_version_is_announced_on_its_first_review_and_flagged_in_the_ledger(self) -> None:
        outs = [self.review_with(version, "--run", f"A{number}")[1]
                for number, version in enumerate([self.OLD, self.OLD, self.NEW, self.NEW], start=1)]
        announcement = f"critic version changed: {self.OLD} -> {self.NEW} (first review on this version)"
        self.assertEqual([announcement in out for out in outs], [False, False, True, False])
        self.assertEqual(outs[2].count("critic version changed"), 1)
        self.assertEqual(self.versions(), [(self.OLD, None), (self.OLD, None), (self.NEW, True), (self.NEW, None)])

    def test_the_version_is_compared_with_the_latest_earlier_review_not_the_first(self) -> None:
        outs = [self.review_with(version, "--run", f"L{number}")[1] for number, version in enumerate("ABBA", start=1)]
        self.assertEqual(self.versions(), [("A", None), ("B", True), ("B", None), ("A", True)])
        self.assertIn("critic version changed: B -> A (first review on this version)", outs[3])

    def test_a_review_without_a_version_is_skipped_when_looking_for_the_previous_one(self) -> None:
        self.review_with("A", "--run", "S1")
        self.assertEqual(self.cli("review", "--from-file", str(self.saved_review()), "--run", "S2")[0], 0)
        self.review(self.fake_claude(review_text(), version_exit_code=1), "--run", "S3")
        _, out, _ = self.review_with("B", "--run", "S4")
        self.assertIn("critic version changed: A -> B (first review on this version)", out)
        self.assertEqual(self.versions(), [("A", None), (None, None), (None, None), ("B", True)])

    def test_a_review_with_no_version_in_between_is_no_change(self) -> None:
        self.review_with("A", "--run", "N1")
        self.review(self.fake_claude(review_text(), version_exit_code=1), "--run", "N2")
        _, out, _ = self.review_with("A", "--run", "N3")
        self.assertNotIn("critic version changed", out)
        self.assertEqual(self.versions(), [("A", None), (None, None), ("A", None)])

    def test_the_announcement_comes_before_the_findings_table(self) -> None:
        self.review_with("A", "--run", "T1")
        _, out, _ = self.review(self.fake_claude(review_text(FINDING_A), version="B"), "--run", "T2")
        self.assertLess(out.index("critic version changed"), out.index("rule_id"))

    def test_a_review_that_fails_leaves_the_announcement_for_the_next_one(self) -> None:
        self.review_with("A", "--run", "F1")
        code, out, _ = self.review_with("B", "--run", "F2", stdout="The model printed no JSON.\n")
        self.assertEqual(code, 1)
        self.assertNotIn("critic version changed", out)
        _, out, _ = self.review_with("B", "--run", "F3")
        self.assertIn("critic version changed: A -> B (first review on this version)", out)
        self.assertEqual(self.versions(), [("A", None), ("B", True)])

    def test_nothing_is_asked_or_announced_when_no_review_runs(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        self.review_with("A", "--run", "D1")
        newer = self.fake_claude(review_text(), version="B")
        self.assertEqual(self.review(newer, "--dry-run")[0], 0)  # a dry run past the cap previews and runs nothing
        self.assertEqual(self.review(newer, "--run", "D2")[0], 3)  # a refused round costs nothing
        _, out, err = self.cli("review", "--from-file", str(self.reviews_dir() / "D1.txt"), "--run", "D1")  # a replay
        self.assertEqual(err, "")
        self.assertNotIn("critic version", out)
        self.assertEqual(self.version_asks(), [["--version"]])
        self.assertEqual(self.versions(), [("A", None)])

    def test_a_missing_claude_is_reported_once_and_does_not_also_warn_about_the_version(self) -> None:
        code, _, err = self.review(self.tmp / "no-such-claude", "--run", "R1")
        self.assertEqual(code, 1)
        self.assertNotIn("warning", err)
        self.assertEqual(err.count("not found on PATH"), 1)


class ReplacedCriticVersionTest(unittest.TestCase):
    def test_it_is_the_latest_recorded_version_when_this_one_differs(self) -> None:
        events = [review_round(reviewer_version="A"), review_finding(), review_round(reviewer_version="B", run="r2")]
        self.assertEqual(replaced_critic_version(events, "C"), "B")
        self.assertIsNone(replaced_critic_version(events, "B"))

    def test_there_is_nothing_to_replace_without_a_version_to_compare(self) -> None:
        self.assertIsNone(replaced_critic_version([review_round(reviewer_version="A")], None))
        self.assertIsNone(replaced_critic_version([], "A"))
        self.assertIsNone(replaced_critic_version([review_round(), review_round(reviewer_version=None)], "A"))  # old, and null

    def test_a_review_with_a_null_version_is_not_the_previous_one(self) -> None:
        events = [review_round(reviewer_version="A"), review_round(reviewer_version=None, run="r2", source="file")]
        self.assertEqual(replaced_critic_version(events, "B"), "A")
        self.assertIsNone(replaced_critic_version(events, "A"))


def numbered(count: int, width: int = 0) -> list[str]:
    """`line 1` .. `line <count>`, each padded with dots to `width` characters."""
    return [f"line {number}".ljust(width, ".") for number in range(1, count + 1)]


def joined(first: int, last: int) -> str:
    return "\n".join(f"line {number}" for number in range(first, last + 1))


class SnippetWindowTest(unittest.TestCase):
    def test_three_lines_either_side_of_the_flagged_one(self) -> None:
        self.assertEqual(snippet_around(numbered(20), 10), joined(7, 13))

    def test_the_start_and_end_of_the_file_clip_the_window_and_say_nothing(self) -> None:
        lines = numbered(10)
        self.assertEqual(snippet_around(lines, 1), joined(1, 4))
        self.assertEqual(snippet_around(lines, 2), joined(1, 5))
        self.assertEqual(snippet_around(lines, 9), joined(6, 10))
        self.assertEqual(snippet_around(lines, 10), joined(7, 10))
        self.assertEqual(snippet_around(["only"], 1), "only")

    def test_a_line_the_file_does_not_have_has_no_snippet(self) -> None:
        for line in (0, 11, 1000):
            self.assertIsNone(snippet_around(numbered(10), line), line)
        self.assertIsNone(snippet_around([], 1))

    def test_seven_lines_that_fit_the_budget_exactly_are_kept_whole(self) -> None:
        lines = ["x" * 85] * 3 + ["x" * 84] + ["x" * 85] * 3  # 6 * 85 + 84 characters and 6 line breaks: 600
        whole = snippet_around(lines, 4)
        self.assertEqual((len(whole), len(whole.splitlines())), (MAX_SNIPPET_CHARS, 7))
        self.assertNotIn(TRUNCATED_MARK, whole)

    def test_one_character_over_the_budget_drops_a_line_and_says_so(self) -> None:
        over = snippet_around(["x" * 85] * 7, 4)  # 601
        shown = over.splitlines()
        self.assertEqual(shown, ["x" * 85] * 6 + [TRUNCATED_MARK])

    def test_long_lines_are_dropped_farthest_first_and_the_flagged_line_stays(self) -> None:
        lines = [f"{number}:" + "y" * 198 for number in range(1, 21)]  # 200 characters each
        shown = snippet_around(lines, 10).splitlines()
        self.assertEqual(shown, [lines[9], lines[10], TRUNCATED_MARK])  # ties between above and below go to the line above

    def test_the_farthest_line_goes_first_when_the_window_is_lopsided(self) -> None:
        lines = [f"{number}:" + "y" * 198 for number in range(1, 21)]
        shown = snippet_around(lines, 2).splitlines()  # one line above the flagged one, three below
        self.assertEqual(shown, [lines[1], lines[2], TRUNCATED_MARK])

    def test_a_flagged_line_longer_than_the_budget_is_cut_and_marked(self) -> None:
        self.assertEqual(snippet_around(["short", "z" * 5000, "short"], 2), "z" * MAX_SNIPPET_CHARS + "\n" + TRUNCATED_MARK)

    def test_the_whole_snippet_stays_within_seven_lines_and_the_budget_plus_the_mark(self) -> None:
        for width in (0, 50, 90, 100, 150, 250, 700):
            for line in (1, 5, 20):
                with self.subTest(width=width, line=line):
                    snippet = snippet_around(numbered(20, width), line)
                    self.assertLessEqual(len(snippet.splitlines()), 7)
                    self.assertLessEqual(len(snippet), MAX_SNIPPET_CHARS + len(TRUNCATED_MARK) + 1)
                    self.assertIn(f"line {line}", snippet)


class SnippetLocatorTest(RepoCase):
    def snapshot(self, basis: str = "local_pre_review") -> dict:
        return {"basis": basis, "head": None, "omitted_files": [], "files": {
            "src/a.py": {"status": "available", "text": "\n".join(numbered(10)) + "\n", "sha256": "x"},
            "src/big.py": {"status": "unavailable", "reason": "Review evidence text budget exceeded."}}}

    def locator(self, snapshot: dict):
        return snapshot_snippets(make_config(root=self.repo, ledger_path=self.repo / ".style-findings.jsonl"), self.repo, snapshot)

    def test_a_review_snippet_is_cut_from_the_snapshot_whichever_way_the_file_is_spelled(self) -> None:
        locate = self.locator(self.snapshot())
        for spelling in ("src/a.py", "./src/a.py", str(self.repo / "src" / "a.py")):
            with self.subTest(spelling=spelling):
                self.assertEqual(locate(spelling, 5), (joined(2, 8), "review"))

    def test_what_the_snapshot_cannot_show_is_unavailable(self) -> None:
        locate = self.locator(self.snapshot())
        for file, line in (("src/missing.py", 1), ("src/big.py", 1), ("src/a.py", 11), ("src/a.py", 0),
                           (str(self.tmp / "outside" / "a.py"), 1), ("../a.py", 1)):
            with self.subTest(file=file, line=line):
                self.assertEqual(locate(file, line), (None, "unavailable"))

    def test_a_snapshot_that_is_not_verified_against_the_reviewed_target_is_not_used(self) -> None:
        locate = self.locator(self.snapshot("local_pre_review_target_unverified"))
        self.assertEqual(locate("src/a.py", 5), (None, "unavailable"))


class CommittedSnippetLocatorTest(RepoCase):
    """What the locator does with a file the snapshot does not hold: one unchanged since HEAD is cut from the HEAD it names."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "same.py").write_text("\n".join(numbered(10)) + "\n", encoding="utf-8")
        (self.repo / ".house-rules").mkdir()
        (self.repo / ".house-rules" / "notes.txt").write_text("\n".join(numbered(10)) + "\n", encoding="utf-8")
        (self.repo / ".style-findings.jsonl").write_text("\n".join(numbered(10)) + "\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        self.head = self.commit("c1")
        self.config = make_config(root=self.repo, ledger_path=self.repo / ".style-findings.jsonl")

    def locate(self, file: str, line: int = 5, **snapshot: object) -> tuple[str | None, str]:
        held = {"basis": "local_pre_review", "head": self.head, "omitted_files": [], "files": {}, **snapshot}
        return snapshot_snippets(self.config, self.repo, held)(file, line)

    def test_a_file_the_snapshot_does_not_hold_is_cut_from_the_head_it_names(self) -> None:
        for spelling in ("src/same.py", "./src/same.py", str(self.repo / "src" / "same.py")):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.locate(spelling), (joined(2, 8), "review"))
        self.assertEqual(self.locate("src/same.py", 11), (None, "unavailable"))

    def test_it_is_the_head_the_snapshot_names_not_the_one_the_repo_has_now(self) -> None:
        (self.repo / "src" / "same.py").write_text("moved on\n", encoding="utf-8")
        git(self.repo, "add", "src")
        self.commit("c2")
        self.assertEqual(self.locate("src/same.py"), (joined(2, 8), "review"))

    def test_a_file_the_snapshot_holds_is_never_replaced_by_the_head(self) -> None:
        kept = {"status": "available", "text": "\n".join(f"snap {number}" for number in range(1, 11))}
        self.assertEqual(self.locate("src/same.py", files={"src/same.py": kept}),
                         ("\n".join(f"snap {number}" for number in range(2, 9)), "review"))
        too_short = {"status": "available", "text": "only\ntwo lines"}  # the reviewer saw no line 5 here, whatever HEAD has
        over_budget = {"status": "unavailable", "reason": "Review evidence text budget exceeded."}
        for entry in (too_short, over_budget):
            with self.subTest(entry=entry):
                self.assertEqual(self.locate("src/same.py", files={"src/same.py": entry}), (None, "unavailable"))

    def test_a_changed_file_the_snapshot_left_out_has_no_snippet(self) -> None:
        self.assertEqual(self.locate("src/same.py", omitted_files=["src/same.py"]), (None, "unavailable"))
        self.assertEqual(self.locate("src/same.py", omitted_files=["src/other.py"]), (joined(2, 8), "review"))

    def test_the_files_house_rules_writes_itself_have_no_snippet(self) -> None:
        for name in (".house-rules/notes.txt", ".style-findings.jsonl"):
            with self.subTest(name=name):
                self.assertEqual(self.locate(name), (None, "unavailable"))

    def test_before_the_first_commit_there_is_nothing_to_read(self) -> None:
        self.assertEqual(self.locate("src/same.py", head=None), (None, "unavailable"))

    def test_a_snapshot_that_is_not_verified_against_the_reviewed_target_reads_nothing_from_the_head_either(self) -> None:
        self.assertEqual(self.locate("src/same.py", basis="local_pre_review_target_unverified"), (None, "unavailable"))

    def test_what_the_commit_does_not_hold_is_unavailable(self) -> None:
        (self.repo / "untracked.py").write_text("\n".join(numbered(10)) + "\n", encoding="utf-8")
        for file in ("untracked.py", "src/missing.py", "src", "../same.py", str(self.tmp / "outside" / "same.py")):
            with self.subTest(file=file):
                self.assertEqual(self.locate(file), (None, "unavailable"))


class WorktreeSnippetLocatorTest(RepoCase):
    def test_a_worktree_snippet_is_read_from_the_file_as_it_is_now(self) -> None:
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("\n".join(numbered(10)) + "\n", encoding="utf-8")
        locate = worktree_snippets(self.repo)
        self.assertEqual(locate("src/a.py", 5), (joined(2, 8), "worktree"))
        self.assertEqual(locate(str(self.repo / "src" / "a.py"), 10), (joined(7, 10), "worktree"))
        (self.repo / "src" / "a.py").write_text("changed\n", encoding="utf-8")
        self.assertEqual(locate("src/a.py", 1), ("changed", "worktree"))

    def test_a_worktree_snippet_never_comes_from_outside_the_repo_or_from_a_secret(self) -> None:
        (self.tmp / "outside.txt").write_text("not yours\n", encoding="utf-8")
        (self.repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.repo / "link.txt").symlink_to(self.tmp / "outside.txt")
        (self.repo / "empty.py").write_text("", encoding="utf-8")
        locate = worktree_snippets(self.repo)
        for file in ("../outside.txt", str(self.tmp / "outside.txt"), ".env", ".git/config", "link.txt", "absent.py",
                     "empty.py", "."):
            with self.subTest(file=file):
                self.assertEqual(locate(file, 1), (None, "unavailable"))


class SnippetCase(ReviewCase):
    """src/a.py is committed as base-1..base-10 and edited to work-1..work-10, so the review sees the edit."""

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src").mkdir()
        self.write_numbered("src/a.py", "base")
        self.write_numbered("src/unchanged.py", "same")
        git(self.repo, "add", "src")
        self.commit("baseline")
        self.write_numbered("src/a.py", "work")

    def write_numbered(self, name: str, word: str) -> None:
        (self.repo / name).write_text("".join(f"{word}-{number}\n" for number in range(1, 11)), encoding="utf-8")

    def window(self, word: str, first: int, last: int) -> str:
        return "\n".join(f"{word}-{number}" for number in range(first, last + 1))

    def by_line(self) -> dict[tuple[str, int], dict]:
        return {(event["file"], event["line"]): event for event in self.findings()}


class ReviewSnippetTest(SnippetCase):
    def test_a_snippet_is_the_file_as_it_was_when_the_review_started(self) -> None:
        rewrite = f"open({str(self.repo / 'src' / 'a.py')!r}, 'w').write('edited while claude was busy\\n')"
        script = self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1")), during_review=rewrite)
        code, _, err = self.review(script, "--run", "R1")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.repo / "src" / "a.py").read_text(encoding="utf-8"), "edited while claude was busy\n")
        (finding,) = self.findings()
        self.assertEqual(finding["snippet"], self.window("work", 2, 8))
        self.assertEqual(finding["snippet_source"], "review")

    def test_the_window_is_clipped_at_the_file_edges_and_the_file_may_be_named_by_absolute_path(self) -> None:
        items = [review_item(file="src/a.py", line=1, category="p0e-1"),
                 review_item(file=str(self.repo / "src" / "a.py"), line=10, category="p0e-1")]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        shown = self.by_line()
        self.assertEqual(shown[("src/a.py", 1)]["snippet"], self.window("work", 1, 4))
        self.assertEqual(shown[(str(self.repo / "src" / "a.py"), 10)]["snippet"], self.window("work", 7, 10))
        self.assertEqual({event["snippet_source"] for event in self.findings()}, {"review"})

    def test_what_the_review_never_saw_is_recorded_as_unavailable_not_left_out(self) -> None:
        items = [review_item(file="src/missing.py", line=1), review_item(file="src/a.py", line=99),
                 review_item(file="/elsewhere/x.py", line=1)]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        self.assertEqual(len(self.findings()), 3)
        for event in self.findings():
            with self.subTest(file=event["file"], line=event["line"]):
                self.assertEqual(event["snippet_source"], "unavailable")
                self.assertNotIn("snippet", event)

    def test_a_secret_file_is_not_read_into_a_snippet_however_its_name_is_spelled(self) -> None:
        (self.repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        git(self.repo, "add", "-f", ".env")
        self.commit("a committed secret")  # unchanged since, so the snippet comes from the commit, not from the review's snapshot
        names = (".env", ":.env", ":(top).env", ":(literal).env", ":/.env", str(self.repo / ".env"), str(self.repo / ":.env"))
        items = [review_item(file=name, line=1, category="p0e-1") for name in names]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        self.assertEqual(len(self.findings()), len(names))
        for event in self.findings():
            with self.subTest(file=event["file"]):
                self.assertEqual(event["snippet_source"], "unavailable")
                self.assertNotIn("snippet", event)
        self.assertNotIn("secret", self.ledger.read_text(encoding="utf-8"))

    def test_findings_on_the_same_line_share_one_snippet(self) -> None:
        items = [review_item(file="src/a.py", line=5, category="p0e-1"),
                 review_item(file="src/a.py", line=5, summary="Another problem", category="p1-1")]
        self.review(self.fake_claude(review_text(*items)), "--run", "R1")
        (finding,) = self.findings()
        self.assertEqual(len(finding["also_matches"]), 1)
        self.assertEqual((finding["snippet"], finding["snippet_source"]), (self.window("work", 2, 8), "review"))

    def test_long_lines_are_shortened_and_the_snippet_says_so(self) -> None:
        (self.repo / "src" / "a.py").write_text("".join(f"{number}: " + "w" * 200 + "\n" for number in range(1, 11)), encoding="utf-8")
        self.review(self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1"))), "--run", "R1")
        (finding,) = self.findings()
        shown = finding["snippet"].splitlines()
        self.assertEqual(shown[-1], TRUNCATED_MARK)
        self.assertTrue(shown[0].startswith("5: "), shown)
        self.assertLessEqual(len(finding["snippet"]), MAX_SNIPPET_CHARS + len(TRUNCATED_MARK) + 1)
        self.assertEqual(finding["snippet_source"], "review")

    def test_a_target_review_has_no_verified_snapshot_and_records_unavailable(self) -> None:
        item = review_item(file="src/a.py", line=5, category="p0e-1")
        self.assertEqual(self.review(self.fake_claude(review_text(item)), "--run", "R1", "--target", "feature/x")[0], 0)
        (finding,) = self.findings()
        self.assertEqual(finding["snippet_source"], "unavailable")
        self.assertNotIn("snippet", finding)
        evidence = json.loads((self.reviews_dir() / "R1.evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["basis"], "local_pre_review_target_unverified")  # the reason it is not trusted

    def test_a_replay_keeps_the_snippet_of_the_original_review(self) -> None:
        item = review_item(file="src/a.py", line=5, category="p0e-1")
        self.review(self.fake_claude(review_text(item)), "--run", "R1")
        before = self.ledger_bytes()
        self.write_numbered("src/a.py", "later")
        code, out, _ = self.cli("review", "--from-file", str(self.reviews_dir() / "R1.txt"), "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn("no new findings", out)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.findings()[0]["snippet"], self.window("work", 2, 8))

    def test_the_snippet_does_not_change_what_the_table_or_the_ids_look_like(self) -> None:
        _, out, _ = self.review(self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1"))), "--run", "R1")
        self.assertIn("R1-1", out)
        self.assertNotIn("work-", out)


class FromFileSnippetTest(SnippetCase):
    def review_file(self, *items: dict) -> str:
        path = self.tmp / "earlier.txt"
        path.write_text(review_text(*items), encoding="utf-8")
        return str(path)

    def test_the_snippet_is_read_from_the_working_tree_and_says_so(self) -> None:
        items = [review_item(file="src/a.py", line=5, category="p0e-1"),
                 review_item(file="src/unchanged.py", line=1, category="p0e-1")]
        self.assertEqual(self.cli("review", "--from-file", self.review_file(*items), "--run", "F1")[0], 0)
        shown = self.by_line()
        self.assertEqual((shown[("src/a.py", 5)]["snippet"], shown[("src/a.py", 5)]["snippet_source"]),
                         (self.window("work", 2, 8), "worktree"))
        self.assertEqual((shown[("src/unchanged.py", 1)]["snippet"], shown[("src/unchanged.py", 1)]["snippet_source"]),
                         (self.window("same", 1, 4), "worktree"))

    def test_a_missing_file_or_line_is_unavailable(self) -> None:
        items = [review_item(file="src/missing.py", line=1), review_item(file="src/a.py", line=99)]
        self.assertEqual(self.cli("review", "--from-file", self.review_file(*items), "--run", "F1")[0], 0)
        self.assertEqual([(event["snippet_source"], "snippet" in event) for event in self.findings()],
                         [("unavailable", False), ("unavailable", False)])

    def test_a_review_file_cannot_pull_text_from_outside_the_repo_into_the_ledger(self) -> None:
        (self.tmp / "outside.txt").write_text("private text\n", encoding="utf-8")
        (self.repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        items = [review_item(file="../outside.txt", line=1), review_item(file=str(self.tmp / "outside.txt"), line=1),
                 review_item(file=".env", line=1)]
        self.assertEqual(self.cli("review", "--from-file", self.review_file(*items), "--run", "F1")[0], 0)
        self.assertEqual({event["snippet_source"] for event in self.findings()}, {"unavailable"})
        self.assertNotIn("private text", self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn("secret", self.ledger.read_text(encoding="utf-8"))

    def test_dry_run_shows_the_snippet_it_would_record(self) -> None:
        item = review_item(file="src/a.py", line=5, category="p0e-1")
        code, out, _ = self.cli("review", "--dry-run", "--from-file", self.review_file(item), "--run", "F1")
        self.assertEqual(code, 0)
        self.assertIn('"snippet_source":"worktree"', out)
        self.assertIn("work-5", out)
        self.assertFalse(self.ledger.exists())


class OtherImportsKeepNoSnippetTest(SnippetCase):
    """Only `review` reads code: `add` records what it is given and `ingest-code-review` records no snippet."""

    def test_ingest_code_review_records_no_snippet_and_no_source(self) -> None:
        item = review_item(file="src/a.py", line=5, category="p0e-1")
        self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps([item]))
        (finding,) = self.findings()
        self.assertNotIn("snippet", finding)
        self.assertNotIn("snippet_source", finding)

    def test_add_keeps_the_snippet_it_was_given_and_adds_no_source(self) -> None:
        item = {"id": "R1-1", "file": "src/a.py", "line": 5, "summary": "s", "failure_scenario": "f", "verdict": "PLAUSIBLE",
                "rule_id": "P0E.1", "snippet": "typed by hand"}
        self.cli_ok("add", "--source", "l1", "--run", "R1", stdin=json.dumps([item]))
        (finding,) = self.findings()
        self.assertEqual(finding["snippet"], "typed by hand")
        self.assertNotIn("snippet_source", finding)


class CommittedFileSnippetTest(SnippetCase):
    """The evidence holds the files that had changed. One it did not hold had not, so the commit holds what the reviewer read."""

    def at(self, file: str, line: int = 5) -> dict:
        return review_item(file=file, line=line, category="p0e-1")

    def commit_files(self, subject: str, **files: str) -> None:
        """Write and commit files under src/, named by the keyword with `_` for `.`; each is ten lines `<word>-<n>`."""
        for name, word in files.items():
            self.write_numbered("src/" + name.replace("_", "."), word)
        git(self.repo, "add", "src")
        self.commit(subject)

    def test_an_unchanged_committed_file_is_cut_from_the_commit_the_review_started_on(self) -> None:
        def commit_while_claude_is_busy(argv: list[str], cwd: Path) -> str:
            self.commit_files("late edit", unchanged_py="late")
            return review_text(self.at("src/unchanged.py"))

        with mock.patch("house_rules.cli.run_claude", side_effect=commit_while_claude_is_busy):
            code, _, err = self.review(self.fake_claude(), "--run", "R1")
        self.assertEqual(code, 0, err)
        (finding,) = self.findings()
        self.assertEqual((finding["snippet"], finding["snippet_source"]), (self.window("same", 2, 8), "review"))

    def test_an_edit_made_while_the_reviewer_was_busy_does_not_reach_the_snippet(self) -> None:
        rewrite = f"open({str(self.repo / 'src' / 'unchanged.py')!r}, 'w').write('edited while claude was busy\\n')"
        script = self.fake_claude(review_text(self.at("src/unchanged.py")), during_review=rewrite)
        self.assertEqual(self.review(script, "--run", "R1")[0], 0)
        self.assertEqual((self.repo / "src" / "unchanged.py").read_text(encoding="utf-8"), "edited while claude was busy\n")
        (finding,) = self.findings()
        self.assertEqual((finding["snippet"], finding["snippet_source"]), (self.window("same", 2, 8), "review"))

    def test_every_file_the_reviewer_could_read_has_a_snippet_whatever_state_it_was_in(self) -> None:
        self.write_numbered("src/staged.py", "staged")
        git(self.repo, "add", "src/staged.py")
        self.write_numbered("untracked.py", "fresh")
        items = [self.at(name) for name in ("src/a.py", "src/staged.py", "untracked.py", "src/unchanged.py")]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        shown = {event["file"]: (event["snippet"], event["snippet_source"]) for event in self.findings()}
        self.assertEqual(shown, {"src/a.py": (self.window("work", 2, 8), "review"),
                                 "src/staged.py": (self.window("staged", 2, 8), "review"),
                                 "untracked.py": (self.window("fresh", 2, 8), "review"),
                                 "src/unchanged.py": (self.window("same", 2, 8), "review")})

    def test_what_the_working_tree_reader_refuses_is_refused_for_the_commit_too(self) -> None:
        (self.repo / "config").mkdir()
        (self.repo / ".env").write_text("TOKEN=secret\nline two\n", encoding="utf-8")
        (self.repo / "config" / ".env.local").write_text("TOKEN=secret\nline two\n", encoding="utf-8")
        (self.repo / "link.py").symlink_to("src/unchanged.py")
        (self.repo / "src" / "big.py").write_text("x" * (MAX_SOURCE_BYTES + 1) + "\n", encoding="utf-8")
        (self.repo / "src" / "binary.dat").write_bytes(b"line one\n\0\nline three\n")
        (self.repo / "src" / "latin.txt").write_bytes(b"caf\xe9\nline two\n")
        git(self.repo, "add", ".env", "config", "link.py", "src/big.py", "src/binary.dat", "src/latin.txt")
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{git(self.repo, 'rev-parse', 'HEAD')},vendor/sub")
        self.commit("files the readers refuse")
        refused = [".env", "config/.env.local", "link.py", "src/big.py", "src/binary.dat", "src/latin.txt", "vendor/sub", "src",
                   "../outside.txt", str(self.tmp / "outside.txt")]
        items = [self.at("src/unchanged.py", 1), *(self.at(name, 1) for name in refused)]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        by_file = {event["file"]: event for event in self.findings()}
        self.assertEqual(by_file["src/unchanged.py"]["snippet_source"], "review")  # the commit is being read at all
        for name in refused:
            with self.subTest(name=name):
                self.assertEqual(by_file[name]["snippet_source"], "unavailable")
                self.assertNotIn("snippet", by_file[name])
        self.assertNotIn("secret", self.ledger.read_text(encoding="utf-8"))

    def test_a_file_the_evidence_saw_change_but_could_not_keep_never_falls_back_to_the_commit(self) -> None:
        self.commit_files("old files", b_py="old", gone_py="old")  # this commits the edit of src/a.py too, so edit it again
        self.write_numbered("src/a.py", "edit")
        (self.repo / "src" / "b.py").write_bytes(b"binary now\0\n")
        (self.repo / "src" / "gone.py").unlink()
        items = [self.at(name) for name in ("src/a.py", "src/b.py", "src/gone.py", "src/unchanged.py")]
        with mock.patch("house_rules.review_evidence.MAX_TEXT_BYTES", 40):  # src/a.py is 70 bytes
            self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        evidence = json.loads((self.reviews_dir() / "R1.evidence.json").read_text(encoding="utf-8"))
        self.assertEqual({name: entry["status"] for name, entry in evidence["files"].items() if name.startswith("src/")},
                         {"src/a.py": "unavailable", "src/b.py": "unavailable", "src/gone.py": "unavailable"})
        by_file = {event["file"]: event for event in self.findings()}
        self.assertEqual(by_file["src/unchanged.py"]["snippet_source"], "review")
        for name in ("src/a.py", "src/b.py", "src/gone.py"):
            with self.subTest(name=name):
                self.assertEqual(by_file[name]["snippet_source"], "unavailable")
                self.assertNotIn("snippet", by_file[name])

    def test_a_changed_file_beyond_the_evidence_cap_never_falls_back_to_the_commit(self) -> None:
        self.commit_files("old file", b_py="old")
        git(self.repo, "add", "rules.md", ".house-rules.json")  # so that only the two src files differ from HEAD
        self.commit("rules and config")
        self.write_numbered("src/a.py", "edit")  # committed together with b.py above; changed again
        self.write_numbered("src/b.py", "new")
        with mock.patch("house_rules.review_evidence.MAX_FILES", 1):
            self.assertEqual(self.review(self.fake_claude(review_text(self.at("src/a.py"), self.at("src/b.py"))), "--run", "R1")[0], 0)
        evidence = json.loads((self.reviews_dir() / "R1.evidence.json").read_text(encoding="utf-8"))
        self.assertEqual((list(evidence["files"]), evidence["omitted_files"]), (["src/a.py"], ["src/b.py"]))
        by_file = {event["file"]: event for event in self.findings()}
        self.assertEqual(by_file["src/a.py"]["snippet_source"], "review")
        self.assertEqual(by_file["src/b.py"]["snippet_source"], "unavailable")
        self.assertNotIn("snippet", by_file["src/b.py"])

    def test_the_files_house_rules_writes_itself_never_fall_back_to_the_commit(self) -> None:
        (self.repo / ".house-rules").mkdir()
        (self.repo / ".house-rules" / "notes.txt").write_text("".join(f"note {number}\n" for number in range(1, 11)), encoding="utf-8")
        self.append_raw(review_finding(display_id="X-1", run="X"))
        git(self.repo, "add", ".house-rules/notes.txt", ".style-findings.jsonl")
        self.commit("the tool's own files")
        (self.repo / ".house-rules" / "notes.txt").write_text("changed\n", encoding="utf-8")
        self.append_raw(review_finding(display_id="X-2", run="X", line=11))
        items = [self.at(".house-rules/notes.txt", 1), self.at(".style-findings.jsonl", 1)]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        by_file = {event["file"]: event for event in self.findings() if event["run_id"] == "R1"}
        self.assertEqual({name: event["snippet_source"] for name, event in by_file.items()},
                         {".house-rules/notes.txt": "unavailable", ".style-findings.jsonl": "unavailable"})

    def test_an_untracked_file_git_ignores_has_no_snippet(self) -> None:
        (self.repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        self.write_numbered("ignored.log", "log")
        self.assertEqual(self.review(self.fake_claude(review_text(self.at("ignored.log"))), "--run", "R1")[0], 0)
        (finding,) = self.findings()
        self.assertEqual(finding["snippet_source"], "unavailable")
        self.assertNotIn("snippet", finding)

    def test_a_target_review_has_no_snippet_even_for_an_unchanged_committed_file(self) -> None:
        item = self.at("src/unchanged.py")
        self.assertEqual(self.review(self.fake_claude(review_text(item)), "--run", "R1", "--target", "feature/x")[0], 0)
        (finding,) = self.findings()
        self.assertEqual(finding["snippet_source"], "unavailable")
        self.assertNotIn("snippet", finding)

    def test_a_replay_keeps_the_snippet_cut_from_the_commit(self) -> None:
        self.review(self.fake_claude(review_text(self.at("src/unchanged.py"))), "--run", "R1")
        before = self.ledger_bytes()
        self.commit_files("moved on", unchanged_py="later")
        code, out, _ = self.cli("review", "--from-file", str(self.reviews_dir() / "R1.txt"), "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn("no new findings", out)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.findings()[0]["snippet"], self.window("same", 2, 8))


class UnbornSnippetTest(ReviewCase):
    def test_nothing_is_read_from_a_commit_when_the_review_began_before_the_first_commit(self) -> None:
        def commit_while_claude_is_busy(argv: list[str], cwd: Path) -> str:
            (self.repo / "late.py").write_text("late\n", encoding="utf-8")  # created and committed after the evidence was taken
            git(self.repo, "add", "late.py")
            self.commit("late")
            return review_text(review_item(file="late.py", line=1, category="p0e-1"))

        with mock.patch("house_rules.cli.run_claude", side_effect=commit_while_claude_is_busy):
            code, _, err = self.review(self.fake_claude(), "--run", "R1")
        self.assertEqual(code, 0, err)
        (finding,) = self.findings()
        self.assertEqual(finding["snippet_source"], "unavailable")
        self.assertNotIn("snippet", finding)
        self.assertEqual(self.review_runs()[0]["head"], "none")


class LinkedWorktreeTest(SnippetCase):
    """`review` is about the worktree it is run in. The ledger, the saved reviews and the config stay in the main one.

    The two worktrees stand on different commits and hold different edits of src/a.py, so a review of the wrong one shows.
    The rules are committed, so each worktree has its own copy of them.
    """

    def setUp(self) -> None:
        super().setUp()  # main: HEAD is the baseline, and src/a.py is edited to work-N, not committed
        git(self.repo, "add", "rules.md")
        self.commit("rules")  # the rules are tracked, so a linked worktree has its own copy from the day it is added
        self.baseline = git(self.repo, "rev-parse", "HEAD")
        self.tree = self.tmp / "feature-tree"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(self.tree))
        self.write_in_tree("src/unchanged.py", "feature")
        git(self.tree, "add", "src")
        git(self.tree, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "feature work")
        self.tree_head = git(self.tree, "rev-parse", "HEAD")
        self.write_in_tree("src/a.py", "feat")  # edited, not committed
        self.main_head = self.commit("main moves on")
        self.feature_key = f"feature@{self.baseline[:12]}"
        self.main_key = f"main@{self.main_head[:12]}"

    def write_in_tree(self, name: str, word: str) -> None:
        (self.tree / name).write_text("".join(f"{word}-{number}\n" for number in range(1, 11)), encoding="utf-8")

    def at(self, file: str) -> dict:
        return review_item(file=file, line=5, category="p0e-1")

    def both_files(self) -> str:
        return review_text(self.at("src/a.py"), self.at("src/unchanged.py"))

    def review_in(self, tree: Path, claude: Path, *argv: str) -> tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(claude)}):
            return self.cli("review", *argv, "--repo", str(tree), with_repo=False)

    def snippets(self) -> dict[str, tuple[str, str]]:
        return {event["file"]: (event["snippet"], event["snippet_source"]) for event in self.findings()}

    def test_claude_runs_in_the_worktree_it_was_started_in_whichever_way_that_is_named(self) -> None:
        script = self.fake_claude(review_text())
        self.assertEqual(self.review_in(self.tree, script, "--run", "R1")[0], 0)
        self.assertEqual(Path(self.call()["cwd"]).resolve(), self.tree.resolve())
        (self.tree / "sub").mkdir()
        cwd = os.getcwd()
        os.chdir(self.tree / "sub")
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(script)}):
            self.assertEqual(self.cli("review", "--run", "R2", with_repo=False)[0], 0)  # no --repo: the current directory, one level down
        self.assertEqual(Path(self.call()["cwd"]).resolve(), self.tree.resolve())

    def test_the_evidence_the_round_key_and_the_findings_are_those_of_the_worktree_that_was_reviewed(self) -> None:
        code, _, err = self.review_in(self.tree, self.fake_claude(self.both_files()), "--run", "R1")
        self.assertEqual((code, err), (0, ""))
        evidence = json.loads((self.reviews_dir() / "R1.evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["head"], self.tree_head)
        self.assertEqual({name: entry["text"] for name, entry in evidence["files"].items()}, {"src/a.py": self.window("feat", 1, 10)})
        self.assertEqual(list(evidence["source_stable_during_review"].items()), [("src/a.py", True)])
        (run,) = self.review_runs()
        self.assertEqual((run["head"], run["round_key"]), (self.tree_head, self.feature_key))
        self.assertEqual(self.snippets(), {"src/a.py": (self.window("feat", 2, 8), "review"),
                                           "src/unchanged.py": (self.window("feature", 2, 8), "review")})
        self.assertEqual({event["sha"] for event in self.findings()}, {self.tree_head[:8]})

    def test_the_ledger_the_saved_review_and_its_evidence_stay_in_the_main_worktree(self) -> None:
        self.assertEqual(self.review_in(self.tree, self.fake_claude(self.both_files()), "--run", "R1")[0], 0)
        self.assertEqual(self.saved(), ["R1.txt"])
        self.assertTrue((self.reviews_dir() / "R1.evidence.json").exists())
        self.assertEqual(len(self.findings()), 2)
        self.assertFalse((self.tree / ".house-rules").exists())
        self.assertFalse((self.tree / ".style-findings.jsonl").exists())

    def test_the_main_worktree_is_reviewed_as_itself_while_a_linked_one_exists(self) -> None:
        self.assertEqual(self.review(self.fake_claude(self.both_files()), "--run", "R1")[0], 0)
        self.assertEqual(Path(self.call()["cwd"]).resolve(), self.repo.resolve())
        (run,) = self.review_runs()
        self.assertEqual((run["head"], run["round_key"]), (self.main_head, self.main_key))
        self.assertEqual(self.snippets(), {"src/a.py": (self.window("work", 2, 8), "review"),
                                           "src/unchanged.py": (self.window("same", 2, 8), "review")})
        self.assertEqual({event["sha"] for event in self.findings()}, {self.main_head[:8]})

    def test_a_from_file_review_takes_its_head_key_and_snippets_from_the_worktree_it_is_run_in(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(self.both_files(), encoding="utf-8")
        code, _, err = self.cli("review", "--from-file", str(saved), "--run", "F1", "--repo", str(self.tree), with_repo=False)
        self.assertEqual((code, err), (0, ""))
        (run,) = self.review_runs()
        self.assertEqual((run["head"], run["round_key"]), (self.tree_head, self.feature_key))
        self.assertEqual(self.snippets(), {"src/a.py": (self.window("feat", 2, 8), "worktree"),
                                           "src/unchanged.py": (self.window("feature", 2, 8), "worktree")})
        self.assertEqual({event["sha"] for event in self.findings()}, {self.tree_head[:8]})

    def test_a_file_named_by_its_absolute_path_is_looked_up_in_the_worktree_that_was_reviewed(self) -> None:
        in_tree = [str(self.tree / "src" / "a.py"), str(self.tree / "src" / "unchanged.py")]
        in_main = str(self.repo / "src" / "a.py")  # the other worktree is outside the one the reviewer was started in
        code, _, err = self.review_in(self.tree, self.fake_claude(review_text(*(self.at(name) for name in (*in_tree, in_main)))), "--run", "R1")
        self.assertEqual((code, err), (0, ""))
        shown = {event["file"]: (event.get("snippet"), event["snippet_source"]) for event in self.findings()}
        self.assertEqual(shown, {in_tree[0]: (self.window("feat", 2, 8), "review"),
                                 in_tree[1]: (self.window("feature", 2, 8), "review"),
                                 in_main: (None, "unavailable")})

    def test_a_dry_run_names_the_worktree_it_would_review(self) -> None:
        code, out, _ = self.review_in(self.tree, self.fake_claude(review_text()), "--dry-run", "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn(f"review round 1/3 for {self.feature_key}", out)
        self.assertIn(f" in {self.tree} (run R1)", out)

    def test_count_ends_with_the_rounds_of_the_worktree_it_is_run_in(self) -> None:
        self.assertEqual(self.review_in(self.tree, self.fake_claude(review_text()), "--run", "R1")[0], 0)
        in_tree = self.cli("count", "--repo", str(self.tree), with_repo=False)[1]
        self.assertEqual(in_tree.splitlines()[-1], f"review rounds on {self.feature_key}: 1/3")
        self.assertEqual(self.cli_ok("count").splitlines()[-1], f"review rounds on {self.main_key}: 0/3")

    def test_each_worktree_counts_its_own_rounds_in_the_one_ledger(self) -> None:
        self.write_config(rules_files=["rules.md"], max_review_rounds=1)
        script = self.fake_claude(review_text())
        self.assertEqual(self.review_in(self.tree, script, "--run", "R1")[0], 0)
        self.assertEqual(self.review_in(self.tree, script, "--run", "R2")[0], 3)  # the feature change has used its round
        self.assertEqual(self.review(script, "--run", "R3")[0], 0)  # the main worktree's has not
        self.assertEqual([run["round_key"] for run in self.review_runs()], [self.feature_key, self.main_key])

    def test_the_real_executable_run_from_inside_a_linked_worktree(self) -> None:
        script = self.fake_claude(self.both_files())
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "house-rules"), "review", "--run", "R1"], cwd=self.tree,
                              capture_output=True, text=True, env={**os.environ, "HOUSE_RULES_CLAUDE_CMD": str(script)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Path(self.call()["cwd"]).resolve(), self.tree.resolve())
        self.assertEqual(self.review_runs()[0]["head"], self.tree_head)
        self.assertEqual(self.snippets()["src/unchanged.py"], (self.window("feature", 2, 8), "review"))


class ReviewTreeTest(SnippetCase):
    """The `tree` of a review_run is the worktree as the review started: what a later `house-rules wrapup` measures new work from."""

    def object_files(self) -> set[str]:
        root = self.repo / ".git" / "objects"
        return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}

    def saved_review(self) -> Path:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(review_item(file="src/a.py", line=5, category="p0e-1")), encoding="utf-8")
        return saved

    def test_the_tree_is_the_worktree_when_claude_started_not_when_it_finished(self) -> None:
        late = self.repo / "late.txt"
        busy = (f"open({str(self.repo / 'src' / 'a.py')!r}, 'w').write('edited while claude was busy\\n')\n"
                f"open({str(late)!r}, 'w').write('created while claude was busy\\n')")
        script = self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1")), during_review=busy)
        code, _, err = self.review(script, "--run", "R1")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(late.exists())
        (run,) = self.review_runs()
        self.assertEqual(git(self.repo, "show", f"{run['tree']}:src/a.py"), self.window("work", 1, 10))  # the edit during the run is not in it
        names = git(self.repo, "ls-tree", "-r", "--name-only", run["tree"]).split("\n")
        self.assertNotIn("late.txt", names)
        self.assertIn("rules.md", names)  # an untracked file is in it

    def test_a_review_of_a_target_takes_no_tree_since_no_wrapup_measures_from_it(self) -> None:
        before = self.object_files()
        script = self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1")))
        code, _, err = self.review(script, "--run", "R1", "--target", "feature/x")
        self.assertEqual((code, err), (0, ""))
        (run,) = self.review_runs()
        self.assertEqual((run["round_key"], "tree" in run), ("target:feature/x", False))  # a key that names no branch
        self.assertEqual(self.object_files(), before)  # and no objects were written for it
        self.assertEqual(self.review(script, "--run", "R2")[0], 0)
        self.assertEqual(["tree" in run for run in self.review_runs()], [False, True])  # a review of the branch still takes one

    def test_a_saved_review_records_the_tree_of_the_worktree_when_the_file_was_read(self) -> None:
        expected = state_tree(self.repo)
        self.assertEqual(self.cli("review", "--from-file", str(self.saved_review()), "--run", "F1")[0], 0)
        (run,) = self.review_runs()
        self.assertEqual((run["source"], run["tree"]), ("file", expected))

    def test_a_dry_run_of_a_saved_review_and_a_replay_of_a_recorded_one_take_no_tree(self) -> None:
        saved = self.saved_review()
        before = self.object_files()
        code, out, _ = self.cli("review", "--dry-run", "--from-file", str(saved), "--run", "F1")
        self.assertEqual(code, 0)
        self.assertNotIn('"tree"', out)  # the event it shows would be written without one
        self.assertEqual((self.object_files(), self.review_runs()), (before, []))
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F1")[0], 0)
        recorded = self.review_runs()
        self.assertEqual(len(recorded), 1)
        (self.repo / "src" / "a.py").write_text("changed after the review\n", encoding="utf-8")  # a tree taken now would be new objects
        before = self.object_files()
        code, out, _ = self.cli("review", "--from-file", str(saved), "--run", "F1")
        self.assertIn("replay of run F1", out)
        self.assertEqual((self.object_files(), self.review_runs()), (before, recorded))

    def test_a_dry_run_that_would_run_claude_takes_no_tree_either(self) -> None:
        before = self.object_files()
        code, out, _ = self.review(self.fake_claude(review_text()), "--dry-run", "--run", "R1")
        self.assertEqual(code, 0)
        self.assertIn("nothing run or written", out)
        self.assertEqual((self.object_files(), self.review_runs()), (before, []))

    def test_a_tree_that_cannot_be_taken_is_said_and_the_review_goes_on_without_it(self) -> None:
        refusal = HouseRulesError("`git add -A` failed in /x (rc=128): no space left\nthe second line is not shown")
        with mock.patch("house_rules.cli.state_tree", side_effect=refusal):
            code, _, err = self.review(self.fake_claude(review_text(review_item(file="src/a.py", line=5, category="p0e-1"))), "--run", "R1")
            saved_code, _, saved_err = self.cli("review", "--from-file", str(self.saved_review()), "--run", "F1")
        warning = ("warning: working tree not recorded (`git add -A` failed in /x (rc=128): no space left); "
                   "this review_run has no tree, so a wrap-up measures from its HEAD")
        self.assertEqual((code, saved_code), (0, 0), (err, saved_err))
        self.assertIn(warning, err)
        self.assertIn(warning, saved_err)
        self.assertNotIn("the second line", err)
        self.assertEqual([("tree" in run, run["source"]) for run in self.review_runs()], [(False, "claude"), (False, "file")])
        self.assertEqual(self.saved(), ["F1.txt", "R1.txt"])  # both reviews were kept


class CaseSpelledSecretSnippetTest(SnippetCase):
    """macOS and Windows open `.ENV` as the file `.env`: no snippet is cut from a secret under any spelling of its name."""

    NAMES = (".ENV", ".Env", ".ENV.local", "sub/.ENV", ".GIT/config", ".Git/config", ".git/CONFIG")

    def assert_no_snippet(self, count: int) -> None:
        self.assertEqual(len(self.findings()), count)
        for event in self.findings():
            with self.subTest(file=event["file"]):
                self.assertEqual(event["snippet_source"], "unavailable")
                self.assertNotIn("snippet", event)
        self.assertNotIn("secret", self.ledger.read_text(encoding="utf-8"))

    def test_a_review_read_from_a_file_cuts_no_snippet_from_the_working_tree(self) -> None:
        (self.repo / "sub").mkdir()
        for name in (".env", ".env.local", "sub/.env"):
            (self.repo / name).write_text("TOKEN=secret\n", encoding="utf-8")
        git(self.repo, "remote", "add", "origin", "https://user:secret@example.invalid/org/repo.git")  # `.git/config` holds a token now
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(*[review_item(file=name, line=1, category="p0e-1") for name in self.NAMES]), encoding="utf-8")
        self.assertEqual(self.cli("review", "--from-file", str(saved), "--run", "F1")[0], 0)
        self.assert_no_snippet(len(self.NAMES))

    def test_a_review_cuts_no_snippet_from_a_committed_secret(self) -> None:
        (self.repo / ".ENV").write_text("TOKEN=secret\n", encoding="utf-8")
        git(self.repo, "add", "-f", ".ENV")
        self.commit("a committed secret")  # unchanged since, so a snippet would come from the commit
        names = (".ENV", ".Env", str(self.repo / ".ENV"))
        items = [review_item(file=name, line=1, category="p0e-1") for name in names]
        self.assertEqual(self.review(self.fake_claude(review_text(*items)), "--run", "R1")[0], 0)
        self.assert_no_snippet(len(names))


class WholeValueRunIdTest(ReviewCase):
    """`$` also matches before a final newline, so `F1\\n` passed the check and became the file name `F1\\n.txt`."""

    def test_a_run_id_with_a_final_newline_is_refused(self) -> None:
        for bad in ("F1\n", "r1\n", "a.b-c_d\n"):
            with self.subTest(run=bad), self.assertRaises(HouseRulesError):
                check_run_id(bad)
        self.assertEqual(check_run_id("a.b-c_d"), "a.b-c_d")

    def test_such_a_run_id_stops_a_review_before_claude_runs_or_anything_is_saved(self) -> None:
        code, _, err = self.review(self.fake_claude(review_text(FINDING_A)), "--run", "F1\n")
        self.assertEqual(code, 1)
        self.assertIn("must match", err)
        self.assertFalse(self.record.exists())  # claude was never called
        self.assertEqual((self.saved(), self.ledger.exists()), ([], False))

    def test_a_saved_review_read_back_with_such_a_run_id_leaves_nothing_behind(self) -> None:
        saved = self.tmp / "earlier.txt"
        saved.write_text(review_text(FINDING_A), encoding="utf-8")
        code, _, err = self.cli("review", "--from-file", str(saved), "--run", "F1\n")
        self.assertEqual(code, 1)
        self.assertIn("must match", err)
        self.assertEqual((self.saved(), self.ledger.exists()), ([], False))


if __name__ == "__main__":
    unittest.main()
