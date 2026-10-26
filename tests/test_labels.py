import json
import re
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, gate_finding, review_finding


class LabelFlowTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.page = self.repo / ".ratchet-label-page.html"
        self.batch = self.repo / ".ratchet-label-batch.json"
        self.first, self.second = review_finding("R1-001", line=1), review_finding("R1-002", line=2)
        self.append_raw(self.first, self.second)

    def make_page(self, *extra: str) -> dict:
        self.cli_ok("label-page", *extra)
        return json.loads(self.batch.read_text(encoding="utf-8"))

    def embedded_data(self) -> dict:
        match = re.search(r'<script type="application/json" id="data">(.*?)</script>', self.page.read_text(encoding="utf-8"), re.S)
        return json.loads(match.group(1))

    def labels(self, batch: dict, *pairs: tuple[str, str]) -> str:
        return json.dumps({"batch_id": batch["batch_id"], "labels": [{"id": i, "outcome": o} for i, o in pairs]})

    def test_label_page_lists_open_findings_and_records_the_batch(self) -> None:
        batch = self.make_page()
        self.assertEqual(batch["finding_ids"], [self.first["id"], self.second["id"]])
        data = self.embedded_data()
        self.assertEqual(data["batch_id"], batch["batch_id"])
        self.assertEqual([f["display_id"] for f in data["findings"]], ["R1-001", "R1-002"])

    def test_page_data_cannot_break_out_of_its_script_block(self) -> None:
        evil = review_finding("R1-003", line=3, summary="</script><script>alert(1)</script><!--")
        self.append_raw(evil)
        self.make_page()
        html = self.page.read_text(encoding="utf-8")
        self.assertEqual(html.count("</script>"), 2)  # the data block and the code block, nothing injected
        self.assertIn("</script><script>alert(1)</script><!--", [f["summary"] for f in self.embedded_data()["findings"]])

    def test_skipped_false_positive_is_preselected(self) -> None:
        (self.repo / "skips.jsonl").write_text(json.dumps(
            {"reason_kind": "false_positive", "finding_ids": [self.second["id"]]}) + "\n", encoding="utf-8")
        self.write_config(skips_file="skips.jsonl")
        self.make_page()
        self.assertEqual(self.embedded_data()["preselect_false_positive"], [self.second["id"]])

    def test_legacy_alias_is_shown_on_the_page_without_touching_the_ledger(self) -> None:
        self.write_config(legacy_unmapped_alias=["R.1"])
        self.make_page()
        self.assertEqual(self.embedded_data()["findings"][0]["rule"], "unmapped (legacy R.1)")

    def test_auto_skips_the_page_at_or_below_the_threshold(self) -> None:
        self.write_config(label_page_threshold=2)
        out = self.cli_ok("label-page", "--auto")
        self.assertIn("no label page generated", out)
        self.assertFalse(self.page.exists())
        self.write_config(label_page_threshold=1)
        self.cli_ok("label-page", "--auto")
        self.assertTrue(self.page.exists())

    def test_nothing_open_means_no_page(self) -> None:
        self.ledger.write_text("", encoding="utf-8")
        self.assertIn("no open findings", self.cli_ok("label-page"))
        self.assertFalse(self.page.exists())

    def test_apply_labels_writes_human_outcomes(self) -> None:
        batch = self.make_page()
        self.cli_ok("apply-labels", stdin=self.labels(batch, (self.first["id"], "fixed"), (self.second["id"], "false_positive")))
        outcomes = [e for e in self.events() if e["kind"] == "outcome"]
        self.assertEqual([(o["id"], o["outcome"], o["outcome_by"]) for o in outcomes],
                         [(self.first["id"], "fixed", "human"), (self.second["id"], "false_positive", "human")])
        self.assertIn(batch["batch_id"], outcomes[0]["evidence"])
        self.assertIn("0 open", self.cli_ok("count"))

    def test_note_becomes_the_evidence(self) -> None:
        batch = self.make_page()
        payload = {"batch_id": batch["batch_id"], "labels": [{"id": self.first["id"], "outcome": "dismissed", "note": "  accepted risk "}]}
        self.cli_ok("apply-labels", stdin=json.dumps(payload))
        self.assertEqual(self.events()[-1]["evidence"], "accepted risk")

    def test_input_file_flag(self) -> None:
        batch = self.make_page()
        path = self.repo / "labels.json"
        path.write_text(self.labels(batch, (self.first["id"], "fixed")), encoding="utf-8")
        self.cli_ok("apply-labels", "--input", str(path))
        self.assertEqual(len(self.events()), 3)

    def assert_rejected(self, payload: str, fragment: str) -> None:
        before = self.ledger_bytes()
        code, _, err = self.cli("apply-labels", stdin=payload)
        self.assertEqual(code, 1)
        self.assertIn("ledger untouched", err)
        self.assertIn(fragment, err)
        self.assertEqual(self.ledger_bytes(), before)

    def test_one_bad_label_rejects_the_whole_batch(self) -> None:
        batch = self.make_page()
        self.assert_rejected(self.labels(batch, (self.first["id"], "fixed"), (self.second["id"], "wontfix")), "outcome='wontfix'")

    def test_wrong_batch_id_is_rejected(self) -> None:
        self.make_page()
        payload = json.dumps({"batch_id": "stale", "labels": [{"id": self.first["id"], "outcome": "fixed"}]})
        self.assert_rejected(payload, "batch_id mismatch")

    def test_unknown_duplicate_and_already_closed_ids_are_rejected(self) -> None:
        batch = self.make_page()
        self.assert_rejected(self.labels(batch, ("demo-repo:l1:R1:NOPE", "fixed")), "not in the ledger")
        self.assert_rejected(self.labels(batch, (self.first["id"], "fixed"), (self.first["id"], "fixed")), "duplicate id")
        self.cli_ok("apply-labels", stdin=self.labels(batch, (self.first["id"], "fixed")))
        self.assert_rejected(self.labels(batch, (self.first["id"], "dismissed")), "already has an outcome")

    def test_finding_added_after_the_page_was_made_is_not_in_the_batch(self) -> None:
        batch = self.make_page()
        late = review_finding("R1-009", line=9)
        self.append_raw(late)
        self.assert_rejected(self.labels(batch, (late["id"], "fixed")), "not in the current batch")

    def test_apply_without_a_batch_is_an_error(self) -> None:
        code, _, err = self.cli("apply-labels", stdin="{}")
        self.assertEqual(code, 1)
        self.assertIn("label-page", err)

    def test_empty_labels_and_wrong_shape_are_rejected(self) -> None:
        batch = self.make_page()
        self.assert_rejected(json.dumps({"batch_id": batch["batch_id"], "labels": []}), "labels is empty")
        self.assert_rejected("[]", "top level must be")

    def test_gate_findings_can_be_labeled_too(self) -> None:
        self.append_raw(gate_finding("a.py"))
        batch = self.make_page()
        self.cli_ok("apply-labels", stdin=self.labels(batch, ("demo-repo:gate:a1b2c3d4e5f6", "dismissed")))
        self.assertEqual(self.events()[-1]["outcome"], "dismissed")


if __name__ == "__main__":
    unittest.main()
