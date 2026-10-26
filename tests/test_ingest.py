import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, review_item

from ratchet.errors import RatchetError
from ratchet.ingest import extract_review_items


class ExtractTest(unittest.TestCase):
    ARRAY = json.dumps([review_item()])

    def test_plain_json_array(self) -> None:
        self.assertEqual(len(extract_review_items(self.ARRAY)), 1)

    def test_findings_wrapper_and_empty_array(self) -> None:
        self.assertEqual(len(extract_review_items(json.dumps({"findings": [review_item()]}))), 1)
        self.assertEqual(extract_review_items("[]"), [])

    def test_first_json_fence_in_review_text(self) -> None:
        text = f"Review notes.\n\n```json\n{self.ARRAY}\n```\n\nAnything else.\n"
        self.assertEqual(extract_review_items(text), [review_item()])

    def test_fence_without_a_trailing_newline_before_the_closing_marker(self) -> None:
        self.assertEqual(len(extract_review_items(f"```json\n{self.ARRAY}```")), 1)

    def test_skips_a_fence_that_is_not_an_array_and_one_that_does_not_parse(self) -> None:
        text = ('```json\n{"example": true}\n```\n```json\n{broken\n```\n'
                f"```json\n{self.ARRAY}\n```\n")
        self.assertEqual(len(extract_review_items(text)), 1)

    def test_only_the_json_tagged_fence_counts(self) -> None:
        with self.assertRaisesRegex(RatchetError, "neither a JSON array"):
            extract_review_items(f"```\n{self.ARRAY}\n```")

    def test_no_array_anywhere_is_an_error_that_says_why(self) -> None:
        with self.assertRaisesRegex(RatchetError, "failed to parse"):
            extract_review_items("text\n```json\n{broken\n```\n")

    def test_non_object_items_are_rejected(self) -> None:
        with self.assertRaisesRegex(RatchetError, "item 1"):
            extract_review_items('["not an object"]')


class IngestTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()

    def ingest(self, items: list[dict], *extra: str, run: str = "R1") -> str:
        return self.cli_ok("ingest-code-review", "--source", "l1", "--run", run, *extra, stdin=json.dumps(items))

    def test_kebab_category_becomes_the_clause_id(self) -> None:
        self.ingest([review_item(category="p0e-6", severity="blocker"), review_item(line=20, category="P3-1", severity="nice")])
        first, second = self.events()
        self.assertEqual((first["rule_id"], first["rule_code"], first["category"]), ("P0E.6", "p0e-6", "P0E"))
        self.assertEqual(second["rule_id"], "P3.1")

    def test_unknown_missing_and_non_string_categories_become_unmapped(self) -> None:
        out = self.ingest([review_item(category="p0e-99"), review_item(line=2, category="error-handling"),
                           review_item(line=3), review_item(line=4, category=7)])
        events = self.events()
        self.assertEqual([e["rule_id"] for e in events], ["unmapped"] * 4)
        self.assertEqual([e["category"] for e in events], ["unmapped"] * 4)
        self.assertEqual(events[0]["rule_code"], "p0e-99")  # what the model said is kept
        self.assertEqual(events[2]["rule_code"], "unmapped")
        self.assertIn("4 unmapped", out)

    def test_rule_id_source_is_model_even_for_unmapped(self) -> None:
        self.ingest([review_item(category="p0e-1"), review_item(line=2)])
        self.assertEqual([e["rule_id_source"] for e in self.events()], ["model", "model"])

    def test_missing_severity_comes_from_the_rules_file_else_should_fix(self) -> None:
        out = self.ingest([review_item(category="p0e-6"), review_item(line=2, category="p3-1"),
                           review_item(line=3, category="p1-1"), review_item(line=4)])
        self.assertEqual([e["severity"] for e in self.events()], ["blocker", "nice", "should-fix", "should-fix"])
        self.assertIn("4 without a usable severity", out)

    def test_invalid_severity_is_treated_as_missing(self) -> None:
        self.ingest([review_item(category="p0e-6", severity="critical")])
        self.assertEqual(self.events()[0]["severity"], "blocker")

    def test_given_severity_wins_over_the_rules_file(self) -> None:
        self.ingest([review_item(category="p0e-6", severity="nice")])
        self.assertEqual(self.events()[0]["severity"], "nice")

    def test_same_file_and_line_collapse_most_severe_first(self) -> None:
        out = self.ingest([
            review_item(summary="minor", category="p3-1", severity="nice"),
            review_item(summary="major", category="p0e-6", severity="blocker", failure_scenario="major scenario"),
            review_item(summary="middle", category="p1-1", severity="should-fix"),
            review_item(file="src/b.py", summary="elsewhere"),
        ])
        primary, other = self.events()
        self.assertEqual((primary["summary"], primary["rule_id"], primary["severity"]), ("major", "P0E.6", "blocker"))
        self.assertEqual([(m["summary"], m["severity"]) for m in primary["also_matches"]],
                         [("minor", "nice"), ("middle", "should-fix")])
        self.assertEqual(set(primary["also_matches"][0]), {"rule_id", "severity", "summary", "failure_scenario"})
        self.assertNotIn("also_matches", other)
        self.assertIn("2 merged by file+line", out)

    def test_equal_severity_keeps_the_first_as_primary(self) -> None:
        self.ingest([review_item(summary="first", severity="nice"), review_item(summary="second", severity="nice")])
        self.assertEqual(self.events()[0]["summary"], "first")

    def test_same_line_in_different_files_is_not_collapsed(self) -> None:
        self.ingest([review_item(file="src/a.py"), review_item(file="src/b.py")])
        self.assertEqual(len(self.events()), 2)

    def test_display_ids_are_run_prefixed_and_stable(self) -> None:
        self.ingest([review_item(file="a.py"), review_item(file="b.py")], run="L1R7")
        self.assertEqual([e["display_id"] for e in self.events()], ["L1R7-001", "L1R7-002"])
        self.assertEqual(self.events()[0]["id"], "demo-repo:l1:L1R7:L1R7-001")

    def test_ingesting_the_same_review_twice_is_a_no_op(self) -> None:
        items = [review_item(file="a.py"), review_item(file="b.py", category="p1-1")]
        self.ingest(items)
        out = self.ingest(items)
        self.assertIn("+0 finding(s)", out)
        self.assertIn("2 already recorded", out)
        self.assertEqual(len(self.events()), 2)

    def test_reusing_a_run_for_a_different_review_is_refused(self) -> None:
        self.ingest([review_item(file="a.py", summary="one")])
        code, _, err = self.cli("ingest-code-review", "--source", "l1", "--run", "R1",
                                stdin=json.dumps([review_item(file="a.py", summary="a different finding")]))
        self.assertEqual(code, 1)
        self.assertIn("different file/line/summary", err)
        self.assertEqual(len(self.events()), 1)

    def test_review_text_with_a_fenced_array_is_accepted_via_stdin(self) -> None:
        text = "Two findings below.\n```json\n" + json.dumps([review_item(category="p0e-1")]) + "\n```\n"
        self.cli_ok("ingest-code-review", "--source", "l2", "--run", "R9", stdin=text)
        (event,) = self.events()
        self.assertEqual((event["source"], event["rule_id"]), ("l2", "P0E.1"))

    def test_input_file_flag(self) -> None:
        path = self.repo / "review.json"
        path.write_text(json.dumps([review_item()]), encoding="utf-8")
        self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", "--input", str(path))
        self.assertEqual(len(self.events()), 1)

    def test_empty_review_writes_nothing(self) -> None:
        out = self.ingest([])
        self.assertIn("+0 finding(s) from 0 item(s)", out)
        self.assertFalse(self.ledger.exists())

    def test_flags_set_verdict_and_discovered_at(self) -> None:
        self.ingest([review_item()], "--verdict", "CONFIRMED", "--discovered-at", "2026-01-07T10:00:00")
        (event,) = self.events()
        self.assertEqual((event["verdict"], event["discovered_at"]), ("CONFIRMED", "2026-01-07T10:00:00"))

    def test_default_verdict_is_plausible(self) -> None:
        self.ingest([review_item()])
        self.assertEqual(self.events()[0]["verdict"], "PLAUSIBLE")

    def test_malformed_discovered_at_is_rejected(self) -> None:
        code, _, err = self.cli("ingest-code-review", "--source", "l1", "--run", "R1", "--discovered-at", "yesterday",
                                stdin=json.dumps([review_item()]))
        self.assertEqual(code, 1)
        self.assertIn("discovered_at", err)

    def test_item_missing_a_core_field_rejects_the_whole_review(self) -> None:
        bad = review_item()
        del bad["line"]
        code, _, err = self.cli("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps([review_item(), bad]))
        self.assertEqual(code, 1)
        self.assertIn("item 2", err)
        self.assertFalse(self.ledger.exists())

    def test_line_must_be_an_integer(self) -> None:
        code, _, err = self.cli("ingest-code-review", "--source", "l1", "--run", "R1",
                                stdin=json.dumps([review_item(line="12")]))
        self.assertEqual(code, 1)
        self.assertIn("'line'", err)

    def test_scenarios_default_covers_a_missing_failure_scenario(self) -> None:
        self.with_sample_rules(scenarios={"unmapped": "Default scenario for unmapped findings"})
        item = review_item()
        del item["failure_scenario"]
        self.ingest([item])
        self.assertEqual(self.events()[0]["failure_scenario"], "Default scenario for unmapped findings")

    def test_without_rules_files_everything_is_unmapped(self) -> None:
        self.write_config()
        self.ingest([review_item(category="p0e-6")])
        self.assertEqual(self.events()[0]["rule_id"], "unmapped")


if __name__ == "__main__":
    unittest.main()
