import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, gate_finding, gate_pass, git, report_item, review_finding

from ratchet import outcomes


class TrailerTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()

    def outcomes(self) -> list[dict]:
        return [e for e in self.events() if e["kind"] == "outcome"]

    def test_trailer_with_display_id_closes_the_finding(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        sha = self.commit("fix: narrow the try block", "Fixes-Finding: R1-001")
        out = self.cli_ok("infer")
        self.assertIn("+1 fixed (auto:commit)", out)
        (outcome,) = self.outcomes()
        self.assertEqual((outcome["id"], outcome["outcome"], outcome["outcome_by"]),
                         ("demo-repo:l1:R1:R1-001", "fixed", "auto:commit"))
        self.assertIn(sha[:8], outcome["evidence"])
        self.assertIn("fix: narrow the try block", outcome["evidence"])

    def test_trailer_with_full_id_closes_the_finding(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("fix: x", "Fixes-Finding: demo-repo:l1:R1:R1-001")
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_several_trailers_and_comma_lists_close_several_findings(self) -> None:
        self.add_findings([report_item(id="R1-001", line=1), report_item(id="R1-002", line=2),
                           report_item(id="R1-003", line=3)])
        self.commit("fix: one commit, three findings", "Fixes-Finding: R1-001\nFixes-Finding: R1-002, R1-003")
        self.assertIn("+3 fixed (auto:commit)", self.cli_ok("infer"))

    def test_a_fix_committed_before_the_finding_was_written_still_matches(self) -> None:
        self.commit("fix: done before the review was ingested", "Fixes-Finding: R1-001", date="2020-01-01T00:00:00")
        self.add_findings([report_item(id="R1-001")])  # ledger ts is now later than the commit
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_scope_tag_in_the_subject_closes_nothing(self) -> None:
        self.add_findings([report_item(id="S1", line=1), report_item(id="R1-001", line=2)])
        self.commit("feat(parser): add streaming support (S1)")
        self.commit("fix: tidy up (R1-001)")
        self.commit("chore: S1 R1-001")
        out = self.cli_ok("infer")
        self.assertIn("+0 fixed (auto:commit)", out)
        self.assertEqual(self.outcomes(), [])

    def test_trailer_must_be_in_the_final_paragraph(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("fix: x", "Fixes-Finding: R1-001\n\nAnd then some more prose that is not a trailer.")
        self.assertIn("+0 fixed (auto:commit)", self.cli_ok("infer"))

    def test_trailer_key_is_case_insensitive(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("fix: x", "fixes-finding: R1-001")
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_unmatched_trailer_warns_and_closes_nothing(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("fix: typo in the trailer", "Fixes-Finding: R1-0001")
        code, out, err = self.cli("infer")
        self.assertEqual(code, 0)
        self.assertIn("+0 fixed (auto:commit)", out)
        self.assertIn("matches no finding", err)
        self.assertIn("'R1-0001'", err)

    def test_ambiguous_display_id_is_not_guessed(self) -> None:
        self.append_raw(review_finding("DUP-1", run="R1"), review_finding("DUP-1", run="R2"))
        self.commit("fix: x", "Fixes-Finding: DUP-1")
        _, out, err = self.cli("infer")
        self.assertIn("+0 fixed (auto:commit)", out)
        self.assertIn("display_id of several findings", err)
        self.commit("fix: y", "Fixes-Finding: demo-repo:l1:R2:DUP-1")
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_infer_is_idempotent(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("fix: x", "Fixes-Finding: R1-001")
        self.cli_ok("infer")
        self.assertIn("+0 fixed (auto:commit)", self.cli_ok("infer"))
        self.assertEqual(len(self.outcomes()), 1)

    def test_head_only_by_default_and_all_refs_on_request(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.commit("chore: base")
        git(self.repo, "switch", "-q", "-c", "side")
        self.commit("fix: on a side branch", "Fixes-Finding: R1-001")
        git(self.repo, "switch", "-q", "main")
        self.assertIn("+0 fixed (auto:commit)", self.cli_ok("infer"))
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer", "--all"))

    def test_repo_without_commits_infers_nothing(self) -> None:
        self.add_findings([report_item(id="R1-001")])
        self.assertIn("+0 fixed (auto:commit), +0 fixed (auto:gate_pass)", self.cli_ok("infer"))
        self.assertIn("+0 fixed (auto:commit), +0 fixed (auto:gate_pass)", self.cli_ok("infer", "--all"))

    def test_old_fix_commit_does_not_close_a_reopened_gate_finding(self) -> None:
        finding = gate_finding("a.py", "a1b2c3d4e5f6")
        sha = self.commit("fix: the first occurrence", "Fixes-Finding: a1b2c3d4e5f6")
        closed = {"v": 1, "kind": "outcome", "id": finding["id"], "outcome": "fixed", "outcome_by": "auto:commit",
                  "evidence": f"commit {sha[:8]} fix: the first occurrence", "ts": "2026-01-08T00:00:00"}
        self.append_raw(finding, closed, finding)  # the violation came back: gate re-detected it
        self.assertIn("+0 fixed (auto:commit)", self.cli_ok("infer"))
        self.commit("fix: the second occurrence", "Fixes-Finding: a1b2c3d4e5f6")
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_commit_trailer_reader_returns_oldest_first(self) -> None:
        self.commit("first", "Fixes-Finding: A")
        self.commit("no trailer here")
        self.commit("second", "Fixes-Finding: B\nFixes-Finding: C")
        commits = outcomes.trailer_commits(self.repo, all_refs=False)
        self.assertEqual([(c.subject, c.tokens) for c in commits], [("first", ("A",)), ("second", ("B", "C"))])


class QuietTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()
        self.add_findings([report_item(id="R1-001")])

    def test_quiet_prints_nothing_when_nothing_was_closed(self) -> None:
        self.commit("chore: nothing to see")
        code, out, err = self.cli("infer", "--quiet")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_quiet_still_reports_what_it_closed(self) -> None:
        self.commit("fix: x", "Fixes-Finding: R1-001")
        code, out, _ = self.cli("infer", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("infer: +1 fixed (auto:commit)", out)

    def test_quiet_keeps_warnings_about_a_trailer_that_matches_nothing(self) -> None:
        self.commit("fix: x", "Fixes-Finding: R9-009")
        code, out, err = self.cli("infer", "--quiet")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("matches no finding", err)

    def test_quiet_skips_the_label_page_nudge_even_when_it_closed_something(self) -> None:
        self.add_findings([report_item(id="R1-002", line=2)])  # a second finding that stays open
        self.write_config(rules_files=["rules.md"], label_page_threshold=0)
        self.commit("fix: x", "Fixes-Finding: R1-001")
        out = self.cli("infer", "--quiet")[1]
        self.assertIn("infer: +1 fixed (auto:commit)", out)
        self.assertNotIn("label-page", out)
        self.commit("fix: y", "Fixes-Finding: R9-009")
        self.assertIn("run `ratchet label-page`", self.cli_ok("infer"))  # without --quiet the nudge is there

    def test_a_second_quiet_run_after_closing_is_silent(self) -> None:
        self.commit("fix: x", "Fixes-Finding: R1-001")
        self.cli_ok("infer", "--quiet")
        self.assertEqual(self.cli("infer", "--quiet")[1:], ("", ""))


class GatePassTest(RepoCase):
    def infer_out(self, *extra: str) -> str:
        return self.cli_ok("infer", *extra)

    def outcome_by(self) -> dict[str, str]:
        return {e["id"]: e["outcome_by"] for e in self.events() if e["kind"] == "outcome"}

    def test_a_later_gate_pass_covering_the_file_closes_the_finding(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py", "b.py"], ts="2026-01-06T10:00:00", sha="1f2e3d4c"))
        self.assertIn("+1 fixed (auto:gate_pass)", self.infer_out())
        (outcome,) = [e for e in self.events() if e["kind"] == "outcome"]
        self.assertEqual(outcome["outcome_by"], "auto:gate_pass")
        self.assertIn("2026-01-06T10:00:00", outcome["evidence"])
        self.assertIn("1f2e3d4c", outcome["evidence"])
        self.assertIn("a.py", outcome["evidence"])

    def test_a_gate_pass_that_precedes_the_finding_in_the_file_does_not_close_it(self) -> None:
        self.append_raw(gate_pass(["a.py"]), gate_finding("a.py"))
        self.assertIn("+0 fixed (auto:gate_pass)", self.infer_out())

    def test_a_gate_pass_covering_other_files_closes_nothing(self) -> None:
        self.append_raw(gate_finding("c.py"), gate_pass(["a.py", "b.py"]))
        self.assertIn("+0 fixed (auto:gate_pass)", self.infer_out())

    def test_only_the_covered_finding_among_several_is_closed(self) -> None:
        self.append_raw(gate_finding("a.py", "a1b2c3d4e5f6"), gate_finding("c.py", "b2c3d4e5f6a1"), gate_pass(["a.py"]))
        self.assertIn("+1 fixed (auto:gate_pass)", self.infer_out())
        self.assertEqual(list(self.outcome_by()), ["demo-repo:gate:a1b2c3d4e5f6"])

    def test_review_findings_are_never_closed_by_a_gate_pass(self) -> None:
        self.append_raw(review_finding("R1-001"), gate_pass(["src/a.py"]))
        self.assertIn("+0 fixed (auto:gate_pass)", self.infer_out())

    def test_skipped_findings_are_excluded(self) -> None:
        skipped, normal = gate_finding("a.py", "a1b2c3d4e5f6"), gate_finding("a.py", "b2c3d4e5f6a1")
        self.append_raw(skipped, normal, gate_pass(["a.py"]))
        (self.repo / ".gate-skips.jsonl").write_text(
            json.dumps({"ts": "2026-01-06T09:59:00", "gate": "style", "finding_ids": [skipped["id"]]}) + "\n", encoding="utf-8")
        self.write_config(skips_file=".gate-skips.jsonl")
        self.assertIn("+1 fixed (auto:gate_pass)", self.infer_out())
        self.assertEqual(list(self.outcome_by()), [normal["id"]])

    def test_without_a_skips_file_in_the_config_nothing_is_excluded(self) -> None:
        finding = gate_finding("a.py")
        self.append_raw(finding, gate_pass(["a.py"]))
        (self.repo / ".gate-skips.jsonl").write_text(json.dumps({"finding_ids": [finding["id"]]}) + "\n", encoding="utf-8")
        self.assertIn("+1 fixed (auto:gate_pass)", self.infer_out())

    def test_a_configured_skips_file_that_does_not_exist_yet_means_no_skips(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py"]))
        self.write_config(skips_file="never-created.jsonl")
        self.assertIn("+1 fixed (auto:gate_pass)", self.infer_out())

    def test_a_malformed_skips_file_is_an_error(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py"]))
        self.write_config(skips_file="skips.jsonl")
        for bad_line, fragment in (("{broken", "not valid JSON"), ('{"finding_ids": "abc"}', "list of strings"), ("[1]", "JSON object")):
            (self.repo / "skips.jsonl").write_text(bad_line + "\n", encoding="utf-8")
            code, _, err = self.cli("infer")
            self.assertEqual(code, 1)
            self.assertIn("skips.jsonl:1", err)
            self.assertIn(fragment, err)

    def test_a_finding_matched_by_trailer_and_gate_pass_gets_one_outcome(self) -> None:
        self.append_raw(gate_finding("a.py", "a1b2c3d4e5f6"), gate_pass(["a.py"]))
        self.commit("fix: x", "Fixes-Finding: a1b2c3d4e5f6")
        out = self.infer_out()
        self.assertIn("+1 fixed (auto:commit), +0 fixed (auto:gate_pass)", out)
        self.assertEqual(len([e for e in self.events() if e["kind"] == "outcome"]), 1)

    def test_infer_is_idempotent(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py"]))
        self.infer_out()
        self.assertIn("+0 fixed (auto:commit), +0 fixed (auto:gate_pass)", self.infer_out())

    def test_hint_appears_above_the_label_page_threshold(self) -> None:
        self.append_raw(*[review_finding(f"R1-{n:03d}", line=n + 1) for n in range(3)])
        self.write_config(label_page_threshold=2)
        self.assertIn("run `ratchet label-page`", self.infer_out())
        self.write_config(label_page_threshold=3)
        self.assertNotIn("label-page", self.infer_out())


if __name__ == "__main__":
    unittest.main()
