import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import FIXTURES, RepoCase, review_item

from house_rules.config import load_config
from house_rules.errors import HouseRulesError
from house_rules.ingest import code_review_events, escape_stray_quotes, extract_review_items, extract_review_items_lenient
from house_rules.rules import load_rules


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

    def test_a_range_review_gives_its_fenced_findings_and_not_the_sentences_after_the_fence(self) -> None:
        # What `/code-review high <base>..<head>` printed in a smoke run: one json fence, then prose about a bug outside the range.
        items = extract_review_items((FIXTURES / "range_review.txt").read_text(encoding="utf-8"))
        self.assertEqual([(item["file"], item["line"]) for item in items], [("stats.py", 4), ("stats.py", 4), ("stats.py", 1)])
        self.assertNotIn("loop.py", json.dumps(items))

    def test_only_the_json_tagged_fence_counts(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "neither a JSON array"):
            extract_review_items(f"```\n{self.ARRAY}\n```")

    def test_no_array_anywhere_is_an_error_that_says_why(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "failed to parse"):
            extract_review_items("text\n```json\n{broken\n```\n")

    def test_non_object_items_are_rejected(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "item 1"):
            extract_review_items('["not an object"]')


# What a pretty-printed review looks like when the model quotes code with unescaped double quotes in a string value.
STRAY = ('[\n  {\n    "file": "src/a.py",\n    "line": 3,\n    "summary": "reads cfg["name"] without a check",\n'
         '    "failure_scenario": "A caller passes {"name": null} and the call raises"\n  }\n]\n')


class StrayQuoteTest(RepoCase):
    def test_quotes_inside_a_string_field_are_escaped_and_counted(self) -> None:
        fixed, count = escape_stray_quotes(STRAY)
        self.assertEqual(count, 4)
        self.assertIn('"summary": "reads cfg[\\"name\\"] without a check",', fixed)
        self.assertEqual(json.loads(fixed)[0]["summary"], 'reads cfg["name"] without a check')

    def test_the_lenient_reader_returns_the_text_as_the_model_meant_it(self) -> None:
        items, count = extract_review_items_lenient(f"Notes.\n\n```json\n{STRAY}```\n")
        self.assertEqual(count, 4)
        self.assertEqual(items[0]["failure_scenario"], 'A caller passes {"name": null} and the call raises')

    def test_input_that_already_parses_is_never_touched(self) -> None:
        valid = json.dumps([review_item(summary='reads cfg["name"]')], indent=2)
        self.assertEqual(extract_review_items_lenient(valid), (extract_review_items(valid), 0))

    def test_valid_lines_with_two_fields_are_not_mistaken_for_one_string(self) -> None:
        valid = '[\n  {\n    "file": "a.py", "line": 1, "summary": "s", "failure_scenario": "f"\n  }\n]\n'
        items, count = extract_review_items_lenient(valid)
        self.assertEqual((count, items[0]["failure_scenario"]), (0, "f"))

    def test_already_escaped_quotes_are_left_alone(self) -> None:
        line = '    "summary": "has \\"escaped\\" quotes and a stray "one",\n'
        fixed, count = escape_stray_quotes(line)
        self.assertEqual((fixed, count), ('    "summary": "has \\"escaped\\" quotes and a stray \\"one",\n', 1))

    def test_a_quote_after_an_escaped_backslash_is_a_stray_quote(self) -> None:
        fixed, count = escape_stray_quotes('    "summary": "ends with a backslash \\\\"x",\n')
        self.assertEqual(count, 1)
        self.assertEqual(json.loads("{" + fixed.rstrip().rstrip(",") + "}")["summary"], 'ends with a backslash \\"x')

    def test_lines_that_are_not_schema_string_fields_are_untouched(self) -> None:
        text = 'Prose with "quotes" in it.\n    "line": 5,\n    "extra": "a"b",\n'
        self.assertEqual(escape_stray_quotes(text), (text, 0))

    def test_line_endings_and_trailing_commas_survive(self) -> None:
        fixed, count = escape_stray_quotes('    "summary": "a"b",\r\n    "severity": "nice"\r\n')
        self.assertEqual((fixed, count), ('    "summary": "a\\"b",\r\n    "severity": "nice"\r\n', 1))

    def test_a_repair_that_still_does_not_parse_raises_the_original_error(self) -> None:
        broken = STRAY.rstrip().removesuffix("]")  # a second defect, after the stray quotes: the closing bracket is missing
        with self.assertRaises(HouseRulesError) as strict:
            extract_review_items(broken)
        with self.assertRaises(HouseRulesError) as lenient:
            extract_review_items_lenient(broken)
        self.assertEqual(str(lenient.exception), str(strict.exception))

    def test_input_with_nothing_to_repair_raises_the_strict_error(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "neither a JSON array"):
            extract_review_items_lenient("no json here")

    def test_a_field_outside_the_schema_with_a_stray_quote_is_not_repaired(self) -> None:
        with self.assertRaises(HouseRulesError):
            extract_review_items_lenient(STRAY.replace('"line": 3,', '"line": 3,\n    "note": "a"b",'))

    def test_compact_one_line_json_is_not_repaired(self) -> None:
        with self.assertRaises(HouseRulesError):
            extract_review_items_lenient('[{"file": "a.py", "line": 1, "summary": "x["k"]", "failure_scenario": "f"}]')

    def test_the_command_ingests_it_stores_the_text_intact_and_says_so(self) -> None:
        self.with_sample_rules()
        out = self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=STRAY)
        self.assertIn("escaped 4 stray quote(s) in the review's JSON", out)
        (event,) = self.events()
        self.assertEqual(event["summary"], 'reads cfg["name"] without a check')

    def test_valid_input_gets_no_repair_note(self) -> None:
        self.with_sample_rules()
        out = self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps([review_item()]))
        self.assertNotIn("stray", out)


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
        self.ingest([review_item(category="p0e-1"), review_item(line=2)])  # the second names no clause anywhere
        self.assertEqual([e["rule_id_source"] for e in self.events()], ["model", "model"])

    def test_a_model_category_is_labeled_model(self) -> None:
        self.ingest([review_item(category="p0e-6"), review_item(line=2, category="no-such-clause")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"]) for e in self.events()], [("P0E.6", "model"), ("unmapped", "model")])

    def test_without_a_category_the_clause_cited_in_the_summary_is_attributed(self) -> None:
        out = self.ingest([review_item(summary="Swallows the error silently (P0E.6, and it hides the cause)")])
        (event,) = self.events()
        self.assertEqual((event["rule_id"], event["rule_id_source"], event["rule_code"], event["category"]),
                         ("P0E.6", "attributed", "P0E.6", "P0E"))
        self.assertEqual(event["severity"], "blocker")  # the clause's own severity, as for a model-given category
        self.assertIn("1 labeled from the text", out)
        self.assertIn("0 unmapped", out)

    def test_the_summary_line_counts_each_kind_of_label_separately(self) -> None:
        out = self.ingest([review_item(category="p0e-6"), review_item(line=2, summary="Breaks P0E.1"),
                           review_item(line=3, summary="Breaks P3.1"), review_item(line=4, category="nonsense"),
                           review_item(line=5, summary="No clause named")])
        self.assertIn("2 unmapped, 2 labeled from the text", out)

    def test_every_notation_and_case_of_a_clause_id_is_found(self) -> None:
        summaries = ["see P0E.6", "see p0e-6", "see P0E-6", "see (p0e.6).", "[P0E.6] violated", "violates P0E.6."]
        self.ingest([review_item(line=number, summary=text) for number, text in enumerate(summaries, start=1)])
        self.assertEqual({e["rule_id"] for e in self.events()}, {"P0E.6"})
        self.assertEqual({e["rule_id_source"] for e in self.events()}, {"attributed"})

    def test_the_summary_is_read_before_the_failure_scenario_and_the_first_mention_wins(self) -> None:
        self.ingest([
            review_item(line=1, summary="Names P3.1 and P0E.6", failure_scenario="Mentions P2.3 only here"),
            review_item(line=2, summary="No clause named", failure_scenario="Breaks P2.3 when the list is empty"),
            review_item(line=3, summary="Names none", failure_scenario="Names none either"),
        ])
        self.assertEqual([(e["rule_id"], e["rule_id_source"]) for e in self.events()],
                         [("P3.1", "attributed"), ("P2.3", "attributed"), ("unmapped", "model")])

    def test_a_reviewer_rule_cited_in_the_text_is_skipped_for_the_violation_cited_after_it(self) -> None:
        self.ingest([review_item(summary="Per R.7 the surrounding code must be read first; also violates P0E.6")])
        (event,) = self.events()
        self.assertEqual((event["rule_id"], event["rule_id_source"], event["rule_code"], event["severity"]),
                         ("P0E.6", "attributed", "P0E.6", "blocker"))

    def test_a_reviewer_rule_is_never_what_the_text_is_attributed_to(self) -> None:
        out = self.ingest([review_item(summary="Breaks R.7"), review_item(line=2, summary="Fine", failure_scenario="R.1 says so")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"], e["rule_code"]) for e in self.events()],
                         [("unmapped", "model", "unmapped")] * 2)
        self.assertIn("2 unmapped, 0 labeled from the text", out)

    def test_an_empty_blank_or_non_string_category_counts_as_missing(self) -> None:
        self.ingest([review_item(line=number, category=value, summary="Breaks P0E.1")
                     for number, value in enumerate(["", "   ", 7, None, ["p0e-6"]], start=1)])
        self.assertEqual({(e["rule_id"], e["rule_id_source"]) for e in self.events()}, {("P0E.1", "attributed")})

    def test_a_category_the_model_gave_is_never_overridden_by_the_text(self) -> None:
        self.ingest([review_item(category="p3-1", summary="Really a P0E.6 problem"),
                     review_item(line=2, category="error-handling", summary="Really a P0E.6 problem")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"]) for e in self.events()], [("P3.1", "model"), ("unmapped", "model")])

    def test_a_category_naming_a_reviewer_rule_is_no_answer_so_the_text_is_read(self) -> None:
        out = self.ingest([review_item(category="r-7", summary="Per R.7 read first; also violates P0E.1"),
                           review_item(line=2, category="R.1", summary="Nothing else named", failure_scenario="Breaks P3.1 here"),
                           review_item(line=3, category="r-7", summary="Nothing else named")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"], e["rule_code"]) for e in self.events()],
                         [("P0E.1", "attributed", "P0E.1"), ("P3.1", "attributed", "P3.1"), ("unmapped", "model", "r-7")])
        self.assertIn("1 unmapped, 2 labeled from the text", out)

    def test_a_category_naming_a_clause_without_a_severity_is_no_answer_either(self) -> None:
        self.ingest([review_item(category="p1-1", severity="nice")])  # P1.1 is defined but has no severity in these rules
        (event,) = self.events()
        self.assertEqual((event["rule_id"], event["rule_id_source"], event["rule_code"]), ("unmapped", "model", "p1-1"))

    def test_ids_that_are_not_clauses_or_only_part_of_a_longer_number_are_not_attributed(self) -> None:
        self.ingest([review_item(summary="Violates P9.9"), review_item(line=2, summary="Violates P1.12"),
                     review_item(line=3, summary="Version P0E.10 of the helper")])
        self.assertEqual([e["rule_id"] for e in self.events()], ["unmapped"] * 3)

    def test_an_attributed_finding_takes_part_in_merging_with_its_own_label(self) -> None:
        self.ingest([review_item(summary="minor P3.1", severity="nice"), review_item(summary="major P0E.6", severity="blocker")])
        (event,) = self.events()
        self.assertEqual((event["rule_id"], event["rule_id_source"]), ("P0E.6", "attributed"))
        self.assertEqual(event["also_matches"][0]["rule_id"], "P3.1")

    def test_a_scenario_default_for_the_attributed_rule_covers_a_missing_failure_scenario(self) -> None:
        self.with_sample_rules(scenarios={"P0E.6": "Default scenario for P0E.6"})
        item = review_item(summary="Swallows the error (P0E.6)")
        del item["failure_scenario"]
        self.ingest([item])
        self.assertEqual(self.events()[0]["failure_scenario"], "Default scenario for P0E.6")

    def test_missing_severity_comes_from_the_rules_file_else_should_fix(self) -> None:
        out = self.ingest([review_item(category="p0e-6"), review_item(line=2, category="p3-1"),
                           review_item(line=3, category="p2-3"), review_item(line=4)])
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
            review_item(summary="middle", category="p2-3", severity="should-fix"),
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
        self.assertEqual([e["display_id"] for e in self.events()], ["L1R7-1", "L1R7-2"])
        self.assertEqual(self.events()[0]["id"], "demo-repo:l1:L1R7:L1R7-1")

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
        self.ingest([review_item(category="p0e-6"),
                     review_item(line=2, summary="Invalid input (P0E.1).")])
        self.assertEqual([event["rule_id"] for event in self.events()], ["unmapped", "unmapped"])
        self.assertEqual(self.events()[1]["summary"], "Invalid input (P0E.1).")


class IngestWithoutSeveritiesTest(RepoCase):
    """A rules file of the user's own with no severity tag and no family table: compile lists every clause under Reviewer rules,
    but none of them is a Reviewer rule for ingest, so categories and prose still label findings."""

    def setUp(self) -> None:
        super().setUp()
        self.write_rules("1. **[A.1]** First rule.\n2. **[A.2]** Second rule.\n")
        self.write_config(rules_files=["rules.md"])

    def ingest(self, items: list[dict]) -> str:
        return self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps(items))

    def test_a_category_naming_a_clause_is_the_models_answer(self) -> None:
        self.ingest([review_item(category="a-1"), review_item(line=2, category="A.2"), review_item(line=3, category="a-9")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"], e["rule_code"], e["severity"]) for e in self.events()],
                         [("A.1", "model", "a-1", "should-fix"), ("A.2", "model", "A.2", "should-fix"),
                          ("unmapped", "model", "a-9", "should-fix")])

    def test_a_clause_cited_in_the_text_is_attributed(self) -> None:
        out = self.ingest([review_item(summary="Breaks A.2 when the list is empty"),
                           review_item(line=2, summary="Fine", failure_scenario="a-1 is violated here"),
                           review_item(line=3, summary="No clause named")])
        self.assertEqual([(e["rule_id"], e["rule_id_source"]) for e in self.events()],
                         [("A.2", "attributed"), ("A.1", "attributed"), ("unmapped", "model")])
        self.assertIn("1 unmapped, 2 labeled from the text", out)


class SnippetHookTest(RepoCase):
    """`code_review_events` records a snippet only for a caller that supplies a `locate` function."""

    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules()
        config = load_config(self.repo, None, None, self.repo)
        self.build = lambda items, locate=None: code_review_events(
            items, config=config, rules=load_rules(config), source="l1", run_id="R1", verdict="PLAUSIBLE", sha="none",
            ts="2026-01-07T11:00:00", discovered_at=None, locate=locate).events

    def test_without_a_locate_function_a_finding_carries_neither_snippet_nor_source(self) -> None:
        (finding,) = self.build([review_item()])
        self.assertNotIn("snippet", finding)
        self.assertNotIn("snippet_source", finding)

    def test_the_snippet_and_where_it_came_from_are_recorded(self) -> None:
        (finding,) = self.build([review_item(file="src/a.py", line=10)], lambda file, line: (f"code at {file}:{line}", "review"))
        self.assertEqual((finding["snippet"], finding["snippet_source"]), ("code at src/a.py:10", "review"))

    def test_no_snippet_is_still_a_recorded_answer(self) -> None:
        (finding,) = self.build([review_item()], lambda file, line: (None, "unavailable"))
        self.assertEqual(finding["snippet_source"], "unavailable")
        self.assertNotIn("snippet", finding)

    def test_the_function_is_asked_once_per_finding_after_findings_on_one_line_collapse(self) -> None:
        asked = []

        def locate(file: str, line: int) -> tuple[str | None, str]:
            asked.append((file, line))
            return "code", "worktree"

        items = [review_item(file=" src/a.py ", line=3, severity="nice"), review_item(file="src/a.py", line=3, severity="blocker"),
                 review_item(file="src/b.py", line=4)]
        events = self.build(items, locate)
        self.assertEqual(asked, [("src/a.py", 3), ("src/b.py", 4)])
        self.assertEqual([(event["file"], event["snippet_source"]) for event in events], [("src/a.py", "worktree"), ("src/b.py", "worktree")])
        self.assertEqual(len(events[0]["also_matches"]), 1)


if __name__ == "__main__":
    unittest.main()
