import copy
import json
from unittest import mock

from support import RepoCase, review_finding
from house_rules.config import load_config
from house_rules.decision_context import build_context
from house_rules.errors import HouseRulesError
from house_rules.native_decisions import apply_answers, prepare, summary
from house_rules.proposal_store import read_json
from house_rules.proposals import CHANGELOG, STATE, apply_batch, rollback_batch

APPLIED_AT = "2026-10-29T12:00:00"


class NativeDecisionsTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        (self.repo / "source.py").write_text("raise ValueError('invalid')\n")
        self.finding = review_finding(file="source.py", line=1, rule_id="unmapped")
        self.append_raw(self.finding)
        self.config = load_config(self.repo, None, None, self.repo)

    def assessment(self, action="add"):
        context = build_context(self.config, self.events())
        changes = [{"action": "add", "rule_id": "REL.1", "file": "rules.md",
                    "after": "- [REL.1] Preserve a usable copy when replacement fails. (should-fix)"}]
        return {"v": 1, "context_id": context["context_id"], "assessments": [
            {"finding_ids": [self.finding["id"]], "verification": {"status": "supported", "basis": "Synthetic inspected example."},
             "action": action, "rule_ids": [], "reason": "A distinct failure mechanism needs a rule.",
             "changes": changes if action == "add" else []}]}

    def batch(self, action="add"):
        return prepare(self.config, self.events(), self.assessment(action))

    def answer(self, batch, choice="accept"):
        return {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test human", "answers": [
            {"id": "a1", "decision": choice, "response": f"Synthetic explicit {choice}"}]}

    def changelog(self) -> str:
        return (self.repo / CHANGELOG).read_text(encoding="utf-8")

    def apply_on(self, payload: dict) -> dict:
        with mock.patch("house_rules.proposals.now_iso", return_value=APPLIED_AT):
            return apply_answers(self.config, self.events(), payload)

    def four_groups(self):
        """One `add` proposal per finding, each its own group: REL.1 ... REL.4 as p1 ... p4."""
        findings = [self.finding, *(review_finding(display_id=f"R1-00{n}", file="source.py", line=1, rule_id="unmapped")
                                    for n in range(2, 5))]
        self.append_raw(*findings[1:])
        assessments = [{"finding_ids": [item["id"]], "verification": {"status": "supported", "basis": "Synthetic inspected example."},
                        "action": "add", "rule_ids": [], "reason": f"Synthetic reason {n}.",
                        "changes": [{"action": "add", "rule_id": f"REL.{n}", "file": "rules.md",
                                     "after": f"- [REL.{n}] Synthetic rule {n}. (should-fix)"}]}
                       for n, item in enumerate(findings, 1)]
        context = build_context(self.config, self.events())
        return prepare(self.config, self.events(), {"v": 1, "context_id": context["context_id"], "assessments": assessments})

    def test_native_batch_contains_sources_and_creates_no_html(self):
        batch = self.batch()
        saved = read_json(self.repo, f"{STATE}/{batch['id']}/batch.json")
        self.assertEqual(saved["native"]["context"]["findings"][0]["source"]["text"], "raise ValueError('invalid')")
        self.assertEqual(saved["native"]["groups"][0]["proposal_ids"], ["p1"])
        self.assertFalse((self.repo / STATE / batch["id"] / "review.html").exists())

    def test_native_accept_updates_rules_and_records_actual_response_and_context(self):
        batch = self.batch()
        payload = self.answer(batch)
        before = self.ledger_bytes()
        apply_answers(self.config, self.events(), payload)
        self.cli_ok("compile", "--check", "--strict")
        receipt = read_json(self.repo, f"{STATE}/{batch['id']}/receipt.json")
        self.assertEqual(receipt["answers"], payload["answers"])
        self.assertEqual(receipt["context_id"], batch["native"]["context"]["context_id"])
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(apply_answers(self.config, self.events(), payload)["status"], "already_applied")
        self.assertIn("### add REL.1", self.changelog())
        rollback_batch(self.config, batch["id"])
        self.assertNotIn("[REL.1]", (self.repo / "rules.md").read_text())
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_reject_defer_and_revise_are_durable_without_rule_mutation(self):
        before = (self.repo / "rules.md").read_bytes()
        for choice in ("reject", "defer", "revise"):
            batch = self.batch()
            apply_answers(self.config, self.events(), self.answer(batch, choice))
            receipt = read_json(self.repo, f"{STATE}/{batch['id']}/receipt.json")
            self.assertEqual(receipt["answers"][0]["decision"], choice)
            self.assertEqual((self.repo / "rules.md").read_bytes(), before)
            self.assertFalse((self.repo / CHANGELOG).exists())

    def test_keep_has_a_human_receipt_even_without_any_mutation_proposal(self):
        batch = self.batch("keep")
        self.assertEqual(batch["proposals"], [])
        result = apply_answers(self.config, self.events(), self.answer(batch))
        self.assertEqual(result["changed"], [])
        self.assertTrue((self.repo / STATE / batch["id"] / "receipt.json").exists())
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_no_default_empty_or_missing_answer_authorizes_writes(self):
        batch = self.batch()
        for choice in ("", "supported", "rewrite"):
            with self.assertRaises(HouseRulesError):
                apply_answers(self.config, self.events(), self.answer(batch, choice))
        payload = self.answer(batch)
        payload["answers"][0]["response"] = ""
        with self.assertRaises(HouseRulesError):
            apply_answers(self.config, self.events(), payload)
        payload["answers"] = []
        with self.assertRaises(HouseRulesError):
            apply_answers(self.config, self.events(), payload)

    def test_current_code_changes_invalidate_the_approval(self):
        batch = self.batch()
        (self.repo / "source.py").write_text("different code\n")
        with self.assertRaisesRegex(HouseRulesError, "source code changed"):
            apply_answers(self.config, self.events(), self.answer(batch))
        self.assertNotIn("[REL.1]", (self.repo / "rules.md").read_text())

    def test_unavailable_selected_range_can_be_deferred_without_false_drift(self):
        locations = {self.finding["id"]: (20, 21)}
        assessment = self.assessment("keep")
        assessment["context_id"] = build_context(self.config, self.events(), locations=locations)["context_id"]
        batch = prepare(self.config, self.events(), assessment, locations=locations)
        before = (self.repo / "rules.md").read_bytes()
        result = apply_answers(self.config, self.events(), self.answer(batch, "defer"))
        self.assertEqual(result["answer_counts"], {"defer": 1})
        self.assertEqual((self.repo / "rules.md").read_bytes(), before)
        receipt = read_json(self.repo, f"{STATE}/{batch['id']}/receipt.json")
        self.assertEqual(receipt["answers"][0]["decision"], "defer")

    def test_previously_unavailable_selected_range_still_detects_real_drift(self):
        locations = {self.finding["id"]: (20, 21)}
        assessment = self.assessment("keep")
        assessment["context_id"] = build_context(self.config, self.events(), locations=locations)["context_id"]
        batch = prepare(self.config, self.events(), assessment, locations=locations)
        (self.repo / "source.py").write_text("new code\n" * 21)
        with self.assertRaisesRegex(HouseRulesError, "source code changed"):
            apply_answers(self.config, self.events(), self.answer(batch, "defer"))

    def test_native_approval_cannot_be_bypassed_through_legacy_command(self):
        batch = self.batch()
        payload = self.answer(batch)
        payload.pop("answers")
        payload["decisions"] = [{"id": "p1", "decision": "accept"}]
        with self.assertRaises(HouseRulesError):
            apply_batch(self.config, self.events(), payload)

    def test_merge_is_one_decision_and_partial_acceptance_is_rejected(self):
        assessment = self.assessment()
        item = assessment["assessments"][0]
        item.update(action="merge", rule_ids=["P0E.1", "P0E.6"], changes=[
            {"action": "rewrite", "rule_id": "P0E.1", "file": "rules.md",
             "after": "- [P0E.1] Guard just the expected operation and preserve errors. (blocker)"},
            {"action": "retire", "rule_id": "P0E.6", "file": "rules.md", "after": ""}])
        batch = prepare(self.config, self.events(), assessment)
        payload = {**self.answer(batch), "decisions": [{"id": "p1", "decision": "accept"}, {"id": "p2", "decision": "reject"}]}
        with self.assertRaisesRegex(HouseRulesError, "whole-group"):
            apply_batch(self.config, self.events(), payload)
        apply_answers(self.config, self.events(), self.answer(batch))
        self.assertIn("house-rules-retired: P0E.6", (self.repo / "rules.md").read_text())

    def test_reconsideration_has_a_new_id_and_can_link_its_parent(self):
        first = self.batch()
        apply_answers(self.config, self.events(), self.answer(first, "defer"))
        second = prepare(self.config, self.events(), self.assessment(), parent=first["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["native"]["parent_batch"], first["id"])
        apply_answers(self.config, self.events(), self.answer(second))

    def test_prepare_and_apply_dry_run_write_nothing(self):
        before = set(self.repo.rglob("*"))
        prepare(self.config, self.events(), self.assessment(), dry_run=True)
        self.assertEqual(before, set(self.repo.rglob("*")))
        batch = self.batch()
        apply_answers(self.config, self.events(), self.answer(batch), dry_run=True)
        self.assertFalse((self.repo / STATE / batch["id"] / "receipt.json").exists())

    def test_an_accepted_group_is_recorded_with_the_humans_own_words(self):
        batch = self.batch()
        payload = self.answer(batch)
        payload["answers"][0]["response"] = "Yes,\n  ship it."  # one bullet line: the newline is collapsed
        self.apply_on(payload)
        self.assertEqual(self.changelog(), "\n".join([
            "# Coding style changes", "",
            f"## Revision 1 — 2026-10-29 (batch {batch['id']})", "",
            "### add REL.1", "",
            "- Before: (new clause)",
            "- After:", "  ```markdown", "  - [REL.1] Preserve a usable copy when replacement fails. (should-fix)", "  ```",
            "- Why: A distinct failure mechanism needs a rule.",
            f"- Evidence: `{self.finding['id']}` — `source.py:1` — synthetic review finding",
            '- Decision: accept by Synthetic test human — "Yes, ship it."', ""]))

    def test_reject_defer_and_revise_groups_are_listed_as_not_applied_with_the_humans_reason(self):
        batch = self.four_groups()
        answers = [("accept", "Ship it."), ("reject", "Too narrow."), ("defer", "Wait for a second example."),
                   ("revise", "Say it as a prohibition.")]
        payload = {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test human", "answers": [
            {"id": f"a{n}", "decision": choice, "response": words} for n, (choice, words) in enumerate(answers, 1)]}
        self.apply_on(payload)
        text = self.changelog()
        self.assertEqual([line for line in text.splitlines() if line.startswith("### ")], ["### add REL.1", "### Not applied"])
        self.assertTrue(text.endswith("\n".join([
            "### Not applied", "",
            '- add REL.2 — reject by Synthetic test human — "Too narrow."',
            '- add REL.3 — defer by Synthetic test human — "Wait for a second example."',
            '- add REL.4 — revise by Synthetic test human — "Say it as a prohibition."', ""])))
        self.assertIn('- Decision: accept by Synthetic test human — "Ship it."\n', text)
        rules = (self.repo / "rules.md").read_text()
        self.assertEqual([f"[REL.{n}]" in rules for n in range(1, 5)], [True, False, False, False])

    def test_an_accepted_merge_records_each_change_with_the_groups_one_decision(self):
        assessment = self.assessment()
        assessment["assessments"][0].update(action="merge", rule_ids=["P0E.1", "P0E.6"], changes=[
            {"action": "rewrite", "rule_id": "P0E.1", "file": "rules.md",
             "after": "- [P0E.1] Guard just the expected operation and preserve errors. (blocker)"},
            {"action": "retire", "rule_id": "P0E.6", "file": "rules.md", "after": ""}])
        batch = prepare(self.config, self.events(), assessment)
        self.apply_on({**self.answer(batch), "answers": [{"id": "a1", "decision": "accept", "response": "Merge them."}]})
        text = self.changelog()
        self.assertEqual([line for line in text.splitlines() if line.startswith("### ")], ["### rewrite P0E.1", "### retire P0E.6"])
        self.assertEqual(text.count('- Decision: accept by Synthetic test human — "Merge them."\n'), 2)
        self.assertNotIn("Not applied", text)

    def test_cli_uses_native_answers_without_json_export_from_a_page(self):
        model_file = self.tmp / "assessment.json"
        model_file.write_text(json.dumps(self.assessment()))
        output = json.loads(self.cli_ok("prepare-decisions", "--assessment", str(model_file)))
        response = self.tmp / "answer.json"
        response.write_text(json.dumps(self.answer({"id": output["batch_id"]})))
        result = json.loads(self.cli_ok("apply-decisions", "--input", str(response)))
        self.assertEqual(result["status"], "applied")

    def test_summary_separates_initial_revision_and_no_change_without_scoring(self):
        first = self.batch()
        apply_answers(self.config, self.events(), self.answer(first, "revise"))
        second = prepare(self.config, self.events(), self.assessment(), parent=first["id"])
        apply_answers(self.config, self.events(), self.answer(second, "accept"))
        unchanged = self.batch("keep")
        pending = summary(self.config)
        self.assertEqual(pending["groups"]["initial"]["change"], {"revise": 1})
        self.assertEqual(pending["groups"]["revision"]["change"], {"accept": 1})
        self.assertEqual(pending["groups"]["initial"]["no_change"], {"pending": 1})
        apply_answers(self.config, self.events(), self.answer(unchanged, "defer"))
        result = json.loads(self.cli_ok("decision-summary"))
        self.assertEqual(result["groups"]["initial"]["no_change"], {"defer": 1})
        self.assertNotIn("accuracy", result)
