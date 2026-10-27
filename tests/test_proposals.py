import json
from pathlib import Path
from unittest import mock
import unittest

from support import RepoCase, review_finding
from ratchet.config import load_config
from ratchet.errors import RatchetError
from ratchet.ledger import read_events
from ratchet.proposal_page import build_page
from ratchet.proposal_store import JOURNAL, encoded, fingerprint, recover, replace_local
from ratchet.proposals import (
    CHANGELOG, STATE, VERSION, apply_batch, create_batch, rollback_batch, statistics,
)


RULES = """# Example rules

1. **[P1.1]** Boolean names must start with is_. (should-fix)
2. **[R.1]** Use only the stated rules.
"""
REPLACEMENT = "1. **[P1.1]** Boolean names must convey boolean meaning; clear idioms need no prefix. (should-fix)"


class ProposalsTest(RepoCase):
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
        self.assertGreater(recover(self.repo), 0)
        self.assertEqual((self.repo / "rules.md").read_text(), RULES)
        self.apply(batch)
        self.cli_ok("compile", "--check")

    def test_recovery_does_not_overwrite_a_user_edit_after_the_crash(self):
        (self.repo / JOURNAL).parent.mkdir(parents=True)
        (self.repo / JOURNAL).write_text(encoded({"v": 1, "changes": {"rules.md": [RULES, "partial"]}}))
        (self.repo / "rules.md").write_text("later edit")
        with self.assertRaisesRegex(RatchetError, "later edit"):
            recover(self.repo)

    def test_html_treats_findings_as_text_and_has_no_default_approval(self):
        batch = self.batch()
        batch["evidence"][self.finding["id"]]["finding"]["summary"] = '</script><script>alert("x")</script>'
        page = build_page(batch)
        self.assertNotIn('</script><script>alert', page)
        self.assertIn('\\u003c/script', page)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("['','请选择 / Choose']", page)


if __name__ == "__main__":
    unittest.main()
