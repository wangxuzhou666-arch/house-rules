import json
from pathlib import Path
import re
from unittest import mock
import unittest

from support import REPO_ROOT, RepoCase, make_config, review_finding
from ratchet.config import load_config
from ratchet.rules import parse_rules_text
from ratchet.errors import RatchetError
from ratchet.ledger import read_events
from ratchet.proposal_page import build_page
from ratchet.proposal_store import JOURNAL, encoded, fingerprint, recover, replace_local
from ratchet.proposals import (
    CHANGELOG, CHANGELOG_TITLE, SNIPPET_MAX_LINES, STATE, VERSION, _add_clause, apply_batch, create_batch, rollback_batch, statistics,
)


RULES = """# Example rules

1. **[P1.1]** Boolean names must start with is_. (should-fix)
2. **[R.1]** Use only the stated rules.
"""
REPLACEMENT = "1. **[P1.1]** Boolean names must convey boolean meaning; clear idioms need no prefix. (should-fix)"
APPLIED_AT = "2026-10-29T12:00:00"
BACKFILL = REPO_ROOT / CHANGELOG
BACKFILL_MARKER = "Backfilled on 2026-10-29 from the clause text; no decision receipt exists."
ENTRY_LABELS = re.compile(r"Before After Why( Evidence)+ Decision")
PLAIN_LOG = "# Coding style changes\r\n\r\n## Revision 1 — kept as written  \r\n\n\n".encode()  # CRLF, trailing blanks: restored as is


def entries(text: str) -> list[tuple[str, str]]:
    """(heading, bullet labels joined by spaces) per `###` entry. Fenced text is indented, so it never reads as a bullet."""
    found = []
    for block in re.split(r"^### ", text, flags=re.MULTILINE)[1:]:
        heading, _, body = block.partition("\n")
        found.append((heading, " ".join(re.findall(r"^- (Before|After|Why|Evidence|Decision):", body, flags=re.MULTILINE))))
    return found


class ProposalFixture(RepoCase):
    def setUp(self):
        super().setUp()
        self.write_rules(RULES)
        (self.repo / "reviewer.md").write_text("Before\n<!-- style-digest:begin -->\nold\n<!-- style-digest:end -->\nAfter\n")
        self.write_config(rules_files=["rules.md"], inject_targets=["reviewer.md"])
        self.cli_ok("compile")
        self.finding = review_finding(rule_id="P1.1", rule_code="P1.1", summary="The prefix rule flags a clear name.")
        self.append_raw(self.finding)
        self.config = load_config(self.repo, None, None, self.repo)

    def drafts(self, **overrides):
        return [{"action": "rewrite", "rule_id": "P1.1", "file": "rules.md", "after": REPLACEMENT,
                 "rationale": "Preserve clear idioms while rejecting ambiguous names.",
                 "evidence_ids": [self.finding["id"]], **overrides}]

    def batch(self, **overrides):
        return create_batch(self.config, self.events(), self.drafts(**overrides))

    def decision(self, batch, choice="accept", **extra):
        return {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test operator",
                "decisions": [{"id": "p1", "decision": choice, **extra}]}

    def apply(self, batch, choice="accept", **extra):
        return apply_batch(self.config, self.events(), self.decision(batch, choice, **extra))


class ProposalsTest(ProposalFixture):
    def test_cli_round_trip_updates_rules_digest_injected_prompt_and_keeps_ledger(self):
        before_ledger = self.ledger.read_bytes()
        draft_file = self.tmp / "drafts.json"
        draft_file.write_text(json.dumps(self.drafts()))
        output = self.cli_ok("distill", "--drafts", str(draft_file))
        batch_id = output.splitlines()[0].split(": ", 1)[1]
        batch = json.loads((self.repo / STATE / batch_id / "batch.json").read_text())
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        decision = json.dumps(self.decision(batch))
        self.cli_ok("apply-proposals", "--input", "-", stdin=decision)
        self.cli_ok("compile", "--check")
        for name in ["rules.md", "CODING-STYLE.digest.md", "reviewer.md"]:
            self.assertIn("Boolean names must convey", (self.repo / name).read_text())
        self.assertEqual(self.ledger.read_bytes(), before_ledger)
        self.assertIn("Revision 1", (self.repo / CHANGELOG).read_text())
        self.assertIn('"revision": 1', (self.repo / VERSION).read_text())
        self.assertIn("already_applied", self.cli_ok("apply-proposals", "--input", "-", stdin=decision))
        self.cli_ok("rollback-proposals", "--batch", batch_id)
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        self.assertFalse((self.repo / CHANGELOG).exists())
        self.cli_ok("compile", "--check")

    def test_readonly_distill_does_not_create_state_and_does_not_call_a_model(self):
        output = json.loads(self.cli_ok("distill"))
        self.assertEqual(output["groups"][0]["outcomes"], {"unlabeled": 1})
        self.assertFalse((self.repo / STATE).exists())

    def test_prepare_and_apply_dry_run_write_nothing(self):
        batch = create_batch(self.config, self.events(), self.drafts(), dry_run=True)
        self.assertFalse((self.repo / STATE).exists())
        batch = self.batch()
        before = {p: p.read_bytes() for p in (self.repo / STATE).rglob("*") if p.is_file()}
        result = apply_batch(self.config, self.events(), self.decision(batch), dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        self.assertEqual(before, {p: p.read_bytes() for p in (self.repo / STATE).rglob("*") if p.is_file()})

    def test_reject_does_not_change_any_rules_or_consumers(self):
        batch = self.batch()
        result = self.apply(batch, "reject")
        self.assertEqual(result["changed"], [])
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        self.assertFalse((self.repo / VERSION).exists())
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_rewrite_uses_the_humans_exact_text(self):
        batch = self.batch()
        text = "1. **[P1.1]** Prefer a boolean predicate except for established flags. (nice)"
        self.apply(batch, "rewrite", after=text)
        self.assertIn(text, (self.repo / "rules.md").read_text())
        self.assertIn("P1.1 | nice | Prefer a boolean predicate", (self.repo / "CODING-STYLE.digest.md").read_text())

    def test_missing_decision_is_not_approval(self):
        batch = self.batch()
        payload = self.decision(batch)
        payload["decisions"] = []
        with self.assertRaisesRegex(RatchetError, "every proposal"):
            apply_batch(self.config, self.events(), payload)
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)

    def test_unknown_evidence_cannot_create_a_proposal(self):
        with self.assertRaisesRegex(RatchetError, "evidence_ids"):
            self.batch(evidence_ids=["invented"])

    def test_foreign_file_and_parent_path_are_rejected(self):
        for name in ["../rules.md", "reviewer.md", ".env", "/tmp/rules.md"]:
            with self.subTest(name=name), self.assertRaises(RatchetError):
                self.batch(file=name)

    def test_symlinked_rules_are_rejected_without_reading_the_target(self):
        path = self.repo / "rules.md"
        path.unlink()
        path.symlink_to(self.tmp / "unreadable-target")
        with self.assertRaisesRegex(RatchetError, "symlink"):
            self.batch()

    def test_rule_edit_after_approval_page_makes_the_batch_stale(self):
        batch = self.batch()
        (self.repo / "rules.md").write_text(RULES + "\nChanged later\n")
        with self.assertRaisesRegex(RatchetError, "changed"):
            self.apply(batch)

    def test_evidence_outcome_change_requires_a_fresh_proposal(self):
        batch = self.batch()
        event = {"v": 1, "kind": "outcome", "id": self.finding["id"], "repo": "demo-repo",
                 "ts": "2026-01-08T11:00:00", "outcome": "false_positive", "outcome_by": "human", "evidence": "test"}
        self.append_raw(event)
        with self.assertRaisesRegex(RatchetError, "evidence changed"):
            self.apply(batch)

    def test_tampered_batch_and_wrong_id_are_rejected(self):
        batch = self.batch()
        path = self.repo / STATE / batch["id"] / "batch.json"
        batch["proposals"][0]["after"] = "different"
        path.write_text(json.dumps(batch))
        with self.assertRaisesRegex(RatchetError, "batch changed"):
            self.apply(batch)
        payload = self.decision(batch)
        payload["batch_id"] = "../unsafe"
        with self.assertRaises(RatchetError):
            apply_batch(self.config, self.events(), payload)

    def test_all_decisions_are_validated_before_any_write(self):
        batch = create_batch(self.config, self.events(), self.drafts() + self.drafts(
            rule_id="P1.2", action="add", after="- [P1.2] A second rule. (nice)"))
        payload = self.decision(batch)
        payload["decisions"].append({"id": "p2", "decision": "rewrite", "after": "- [X9.1] Wrong ID."})
        with self.assertRaises(RatchetError):
            apply_batch(self.config, self.events(), payload)
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)

    def test_retirement_reserves_the_id_and_does_not_renumber(self):
        batch = self.batch(action="retire", after="")
        self.apply(batch)
        text = (self.repo / "rules.md").read_text()
        self.assertIn("ratchet-retired: P1.1", text)
        self.assertIn("[R.1]", text)
        with self.assertRaisesRegex(RatchetError, "cannot be reused"):
            self.batch(action="add")

    def test_rollback_refuses_to_destroy_later_edits(self):
        batch = self.batch()
        self.apply(batch)
        path = self.repo / "rules.md"
        path.write_text(path.read_text() + "\nUser edit\n")
        with self.assertRaisesRegex(RatchetError, "later edit"):
            rollback_batch(self.config, batch["id"])

    def test_crash_after_a_partial_write_is_recoverable_and_blocks_next_apply(self):
        batch = self.batch()
        original = replace_local
        def failing(root, name, text):
            if name == "CODING-STYLE.digest.md":
                raise OSError("synthetic disk failure")
            return original(root, name, text)
        with mock.patch("ratchet.proposal_store.replace_local", side_effect=failing):
            with self.assertRaisesRegex(RatchetError, "interrupted"):
                self.apply(batch)
        self.assertTrue((self.repo / JOURNAL).exists())
        with self.assertRaisesRegex(RatchetError, "interrupted"):
            self.apply(batch)
        self.assertGreater(recover(self.repo, self.repo), 0)
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        self.apply(batch)
        self.cli_ok("compile", "--check")

    def test_recovery_does_not_overwrite_a_user_edit_after_the_crash(self):
        (self.repo / JOURNAL).parent.mkdir(parents=True)
        (self.repo / JOURNAL).write_text(encoded({"v": 1, "changes": {"rules.md": [RULES, "partial"]}}))
        (self.repo / "rules.md").write_text("later edit")
        with self.assertRaisesRegex(RatchetError, "later edit"):
            recover(self.repo, self.repo)

    def test_html_treats_findings_as_text_and_has_no_default_approval(self):
        batch = self.batch()
        batch["evidence"][self.finding["id"]]["finding"]["summary"] = '</script><script>alert("x")</script>'
        page = build_page(batch)
        self.assertNotIn('</script><script>alert', page)
        self.assertIn('\\u003c/script', page)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("['','请选择 / Choose']", page)


class PlacementTest(unittest.TestCase):
    """A new clause goes behind the last clause block of its family, numbered on if the family is an ordered list."""

    config = make_config()

    def add(self, text, rule_id, after):
        parsed = parse_rules_text(after, "draft", self.config.clause_id_pattern)
        return _add_clause(self.config, text, rule_id, after, parsed[rule_id].line)

    def test_a_clause_joins_its_own_family_and_not_the_end_of_the_file(self):
        text = "## A\n\n1. **[P0F.1]** a\n2. **[P0F.2]** b\n\n## B\n\n1. **[P2.1]** c\n\n## R\n\n1. **[R.1]** r\n"
        text = self.add(text, "P0F.3", "3. **[P0F.3]** new")
        text = self.add(text, "P2.2", "2. **[P2.2]** newer")
        self.assertEqual(text, "## A\n\n1. **[P0F.1]** a\n2. **[P0F.2]** b\n3. **[P0F.3]** new\n\n## B\n\n"
                               "1. **[P2.1]** c\n2. **[P2.2]** newer\n\n## R\n\n1. **[R.1]** r\n")
        self.assertEqual(list(parse_rules_text(text, "t", self.config.clause_id_pattern)), ["P0F.1", "P0F.2", "P0F.3", "P2.1", "P2.2", "R.1"])

    def test_a_family_that_is_not_in_the_file_gets_the_end_of_it(self):
        self.assertEqual(self.add("1. **[P1.1]** a\n", "Z.1", "1. **[Z.1]** z"), "1. **[P1.1]** a\n\n1. **[Z.1]** z\n")

    def test_the_block_of_the_last_clause_is_its_continuation_and_the_family_spacing_is_kept(self):
        loose = "1. **[PRJ.1]** One.\n   more\n\n2. **[PRJ.2]** Two.\n   more\n   - sub\n\n   after a blank\n\n## REL\n\n1. **[REL.1]** r\n"
        self.assertEqual(self.add(loose, "PRJ.3", "3. **[PRJ.3]** Three.\n   more"),
                         "1. **[PRJ.1]** One.\n   more\n\n2. **[PRJ.2]** Two.\n   more\n   - sub\n\n   after a blank\n\n"
                         "3. **[PRJ.3]** Three.\n   more\n\n## REL\n\n1. **[REL.1]** r\n")
        tight = "1. **[Q.1]** a\n   more\n2. **[Q.2]** b\n   more\n\nTrailing prose.\n"
        self.assertEqual(self.add(tight, "Q.3", "3. **[Q.3]** c"), "1. **[Q.1]** a\n   more\n2. **[Q.2]** b\n   more\n3. **[Q.3]** c\n\nTrailing prose.\n")
        self.assertEqual(self.add("1. **[Q.1]** a\n2. **[R.1]** r\n", "Q.2", "2. **[Q.2]** b"), "1. **[Q.1]** a\n2. **[Q.2]** b\n2. **[R.1]** r\n")  # a lone one-liner
        self.assertEqual(self.add("1. **[Q.1]** a\n   more\n2. **[R.1]** r\n", "Q.2", "2. **[Q.2]** b"), "1. **[Q.1]** a\n   more\n\n2. **[Q.2]** b\n2. **[R.1]** r\n")  # a lone block of two lines

    def test_an_ordered_family_numbers_the_new_clause_on_whatever_the_draft_carries(self):
        text = "7. **[P1.1]** a\n8. **[P1.2]** b\n"
        for draft in ("1. **[P1.3]** c", "- **[P1.3]** c", "**[P1.3]** c", "  42) **[P1.3]** c"):
            with self.subTest(draft=draft):
                self.assertEqual(self.add(text, "P1.3", draft).splitlines()[2].split(" ", 1)[0], "9.")
        self.assertEqual(self.add("- [P3.1] a\n", "P3.2", "1. [P3.2] b"), "- [P3.1] a\n1. [P3.2] b\n")  # a bullet family is left alone

    def test_a_heading_clause_keeps_its_body_and_a_table_row_stays_in_its_table(self):
        heading = "### [R.1] one\nbody\n\n### [R.2] two\nbody two\n\n```sh\n# not a heading\n```\nmore\n\n## Next\n"
        self.assertEqual(self.add(heading, "R.3", "### [R.3] three\nbody three"), heading.replace("\n## Next", "\n### [R.3] three\nbody three\n\n## Next"))
        table = "| ID | Rule |\n|---|---|\n| P2.1 | a |\n| P2.2 | b |\n\nafter\n"
        self.assertEqual(self.add(table, "P2.3", "| P2.3 | c |"), table.replace("| P2.2 | b |\n", "| P2.2 | b |\n| P2.3 | c |\n"))
        self.assertIn("| P2.2 | b |\n\n- [P2.3] c\n", self.add(table, "P2.3", "- [P2.3] c"))  # a list item is not a row: set apart

    def test_the_last_clause_of_a_file_without_a_final_newline_and_crlf_endings(self):
        self.assertEqual(self.add("1. **[P1.1]** a\n2. **[P1.2]** b", "P1.3", "3. **[P1.3]** c"), "1. **[P1.1]** a\n2. **[P1.2]** b\n3. **[P1.3]** c\n")
        self.assertEqual(self.add("1. **[P1.1]** a\r\n2. **[P1.2]** b\r\n\r\n## X\r\n", "P1.3", "3. **[P1.3]** c"),
                         "1. **[P1.1]** a\r\n2. **[P1.2]** b\r\n3. **[P1.3]** c\r\n\r\n## X\r\n")


class ChangelogTest(ProposalFixture):
    """The readable record of an applied batch, and its restoration with the rules."""

    def changelog(self) -> str:
        return (self.repo / CHANGELOG).read_text(encoding="utf-8")

    def files(self) -> dict[str, bytes | None]:
        names = ["rules.md", "CODING-STYLE.digest.md", "reviewer.md", CHANGELOG, VERSION]
        return {name: (self.repo / name).read_bytes() if (self.repo / name).exists() else None for name in names}

    def apply_payload(self, payload):
        with mock.patch("ratchet.proposals.now_iso", return_value=APPLIED_AT):
            return apply_batch(self.config, self.events(), payload)

    def apply_on(self, batch, choice="accept", **extra):
        return self.apply_payload(self.decision(batch, choice, **extra))

    def second_rule(self, **overrides):
        return self.drafts(action="add", rule_id="P1.2", after="- [P1.2] A second rule. (nice)", **overrides)

    def test_a_rewrite_entry_records_before_after_why_evidence_and_the_decision(self):
        batch = self.batch()
        self.apply_on(batch)
        self.assertEqual(self.changelog(), "\n".join([
            CHANGELOG_TITLE.rstrip("\n"), "",
            f"## Revision 1 — 2026-10-29 (batch {batch['id']})", "",
            "### rewrite P1.1", "",
            "- Before:", "  ```markdown", "  1. **[P1.1]** Boolean names must start with is_. (should-fix)", "  ```",
            "- After:", "  ```markdown", f"  {REPLACEMENT}", "  ```",
            "- Why: Preserve clear idioms while rejecting ambiguous names.",
            f"- Evidence: `{self.finding['id']}` — `src/a.py:10` — The prefix rule flags a clear name.",
            "- Decision: accept by Synthetic test operator", ""]))

    def test_a_dry_run_previews_the_entry_and_writes_nothing(self):
        preview = apply_batch(self.config, self.events(), self.decision(self.batch()), dry_run=True)
        self.assertIn("### rewrite P1.1", preview["changes"][CHANGELOG][1])
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_a_recorded_snippet_is_a_short_fenced_block_and_a_missing_one_adds_nothing(self):
        coded = review_finding(display_id="R1-002", summary="Second finding.", snippet="def f(flag):\n    return flag\n")
        self.append_raw(coded)
        self.apply_on(self.batch(evidence_ids=[self.finding["id"], coded["id"]]))
        plain = f"- Evidence: `{self.finding['id']}` — `src/a.py:10` — The prefix rule flags a clear name.\n"
        with_code = (f"- Evidence: `{coded['id']}` — `src/a.py:10` — Second finding.\n"
                     "  ```\n  def f(flag):\n      return flag\n  ```\n- Decision:")
        self.assertIn(plain + with_code, self.changelog())  # the first bullet is followed by the second, not by a fence

    def test_a_long_snippet_is_cut_and_a_fence_inside_it_cannot_close_the_block(self):
        lines = ["```", *(f"line {number}" for number in range(1, SNIPPET_MAX_LINES + 4))]
        tall = review_finding(display_id="R1-002", snippet="\n".join(lines))
        self.append_raw(tall)
        self.apply_on(self.batch(evidence_ids=[tall["id"]]))
        kept = [f"  {line}" for line in lines[:SNIPPET_MAX_LINES]]
        self.assertIn("\n".join(["  ````", *kept, "  ...", "  ````"]), self.changelog())

    def test_a_worktree_snippet_says_where_it_was_read_and_a_review_one_does_not(self):
        note = "  Snippet from the working tree when the review was imported; it may differ from the code the reviewer saw:"
        code = "def f(flag):\n    return flag\n"
        late = review_finding(display_id="R1-002", summary="Second finding.", snippet=code, snippet_source="worktree")
        seen = review_finding(display_id="R1-003", summary="Third finding.", snippet="x = 1\n", snippet_source="review")
        bare = review_finding(display_id="R1-004", summary="Fourth finding.", snippet_source="unavailable")
        self.append_raw(late, seen, bare)
        self.apply_on(self.batch(evidence_ids=[late["id"], seen["id"], bare["id"]]))
        text = self.changelog()
        self.assertIn(f"Second finding.\n{note}\n  ```\n  def f(flag):\n      return flag\n  ```\n- Evidence:", text)
        self.assertIn("Third finding.\n  ```\n  x = 1\n  ```\n- Evidence:", text)
        self.assertEqual(text.count(note), 1)
        self.assertIn("Fourth finding.\n- Decision:", text)

    def test_add_and_retire_show_a_placeholder_for_the_side_that_does_not_exist(self):
        drafts = [*self.drafts(action="retire", after=""), *self.second_rule()]
        batch = create_batch(self.config, self.events(), drafts)
        payload = {**self.decision(batch), "decisions": [{"id": "p1", "decision": "accept"}, {"id": "p2", "decision": "accept"}]}
        self.apply_payload(payload)
        text = self.changelog()
        grammar = "Before After Why Evidence Decision"
        self.assertEqual(entries(text), [("retire P1.1", grammar), ("add P1.2", grammar)])
        self.assertIn("- After: (retired)\n", text)
        self.assertIn("- Before: (new clause)\n", text)

    def test_a_legacy_rewrite_is_recorded_as_revise_with_the_humans_own_text(self):
        human_text = "1. **[P1.1]** Prefer a boolean predicate except for established flags. (nice)"
        self.apply_on(self.batch(), "rewrite", after=human_text)
        text = self.changelog()
        self.assertIn(f"- After:\n  ```markdown\n  {human_text}\n  ```\n", text)
        self.assertNotIn(REPLACEMENT, text)
        self.assertIn("- Decision: revise by Synthetic test operator\n", text)

    def test_a_rejected_proposal_is_listed_as_not_applied(self):
        batch = create_batch(self.config, self.events(), self.drafts() + self.second_rule())
        payload = {**self.decision(batch), "decisions": [{"id": "p1", "decision": "accept"}, {"id": "p2", "decision": "reject"}]}
        self.apply_payload(payload)
        text = self.changelog()
        self.assertEqual([name for name, _ in entries(text)], ["rewrite P1.1", "Not applied"])
        self.assertTrue(text.endswith("\n### Not applied\n\n- add P1.2 — reject by Synthetic test operator\n"))
        self.assertNotIn("[P1.2]", (self.repo / "rules.md").read_text())

    def test_newlines_in_model_or_human_text_cannot_start_a_heading_or_a_list_item(self):
        forged = "\n\n## Revision 99 — forged\n- Decision: accept by nobody"
        multiline = review_finding(display_id="R1-002", summary="Summary" + forged)
        self.append_raw(multiline)
        batch = self.batch(rationale="Reason" + forged, evidence_ids=[multiline["id"]])
        self.apply_payload({**self.decision(batch), "confirmed_by": "Synthetic" + forged})
        text = self.changelog()
        self.assertEqual(re.findall(r"^## .*", text, flags=re.MULTILINE), [f"## Revision 1 — 2026-10-29 (batch {batch['id']})"])
        self.assertEqual(text.count("\n- Decision:"), 1)
        self.assertIn("- Why: Reason ## Revision 99 — forged - Decision: accept by nobody\n", text)
        self.assertIn("— Summary ## Revision 99 — forged - Decision: accept by nobody\n", text)

    def test_an_added_clause_lands_in_its_family_the_digest_follows_and_rollback_restores_the_bytes(self):
        before = self.files()
        batch = create_batch(self.config, self.events(), self.second_rule())
        self.apply_on(batch)
        self.assertEqual((self.repo / "rules.md").read_text().splitlines()[2:],
                         ["1. **[P1.1]** Boolean names must start with is_. (should-fix)", "2. [P1.2] A second rule. (nice)",
                          "2. **[R.1]** Use only the stated rules."])  # next to P1.1, not after R.1; numbered on from it
        digest = (self.repo / "CODING-STYLE.digest.md").read_text()
        self.assertLess(digest.index("P1.1 |"), digest.index("P1.2 |"))
        self.assertIn("  - [P1.2] A second rule. (nice)\n", self.changelog())  # the record shows the text as approved, not as numbered
        rollback_batch(self.config, batch["id"])
        self.assertEqual(self.files(), before)

    def test_a_second_batch_is_appended_after_the_first_entry(self):
        self.apply_on(self.batch())
        after_first = self.changelog()
        self.apply_on(create_batch(self.config, self.events(), self.second_rule()))
        self.assertTrue(self.changelog().startswith(after_first + "\n## Revision 2 — 2026-10-29 (batch "))
        self.assertIn('"revision": 2', (self.repo / VERSION).read_text())

    def test_revision_numbers_continue_from_the_changelog_when_the_local_state_is_gone(self):
        # version.json lives under the ignored .ratchet/, the changelog is committed: a fresh clone has only the changelog.
        cases = {"this version's heading": ("## Revision 5 — 2026-10-01 (batch x)\n\n### add A.1\n", 6),
                 "an earlier version's heading": ("## Revision 3\n\nBatch `x`.\n\n- add A.1 (p1)\n", 4)}
        for label, (entry, expected) in cases.items():
            with self.subTest(label):
                (self.repo / CHANGELOG).write_text(CHANGELOG_TITLE + "\n" + entry, encoding="utf-8")
                batch = self.batch()
                self.apply_on(batch)
                self.assertEqual(re.findall(r"^## Revision (\d+)", self.changelog(), flags=re.MULTILINE)[-1], str(expected))
                self.assertIn(f'"revision": {expected}', (self.repo / VERSION).read_text())
                rollback_batch(self.config, batch["id"])

    def test_rollback_restores_the_changelog_byte_for_byte_with_the_rules_and_digest(self):
        (self.repo / CHANGELOG).write_bytes(PLAIN_LOG)
        before = self.files()
        batch = self.batch()
        self.apply_on(batch)
        self.assertNotEqual(self.files()[CHANGELOG], PLAIN_LOG)
        rollback_batch(self.config, batch["id"])
        self.assertEqual(self.files(), before)

    def test_rollback_removes_a_changelog_the_batch_created(self):
        batch = self.batch()
        self.apply_on(batch)
        self.assertTrue((self.repo / CHANGELOG).exists())
        rollback_batch(self.config, batch["id"])
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_rollback_refuses_after_a_later_hand_edit_of_the_changelog(self):
        batch = self.batch()
        self.apply_on(batch)
        path = self.repo / CHANGELOG
        path.write_text(path.read_text(encoding="utf-8") + "\nA note added by hand.\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "later edit: " + re.escape(CHANGELOG)):
            rollback_batch(self.config, batch["id"])
        self.assertIn("convey boolean meaning", (self.repo / "rules.md").read_text())

    def test_recovery_restores_the_changelog_with_everything_else_after_an_interrupted_apply(self):
        (self.repo / CHANGELOG).write_bytes(PLAIN_LOG)
        before = self.files()
        batch = self.batch()
        original = replace_local

        def failing(root, name, text):
            if name.endswith("receipt.json"):  # the last write: the changelog and the rules are already on disk
                raise OSError("synthetic disk failure")
            return original(root, name, text)

        with mock.patch("ratchet.proposal_store.replace_local", side_effect=failing):
            with self.assertRaisesRegex(RatchetError, "interrupted"):
                self.apply_on(batch)
        self.assertNotEqual(self.files()[CHANGELOG], PLAIN_LOG)
        recover(self.repo, self.repo)
        self.assertEqual(self.files(), before)

    def test_the_committed_backfill_is_in_the_format_new_entries_are_appended_to(self):
        changelog = BACKFILL.read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith(CHANGELOG_TITLE + "\n## "))
        self.assertTrue(changelog.endswith("\n") and not changelog.endswith("\n\n"))
        # Entries written later may follow the backfill: only the backfill section is pinned, the entry grammar is pinned everywhere.
        sections = [section for section in re.split(r"^## ", changelog, flags=re.MULTILINE) if section.startswith("Backfill")]
        self.assertEqual(len(sections), 1)
        backfill = sections[0]
        self.assertEqual([name for name, _ in entries(backfill)], ["add PRJ.1", "add PRJ.2", "add REL.1", "add REL.2", "rewrite R.7"])
        for name, labels in entries(changelog):
            if name != "Not applied":
                with self.subTest(entry=name):
                    self.assertIsNotNone(ENTRY_LABELS.fullmatch(labels), labels)
        decisions = re.findall(r"^- Decision: .*$", backfill, flags=re.MULTILINE)
        self.assertEqual(len(decisions), 5)
        self.assertTrue(all(line.endswith(BACKFILL_MARKER) for line in decisions))
        self.assertNotRegex(backfill, r"/(Users|home|private|tmp|var)/")  # the file is public: no local paths
        self.assertNotRegex(backfill, r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")  # and no commit hashes
        (self.repo / CHANGELOG).write_bytes(changelog.encode("utf-8"))
        next_revision = max([0, *(int(number) for number in re.findall(r"^## Revision (\d+)", changelog, flags=re.MULTILINE))]) + 1
        self.apply_on(self.batch())
        grown = self.changelog()
        self.assertTrue(grown.startswith(changelog + f"\n## Revision {next_revision} — 2026-10-29 (batch "))
        self.assertEqual([name for name, _ in entries(grown)], [name for name, _ in entries(changelog)] + ["rewrite P1.1"])


if __name__ == "__main__":
    unittest.main()
