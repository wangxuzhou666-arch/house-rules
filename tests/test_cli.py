import json
import subprocess
import sys
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import REPO_ROOT, RepoCase, gate_finding, gate_pass, git, report_item, review_finding, review_item


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
        batch = json.loads((self.repo / ".ratchet-label-batch.json").read_text(encoding="utf-8"))
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
        (self.repo / ".ratchet.json").write_text("{not json", encoding="utf-8")
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


class ExecutableTest(RepoCase):
    def test_bin_ratchet_runs_end_to_end(self) -> None:
        self.with_sample_rules()
        base = [sys.executable, str(REPO_ROOT / "bin" / "ratchet")]
        add = subprocess.run([*base, "add", "--source", "l1", "--run", "R1", "--repo", str(self.repo)],
                             input=json.dumps([report_item()]), capture_output=True, text=True)
        self.assertEqual(add.returncode, 0, add.stderr)
        count = subprocess.run([*base, "count", "--repo", str(self.repo)], capture_output=True, text=True)
        self.assertIn("1 total, 1 open", count.stdout)

    def test_cwd_is_used_when_repo_is_not_given(self) -> None:
        self.with_sample_rules()
        base = [sys.executable, str(REPO_ROOT / "bin" / "ratchet")]
        subprocess.run([*base, "add", "--source", "l1", "--run", "R1"], input=json.dumps([report_item()]),
                       capture_output=True, text=True, cwd=self.repo, check=True)
        self.assertTrue(self.ledger.exists())

    def test_error_exit_code_and_no_traceback(self) -> None:
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "ratchet"), "count", "--repo", str(self.tmp)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ratchet: error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
