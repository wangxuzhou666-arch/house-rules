import re
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase

from house_rules.onboard import TEMPLATE_PATH

DIGEST = "CODING-STYLE.digest.md"
FAMILIES = ("P0E", "P0F", "P1", "P2", "P3", "R")
CLAUSE_LINE_RE = re.compile(r"^\d+\. \*\*\[(?P<id>[A-Z0-9]+\.\d+)\]\*\* \S")


class TemplateTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "CODING-STYLE.md").write_bytes(TEMPLATE_PATH.read_bytes())
        self.write_config(rules_files=["CODING-STYLE.md"])
        self.template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def digest(self) -> str:
        return (self.repo / DIGEST).read_text(encoding="utf-8")

    def test_it_compiles_cleanly_with_strict(self) -> None:
        code, out, err = self.cli("compile", "--strict")
        self.assertEqual((code, err), (0, ""), out)

    def test_the_digest_fits_the_default_budget_with_room_to_grow(self) -> None:
        self.cli_ok("compile")
        size = len((self.repo / DIGEST).read_bytes())
        self.assertLessEqual(size, 4096)
        self.assertLess(size, 3600, "keep headroom so users can add clauses before hitting the budget")

    def test_every_family_has_clauses_and_the_severity_table_maps_the_violation_families(self) -> None:
        self.cli_ok("compile")
        ids = re.findall(r"^(\S+) \| ", self.digest(), flags=re.MULTILINE)
        self.assertEqual({clause.split(".")[0] for clause in ids if "." in clause}, set(FAMILIES))
        violations = self.digest().split("## Violations\n", 1)[1].split("\n\n", 1)[0].splitlines()
        reviewer = self.digest().split("## Reviewer rules\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual({line.split(".")[0] for line in violations}, {"P0E", "P0F", "P1", "P2", "P3"})
        self.assertEqual({line.split(".")[0] for line in reviewer}, {"R"})

    def test_severities_follow_the_family_table(self) -> None:
        self.cli_ok("compile")
        expected = {"P0E": "blocker", "P0F": "blocker", "P1": "should-fix", "P2": "should-fix", "P3": "nice"}
        for line in self.digest().split("## Violations\n", 1)[1].split("\n\n", 1)[0].splitlines():
            clause, severity, _ = line.split(" | ", 2)
            if clause == "P1.2":
                self.assertEqual(severity, "nice", "the advisory complexity clause carries its own tag")
            else:
                self.assertEqual(severity, expected[clause.split(".")[0]], clause)

    def test_every_clause_is_written_as_a_numbered_bold_bracketed_id(self) -> None:
        lines = [line for line in self.template.splitlines() if re.match(r"^\d+\. \*\*\[", line)]
        self.assertGreaterEqual(len(lines), 25)
        for line in lines:
            self.assertRegex(line, CLAUSE_LINE_RE.pattern)

    def test_every_knob_is_marked_adjust(self) -> None:
        adjust = re.findall(r"<!-- ADJUST:[^>]*-->", self.template)
        self.assertGreaterEqual(len(adjust), 12)
        for knob in ("depth threshold", "thresholds", "legal-case whitelist", "family list", "family-to-severity table",
                     "banned-name list", "tool code", "language"):
            self.assertTrue(any(knob in comment for comment in adjust), f"no ADJUST comment about the {knob}")

    def test_a_clause_with_a_threshold_or_tool_code_sits_next_to_an_adjust_comment(self) -> None:
        for clause in ("P0E.1", "P0E.3", "P0E.4", "P0E.5", "P0E.6", "P1.1", "P1.2", "P2.4", "P3.3", "R.4"):
            line = next(line for line in self.template.splitlines() if re.match(rf"^\d+\. \*\*\[{re.escape(clause)}\]", line))
            self.assertIn("ADJUST", line, clause)

    def test_it_is_labelled_a_draft(self) -> None:
        self.assertIn("DRAFT", self.template.splitlines()[0])

    def test_no_project_specific_leftovers(self) -> None:
        self.assertIsNone(re.search(r"[　-〿㐀-鿿＀-￯]", self.template), "non-English text")
        self.assertIsNone(re.search(r"\b\d{4}-\d{2}-\d{2}\b", self.template), "a date")
        self.assertIsNone(re.search(r"\b[0-9a-f]{8,40}\b", self.template), "a commit hash")
        self.assertIsNone(re.search(r"https?://", self.template), "a link")
        self.assertIsNone(re.search(r"(?i)\bfixme\b", self.template), "a fixme marker")
        self.assertIsNone(re.search(r"\b\w+\.(py|ts|tsx|js|sh|md)\b", self.template.replace("`CODING-STYLE.md`", "")
                                    .replace("CODING-STYLE.md", "")), "a file name")

    def test_it_is_stable_under_a_second_compile(self) -> None:
        self.cli_ok("compile")
        self.assertEqual(self.cli("compile", "--check")[0], 0)

    def test_every_clause_is_usable_as_a_category_for_ingest(self) -> None:
        self.cli_ok("compile")
        from house_rules.config import load_config
        from house_rules.rules import load_rules
        rules = load_rules(load_config(self.repo, None, None, self.repo))
        for clause in rules.clauses:
            self.assertEqual(rules.from_kebab(clause.lower()), clause)


if __name__ == "__main__":
    unittest.main()
