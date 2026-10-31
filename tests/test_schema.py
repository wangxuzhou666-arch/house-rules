import copy
import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
import schema_check
from support import FIXTURES, REPO_ROOT, gate_finding, gate_pass, review_finding, review_round

from house_rules import ledger

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

    def test_review_run_events_change_neither_the_fold_nor_what_is_open(self) -> None:
        events = ledger.read_events(self.path)
        with_rounds = [review_round("main@abc", run="r1"), *events, review_round("main@abc", run="r2", round=2)]
        self.assertEqual(ledger.fold(with_rounds), ledger.fold(events))
        self.assertEqual(ledger.open_findings(with_rounds), ledger.open_findings(events))

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
        rounds = [review_round(), review_round("target:42", run="R7", round=4, over_cap=True, finding_count=3, source="file"),
                  review_round("main@unborn", max_rounds=1), review_round(trigger="auto"),
                  review_round(trigger="a-trigger-a-later-version-adds")]  # structural only: any non-empty string
        return [full, outcome, unmapped, tool, gate_pass(["a.py"]), *rounds]

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
        cases += [review_round(round=0), review_round(round=True), review_round(round="1"), review_round(max_rounds=0),
                  review_round(over_cap="yes"), review_round(over_cap=1), review_round(finding_count=-1),
                  review_round(finding_count=True), review_round(source="human"), review_round(round_key=""),
                  review_round(run="has space"), review_round(repo="Has Caps"), review_round(ts="today"),
                  review_round(trigger=""), review_round(trigger=1), review_round(trigger=None), review_round(trigger=True),
                  review_round(trigger=["auto"]),
                  review_round(extra_field=1), {k: v for k, v in review_round().items() if k != "round_key"},
                  {k: v for k, v in review_round().items() if k != "over_cap"}, {**review_round(), "v": 2}]
        return cases

    def test_accepted_events_agree(self) -> None:
        for event in self.good_events():
            self.assertEqual(schema_errors(event), [], event)
            self.assertEqual(ledger.validation_problems(event), [], event)

    def test_rejected_events_agree(self) -> None:
        for event in self.bad_events():
            self.assertNotEqual(schema_errors(event), [], f"schema accepted {event!r}")
            self.assertNotEqual(ledger.validation_problems(event), [], f"code accepted {event!r}")

    def test_a_pattern_is_matched_against_the_whole_value_not_up_to_a_final_newline(self) -> None:
        """Python's `$` also matches before a final newline; ECMA-262's, which the schema file is written in, does not."""
        checked = [(review_finding(discovered_at="2026-01-07T10:59:00"),
                    ("id", "display_id", "run_id", "repo", "ts", "sha", "rule_id", "discovered_at")),
                   (gate_finding("a.py"), ("id", "display_id")),
                   (gate_pass(["a.py"]), ("repo", "ts", "sha")),
                   (review_round(head="0123456789abcdef0123456789abcdef01234567", tree="4b825dc642cb6eb9a060e54bf8d69288fbee4904"),
                    ("repo", "ts", "run", "head", "tree"))]
        for event, fields in checked:
            self.assertEqual((schema_errors(event), ledger.validation_problems(event)), ([], []), event)
            for field in fields:
                with self.subTest(kind=event["kind"], field=field):
                    with_newline = {**event, field: event[field] + "\n"}
                    self.assertNotEqual(schema_errors(with_newline), [], f"schema accepted {with_newline!r}")
                    self.assertNotEqual(ledger.validation_problems(with_newline), [], f"code accepted {with_newline!r}")

    def test_enums_in_code_match_the_schema_file(self) -> None:
        finding = SCHEMA["$defs"]["finding"]["properties"]
        outcome = SCHEMA["$defs"]["outcome"]["properties"]
        self.assertEqual(tuple(finding["source"]["enum"]), ledger.SOURCES)
        self.assertEqual(tuple(finding["verdict"]["enum"]), ledger.VERDICTS)
        self.assertEqual(tuple(finding["rule_id_source"]["enum"]), ledger.RULE_ID_SOURCES)
        self.assertEqual(tuple(SCHEMA["$defs"]["severity"]["enum"]), ledger.SEVERITIES)
        self.assertEqual(tuple(outcome["outcome"]["enum"]), ledger.OUTCOMES)
        self.assertEqual(tuple(outcome["outcome_by"]["enum"]), ledger.OUTCOME_BY)
        self.assertEqual(tuple(SCHEMA["$defs"]["review_run"]["properties"]["source"]["enum"]), ledger.REVIEW_RUN_SOURCES)

    def test_field_sets_in_code_match_the_schema_file(self) -> None:
        for kind in ("finding", "outcome", "gate_pass", "review_run"):
            definition = SCHEMA["$defs"][kind]
            checks, optional = ledger._FIELDS_BY_KIND[kind]
            self.assertEqual(set(checks), set(definition["properties"]), kind)
            self.assertEqual(set(checks) - optional, set(definition["required"]), kind)


if __name__ == "__main__":
    unittest.main()
