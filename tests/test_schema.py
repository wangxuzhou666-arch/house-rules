import copy
import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
import schema_check
from support import FIXTURES, REPO_ROOT, gate_finding, gate_pass, review_finding

from ratchet import ledger

SCHEMA = json.loads((REPO_ROOT / "schemas" / "finding.v1.schema.json").read_text(encoding="utf-8"))


def schema_errors(event: object) -> list[str]:
    return schema_check.validate(event, SCHEMA)


class LegacyLedgerTest(unittest.TestCase):
    """Ledgers written before the relaxed schema must load and fold unchanged."""

    def setUp(self) -> None:
        self.path = FIXTURES / "legacy_ledger.jsonl"
        self.lines = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_every_line_validates_against_the_schema_file(self) -> None:
        for number, event in enumerate(self.lines, start=1):
            self.assertEqual(schema_errors(event), [], f"fixture line {number}")

    def test_every_line_validates_in_code(self) -> None:
        for number, event in enumerate(self.lines, start=1):
            self.assertEqual(ledger.validation_problems(event), [], f"fixture line {number}")

    def test_fixture_has_no_field_added_after_the_first_v1_ledgers(self) -> None:
        new_fields = {"discovered_at", "also_matches", "rule_id_source"}
        self.assertFalse(any(new_fields & set(event) for event in self.lines))

    def test_loads_and_folds(self) -> None:
        events = ledger.read_events(self.path)
        self.assertEqual(len(events), 9)
        state = ledger.fold(events)
        self.assertEqual(len(state), 6)
        closed = [fid for fid, item in state.items() if item.outcome is not None]
        self.assertEqual(closed, ["demo-app:l1:L1R1:L1R1-001"])
        self.assertEqual(len(ledger.open_findings(events)), 5)


class SchemaAgreementTest(unittest.TestCase):
    """The hand-written validator and the schema file must accept and reject the same events."""

    def good_events(self) -> list[dict]:
        full = review_finding(
            "R2-001", run="R2", rule_id="P0E.3", rule_code="p0e-3", category="P0E", discovered_at="2026-01-07T10:59:00",
            rule_id_source="model", snippet="x = 1",
            also_matches=[{"rule_id": "unmapped", "severity": "nice", "summary": "s", "failure_scenario": "f"}])
        outcome = {"v": 1, "kind": "outcome", "id": "demo-repo:l1:R1:R1-001", "outcome": "fixed",
                   "outcome_by": "auto:commit", "evidence": "commit 1234abcd subject", "ts": "2026-01-08T00:00:00"}
        unmapped = review_finding("R2-002", run="R2", rule_id="unmapped", category="unmapped")
        tool = gate_finding("src/x.py", rule_id="eslint:no-unused-vars", rule_code="no-unused-vars")
        return [full, outcome, unmapped, tool, gate_pass(["a.py"])]

    def bad_events(self) -> list[dict]:
        base = review_finding()
        cases = []

        def mutated(**changes: object) -> dict:
            event = copy.deepcopy(base)
            for key, value in changes.items():
                if value is None:
                    event.pop(key)
                else:
                    event[key] = value
            return event

        cases += [mutated(line=0), mutated(line=True), mutated(ts="2026-01-07 11:00:00"), mutated(sha="XYZ"),
                  mutated(severity="high"), mutated(verdict="MAYBE"), mutated(source="l3"), mutated(v=2), mutated(v=True),
                  mutated(rule_id="has space"), mutated(rule_id=""), mutated(summary=""), mutated(extra_field=1),
                  mutated(rule_id_source="guess"), mutated(discovered_at="yesterday"), mutated(also_matches=[{"rule_id": "R.1"}]),
                  mutated(also_matches="no"), mutated(file=None), mutated(id="not-a-review-id"), mutated(repo="Has Caps"),
                  mutated(category="")]
        cases += [gate_finding("a.py", run_id="not-gate"), gate_finding("a.py", display_id="short"),
                  gate_pass([]), gate_pass(["a.py"], ts="bad"), {"v": 1, "kind": "outcome", "id": "x"},
                  {"v": 1, "kind": "mystery"}, "not an object"]
        return cases

    def test_accepted_events_agree(self) -> None:
        for event in self.good_events():
            self.assertEqual(schema_errors(event), [], event)
            self.assertEqual(ledger.validation_problems(event), [], event)

    def test_rejected_events_agree(self) -> None:
        for event in self.bad_events():
            self.assertNotEqual(schema_errors(event), [], f"schema accepted {event!r}")
            self.assertNotEqual(ledger.validation_problems(event), [], f"code accepted {event!r}")

    def test_enums_in_code_match_the_schema_file(self) -> None:
        finding = SCHEMA["$defs"]["finding"]["properties"]
        outcome = SCHEMA["$defs"]["outcome"]["properties"]
        self.assertEqual(tuple(finding["source"]["enum"]), ledger.SOURCES)
        self.assertEqual(tuple(finding["verdict"]["enum"]), ledger.VERDICTS)
        self.assertEqual(tuple(finding["rule_id_source"]["enum"]), ledger.RULE_ID_SOURCES)
        self.assertEqual(tuple(SCHEMA["$defs"]["severity"]["enum"]), ledger.SEVERITIES)
        self.assertEqual(tuple(outcome["outcome"]["enum"]), ledger.OUTCOMES)
        self.assertEqual(tuple(outcome["outcome_by"]["enum"]), ledger.OUTCOME_BY)

    def test_field_sets_in_code_match_the_schema_file(self) -> None:
        for kind in ("finding", "outcome", "gate_pass"):
            definition = SCHEMA["$defs"][kind]
            checks, optional = ledger._FIELDS_BY_KIND[kind]
            self.assertEqual(set(checks), set(definition["properties"]), kind)
            self.assertEqual(set(checks) - optional, set(definition["required"]), kind)


if __name__ == "__main__":
    unittest.main()
