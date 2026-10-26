import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts the repo root on sys.path)
from support import FIXTURES, SAMPLE_CLAUSES, SAMPLE_RULES, SEVERITY_TABLE, STYLE_RULES, make_config

from ratchet.config import DEFAULT_CLAUSE_ID_PATTERN
from ratchet.errors import RatchetError
from ratchet.rules import category_of, family_of, load_rules, parse_rules_text, parse_severity_table


class ParseRulesTest(unittest.TestCase):
    def parse(self, text: str, pattern: str = DEFAULT_CLAUSE_ID_PATTERN) -> dict:
        return parse_rules_text(text, "rules.md", pattern)

    def test_finds_bracketed_and_table_definitions(self) -> None:
        self.assertEqual(set(self.parse(SAMPLE_RULES)), SAMPLE_CLAUSES)

    def test_ignores_cross_references_and_code_fences(self) -> None:
        found = self.parse(SAMPLE_RULES)
        self.assertNotIn("X9.9", found)
        self.assertEqual(found["P0E.1"].line, 5)  # the defining line, not the later "See [P0E.1]" line

    def test_reads_severity_tags_from_the_defining_line(self) -> None:
        severities = {clause_id: clause.tag for clause_id, clause in self.parse(SAMPLE_RULES).items()}
        self.assertEqual(severities["P0E.1"], "blocker")
        self.assertEqual(severities["P3.1"], "nice")
        self.assertEqual(severities["P2.3"], "should-fix")  # table cell
        self.assertIsNone(severities["P1.1"])  # untagged

    def test_severity_notations(self) -> None:
        text = ("- [P1.1] one [should-fix]\n- [P1.2] two `nice`\n- [P1.3] three severity: blocker\n"
                "- [P1.4] this is a nice thing to have\n")
        severities = {clause_id: clause.tag for clause_id, clause in self.parse(text).items()}
        self.assertEqual(severities, {"P1.1": "should-fix", "P1.2": "nice", "P1.3": "blocker", "P1.4": None})

    def test_duplicate_definition_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "already defined"):
            self.parse("1. **[P1.1]** a\n2. **[P1.1]** b\n")

    def test_conflicting_severity_tags_are_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "conflicting severity"):
            self.parse("1. **[P1.1]** a (blocker) (nice)\n")

    def test_custom_id_pattern(self) -> None:
        found = self.parse("- [RULE-12] custom notation (nice)\n- [P0E.1] not matched by the custom pattern\n", r"RULE-\d+")
        self.assertEqual(set(found), {"RULE-12"})


class LoadRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.rules_path = self.dir / "rules.md"
        self.rules_path.write_text(SAMPLE_RULES, encoding="utf-8")

    def rules(self, **overrides: object):
        return load_rules(make_config((self.rules_path,), **overrides))

    def test_known_clauses_and_severity(self) -> None:
        rules = self.rules()
        self.assertTrue(rules.is_known_clause("P0E.6"))
        self.assertFalse(rules.is_known_clause("P9.9"))
        self.assertEqual(rules.severity_of("P0E.6"), "blocker")
        self.assertIsNone(rules.severity_of("P1.1"))

    def test_kebab_matches_known_ids_case_insensitively(self) -> None:
        rules = self.rules()
        self.assertEqual(rules.from_kebab("p0e-6"), "P0E.6")
        self.assertEqual(rules.from_kebab("P0E-6"), "P0E.6")
        self.assertEqual(rules.from_kebab(" r-7 "), "R.7")
        self.assertEqual(rules.from_kebab("p0e.6"), "P0E.6")
        self.assertIsNone(rules.from_kebab("p0e-9"))
        self.assertIsNone(rules.from_kebab("exception-handling"))
        self.assertIsNone(rules.from_kebab(""))

    def test_resolve_reported_accepts_the_three_forms(self) -> None:
        rules = self.rules(tool_rule_map={"ruff:F811": "P2.3"})
        self.assertEqual(rules.resolve_reported("P0E.1"), ("P0E.1", None))
        self.assertEqual(rules.resolve_reported("unmapped"), ("unmapped", None))
        self.assertEqual(rules.resolve_reported("eslint:no-undef"), ("eslint:no-undef", "tool"))
        self.assertEqual(rules.resolve_reported("ruff:F811"), ("P2.3", "tool"))

    def test_unknown_clause_is_rejected_not_coerced(self) -> None:
        rules = self.rules()
        for bad in ("P9.9", "p0e.1", "R", "whatever"):
            with self.assertRaisesRegex(RatchetError, "unknown rule_id"):
                rules.resolve_reported(bad)

    def test_tool_rule_map_must_point_at_a_known_clause(self) -> None:
        with self.assertRaisesRegex(RatchetError, "no rules file defines"):
            self.rules(tool_rule_map={"ruff:F811": "P9.9"})

    def test_scenarios_keys_are_checked(self) -> None:
        with self.assertRaisesRegex(RatchetError, "scenarios key"):
            self.rules(scenarios={"nonsense": "text"})

    def test_rules_file_without_clauses_is_an_error(self) -> None:
        self.rules_path.write_text("# nothing defined here\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "no clause definitions"):
            self.rules()

    def test_missing_rules_file_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "cannot read rules file"):
            load_rules(make_config((self.dir / "absent.md",)))

    def test_same_clause_in_two_files_is_an_error(self) -> None:
        second = self.dir / "more.md"
        second.write_text("1. **[P1.1]** again\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "already defined in"):
            load_rules(make_config((self.rules_path, second)))

    def test_no_rules_files_means_no_known_clauses(self) -> None:
        rules = load_rules(make_config(()))
        self.assertFalse(rules.is_known_clause("P0E.1"))
        self.assertEqual(rules.resolve_reported("ruff:E501"), ("ruff:E501", "tool"))
        with self.assertRaisesRegex(RatchetError, "no rules_files configured"):
            rules.resolve_reported("P0E.1")

    def test_category_labels(self) -> None:
        self.assertEqual(category_of("P0E.3"), "P0E")
        self.assertEqual(category_of("R.7"), "R")
        self.assertEqual(category_of("ruff:F811"), "ruff")
        self.assertEqual(category_of("unmapped"), "unmapped")


class ClauseTextTest(unittest.TestCase):
    """What the parser keeps from a defining line besides the id: the text `ratchet compile` shortens."""

    def clause(self, line: str):
        (clause,) = parse_rules_text(line + "\n", "rules.md", DEFAULT_CLAUSE_ID_PATTERN).values()
        return clause

    def test_text_follows_the_closing_bold_marker(self) -> None:
        self.assertEqual(self.clause("1. **[P1.1]** Keep it flat.").text, "Keep it flat.")

    def test_text_of_a_bullet_and_a_heading(self) -> None:
        self.assertEqual(self.clause("- [P1.1] Keep it flat.").text, "Keep it flat.")
        self.assertEqual(self.clause("### [R.7] A heading title").text, "A heading title")

    def test_table_row_text_drops_the_id_and_severity_cells(self) -> None:
        self.assertEqual(self.clause("| P2.3 | should-fix | No config layer for a one-off need |").text,
                         "No config layer for a one-off need")

    def test_severity_tags_do_not_stay_in_the_text(self) -> None:
        self.assertEqual(self.clause("1. **[P1.1]** Keep it flat (blocker)").text, "Keep it flat")
        self.assertEqual(self.clause("1. **[P1.1]** Keep it flat. severity: nice").text, "Keep it flat.")

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(self.clause("1. **[P1.1]**   too    many   spaces  ").text, "too many spaces")

    def test_digest_override_comment_is_extracted_and_removed_from_the_text(self) -> None:
        clause = self.clause("1. **[P1.1]** Long original wording. <!-- digest: Short form --> more")
        self.assertEqual(clause.digest_override, "Short form")
        self.assertEqual(clause.text, "Long original wording. more")

    def test_other_html_comments_are_removed_but_are_not_overrides(self) -> None:
        clause = self.clause("1. **[P1.1]** Keep it flat. <!-- reviewed 2026 -->")
        self.assertIsNone(clause.digest_override)
        self.assertEqual(clause.text, "Keep it flat.")

    def test_override_must_be_non_empty(self) -> None:
        self.assertIsNone(self.clause("1. **[P1.1]** Keep it flat. <!-- digest: -->").digest_override)

    def test_cjk_text_is_kept_as_is(self) -> None:
        text = (FIXTURES / "rules_cjk.md").read_text(encoding="utf-8")
        clauses = parse_rules_text(text, "rules_cjk.md", DEFAULT_CLAUSE_ID_PATTERN)
        self.assertEqual(set(clauses), {"P0E.1", "P1.1", "P1.2", "R.1", "R.2"})
        self.assertFalse(clauses["P0E.1"].text.isascii())
        self.assertFalse(clauses["P0E.1"].text.startswith("*"))  # the bold closer is not part of the text


class SeverityTableTest(unittest.TestCase):
    def parse(self, text: str) -> dict[str, str]:
        return {family: severity for family, (severity, _) in parse_severity_table(text, "rules.md").items()}

    def test_families_map_to_severities(self) -> None:
        self.assertEqual(self.parse(SEVERITY_TABLE),
                         {"P0E": "blocker", "P0F": "blocker", "P1": "should-fix", "P2": "should-fix", "P3": "nice"})

    def test_header_and_severity_words_are_case_insensitive(self) -> None:
        text = "| Family | SEVERITY |\n|:--|--:|\n| P0E | Blocker |\n"
        self.assertEqual(self.parse(text), {"P0E": "blocker"})

    def test_missing_table_means_no_severities(self) -> None:
        self.assertEqual(self.parse("1. **[P1.1]** no table in this file\n"), {})

    def test_other_tables_are_ignored(self) -> None:
        self.assertEqual(self.parse(SAMPLE_RULES), {})  # its table has the columns ID, Severity, Rule

    def test_a_table_inside_a_code_fence_is_ignored(self) -> None:
        self.assertEqual(self.parse("```\n" + SEVERITY_TABLE + "```\n"), {})

    def test_table_found_among_prose_and_clauses(self) -> None:
        self.assertEqual(self.parse("intro\n\n" + STYLE_RULES + "\ntrailing text\n")["P3"], "nice")

    def test_family_cells_tolerate_backticks_and_spacing(self) -> None:
        text = "| family | severity |\n|---|---|\n| `P0E` ,  P0F | `blocker` |\n"
        self.assertEqual(self.parse(text), {"P0E": "blocker", "P0F": "blocker"})

    def test_cjk_prose_around_the_table_does_not_matter(self) -> None:
        text = (FIXTURES / "rules_cjk.md").read_text(encoding="utf-8")
        self.assertEqual(self.parse(text), {"P0E": "blocker", "P1": "should-fix"})

    def test_header_without_a_separator_row_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "separator row"):
            self.parse("| family | severity |\n| P0E | blocker |\n")

    def test_unknown_severity_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "rules.md:3: severity 'critical'"):
            self.parse("| family | severity |\n|---|---|\n| P0E | critical |\n")

    def test_row_with_the_wrong_cell_count_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "two cells"):
            self.parse("| family | severity |\n|---|---|\n| P0E | blocker | extra |\n")

    def test_empty_family_name_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "empty family"):
            self.parse("| family | severity |\n|---|---|\n| P0E, | blocker |\n")

    def test_same_family_twice_with_different_severities_is_an_error(self) -> None:
        with self.assertRaisesRegex(RatchetError, "family P1 is nice here but should-fix"):
            self.parse("| family | severity |\n|---|---|\n| P1 | should-fix |\n| P1 | nice |\n")

    def test_same_family_twice_with_the_same_severity_is_fine(self) -> None:
        self.assertEqual(self.parse("| family | severity |\n|---|---|\n| P1 | nice |\n| P1 | nice |\n"), {"P1": "nice"})

    def test_two_tables_in_one_file_conflict_when_they_disagree(self) -> None:
        text = SEVERITY_TABLE + "\nbetween\n\n" + SEVERITY_TABLE.replace("| P3 | nice |", "| P3 | blocker |")
        with self.assertRaisesRegex(RatchetError, "family P3 is blocker here but nice"):
            self.parse(text)

    def test_family_of_is_the_part_before_the_dot(self) -> None:
        self.assertEqual((family_of("P0E.6"), family_of("R.7"), family_of("RULE-12")), ("P0E", "R", "RULE-12"))


class LayeredRulesTest(unittest.TestCase):
    """rules_files are layers: global first, project last. A later file appends; it never overrides."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def rules(self, *paths: Path):
        return load_rules(make_config(tuple(paths)))

    def test_a_later_file_may_add_clauses_and_keeps_file_order(self) -> None:
        base = self.write("global.md", STYLE_RULES)
        extra = self.write("project.md", "1. **[P1.9]** Project-only nesting rule.\n2. **[X.1]** Project-only extra.\n")
        rules = self.rules(base, extra)
        self.assertEqual(list(rules.clauses), ["P0E.1", "P0E.2", "P0F.1", "P1.1", "P2.1", "P3.1", "R.1", "R.2", "P1.9", "X.1"])
        self.assertEqual([source.path for source in rules.sources], [base, extra])

    def test_redefining_a_clause_in_a_later_file_is_an_error(self) -> None:
        base = self.write("global.md", STYLE_RULES)
        override = self.write("project.md", "1. **[P1.1]** Loosened: depth of six is fine.\n")
        with self.assertRaisesRegex(RatchetError, "project.md:1: clause P1.1 is already defined in .*global.md"):
            self.rules(base, override)

    def test_a_table_in_the_later_file_may_add_families(self) -> None:
        base = self.write("global.md", STYLE_RULES)
        extra = self.write("project.md", "1. **[X.1]** Project-only extra.\n\n| family | severity |\n|---|---|\n| X | blocker |\n")
        rules = self.rules(base, extra)
        self.assertEqual((rules.severity_of("X.1"), rules.severity_of("P3.1")), ("blocker", "nice"))

    def test_a_table_in_the_later_file_may_repeat_an_agreeing_row(self) -> None:
        base = self.write("global.md", STYLE_RULES)
        extra = self.write("project.md", "1. **[X.1]** Extra.\n\n| family | severity |\n|---|---|\n| P3 | nice |\n")
        self.assertEqual(self.rules(base, extra).severity_of("P3.1"), "nice")

    def test_conflicting_tables_across_files_are_an_error(self) -> None:
        base = self.write("global.md", STYLE_RULES)
        extra = self.write("project.md", "1. **[X.1]** Extra.\n\n| family | severity |\n|---|---|\n| P3 | blocker |\n")
        with self.assertRaisesRegex(RatchetError, "family P3 is blocker here but nice at .*global.md"):
            self.rules(base, extra)

    def test_a_table_may_live_in_a_file_other_than_the_one_with_the_clauses(self) -> None:
        clauses = self.write("clauses.md", "1. **[P1.1]** Nesting.\n")
        table = self.write("table.md", "1. **[X.1]** Extra.\n\n" + SEVERITY_TABLE)
        self.assertEqual(self.rules(clauses, table).severity_of("P1.1"), "should-fix")

    def test_sources_carry_the_sha256_prefix_of_each_file(self) -> None:
        import hashlib
        base = self.write("global.md", STYLE_RULES)
        (source,) = self.rules(base).sources
        self.assertEqual(source.sha12, hashlib.sha256(base.read_bytes()).hexdigest()[:12])


class EffectiveSeverityTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def rules(self, text: str):
        path = self.dir / "rules.md"
        path.write_text(text, encoding="utf-8")
        return load_rules(make_config((path,)))

    def test_clause_severity_is_its_family_severity(self) -> None:
        rules = self.rules(STYLE_RULES)
        self.assertEqual([rules.severity_of(c) for c in ("P0E.1", "P0F.1", "P1.1", "P2.1", "P3.1")],
                         ["blocker", "blocker", "should-fix", "should-fix", "nice"])

    def test_families_outside_the_table_have_no_severity(self) -> None:
        self.assertIsNone(self.rules(STYLE_RULES).severity_of("R.1"))

    def test_without_a_table_nothing_has_a_severity(self) -> None:
        rules = self.rules("1. **[P1.1]** one\n2. **[R.1]** two\n")
        self.assertEqual((rules.severity_of("P1.1"), rules.severity_of("R.1")), (None, None))

    def test_the_family_match_is_exact_not_a_prefix(self) -> None:
        rules = self.rules("1. **[P0E.1]** one\n2. **[P1.1]** two\n3. **[P10.1]** three\n\n"
                           "| family | severity |\n|---|---|\n| P1 | nice |\n| P0 | blocker |\n")
        self.assertEqual([rules.severity_of(c) for c in ("P0E.1", "P1.1", "P10.1")], [None, "nice", None])

    def test_a_tag_on_the_clause_line_overrides_its_family(self) -> None:
        rules = self.rules("1. **[P3.1]** one (blocker)\n2. **[P3.2]** two\n\n" + SEVERITY_TABLE)
        self.assertEqual((rules.severity_of("P3.1"), rules.severity_of("P3.2")), ("blocker", "nice"))

    def test_unknown_ids_have_no_severity(self) -> None:
        rules = self.rules(STYLE_RULES)
        self.assertIsNone(rules.severity_of("ruff:F811"))
        self.assertIsNone(rules.severity_of("unmapped"))


if __name__ == "__main__":
    unittest.main()
