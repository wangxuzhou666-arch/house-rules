import ast
import json
import shutil
import subprocess
import sys
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import (
    FIXTURES, REPO_ROOT, RepoCase, gate_finding, gate_pass, git, report_item, review_finding, review_item, review_round,
)

from house_rules.config import load_config


class DryRunTest(RepoCase):
    """Every writing command must leave the disk exactly as it found it and say what it would have written."""

    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()

    def repo_files(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.repo)): p.read_bytes()
                for p in sorted(self.repo.rglob("*")) if p.is_file() and ".git" not in p.relative_to(self.repo).parts}

    def test_add_dry_run(self) -> None:
        before = self.repo_files()
        out = self.cli_ok("add", "--source", "l1", "--run", "R1", "--dry-run", stdin=json.dumps([report_item()]))
        self.assertIn("[dry-run] add: +1 finding(s)", out)
        self.assertIn('"id":"demo-repo:l1:R1:R1-001"', out)
        self.assertEqual(self.repo_files(), before)
        self.assertFalse(self.ledger.exists())

    def test_ingest_dry_run(self) -> None:
        before = self.repo_files()
        out = self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", "--dry-run", stdin=json.dumps([review_item()]))
        self.assertIn("[dry-run] ingest-code-review: +1 finding(s)", out)
        self.assertEqual(self.repo_files(), before)

    def test_infer_dry_run(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py"]), review_finding("R1-001"))
        self.commit("fix: x", "Fixes-Finding: R1-001")
        before = self.repo_files()
        out = self.cli_ok("infer", "--dry-run")
        self.assertIn("[dry-run] infer: +1 fixed (auto:commit), +1 fixed (auto:gate_pass)", out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("{")]), 2)
        self.assertEqual(self.repo_files(), before)

    def test_label_page_dry_run(self) -> None:
        self.append_raw(review_finding("R1-001"))
        before = self.repo_files()
        out = self.cli_ok("label-page", "--dry-run")
        self.assertIn("[dry-run] label-page: would write", out)
        self.assertEqual(self.repo_files(), before)

    def test_apply_labels_dry_run(self) -> None:
        finding = review_finding("R1-001")
        self.append_raw(finding)
        self.cli_ok("label-page")
        batch = json.loads((self.repo / ".house-rules-label-batch.json").read_text(encoding="utf-8"))
        before = self.repo_files()
        payload = json.dumps({"batch_id": batch["batch_id"], "labels": [{"id": finding["id"], "outcome": "fixed"}]})
        out = self.cli_ok("apply-labels", "--dry-run", stdin=payload)
        self.assertIn("[dry-run] apply-labels: 1 human outcome(s)", out)
        self.assertEqual(self.repo_files(), before)

    def test_dry_run_still_runs_every_check(self) -> None:
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", "--dry-run", stdin=json.dumps([report_item(rule_id="P9.9")]))
        self.assertEqual(code, 1)
        self.assertIn("unknown rule_id", err)

    def test_the_non_dry_run_would_have_written_what_dry_run_printed(self) -> None:
        items = json.dumps([report_item()])
        dry = self.cli_ok("add", "--source", "l1", "--run", "R1", "--dry-run", stdin=items)
        printed = json.loads(dry.splitlines()[1])
        self.cli_ok("add", "--source", "l1", "--run", "R1", stdin=items)
        (written,) = self.events()
        printed.pop("ts")
        written.pop("ts")
        self.assertEqual(printed, written)


class CountByRuleTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()

    def ingest(self, *items: dict) -> None:
        self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps(list(items)))

    def test_by_rule_alone_prints_plain_counts(self) -> None:
        self.ingest(review_item(category="p0e-1"), review_item(line=2, summary="Breaks P0E.1"))
        self.assertIn("  P0E.1: 2\n", self.cli_ok("count", "--by-rule"))

    def test_label_source_splits_each_rule_by_how_it_was_labeled(self) -> None:
        self.ingest(review_item(category="p0e-1"), review_item(line=2, category="p0e-1"), review_item(line=3, summary="Breaks P0E.1"),
                    review_item(line=4, summary="No clause named"))
        out = self.cli_ok("count", "--by-rule", "--label-source")
        self.assertIn("  P0E.1: 3 (model 2, attributed 1)\n", out)
        self.assertIn("  unmapped: 1 (model 1)\n", out)

    def test_events_from_before_the_field_existed_are_counted_as_none(self) -> None:
        self.append_raw(review_finding("R1-001", rule_id="P1.1"), review_finding("R1-002", rule_id="P1.1", line=11, rule_id_source="human"))
        self.assertIn("  P1.1: 2 (human 1, none 1)\n", self.cli_ok("count", "--by-rule", "--label-source"))

    def test_label_source_without_by_rule_is_refused_before_anything_is_printed(self) -> None:
        self.ingest(review_item(category="p0e-1"))
        code, out, err = self.cli("count", "--label-source")
        self.assertEqual(code, 1)
        self.assertIn("add --by-rule", err)
        self.assertEqual(out, "")

    def test_only_open_findings_are_counted(self) -> None:
        self.ingest(review_item(category="p0e-1"))
        self.commit("fix: x", "Fixes-Finding: R1-1")
        self.cli_ok("infer")
        self.assertNotIn("P0E.1", self.cli_ok("count", "--by-rule", "--label-source"))


class CountByReviewerVersionTest(RepoCase):
    """With findings from more than one reviewer version `count` lists them per version; with one it prints what it always did."""

    A, B = "2.1.0 (Claude Code)", "2.2.0 (Claude Code)"
    HEADING = "by reviewer version:"

    def fixed(self, finding: dict) -> dict:
        return {"v": 1, "kind": "outcome", "id": finding["id"], "outcome": "fixed", "outcome_by": "human", "evidence": "e",
                "ts": "2026-01-08T00:00:00"}

    def lines_after_heading(self, out: str) -> list[str]:
        """The rows between the heading and the rounds line, which stays last."""
        lines = out.splitlines()
        return lines[lines.index(self.HEADING) + 1:-1]

    def test_findings_are_listed_per_version_through_their_runs(self) -> None:
        closed = review_finding("r1-2", run="r1", line=11)
        self.append_raw(
            review_round(run="r1", reviewer_version=self.A), review_finding("r1-1", run="r1"), closed, self.fixed(closed),
            review_round(run="r2", round=2, reviewer_version=self.B), review_finding("r2-1", run="r2", source="l2"),
            review_round(run="r3", round=3, source="file", reviewer=None, reviewer_version=None), review_finding("r3-1", run="r3"),
            review_finding("r4-1", run="r4"),  # from before review_run events: no run to say which reviewer
            gate_finding("a.py"))
        out = self.cli_ok("count")
        self.assertEqual(self.lines_after_heading(out), [f"  {self.A}: 2 total, 1 open", f"  {self.B}: 1 total, 1 open",
                                                         "  no version recorded: 2 total, 2 open"])
        self.assertEqual(out.splitlines()[-1], "review rounds on main@unborn: 0/3")
        self.assertTrue(out.startswith("findings: 6 total, 5 open (gate 1 / l1 3 / l2 1)"), out)

    def test_versions_are_in_the_order_they_first_reviewed_and_a_version_that_returns_keeps_its_place(self) -> None:
        self.append_raw(
            review_round(run="r1", reviewer_version="2.10.0"), review_finding("r1-1", run="r1"),
            review_round(run="r2", round=2, reviewer_version="2.9.0"), review_finding("r2-1", run="r2"),
            review_round(run="r3", round=3, reviewer_version="2.10.0"), review_finding("r3-1", run="r3"))
        self.assertEqual(self.lines_after_heading(self.cli_ok("count")), ["  2.10.0: 2 total, 2 open", "  2.9.0: 1 total, 1 open"])

    def test_a_single_version_changes_nothing(self) -> None:
        self.append_raw(review_round(run="r1", reviewer_version=self.A), review_finding("r1-1", run="r1"),
                        review_round(run="r2", round=2, reviewer_version=self.A), review_finding("r2-1", run="r2"),
                        review_finding("r3-1", run="r3"))  # an unversioned review next to one version is still one version
        out = self.cli_ok("count")
        self.assertNotIn(self.HEADING, out)
        self.assertEqual(len(out.splitlines()), 2)  # the totals line and the rounds line

    def test_a_version_whose_reviews_found_nothing_is_not_a_second_version(self) -> None:
        self.append_raw(review_round(run="r1", reviewer_version=self.A), review_finding("r1-1", run="r1"),
                        review_round(run="r2", round=2, reviewer_version=self.B))  # a clean review
        self.assertNotIn(self.HEADING, self.cli_ok("count"))

    def test_a_ledger_from_before_the_reviewer_fields_prints_what_it_always_did(self) -> None:
        self.ledger.write_text((FIXTURES / "legacy_ledger.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        self.append_raw(review_round(run="r1"), review_round(run="r2", round=2))
        self.assertNotIn(self.HEADING, self.cli_ok("count"))

    def test_the_listing_follows_by_rule_and_precedes_the_rounds_line(self) -> None:
        self.append_raw(review_round(run="r1", reviewer_version=self.A), review_finding("r1-1", run="r1", rule_id="P1.1"),
                        review_round(run="r2", round=2, reviewer_version=self.B), review_finding("r2-1", run="r2", rule_id="P1.1"))
        lines = self.cli_ok("count", "--by-rule").splitlines()
        self.assertEqual(lines[1:], ["  P1.1: 2", self.HEADING, f"  {self.A}: 1 total, 1 open", f"  {self.B}: 1 total, 1 open",
                                     "review rounds on main@unborn: 0/3"])


class ConfigTest(RepoCase):
    def slug_of_new_finding(self) -> str:
        self.add_findings([report_item(rule_id="unmapped")])
        return self.events()[-1]["repo"]

    def test_slug_defaults_to_the_directory_name(self) -> None:
        self.assertEqual(self.slug_of_new_finding(), "demo-repo")

    def test_slug_comes_from_the_origin_remote_basename(self) -> None:
        git(self.repo, "remote", "add", "origin", "git@example.invalid:some-org/My_Project.git")
        self.assertEqual(self.slug_of_new_finding(), "my-project")

    def test_explicit_slug_wins(self) -> None:
        git(self.repo, "remote", "add", "origin", "https://example.invalid/org/other.git")
        self.write_config(repo_slug="custom-slug")
        self.assertEqual(self.slug_of_new_finding(), "custom-slug")

    def test_invalid_slug_is_rejected(self) -> None:
        self.write_config(repo_slug="Not A Slug")
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("repo_slug", err)

    def test_custom_ledger_name_from_config(self) -> None:
        self.write_config(ledger="findings.jsonl")
        self.add_findings([report_item(rule_id="unmapped")])
        self.assertTrue((self.repo / "findings.jsonl").exists())
        self.assertFalse(self.ledger.exists())

    def test_ledger_flag_overrides_the_config(self) -> None:
        self.write_config(ledger="findings.jsonl")
        elsewhere = self.tmp / "elsewhere.jsonl"
        self.cli_ok("add", "--source", "l1", "--run", "R1", "--ledger", str(elsewhere),
                    stdin=json.dumps([report_item(rule_id="unmapped")]))
        self.assertTrue(elsewhere.exists())
        self.assertFalse((self.repo / "findings.jsonl").exists())

    def test_unknown_config_key_is_rejected(self) -> None:
        self.write_config(rule_files=["typo.md"])
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("unknown config key", err)

    def test_malformed_config_is_rejected(self) -> None:
        (self.repo / ".house-rules.json").write_text("{not json", encoding="utf-8")
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)

    def test_config_flag_reads_another_file_and_resolves_paths_against_the_repo(self) -> None:
        self.write_rules()
        other = self.tmp / "alt-config.json"
        other.write_text(json.dumps({"rules_files": ["rules.md"]}), encoding="utf-8")
        self.cli_ok("add", "--source", "l1", "--run", "R1", "--config", str(other), stdin=json.dumps([report_item()]))
        self.assertEqual(self.events()[0]["rule_id"], "P0E.1")

    def test_missing_explicit_config_is_an_error(self) -> None:
        code, _, err = self.cli("count", "--config", str(self.tmp / "absent.json"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read config", err)

    def test_legacy_alias_defaults_to_empty(self) -> None:
        self.append_raw(review_finding("R1-001", rule_id="R.1"))
        self.assertIn("R.1: 1", self.cli_ok("count", "--by-rule"))

    def test_linked_worktree_shares_the_main_ledger(self) -> None:
        self.commit("base")
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        code, out, err = self.cli("add", "--source", "l1", "--run", "R1", "--repo", str(linked),
                                  stdin=json.dumps([report_item(rule_id="unmapped")]), with_repo=False)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.ledger.exists())
        self.assertFalse((linked / ".style-findings.jsonl").exists())

    def test_not_a_git_repo_is_a_clear_error(self) -> None:
        code, _, err = self.cli("count", "--repo", str(self.tmp), with_repo=False)
        self.assertEqual(code, 1)
        self.assertIn("not a git repository", err)

    def test_missing_input_file_is_an_error(self) -> None:
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", "--input", str(self.tmp / "absent.json"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read input", err)

    def test_wrapup_thresholds_default_to_200_lines_and_5_files(self) -> None:
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.wrapup_min_changed_lines, config.wrapup_min_files), (200, 5))

    def test_wrapup_thresholds_come_from_the_config_each_on_its_own(self) -> None:
        self.write_config(wrapup_min_changed_lines=50, wrapup_min_files=2)
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.wrapup_min_changed_lines, config.wrapup_min_files), (50, 2))
        self.write_config(wrapup_min_files=9)
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.wrapup_min_changed_lines, config.wrapup_min_files), (200, 9))
        self.write_config(wrapup_min_changed_lines=1000)
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.wrapup_min_changed_lines, config.wrapup_min_files), (1000, 5))

    def test_wrapup_thresholds_must_be_positive_integers(self) -> None:
        for key in ("wrapup_min_changed_lines", "wrapup_min_files"):
            for bad in (0, -3, "200", 2.5, True, None):
                with self.subTest(key=key, bad=bad):
                    self.write_config(**{key: bad})
                    code, _, err = self.cli("count")
                    self.assertEqual(code, 1)
                    self.assertIn(f"config {key!r} must be a positive integer", err)


class ExecutableTest(RepoCase):
    def test_bin_house_rules_runs_end_to_end(self) -> None:
        self.with_sample_rules()
        base = [sys.executable, str(REPO_ROOT / "bin" / "house-rules")]
        add = subprocess.run([*base, "add", "--source", "l1", "--run", "R1", "--repo", str(self.repo)],
                             input=json.dumps([report_item()]), capture_output=True, text=True)
        self.assertEqual(add.returncode, 0, add.stderr)
        count = subprocess.run([*base, "count", "--repo", str(self.repo)], capture_output=True, text=True)
        self.assertIn("1 total, 1 open", count.stdout)

    def test_cwd_is_used_when_repo_is_not_given(self) -> None:
        self.with_sample_rules()
        base = [sys.executable, str(REPO_ROOT / "bin" / "house-rules")]
        subprocess.run([*base, "add", "--source", "l1", "--run", "R1"], input=json.dumps([report_item()]),
                       capture_output=True, text=True, cwd=self.repo, check=True)
        self.assertTrue(self.ledger.exists())

    def test_error_exit_code_and_no_traceback(self) -> None:
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "house-rules"), "count", "--repo", str(self.tmp)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("house-rules: error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class LauncherPythonGuardTest(unittest.TestCase):
    """Under Python 3.9 (the python3 macOS ships) the package died with a TypeError traceback from ingest.py; the launcher says why."""

    LAUNCHER = REPO_ROOT / "bin" / "house-rules"
    # Runs the launcher as __main__ in a child that believes it is the Python given as the first argument.
    AS_PYTHON = (
        "import collections, runpy, sys\n"
        "version, launcher, *args = sys.argv[1:]\n"
        "fake = collections.namedtuple('version_info', 'major minor micro releaselevel serial')\n"
        "sys.version_info = fake(*map(int, version.split('.')), 'final', 0)\n"
        "sys.argv = [launcher, *args]\n"
        "runpy.run_path(launcher, run_name='__main__')\n"
    )

    def run_as(self, version: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", self.AS_PYTHON, version, str(self.LAUNCHER), *args],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_python_3_9_gets_one_line_naming_the_requirement_the_version_and_the_interpreter(self) -> None:
        proc = self.run_as("3.9.6", "--help")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, f"house-rules: error: Python 3.10 or newer is required, but this is Python 3.9.6 ({sys.executable})\n")

    def test_python_3_10_gets_past_the_guard(self) -> None:
        proc = self.run_as("3.10.0", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)
        self.assertNotIn("Python 3.10 or newer", proc.stderr)

    def test_the_launcher_uses_no_syntax_newer_than_python_3_9(self) -> None:
        """The guard can only speak if an old Python gets as far as running the launcher, so it has to parse there."""
        ast.parse(self.LAUNCHER.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_a_real_older_python_prints_the_line_instead_of_a_traceback(self) -> None:
        for name in ("python3.9", "python3.8", "/usr/bin/python3"):
            older = shutil.which(name)
            if older and subprocess.run([older, "-c", "import sys; sys.exit(sys.version_info >= (3, 10))"]).returncode == 0:
                break
        else:
            self.skipTest("no Python older than 3.10 on this machine")
        proc = subprocess.run([older, str(self.LAUNCHER), "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stderr, r"^house-rules: error: Python 3\.10 or newer is required, but this is Python 3\.\d+\.\d+ \(.+\)\n$")
        self.assertNotIn("Traceback", proc.stderr)


class WholeValueSlugTest(RepoCase):
    """`$` also matches before a final newline, so `demo\\n` passed as a slug and failed later, in the ledger."""

    def test_a_repo_slug_with_a_final_newline_is_rejected_when_the_config_is_read(self) -> None:
        self.write_config(repo_slug="demo\n")
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("repo_slug", err)

    def test_a_plain_slug_is_still_accepted(self) -> None:
        self.write_config(repo_slug="demo-2")
        self.assertEqual(self.cli("count")[0], 0)


if __name__ == "__main__":
    unittest.main()
