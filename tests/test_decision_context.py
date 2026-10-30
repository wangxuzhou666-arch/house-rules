import copy
import json
from pathlib import Path
from unittest import mock

from support import RepoCase, git, review_finding
from ratchet.config import load_config
from ratchet.decision_context import _read_source, assess, build_context
from ratchet.errors import RatchetError
from ratchet.proposals import apply_batch, create_batch, rollback_batch


class DecisionContextTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        (self.repo / "src").mkdir()
        (self.repo / "src/a.py").write_text("first\nsecond\nthird\n")
        self.finding = review_finding(file="src/a.py", line=2, rule_id="unmapped")
        self.append_raw(self.finding)
        self.config = load_config(self.repo, None, None, self.repo)

    def context(self):
        return build_context(self.config, self.events())

    def payload(self, **overrides):
        entry = {"finding_ids": [self.finding["id"]],
                 "verification": {"status": "uncertain", "basis": "Historical snapshot is absent."},
                 "action": "keep", "rule_ids": [], "reason": "Insufficient evidence for a rule change.", "changes": []}
        return {"v": 1, "context_id": self.context()["context_id"], "assessments": [{**entry, **overrides}]}

    def prepare(self, payload):
        return assess(self.config, self.events(), self.context(), payload)

    def test_source_coordinates_and_original_finding_are_tool_owned(self):
        packet = json.loads(self.cli_ok("decision-context", "--finding", self.finding["id"]))
        item = packet["findings"][0]
        self.assertEqual(item["finding"], self.finding)
        self.assertEqual(item["source"]["absolute_path"], str(self.repo / "src/a.py"))
        self.assertEqual(item["source"]["line"], 2)
        self.assertEqual(item["source"]["line_start"], 1)
        self.assertEqual(item["source"]["line_end"], 3)
        self.assertEqual(item["source"]["text"], "first\nsecond\nthird")
        self.assertEqual(item["source"]["review_snapshot"], "unavailable")
        self.assertEqual(item["source"]["recorded_sha"], self.finding["sha"])

    def test_keep_decision_remains_in_output_without_mutating_rules_or_ledger(self):
        before = self.ledger_bytes(), (self.repo / "rules.md").read_bytes()
        result = self.prepare(self.payload())
        self.assertEqual(result["cards"][0]["action"], "keep")
        self.assertEqual(result["cards"][0]["verification"]["by"], "model")
        self.assertEqual(result["drafts"], [])
        self.assertFalse((self.repo / ".ratchet").exists())
        self.assertEqual(before, (self.ledger_bytes(), (self.repo / "rules.md").read_bytes()))

    def test_missing_or_moved_source_is_not_replaced_with_invented_code(self):
        source = _read_source(self.repo, "src/a.py", 90)
        self.assertEqual(source["status"], "unavailable")
        self.assertNotIn("text", source)
        self.assertEqual(_read_source(self.repo, "missing.py", 1)["status"], "unavailable")

    def test_relocated_range_preserves_original_location_and_is_not_a_historical_snapshot(self):
        packet = json.loads(self.cli_ok("decision-context", "--location", self.finding["id"] + "=1:3"))
        source = packet["findings"][0]["source"]
        self.assertEqual(source["reported_line"], 2)
        self.assertEqual(source["line_start"], 1)
        self.assertEqual(source["line_end"], 3)
        self.assertEqual(source["location_basis"], "selected_current_range")
        self.assertEqual(source["review_snapshot"], "unavailable")
        for span in ("0:3", "3:2", "1:90", "bad"):
            code, _, _ = self.cli("decision-context", "--location", self.finding["id"] + "=" + span)
            self.assertNotEqual(code, 0)

    def test_unsafe_paths_are_rejected_before_opening(self):
        for name in ("../outside", ".env", ".env.local", ".git/config", "/outside/file"):
            with self.subTest(name=name), mock.patch.object(Path, "open", side_effect=AssertionError("read forbidden")):
                self.assertEqual(_read_source(self.repo, name, 1)["status"], "unavailable")
        link = self.repo / "link.py"
        link.symlink_to(self.repo / "src/a.py")
        with mock.patch.object(Path, "open", side_effect=AssertionError("followed link")):
            self.assertEqual(_read_source(self.repo, "link.py", 1)["status"], "unavailable")

    def test_context_change_invalidates_assessment_for_code_rules_or_ledger(self):
        for path in (self.repo / "src/a.py", self.repo / "rules.md"):
            payload = self.payload()
            path.write_text(path.read_text() + "\nchanged\n")
            with self.assertRaisesRegex(RatchetError, "context changed"):
                self.prepare(payload)
        payload = self.payload()
        self.append_raw({"v": 1, "kind": "outcome", "id": self.finding["id"], "outcome": "false_positive",
                         "outcome_by": "human", "evidence": "reviewed", "ts": "2026-01-08T10:00:00"})
        with self.assertRaisesRegex(RatchetError, "context changed"):
            self.prepare(payload)

    def test_covered_requires_real_rule_and_includes_definition_location(self):
        result = self.prepare(self.payload(action="covered", rule_ids=["P0E.1"]))
        rule = result["cards"][0]["matched_rules"][0]
        self.assertEqual(rule["absolute_path"], str(self.repo / "rules.md"))
        self.assertIn("[P0E.1]", rule["text"])
        self.assertGreater(rule["line"], 0)
        with self.assertRaisesRegex(RatchetError, "existing clauses"):
            self.prepare(self.payload(action="covered", rule_ids=["UNKNOWN.1"]))

    def test_omitted_duplicate_and_invented_findings_are_rejected(self):
        payload = self.payload()
        payload["assessments"] *= 2
        with self.assertRaisesRegex(RatchetError, "exactly once"):
            self.prepare(payload)
        with self.assertRaisesRegex(RatchetError, "exactly once"):
            self.prepare(self.payload(finding_ids=["invented"]))
        self.append_raw(review_finding(display_id="R1-002", file="src/a.py", line=3))
        with self.assertRaisesRegex(RatchetError, "every selected finding"):
            self.prepare(self.payload())

    def test_model_cannot_inject_code_locations_or_change_without_supported_basis(self):
        payload = self.payload()
        payload["assessments"][0]["absolute_path"] = "/invented"
        with self.assertRaisesRegex(RatchetError, "exactly"):
            self.prepare(payload)
        change = {"action": "add", "rule_id": "REL.1", "file": "rules.md",
                  "after": "- [REL.1] Preserve usable state during replacement. (should-fix)"}
        with self.assertRaisesRegex(RatchetError, "uncertain/refuted"):
            self.prepare(self.payload(action="add", changes=[change]))
        with self.assertRaisesRegex(RatchetError, "no changes"):
            self.prepare(self.payload(changes=[change]))

    def test_structured_assessment_to_existing_proposal_apply_and_rollback(self):
        payload = self.payload(action="rewrite", rule_ids=["P0E.1"],
                               verification={"status": "supported", "basis": "Synthetic code and negative control inspected."},
                               changes=[{"action": "rewrite", "rule_id": "P0E.1", "file": "rules.md",
                                         "after": "- [P0E.1] Guard the operation, excluding unrelated work. (blocker)"}])
        payload_file = self.tmp / "assessment.json"
        payload_file.write_text(json.dumps(payload))
        result = json.loads(self.cli_ok("decision-context", "--assessment", str(payload_file)))
        self.assertFalse((self.repo / ".ratchet").exists())
        batch = create_batch(self.config, self.events(), result["drafts"])
        before = (self.repo / "rules.md").read_bytes()
        rejected = {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic operator", "decisions": []}
        with self.assertRaisesRegex(RatchetError, "every proposal"):
            apply_batch(self.config, self.events(), rejected)
        self.assertEqual((self.repo / "rules.md").read_bytes(), before)
        decisions = {**rejected, "decisions": [{"id": "p1", "decision": "accept"}]}
        apply_batch(self.config, self.events(), decisions)
        self.cli_ok("compile", "--check", "--strict")
        self.assertIn("excluding unrelated work", (self.repo / "rules.md").read_text())
        self.assertEqual(self.events(), [self.finding])
        rollback_batch(self.config, batch["id"])
        self.assertEqual((self.repo / "rules.md").read_bytes(), before)

    def test_merge_requires_one_survivor_and_all_other_rules_retired(self):
        payload = self.payload(action="merge", rule_ids=["P0E.1", "P0E.6"],
                               verification={"status": "supported", "basis": "Synthetic overlapping requirements."},
                               changes=[{"action": "rewrite", "rule_id": "P0E.1", "file": "rules.md",
                                         "after": "- [P0E.1] Guard only the failing operation and propagate its error. (blocker)"},
                                        {"action": "retire", "rule_id": "P0E.6", "file": "rules.md", "after": ""}])
        self.assertEqual(len(self.prepare(payload)["drafts"]), 2)
        broken = copy.deepcopy(payload)
        broken["assessments"][0]["changes"].pop()
        with self.assertRaisesRegex(RatchetError, "merge must"):
            self.prepare(broken)


class ProtectedNameContextTest(RepoCase):
    """A ledger finding can name any file; the packet must not carry a protected one just because it is spelled in another case."""

    NAMES = (".ENV", ".Env", ".ENV.local", "sub/.ENV", ".GIT/config", ".Git/config", ".git/CONFIG")

    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        (self.repo / "sub").mkdir()
        for name in (".env", ".env.local", "sub/.env"):
            (self.repo / name).write_text("TOKEN=secret\nMORE=secret\n")
        (self.repo / "a.py").write_text("first\nsecond\n")
        git(self.repo, "remote", "add", "origin", "https://user:secret@example.invalid/org/repo.git")  # `.git/config` holds a token now
        self.append_raw(*[review_finding(display_id=f"R1-{number}", file=name, line=1) for number, name in enumerate(self.NAMES, 1)],
                        review_finding(display_id="R1-99", file="a.py", line=1))

    def test_a_protected_file_in_another_case_is_unavailable_and_an_ordinary_one_is_read(self):
        packet = json.loads(self.cli_ok("decision-context"))
        sources = {item["finding"]["file"]: item["source"] for item in packet["findings"]}
        self.assertEqual(sources["a.py"]["text"], "first\nsecond")
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(sources[name]["status"], "unavailable")
                self.assertIn("unsafe proposal path", sources[name]["reason"])
        self.assertFalse("secret" in json.dumps(packet), "a secret reached the packet")

    def test_a_chosen_range_of_such_a_file_is_refused_too(self):
        finding_id = f"demo-repo:l1:R1:R1-{self.NAMES.index('.ENV') + 1}"
        packet = json.loads(self.cli_ok("decision-context", "--finding", finding_id, "--location", finding_id + "=1:2"))
        self.assertEqual(packet["findings"][0]["source"]["status"], "unavailable")
        self.assertFalse("secret" in json.dumps(packet), "a secret reached the packet")
