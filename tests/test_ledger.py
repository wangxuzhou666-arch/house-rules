import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, gate_finding, gate_pass, report_item, review_finding

from ratchet import ledger
from ratchet.errors import RatchetError


class FoldTest(unittest.TestCase):
    def outcome(self, finding_id: str, by: str = "human") -> dict:
        return {"v": 1, "kind": "outcome", "id": finding_id, "outcome": "fixed", "outcome_by": by,
                "evidence": "e", "ts": "2026-01-08T00:00:00"}

    def test_open_until_an_outcome_arrives(self) -> None:
        first, second = review_finding("R1-001"), review_finding("R1-002")
        events = [first, second, self.outcome(first["id"])]
        self.assertEqual([f["display_id"] for f in ledger.open_findings(events)], ["R1-002"])

    def test_gate_redetection_after_an_outcome_reopens(self) -> None:
        finding = gate_finding("a.py")
        events = [finding, self.outcome(finding["id"], "auto:gate_pass")]
        self.assertEqual(ledger.open_findings(events), [])
        self.assertEqual(len(ledger.open_findings(events + [finding])), 1)

    def test_outcome_for_unknown_finding_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "not in the ledger"):
            ledger.fold([self.outcome("demo-repo:l1:R1:R1-001")])

    def test_select_new_events_drops_review_replays_but_reopens_gate_findings(self) -> None:
        review, gate = review_finding("R1-001"), gate_finding("a.py")
        existing = [review, gate, self.outcome(review["id"]), self.outcome(gate["id"], "auto:gate_pass")]
        self.assertEqual(ledger.select_new_events(existing, [review]), [])
        self.assertEqual(ledger.select_new_events(existing, [gate]), [gate])

    def test_select_new_events_drops_the_outcome_of_a_dropped_duplicate(self) -> None:
        review = review_finding("R1-001")
        replay_outcome = self.outcome(review["id"])
        self.assertEqual(ledger.select_new_events([review], [review, replay_outcome]), [])

    def test_reused_review_id_with_different_content_is_refused(self) -> None:
        existing = [review_finding("R1-001", summary="original")]
        with self.assertRaisesRegex(RatchetError, "different file/line/summary"):
            ledger.select_new_events(existing, [review_finding("R1-001", summary="something else")])

    def test_legacy_padded_review_replay_keeps_original_id_and_outcome(self):
        old = review_finding("R1-001")
        replay = review_finding("R1-1")
        existing = [old, self.outcome(old["id"])]
        self.assertEqual(ledger.select_new_events(existing, [replay, self.outcome(replay["id"])]), [])
        self.assertEqual(ledger.open_findings(existing), [])

    def test_legacy_alias_still_rejects_changed_content_and_does_not_merge_other_runs(self):
        old = review_finding("R1-001")
        with self.assertRaisesRegex(RatchetError, "different file/line/summary"):
            ledger.select_new_events([old], [review_finding("R1-1", summary="different")])
        other = review_finding("R2-1", run="R2")
        self.assertEqual(ledger.select_new_events([old], [other]), [other])


class ValidateTest(unittest.TestCase):
    def test_invalid_event_is_rejected_with_every_problem(self) -> None:
        with self.assertRaisesRegex(RatchetError, "line.*severity|severity.*line"):
            ledger.validate(review_finding(line=0, severity="high"))

    def test_effective_rule_id_alias_is_scoped_to_review_sources(self) -> None:
        alias = frozenset({"R.1"})
        review = review_finding(rule_id="R.1")
        gate = gate_finding("a.py", rule_id="R.1")
        self.assertEqual(ledger.effective_rule_id(review, alias), "unmapped")
        self.assertEqual(ledger.effective_rule_id(review, frozenset()), "R.1")
        self.assertEqual(ledger.effective_rule_id(gate, alias), "R.1")
        self.assertEqual(ledger.effective_rule_id(review_finding(rule_id="P0E.1"), alias), "P0E.1")


class FileIoTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.jsonl"

    def test_round_trip_and_appends_accumulate(self) -> None:
        ledger.append_events(self.path, [review_finding("R1-001")])
        ledger.append_events(self.path, [review_finding("R1-002")])
        self.assertEqual([e["display_id"] for e in ledger.read_events(self.path)], ["R1-001", "R1-002"])

    def test_invalid_batch_writes_nothing(self) -> None:
        with self.assertRaises(RatchetError):
            ledger.append_events(self.path, [review_finding("R1-001"), review_finding("R1-002", line=0)])
        self.assertFalse(self.path.exists())

    def test_missing_ledger_reads_as_empty(self) -> None:
        self.assertEqual(ledger.read_events(self.path), [])

    def test_bad_json_line_is_reported_with_its_line_number(self) -> None:
        self.path.write_text(json.dumps(review_finding()) + "\n{broken\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, r"ledger\.jsonl:2: not valid JSON"):
            ledger.read_events(self.path)

    def test_invalid_event_line_is_reported_with_its_line_number(self) -> None:
        self.path.write_text(json.dumps(review_finding(severity="high")) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, r"ledger\.jsonl:1: .*severity"):
            ledger.read_events(self.path)

    def test_blank_lines_are_skipped(self) -> None:
        self.path.write_text("\n" + json.dumps(review_finding()) + "\n\n", encoding="utf-8")
        self.assertEqual(len(ledger.read_events(self.path)), 1)


class AddAndCountTest(RepoCase):
    def test_add_then_count(self) -> None:
        self.with_sample_rules()
        out = self.add_findings([report_item(id="R1-001"), report_item(id="R1-002", line=20, rule_id="P3.1")])
        self.assertIn("+2 finding(s), 0 duplicate(s) ignored", out)
        count = self.cli_ok("count")
        self.assertIn("2 total, 2 open (gate 0 / l1 2 / l2 0)", count)

    def test_added_event_shape(self) -> None:
        self.with_sample_rules()
        self.add_findings([report_item(rule_id="P0E.6")])
        (event,) = self.events()
        self.assertEqual(event["id"], "demo-repo:l1:R1:R1-001")
        self.assertEqual(event["repo"], "demo-repo")
        self.assertEqual(event["rule_id"], "P0E.6")
        self.assertEqual(event["category"], "P0E")
        self.assertEqual(event["severity"], "blocker")  # from the rules file
        self.assertNotIn("rule_id_source", event)
        self.assertRegex(event["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(event["sha"], "none")  # unborn branch

    def test_replaying_the_same_report_adds_nothing(self) -> None:
        self.with_sample_rules()
        self.add_findings([report_item()])
        out = self.add_findings([report_item()])
        self.assertIn("+0 finding(s), 1 duplicate(s) ignored", out)
        self.assertEqual(len(self.events()), 1)

    def test_status_writes_a_human_outcome(self) -> None:
        self.with_sample_rules()
        self.add_findings([report_item(status="no_change_needed")])
        kinds = [(e["kind"], e.get("outcome"), e.get("outcome_by")) for e in self.events()]
        self.assertEqual(kinds, [("finding", None, None), ("outcome", "false_positive", "human")])
        self.assertIn("0 open", self.cli_ok("count"))

    def test_unknown_clause_is_rejected_and_nothing_is_written(self) -> None:
        self.with_sample_rules()
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", stdin=json.dumps([report_item(rule_id="P9.9")]))
        self.assertEqual(code, 1)
        self.assertIn("unknown rule_id 'P9.9'", err)
        self.assertFalse(self.ledger.exists())

    def test_tool_rule_map_translates_tool_ids_and_marks_the_source(self) -> None:
        self.with_sample_rules(tool_rule_map={"ruff:F811": "P2.3"})
        self.add_findings([report_item(rule_id="ruff:F811")])
        (event,) = self.events()
        self.assertEqual((event["rule_id"], event["rule_code"], event["rule_id_source"]), ("P2.3", "ruff:F811", "tool"))

    def test_scenarios_default_fills_a_missing_failure_scenario(self) -> None:
        self.with_sample_rules(scenarios={"P0E.1": "Configured default scenario"})
        item = report_item()
        del item["failure_scenario"]
        self.add_findings([item])
        self.assertEqual(self.events()[0]["failure_scenario"], "Configured default scenario")
        del_item = report_item(id="R1-002", rule_id="P1.1")
        del del_item["failure_scenario"]
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", stdin=json.dumps([del_item]))
        self.assertEqual(code, 1)
        self.assertIn("no default in config scenarios", err)

    def test_missing_required_field_names_the_item(self) -> None:
        self.with_sample_rules()
        item = report_item()
        del item["summary"]
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", stdin=json.dumps([report_item(), item]))
        self.assertEqual(code, 1)
        self.assertIn("item 2", err)
        self.assertFalse(self.ledger.exists())

    def test_gate_pass_and_gate_findings_written_by_other_tools_are_counted(self) -> None:
        self.append_raw(gate_finding("a.py"), gate_pass(["a.py"]))
        self.assertIn("1 total, 1 open (gate 1 / l1 0 / l2 0)", self.cli_ok("count"))

    def test_count_by_rule_applies_the_legacy_alias_only_when_configured(self) -> None:
        self.append_raw(review_finding("R1-001", rule_id="R.1"), review_finding("R1-002", rule_id="P0E.1"))
        self.assertIn("R.1: 1", self.cli_ok("count", "--by-rule"))
        self.write_config(legacy_unmapped_alias=["R.1"])
        by_rule = self.cli_ok("count", "--by-rule")
        self.assertIn("unmapped: 1", by_rule)
        self.assertNotIn("R.1:", by_rule)
        self.assertEqual(self.events()[0]["rule_id"], "R.1")  # stored events are never rewritten

    def test_count_on_a_missing_default_ledger_is_zero_with_a_note(self) -> None:
        out = self.cli_ok("count")
        self.assertIn("0 total, 0 open", out)
        self.assertIn("no ledger file", out)

    def test_count_on_a_missing_explicit_ledger_is_an_error(self) -> None:
        code, _, err = self.cli("count", "--ledger", str(self.repo / "typo.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("ledger not found", err)


if __name__ == "__main__":
    unittest.main()
