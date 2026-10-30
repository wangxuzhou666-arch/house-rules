import contextlib
import io
import json
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, gate_finding, review_finding

from ratchet.cli import build_parser
from ratchet.ledger import OUTCOMES


class DirectLabelTest(RepoCase):
    """`ratchet label ID... --outcome X`: the one-call route for a few findings; label-page + apply-labels is the batch route."""

    def setUp(self) -> None:
        super().setUp()
        self.first, self.second, self.third = (review_finding(f"r1-{number}", run="r1", line=number) for number in (1, 2, 3))
        self.append_raw(self.first, self.second, self.third)

    def outcomes(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "outcome"]

    def repo_files(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.repo)): path.read_bytes()
                for path in sorted(self.repo.rglob("*")) if path.is_file() and ".git" not in path.relative_to(self.repo).parts}

    def test_a_display_id_records_a_human_outcome(self) -> None:
        out = self.cli_ok("label", "r1-2", "--outcome", "false_positive")
        (outcome,) = self.outcomes()
        self.assertEqual((outcome["id"], outcome["outcome"], outcome["outcome_by"], outcome["evidence"]),
                         (self.second["id"], "false_positive", "human", "ratchet label"))
        self.assertIn("label: 1 human outcome(s), false_positive (r1-2)", out)
        self.assertIn("2 open", self.cli_ok("count"))

    def test_a_full_id_works_and_several_ids_are_recorded_in_the_order_given(self) -> None:
        self.cli_ok("label", self.third["id"], "r1-1", "--outcome", "dismissed")
        self.assertEqual([(outcome["id"], outcome["outcome"]) for outcome in self.outcomes()],
                         [(self.third["id"], "dismissed"), (self.first["id"], "dismissed")])

    def test_each_outcome_the_ledger_knows_is_accepted(self) -> None:
        for number, outcome in enumerate(OUTCOMES, start=10):
            finding = review_finding(f"r1-{number}", run="r1", line=number)
            self.append_raw(finding)
            with self.subTest(outcome=outcome):
                self.cli_ok("label", finding["display_id"], "--outcome", outcome)
                self.assertEqual(self.outcomes()[-1]["outcome"], outcome)

    def test_an_outcome_the_ledger_does_not_know_is_refused_before_anything_runs(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            build_parser().parse_args(["label", "r1-1", "--outcome", "wontfix"])
        self.assertIn("invalid choice: 'wontfix'", stderr.getvalue())
        for known in OUTCOMES:
            self.assertIn(repr(known), stderr.getvalue())  # the message names what is allowed
        before = self.ledger_bytes()
        with self.assertRaises(SystemExit):
            self.cli("label", "r1-1", "--outcome", "wontfix")
        self.assertEqual(self.ledger_bytes(), before)

    def test_an_outcome_and_at_least_one_id_are_required(self) -> None:
        before = self.ledger_bytes()
        for argv in (("label", "r1-1"), ("label", "--outcome", "fixed")):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.cli(*argv)
        self.assertEqual(self.ledger_bytes(), before)

    def test_the_event_is_the_one_apply_labels_writes(self) -> None:
        self.cli_ok("label", "r1-1", "--outcome", "dismissed", "--note", "accepted risk")
        self.cli_ok("label-page")
        batch = json.loads((self.repo / ".ratchet-label-batch.json").read_text(encoding="utf-8"))
        payload = {"batch_id": batch["batch_id"],
                   "labels": [{"id": self.second["id"], "outcome": "dismissed", "note": "accepted risk"}]}
        self.cli_ok("apply-labels", stdin=json.dumps(payload))
        direct, applied = self.outcomes()
        self.assertEqual(list(direct), list(applied))  # the same fields, in the same order
        for key in ("v", "kind", "outcome", "outcome_by", "evidence"):
            self.assertEqual(direct[key], applied[key], key)

    def test_the_note_is_the_evidence_and_a_blank_one_is_not(self) -> None:
        self.cli_ok("label", "r1-1", "--outcome", "dismissed", "--note", "  accepted risk ")
        self.cli_ok("label", "r1-2", "--outcome", "dismissed", "--note", "   ")
        self.assertEqual([outcome["evidence"] for outcome in self.outcomes()], ["accepted risk", "ratchet label"])

    def test_a_gate_finding_is_named_by_its_hex_display_id(self) -> None:
        gate = gate_finding("a.py")
        self.append_raw(gate)
        self.cli_ok("label", gate["display_id"], "--outcome", "dismissed")
        self.assertEqual(self.outcomes()[-1]["id"], gate["id"])

    def test_a_finding_that_came_back_is_open_and_can_be_labeled(self) -> None:
        gate = gate_finding("a.py")
        closed = {"v": 1, "kind": "outcome", "id": gate["id"], "outcome": "fixed", "outcome_by": "auto:gate_pass",
                  "evidence": "gate_pass covers a.py", "ts": "2026-01-08T00:00:00"}
        self.append_raw(gate, closed, gate)  # the gate saw the violation again
        self.cli_ok("label", gate["display_id"], "--outcome", "false_positive")
        self.assertEqual(self.outcomes()[-1]["outcome"], "false_positive")

    # ---- refusals: each leaves the ledger as it was ----

    def assert_refused(self, fragment: str, *ids: str) -> str:
        before = self.ledger_bytes()
        code, out, err = self.cli("label", *ids, "--outcome", "false_positive")
        self.assertEqual(code, 1)
        self.assertIn("label rejected, ledger untouched", err)
        self.assertIn(fragment, err)
        self.assertEqual(out, "")
        self.assertEqual(self.ledger_bytes(), before)
        return err

    def test_an_unknown_id_is_refused(self) -> None:
        self.assert_refused("'nope-9' matches no finding", "nope-9")

    def test_a_closed_finding_is_refused_and_keeps_its_outcome(self) -> None:
        self.cli_ok("label", "r1-1", "--outcome", "fixed")
        self.assert_refused("'r1-1' already has an outcome (fixed by human)", "r1-1")
        self.assertEqual([outcome["outcome"] for outcome in self.outcomes()], ["fixed"])

    def test_a_finding_named_twice_is_refused_whichever_way_it_is_spelled(self) -> None:
        self.assert_refused("duplicate id: 'r1-1'", "r1-1", "r1-1")
        self.assert_refused(f"duplicate id: {self.first['id']!r}", "r1-1", self.first["id"])

    def test_a_display_id_shared_by_several_findings_is_not_guessed(self) -> None:
        one, other = review_finding("DUP-1", run="R1"), review_finding("DUP-1", run="R2")
        self.append_raw(one, other)
        err = self.assert_refused("'DUP-1' is the display_id of several findings", "DUP-1")
        self.assertIn(one["id"], err)  # the message says which full ids to choose from
        self.assertIn(other["id"], err)
        self.cli_ok("label", other["id"], "--outcome", "false_positive")
        self.assertEqual([outcome["id"] for outcome in self.outcomes()], [other["id"]])

    def test_one_bad_id_refuses_the_whole_batch(self) -> None:
        self.assert_refused("'nope-9' matches no finding", "r1-1", "nope-9", "r1-2")
        self.assertIn("3 open", self.cli_ok("count"))

    def test_every_problem_is_reported_at_once(self) -> None:
        self.cli_ok("label", "r1-3", "--outcome", "fixed")
        err = self.assert_refused("'nope-9' matches no finding", "nope-9", "r1-3", "r1-1", "r1-1")
        self.assertIn("'r1-3' already has an outcome", err)
        self.assertIn("duplicate id: 'r1-1'", err)

    def test_no_ledger_means_nothing_to_label_and_none_is_created(self) -> None:
        self.ledger.unlink()
        self.assert_refused("'r1-1' matches no finding", "r1-1")
        self.assertFalse(self.ledger.exists())

    def test_a_ledger_option_that_names_no_file_is_a_typo_and_creates_nothing(self) -> None:
        missing = self.tmp / "typo.jsonl"
        code, _, err = self.cli("label", "r1-1", "--outcome", "fixed", "--ledger", str(missing))
        self.assertEqual(code, 1)
        self.assertIn("ledger not found", err)
        self.assertFalse(missing.exists())

    def test_the_ledger_option_picks_the_file_that_is_read_and_written(self) -> None:
        other = self.tmp / "other.jsonl"
        other.write_bytes(self.ledger.read_bytes())
        before = self.ledger_bytes()
        self.cli_ok("label", "r1-1", "--outcome", "fixed", "--ledger", str(other))
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(json.loads(other.read_text(encoding="utf-8").splitlines()[-1])["id"], self.first["id"])

    # ---- --dry-run ----

    def test_dry_run_prints_the_events_and_writes_nothing(self) -> None:
        before = self.repo_files()
        out = self.cli_ok("label", "r1-1", "r1-2", "--outcome", "false_positive", "--note", "n", "--dry-run")
        self.assertIn("[dry-run] label: 2 human outcome(s), false_positive (r1-1, r1-2); nothing written", out)
        printed = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        self.assertEqual([(event["id"], event["outcome"], event["outcome_by"], event["evidence"]) for event in printed],
                         [(self.first["id"], "false_positive", "human", "n"), (self.second["id"], "false_positive", "human", "n")])
        self.assertEqual(self.repo_files(), before)

    def test_the_real_run_writes_what_dry_run_printed(self) -> None:
        argv = ("label", "r1-1", "r1-2", "--outcome", "dismissed")
        printed = [json.loads(line) for line in self.cli_ok(*argv, "--dry-run").splitlines() if line.startswith("{")]
        self.cli_ok(*argv)
        written = self.outcomes()
        for event in (*printed, *written):
            event.pop("ts")
        self.assertEqual(printed, written)

    def test_dry_run_still_refuses_what_a_real_run_refuses(self) -> None:
        before = self.repo_files()
        code, _, err = self.cli("label", "r1-1", "nope-9", "--outcome", "fixed", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("matches no finding", err)
        self.assertEqual(self.repo_files(), before)

    # ---- the batch route is untouched ----

    def test_a_finding_labeled_here_drops_out_of_the_label_page_and_is_refused_by_a_stale_batch(self) -> None:
        self.cli_ok("label-page")
        batch = json.loads((self.repo / ".ratchet-label-batch.json").read_text(encoding="utf-8"))
        self.cli_ok("label", "r1-1", "--outcome", "fixed")
        stale = json.dumps({"batch_id": batch["batch_id"], "labels": [{"id": self.first["id"], "outcome": "dismissed"}]})
        code, _, err = self.cli("apply-labels", stdin=stale)
        self.assertEqual(code, 1)
        self.assertIn("already has an outcome", err)
        self.cli_ok("label-page")
        again = json.loads((self.repo / ".ratchet-label-batch.json").read_text(encoding="utf-8"))
        self.assertEqual(again["finding_ids"], [self.second["id"], self.third["id"]])


if __name__ == "__main__":
    unittest.main()
