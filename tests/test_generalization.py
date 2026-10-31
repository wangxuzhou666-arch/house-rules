"""The harness of the generalization check (samples/generalization/run_check.py): scoring, provenance, the digest snapshots, the
without_rule digest, base versions, the spending guard, the repeat of a failed run and the live recording, all on synthetic cases
under tests/fixtures/generalization/ and a fake reviewer.

The last class runs the oracles of the real cases under samples/generalization/cases/; it needs them to exist."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import FIXTURES, REPO_ROOT, git, review_item

from house_rules.cli import main as house_rules_main
from house_rules.config import DEFAULT_CLAUSE_ID_PATTERN
from house_rules.errors import HouseRulesError
from house_rules.rules import parse_rules_text

_SPEC = importlib.util.spec_from_file_location("generalization_run_check", REPO_ROOT / "samples" / "generalization" / "run_check.py")
rc = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rc  # dataclasses look their module up here
_SPEC.loader.exec_module(rc)

GEN_FIXTURES = FIXTURES / "generalization"
VIOLATION_1, CONTROL_1 = "code/store_a.py", "code/store_b.py"  # case fix-1 (clause REL.1)
VIOLATION_2, CONTROL_2 = "code/ids_a.py", "code/ids_b.py"  # case fix-2 (clause REL.2)
MIN_REAL_CASES = 4


def review_text(*items: dict) -> str:
    """What the built-in code review prints: prose, then the findings in a json fence."""
    return f"Reviewed the diff.\n\n```json\n{json.dumps(list(items), indent=2)}\n```\n"


def on(file: str, label: str | None = None, line: int = 1) -> str:
    """A whole review with one finding on `file` that names `label` as its category (none when label is None)."""
    extra = {"category": label} if label is not None else {}
    return review_text(review_item(file=file, line=line, summary="Synthetic summary", severity="should-fix", **extra))


NOTHING = review_text()  # a review that found nothing: an empty array


class CheckCase(unittest.TestCase):
    """A copy of the fixture repo in a temp dir (its own git repo, digest compiled), and helpers to record and score."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.root = self.tmp / "repo"
        shutil.copytree(GEN_FIXTURES / "repo", self.root)
        git(self.root, "init", "-q", "-b", "main")
        self.compile_digest()
        self.layout = rc.Layout(self.root)

    def compile_digest(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = house_rules_main(["compile", "--repo", str(self.root)])
        self.assertEqual(code, 0)

    def check(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rc.main([*argv, "--repo-root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def config(self):
        return rc.load_repo_config(self.root)

    def digests(self, *clauses: str):
        return rc.build_digests(self.config(), clauses or ("REL.1", "REL.2"))

    def case(self, case_id: str):
        return rc.read_case(self.layout.cases_dir / case_id)

    def current_digest(self) -> str:
        return (self.root / "CODING-STYLE.digest.md").read_text(encoding="utf-8")

    def record(self, case_id: str, condition: str, run: int, text: str, *, exit_code: int | None = 0, attempt: int = 1,
               failed_first_attempt: bool = False, digest: str | None = None, **overrides: object) -> None:
        """Write a recording the way --live does, with the hashes of the files as they are now (and `overrides` on top), and the
        snapshot of its digest unless one is there. `failed_first_attempt` files it as the attempt before a repeat."""
        case_dir = self.layout.cases_dir / case_id
        meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        code = {rel: hashlib.sha256((case_dir / rel).read_bytes()).hexdigest() for rel in (meta["violation"], meta["control"])}
        base = {path.relative_to(case_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((case_dir / "base").rglob("*")) if path.is_file()}
        digests = self.digests(meta["clause"])
        digest = digest if digest is not None else digests.full if condition == "with_rule" else digests.without[meta["clause"]]
        snapshot = self.layout.snapshot(case_id, condition)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            snapshot.write_text(digest, encoding="utf-8")
        prov = {"v": 1, "case": case_id, "condition": condition, "run": run, "attempt": attempt, "level": "high",
                "date_utc": "2026-01-01T00:00:00Z", "command": ["claude", "-p", "/code-review high a..b"], "range": "a..b",
                "reviewer_version": "2.1.0 (Claude Code)", "reviewer_version_error": None, "code_sha256": code, "base_sha256": base,
                "digest_sha256": hashlib.sha256(digest.encode()).hexdigest(),
                "clause_removed": meta["clause"] if condition == "without_rule" else None, "exit_code": exit_code, "error": None,
                "model_id": None, "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "status": "ok" if exit_code == 0 else "failed", "failure": None}
        prov.update(overrides)
        raw_path, prov_path = self.layout.recording(case_id, condition, run, failed_first_attempt=failed_first_attempt)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(text, encoding="utf-8")
        prov_path.write_text(json.dumps(prov, indent=2) + "\n", encoding="utf-8")

    def record_repeated(self, case_id: str, condition: str, run: int, final_text: str, *, final_exit_code: int = 0) -> None:
        """A run whose first attempt failed and was repeated: both attempts on disk."""
        self.record(case_id, condition, run, "partial output", exit_code=1, failed_first_attempt=True)
        self.record(case_id, condition, run, final_text, exit_code=final_exit_code, attempt=2)

    def record_runs(self, case_id: str, condition: str, texts: list[str]) -> None:
        for run, text in enumerate(texts, start=1):
            self.record(case_id, condition, run, text)

    def score(self, case_id: str = "fix-1", runs: int = 3) -> dict:
        """The row of `results` for one case, scored from the recordings on disk."""
        case = self.case(case_id)
        return rc.replay(self.layout, [case], self.current_digest(), runs)["cases"][0]

    def with_rule(self, texts: list[str], case_id: str = "fix-1") -> dict:
        self.record_runs(case_id, "with_rule", texts)
        return self.score(case_id)


class ScoringTest(CheckCase):
    def test_a_finding_on_the_violation_with_the_cases_label_is_counted(self) -> None:
        counts = self.with_rule([on(VIOLATION_1, "rel-1"), NOTHING, NOTHING])["conditions"]["with_rule"]
        self.assertEqual((counts["violation_labeled_runs"], counts["violation_flagged_runs"]), (1, 1))
        self.assertEqual((counts["control_labeled_runs"], counts["control_flagged_runs"]), (0, 0))

    def test_runs_are_counted_not_findings(self) -> None:
        twice = review_text(*[review_item(file=VIOLATION_1, line=n, summary="s", category="rel-1") for n in (1, 2)])
        counts = self.with_rule([twice, NOTHING, NOTHING])["conditions"]["with_rule"]
        self.assertEqual(counts["violation_labeled_runs"], 1)
        self.assertEqual(counts["runs"][0]["findings"], 2)

    def test_a_labeled_finding_on_the_control_is_a_false_positive(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1"), on(CONTROL_1, "rel-1"), on(VIOLATION_1, "rel-1")])
        counts = row["conditions"]["with_rule"]
        self.assertEqual((counts["control_labeled_runs"], counts["control_flagged_runs"]), (1, 1))

    def test_a_finding_on_another_file_is_ignored(self) -> None:
        row = self.with_rule([on("code/notes.py", "rel-1")] * 3)
        counts = row["conditions"]["with_rule"]
        self.assertEqual([counts[key] for key in ("violation_labeled_runs", "violation_flagged_runs", "control_labeled_runs",
                                                  "control_flagged_runs")], [0, 0, 0, 0])
        self.assertEqual([run["other_file_findings"] for run in counts["runs"]], [1, 1, 1])
        self.assertEqual(row["verdict"], "FAIL")

    def test_it_passes_at_exactly_two_of_three(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1"), NOTHING, on(VIOLATION_1, "rel-1")])
        self.assertEqual(row["conditions"]["with_rule"]["violation_labeled_runs"], 2)
        self.assertEqual(row["verdict"], "PASS")

    def test_it_fails_at_one_of_three(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1"), NOTHING, NOTHING])
        self.assertEqual(row["verdict"], "FAIL")

    def test_it_passes_at_three_of_three(self) -> None:
        self.assertEqual(self.with_rule([on(VIOLATION_1, "rel-1")] * 3)["verdict"], "PASS")

    def test_one_false_positive_on_the_control_fails_even_at_three_of_three(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1"), on(VIOLATION_1, "rel-1"),
                              review_text(review_item(file=VIOLATION_1, line=1, summary="s", category="rel-1"),
                                          review_item(file=CONTROL_1, line=1, summary="s", category="rel-1"))])
        self.assertEqual(row["conditions"]["with_rule"]["violation_labeled_runs"], 3)
        self.assertEqual(row["verdict"], "FAIL")

    def test_unmapped_on_the_violation_is_flagged_but_not_labeled(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "unmapped")] * 3)
        counts = row["conditions"]["with_rule"]
        self.assertEqual((counts["violation_flagged_runs"], counts["violation_labeled_runs"]), (3, 0))
        self.assertEqual(row["verdict"], "FAIL")

    def test_another_clauses_label_on_the_violation_is_flagged_but_not_labeled(self) -> None:
        counts = self.with_rule([on(VIOLATION_1, "p0e-1")] * 3)["conditions"]["with_rule"]
        self.assertEqual((counts["violation_flagged_runs"], counts["violation_labeled_runs"]), (3, 0))

    def test_a_control_finding_with_another_label_is_reported_but_is_not_a_false_positive(self) -> None:
        both = review_text(review_item(file=VIOLATION_1, line=1, summary="s", category="rel-1"),
                           review_item(file=CONTROL_1, line=1, summary="s", category="unmapped"))
        row = self.with_rule([both] * 3)
        counts = row["conditions"]["with_rule"]
        self.assertEqual((counts["control_flagged_runs"], counts["control_labeled_runs"]), (3, 0))
        self.assertEqual(row["verdict"], "PASS")

    def test_a_failed_with_rule_run_makes_the_case_incomplete(self) -> None:
        self.record("fix-1", "with_rule", 1, on(VIOLATION_1, "rel-1"))
        self.record("fix-1", "with_rule", 2, "partial output", exit_code=1)
        self.record("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"))
        row = self.score()
        counts = row["conditions"]["with_rule"]
        self.assertEqual((counts["runs_ok"], counts["runs_failed"]), (2, 1))
        self.assertEqual(row["verdict"], "INCOMPLETE")

    def test_a_run_is_scored_from_its_last_attempt_and_the_failed_first_one_is_reported(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.record_repeated("fix-1", "with_rule", 2, on(VIOLATION_1, "rel-1"))
        self.record_repeated("fix-1", "without_rule", 1, NOTHING)
        case = self.case("fix-1")
        results = rc.replay(self.layout, [case], self.current_digest(), 3)
        row = results["cases"][0]
        run = row["conditions"]["with_rule"]["runs"][1]
        self.assertEqual((run["status"], run["attempts"], run["first_attempt_failure"]), ("ok", 2, "the reviewer exited with 1"))
        self.assertEqual(row["conditions"]["with_rule"]["runs"][0]["attempts"], 1)
        self.assertEqual(row["verdict"], "PASS")
        self.assertEqual(results["repeats"]["slots_repeated"], 2)
        self.assertEqual(results["repeats"]["failed_attempts"],
                         [{"case": "fix-1", "condition": c, "run": r, "attempt": 1, "reason": "the reviewer exited with 1"}
                          for c, r in (("with_rule", 2), ("without_rule", 1))])

    def test_a_run_whose_repeat_failed_too_stays_failed_and_makes_the_case_incomplete(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 2)
        self.record_repeated("fix-1", "with_rule", 3, "still partial", final_exit_code=1)
        results = rc.replay(self.layout, [self.case("fix-1")], self.current_digest(), 3)
        row = results["cases"][0]
        self.assertEqual(row["verdict"], "INCOMPLETE")
        self.assertEqual((row["conditions"]["with_rule"]["runs_ok"], row["conditions"]["with_rule"]["runs_failed"]), (2, 1))
        self.assertEqual([entry["attempt"] for entry in results["repeats"]["failed_attempts"]], [1, 2])
        self.assertEqual(results["repeats"]["slots_repeated"], 1)

    def test_the_failed_attempts_and_the_repeats_are_printed_and_written(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 2)
        self.record_repeated("fix-1", "with_rule", 3, "still partial", final_exit_code=1)
        code, out, _ = self.check("--case", "fix-1")
        self.assertEqual(code, 0)
        self.assertIn("needed a repeat after a failed first attempt (both attempts are kept): 1", out)
        self.assertIn("fix-1 with_rule-3: attempt 1 failed (the reviewer exited with 1); the repeat is failed", out)
        self.assertIn("fix-1 with_rule-3: failed (the reviewer exited with 1)", out)
        self.record_runs("fix-1", "without_rule", [NOTHING] * 3)
        self.assertEqual(self.check()[0], 0)
        self.assertIn("fix-1 with_rule-3: attempt 1 failed", self.layout.results_md.read_text())
        self.assertEqual(json.loads(self.layout.results_json.read_text())["repeats"]["slots_repeated"], 1)

    def test_a_run_with_only_its_failed_first_attempt_is_missing_its_repeat(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 2)
        self.record("fix-1", "with_rule", 3, "partial output", exit_code=1, failed_first_attempt=True)
        row = self.score()
        run = row["conditions"]["with_rule"]["runs"][2]
        self.assertEqual((run["status"], run["attempts"]), ("missing", 1))
        self.assertIn("repeat", run["reason"])
        self.assertEqual(row["verdict"], "INCOMPLETE")

    def test_a_with_rule_run_whose_output_has_no_json_findings_is_failed(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1"), on(VIOLATION_1, "rel-1"), (GEN_FIXTURES / "outputs" / "no_findings_prose.txt").read_text()])
        self.assertEqual(row["conditions"]["with_rule"]["runs"][2]["status"], "failed")
        self.assertIn("no JSON findings", row["conditions"]["with_rule"]["runs"][2]["reason"])
        self.assertEqual(row["verdict"], "INCOMPLETE")

    def test_an_empty_array_is_a_review_that_found_nothing_not_a_failed_run(self) -> None:
        row = self.with_rule([(GEN_FIXTURES / "outputs" / "empty_array.txt").read_text()] * 3)
        self.assertEqual(row["conditions"]["with_rule"]["runs_failed"], 0)
        self.assertEqual(row["verdict"], "FAIL")

    def test_a_timed_out_run_is_failed(self) -> None:
        self.record("fix-1", "with_rule", 1, "", exit_code=None, error="timed out after 1800s")
        run = self.score()["conditions"]["with_rule"]["runs"][0]
        self.assertEqual((run["status"], run["reason"]), ("failed", "timed out after 1800s"))

    def test_a_missing_with_rule_recording_makes_the_case_incomplete(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "rel-1")] * 2)
        self.assertEqual(row["conditions"]["with_rule"]["runs_missing"], 1)
        self.assertEqual(row["verdict"], "INCOMPLETE")

    def test_a_provenance_file_without_its_raw_output_is_a_missing_run(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.layout.recording("fix-1", "with_rule", 3)[0].unlink()
        self.assertEqual(self.score()["conditions"]["with_rule"]["runs_missing"], 1)

    def test_a_failed_without_rule_run_does_not_change_the_verdict(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.record("fix-1", "without_rule", 1, "boom", exit_code=2)
        row = self.score()
        self.assertEqual(row["verdict"], "PASS")
        self.assertEqual(row["conditions"]["without_rule"]["runs_failed"], 1)

    def test_without_rule_is_counted_the_same_way_with_no_pass_line(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.record_runs("fix-1", "without_rule", [on(VIOLATION_1, "unmapped"), on(VIOLATION_1, "rel-1"), on(CONTROL_1, "unmapped")])
        counts = self.score()["conditions"]["without_rule"]
        self.assertEqual((counts["violation_flagged_runs"], counts["violation_labeled_runs"]), (2, 1))
        self.assertEqual((counts["control_flagged_runs"], counts["control_labeled_runs"]), (1, 0))

    def test_the_label_is_read_whatever_its_spelling(self) -> None:
        row = self.with_rule([on(VIOLATION_1, "REL.1"), on(VIOLATION_1, " Rel-1 "), on(VIOLATION_1, "rel_1")])
        self.assertEqual(row["conditions"]["with_rule"]["violation_labeled_runs"], 3)

    def test_a_finding_without_a_category_counts_as_unmapped(self) -> None:
        run = self.with_rule([on(VIOLATION_1)] * 3)["conditions"]["with_rule"]["runs"][0]
        self.assertEqual(run["violation_labels"], ["unmapped"])

    def test_a_finding_is_matched_to_its_file_however_the_path_is_written(self) -> None:
        for spelling in ("./code/store_a.py", "/work/scratch/code/store_a.py", "store_a.py", "code\\store_a.py"):
            with self.subTest(spelling):
                self.record_runs("fix-1", "with_rule", [on(spelling, "rel-1")] * 3)
                self.assertEqual(self.score()["conditions"]["with_rule"]["violation_labeled_runs"], 3)
        self.record_runs("fix-1", "with_rule", [on("./code/store_b.py", "rel-1")] * 3)
        self.assertEqual(self.score()["conditions"]["with_rule"]["control_labeled_runs"], 3)

    def test_the_recorded_fixture_with_a_stray_quote_parses_as_ingest_would_read_it(self) -> None:
        text = (GEN_FIXTURES / "outputs" / "stray_quotes.txt").read_text()
        self.assertEqual(self.with_rule([text] * 3)["conditions"]["with_rule"]["violation_labeled_runs"], 3)

    def test_the_recorded_fixture_review_counts_its_violation_and_ignores_the_other_file(self) -> None:
        text = (GEN_FIXTURES / "outputs" / "violation_labeled.txt").read_text()
        run = self.with_rule([text] * 3)["conditions"]["with_rule"]["runs"][0]
        self.assertEqual((run["findings"], run["violation_labels"], run["other_file_findings"]), (2, ["rel-1"], 1))

    def test_only_the_runs_asked_for_are_read(self) -> None:
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.assertEqual(self.score(runs=2)["conditions"]["with_rule"]["runs_ok"], 2)


class ReplayCommandTest(CheckCase):
    def record_everything(self) -> None:
        for case_id, violation in (("fix-1", VIOLATION_1), ("fix-2", VIOLATION_2)):
            label = "rel-1" if case_id == "fix-1" else "rel-2"
            self.record_runs(case_id, "with_rule", [on(violation, label)] * 3)
            self.record_runs(case_id, "without_rule", [NOTHING] * 3)

    def test_it_prints_a_table_and_writes_results_that_do_not_depend_on_the_clock(self) -> None:
        self.record_everything()
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("fix-1", out)
        self.assertIn("PASS 2  FAIL 0  INCOMPLETE 0", out)
        first = (self.layout.results_json.read_bytes(), self.layout.results_md.read_bytes())
        for path in self.layout.recordings_dir.glob("*/*.json"):  # a later date of recording is not a different result
            path.write_text(path.read_text().replace("2026-01-01T00:00:00Z", "2030-06-06T06:06:06Z"))
        self.assertEqual(self.check()[0], 0)
        self.assertEqual((self.layout.results_json.read_bytes(), self.layout.results_md.read_bytes()), first)
        self.assertNotIn(b"2026-01-01", first[0] + first[1])
        self.assertIn("| fix-1 | REL.1 | compliant | with_rule | 3/3 | 3/3 | 3/3 | 0/3 | 0/3 | PASS |", first[1].decode())
        self.assertEqual(json.loads(first[0])["totals"], {"PASS": 2, "FAIL": 0, "INCOMPLETE": 0})

    def test_a_case_without_recordings_is_incomplete(self) -> None:
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn("PASS 0  FAIL 0  INCOMPLETE 2", out)
        self.assertIn("fix-1 with_rule-1: missing (no recording)", out)
        self.assertIn("nothing is recorded yet", out)
        self.assertFalse(self.layout.results_json.exists())

    def test_a_partial_selection_does_not_write_results(self) -> None:
        self.record_everything()
        code, out, _ = self.check("--case", "fix-1")
        self.assertEqual(code, 0)
        self.assertIn("not written", out)
        self.assertFalse(self.layout.results_json.exists())

    def test_an_unknown_case_is_refused(self) -> None:
        code, _, err = self.check("--case", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown case ['nope']", err)

    def test_no_cases_at_all_is_refused(self) -> None:
        shutil.rmtree(self.layout.cases_dir)
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("no cases found", err)

    def test_a_case_file_that_does_not_fit_is_refused(self) -> None:
        path = self.layout.cases_dir / "fix-1" / "case.json"
        (self.layout.cases_dir / "fix-1" / "elsewhere").mkdir()
        shutil.copy(self.layout.cases_dir / "fix-1" / VIOLATION_1, self.layout.cases_dir / "fix-1" / "elsewhere" / "store_a.py")
        for change in ({"control_kind": "other"}, {"violation": "elsewhere/store_a.py"}, {"violation": "code/../elsewhere/store_a.py"},
                       {"control": VIOLATION_1},
                       {"id": "fix-9"}, {"v": 2}, {"violation": "code/missing.py"}, {"clause": ""}):
            with self.subTest(change):
                path.write_text(json.dumps({**json.loads((GEN_FIXTURES / "repo" / "samples/generalization/cases/fix-1/case.json").read_text()),
                                            **change}))
                self.assertEqual(self.check("--case", "fix-1")[0], 1)

    def test_a_base_file_without_a_counterpart_among_the_two_code_files_is_refused(self) -> None:
        case_dir = self.layout.cases_dir / "fix-2"
        (case_dir / "code" / "third.py").write_text("x = 2\n")  # a file of code/ that the case does not name
        (case_dir / "base" / "sub").mkdir()
        for name in ("other.py", "third.py", "sub/ids_a.py"):
            with self.subTest(name):
                (case_dir / "base" / name).write_text("x = 1\n")
                code, _, err = self.check("--case", "fix-2")
                self.assertEqual(code, 1)
                self.assertIn(f"base/{name}", err)
                self.assertIn("has no counterpart among the case's two code files", err)
                (case_dir / "base" / name).unlink()
        self.assertEqual(self.check("--case", "fix-2")[0], 0)

    def test_a_base_file_that_is_the_same_as_its_code_file_is_refused(self) -> None:
        case_dir = self.layout.cases_dir / "fix-2"
        shutil.copy(case_dir / "code" / "ids_a.py", case_dir / "base" / "ids_a.py")
        code, _, err = self.check("--case", "fix-2")
        self.assertEqual(code, 1)
        self.assertIn("is the same as code/ids_a.py", err)

    def test_a_base_that_is_not_a_directory_is_refused(self) -> None:
        (self.layout.cases_dir / "fix-1" / "base").write_text("not a directory")
        code, _, err = self.check("--case", "fix-1")
        self.assertEqual(code, 1)
        self.assertIn("must be a directory", err)

    def test_a_case_reads_the_files_of_its_base_directory_by_the_name_of_their_counterpart(self) -> None:
        self.assertEqual(self.case("fix-1").base, {})
        base = self.case("fix-2").base
        self.assertEqual(sorted(base), ["base/ids_a.py", "base/ids_b.py"])
        self.assertEqual(base["base/ids_a.py"], (self.layout.cases_dir / "fix-2" / "base" / "ids_a.py").read_bytes())
        self.assertEqual(rc.code_path_of("base/ids_a.py"), "code/ids_a.py")

    def test_a_run_count_other_than_the_pre_registered_one_is_said_to_be_off_the_criteria(self) -> None:
        self.record_everything()
        code, out, _ = self.check("--runs", "2")
        self.assertEqual(code, 0)
        self.assertIn("not pre-registered", out)

    def test_yes_spend_belongs_to_live(self) -> None:
        self.assertEqual(self.check("--yes-spend", "1")[0], 1)


class ProvenanceTest(CheckCase):
    def setUp(self) -> None:
        super().setUp()
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.record_runs("fix-1", "without_rule", [NOTHING] * 3)

    def assert_refused(self, *fragments: str) -> None:
        code, out, err = self.check("--case", "fix-1")
        self.assertEqual(code, 1, out)
        self.assertIn("refusing to score", err)
        for fragment in fragments:
            self.assertIn(fragment, err)
        self.assertEqual(out, "")

    def test_matching_provenance_is_scored(self) -> None:
        self.assertEqual(self.check("--case", "fix-1")[0], 0)

    def test_provenance_with_a_stderr_tail_key_and_without_one_are_scored_alike(self) -> None:
        before = self.check("--case", "fix-1")
        _, prov_path = self.layout.recording("fix-1", "with_rule", 1)
        prov = json.loads(prov_path.read_text())
        self.assertNotIn("stderr_tail", prov)  # what --live writes now
        prov_path.write_text(json.dumps({**prov, "stderr_tail": "kept by an older version"}, indent=2) + "\n")
        self.assertEqual(self.check("--case", "fix-1"), before)

    def test_a_changed_code_file_refuses_the_replay(self) -> None:
        path = self.layout.cases_dir / "fix-1" / VIOLATION_1
        path.write_text(path.read_text() + "# edited after the recording\n")
        self.assert_refused("fix-1/with_rule-1: code/store_a.py is not the file that was reviewed")

    def test_a_changed_control_file_refuses_the_replay(self) -> None:
        path = self.layout.cases_dir / "fix-1" / CONTROL_1
        path.write_text(path.read_text() + "# edited after the recording\n")
        self.assert_refused("code/store_b.py is not the file that was reviewed")

    def test_a_base_file_changed_after_the_recording_refuses_the_replay(self) -> None:
        self.record_runs("fix-2", "with_rule", [on(VIOLATION_2, "rel-2")] * 3)
        path = self.layout.cases_dir / "fix-2" / "base" / "ids_a.py"
        path.write_text(path.read_text() + "# edited after the recording\n")
        code, out, err = self.check("--case", "fix-2")
        self.assertEqual(code, 1, out)
        self.assertIn("fix-2/with_rule-1: base/ids_a.py is not the file that was reviewed", err)

    def test_a_recording_that_did_not_cover_the_base_files_refuses_the_replay(self) -> None:
        self.record("fix-2", "with_rule", 1, on(VIOLATION_2, "rel-2"), base_sha256={})
        code, _, err = self.check("--case", "fix-2")
        self.assertEqual(code, 1)
        self.assertIn("the review covered the base files []", err)

    def test_a_modified_snapshot_refuses_the_replay(self) -> None:
        snapshot = self.layout.snapshot("fix-1", "with_rule")
        snapshot.write_text(snapshot.read_text() + "\nAn extra line.\n")
        self.assert_refused("fix-1/with_rule-1: the digest it ran with is not the one in its snapshot fix-1/with_rule.digest.md")

    def test_a_missing_snapshot_refuses_the_replay(self) -> None:
        self.layout.snapshot("fix-1", "without_rule").unlink()
        self.assert_refused("fix-1/without_rule-1: its digest snapshot fix-1/without_rule.digest.md is missing")

    def test_a_with_rule_snapshot_that_does_not_list_the_clause_refuses_the_replay(self) -> None:
        digest = self.digests("REL.1").without["REL.1"]
        self.layout.snapshot("fix-1", "with_rule").write_text(digest)
        for run in (1, 2, 3):
            _, prov_path = self.layout.recording("fix-1", "with_rule", run)
            prov = json.loads(prov_path.read_text())
            prov["digest_sha256"] = hashlib.sha256(digest.encode()).hexdigest()
            prov_path.write_text(json.dumps(prov))
        self.assert_refused("fix-1/with_rule.digest.md: does not list REL.1 exactly once")

    def test_a_without_rule_snapshot_that_names_the_clause_refuses_the_replay(self) -> None:
        digest = self.digests("REL.1").full
        self.layout.snapshot("fix-1", "without_rule").write_text(digest)
        for run in (1, 2, 3):
            _, prov_path = self.layout.recording("fix-1", "without_rule", run)
            prov = json.loads(prov_path.read_text())
            prov["digest_sha256"] = hashlib.sha256(digest.encode()).hexdigest()
            prov_path.write_text(json.dumps(prov))
        self.assert_refused("fix-1/without_rule.digest.md: still names REL.1")

    def test_an_attempt_number_that_does_not_fit_the_files_on_disk_refuses_the_replay(self) -> None:
        self.record("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"), attempt=2)  # says it was a repeat, but no first attempt is on disk
        self.assert_refused("fix-1/with_rule-3: recorded attempt 2, expected 1")

    def test_a_repeat_whose_first_attempt_did_not_fail_refuses_the_replay(self) -> None:
        self.record("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"), failed_first_attempt=True)  # a first attempt that was fine
        self.record("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"), attempt=2)
        self.assert_refused("fix-1/with_rule-3.attempt1: the run was repeated although its first attempt did not fail")

    def test_a_first_attempt_is_checked_like_any_other_recording(self) -> None:
        self.record_repeated("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"))
        raw_path, _ = self.layout.recording("fix-1", "with_rule", 3, failed_first_attempt=True)
        raw_path.write_text("something else")
        self.assert_refused("fix-1/with_rule-3.attempt1.txt: the raw output was changed after it was recorded")

    def test_a_modified_raw_output_refuses_the_replay(self) -> None:
        raw_path, _ = self.layout.recording("fix-1", "with_rule", 2)
        raw_path.write_text(raw_path.read_text() + "\nAlso: tampered.\n")
        self.assert_refused("fix-1/with_rule-2.txt: the raw output was changed after it was recorded")

    def test_a_recording_filed_under_another_condition_refuses_the_replay(self) -> None:
        raw, prov = self.layout.recording("fix-1", "with_rule", 1)
        other_raw, other_prov = self.layout.recording("fix-1", "without_rule", 1)
        shutil.copy(raw, other_raw)
        shutil.copy(prov, other_prov)
        self.assert_refused("fix-1/without_rule-1: recorded condition 'with_rule', expected 'without_rule'")

    def test_a_recording_made_at_another_level_refuses_the_replay(self) -> None:
        self.record("fix-1", "with_rule", 2, on(VIOLATION_1, "rel-1"), level="max")
        self.assert_refused("recorded level 'max', expected 'high'")

    def test_a_recording_of_another_run_refuses_the_replay(self) -> None:
        for old, new in zip(self.layout.recording("fix-1", "with_rule", 1), self.layout.recording("fix-1", "with_rule", 3)):
            shutil.copy(old, new)  # the third run's files hold what the first run recorded
        self.assert_refused("recorded run 1, expected 3")

    def test_a_recording_of_other_files_refuses_the_replay(self) -> None:
        self.record("fix-1", "with_rule", 3, on(VIOLATION_1, "rel-1"), code_sha256={VIOLATION_1: "0" * 64})
        self.assert_refused("the review covered the files")

    def test_unreadable_provenance_refuses_the_replay(self) -> None:
        self.layout.recording("fix-1", "with_rule", 1)[1].write_text("{not json")
        self.assert_refused("not valid JSON")

    def test_nothing_is_written_when_the_replay_is_refused(self) -> None:
        self.layout.recording("fix-1", "with_rule", 1)[0].write_text("tampered")
        self.assertEqual(self.check()[0], 1)
        self.assertFalse(self.layout.results_json.exists())
        self.assertFalse(self.layout.results_md.exists())


class RulesChangedAfterRecordingTest(CheckCase):
    """A recording keeps the digest it was made with: a later edit of the rules is a normal event, not a reason to refuse it."""

    def setUp(self) -> None:
        super().setUp()
        self.record_runs("fix-1", "with_rule", [on(VIOLATION_1, "rel-1")] * 3)
        self.record_runs("fix-1", "without_rule", [NOTHING] * 3)

    def edit_rules(self, old: str, new: str) -> None:
        path = self.root / "CODING-STYLE.md"
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))
        self.compile_digest()

    def test_an_edit_of_the_clause_does_not_break_the_replay_and_is_noted(self) -> None:
        self.edit_rules("Keep usable state until the replacement is ready", "Keep the old copy until the new one is ready")
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("fix-1: the rules changed since recording", out)
        self.assertIn("the scores describe the recorded digests", out)
        self.assertIn("PASS 1  FAIL 0  INCOMPLETE 1", out)
        results = json.loads(self.layout.results_json.read_text())
        self.assertEqual([case["notes"] for case in results["cases"]],
                         [["fix-1: the rules changed since recording: the REL.1 line of the digest now differs from the recorded one; "
                           "the scores describe the recorded digests"], []])
        self.assertIn("the rules changed since recording", self.layout.results_md.read_text())

    def test_an_edit_that_leaves_the_digest_line_of_the_clause_alone_is_not_noted(self) -> None:
        self.edit_rules("Scope: installed files", "Scope: installed or deployed files")
        self.assertNotEqual(self.current_digest().splitlines()[0], self.layout.snapshot("fix-1", "with_rule").read_text().splitlines()[0])
        code, out, _ = self.check("--case", "fix-1")
        self.assertEqual(code, 0)
        self.assertNotIn("rules changed", out)
        self.assertIn("PASS 1", out)

    def test_taking_the_clause_out_of_the_rules_still_replays_with_a_note(self) -> None:
        path = self.root / "CODING-STYLE.md"
        text = path.read_text()
        start = text.index("1. **[REL.1]**")
        path.write_text(text[:start] + text[text.index("2. **[REL.2]**"):])
        self.compile_digest()
        self.assertNotIn("REL.1 |", self.current_digest())
        code, out, err = self.check("--case", "fix-1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("fix-1: the rules changed since recording", out)
        self.assertIn("PASS 1", out)

    def test_a_digest_file_that_is_gone_is_noted_too(self) -> None:
        (self.root / "CODING-STYLE.digest.md").unlink()
        code, out, _ = self.check("--case", "fix-1")
        self.assertEqual(code, 0)
        self.assertIn("the rules changed since recording", out)

    def test_a_replay_without_any_edit_has_no_note(self) -> None:
        code, out, _ = self.check("--case", "fix-1")
        self.assertEqual(code, 0)
        self.assertNotIn("rules changed", out)
        self.assertNotIn("notes:", out)


class WithoutRuleDigestTest(CheckCase):
    @staticmethod
    def listed(digest: str, heading: str) -> list[str]:
        """The lines under `## heading` up to the next blank line."""
        lines = digest.splitlines()
        start = lines.index(f"## {heading}") + 1
        end = next((i for i in range(start, len(lines)) if not lines[i].strip()), len(lines))
        return lines[start:end]

    def test_it_lacks_exactly_that_clause_and_keeps_all_others(self) -> None:
        digests = self.digests("REL.1")
        full, without = self.listed(digests.full, "Violations"), self.listed(digests.without["REL.1"], "Violations")
        self.assertEqual([line.split(" | ")[0] for line in full], ["P0E.1", "REL.1", "REL.2", "REL.3"])
        self.assertEqual(without, [line for line in full if not line.startswith("REL.1 | ")])
        self.assertEqual(self.listed(digests.without["REL.1"], "Reviewer rules"), self.listed(digests.full, "Reviewer rules"))
        self.assertIn("REL.1 | should-fix |", digests.full)

    def test_the_removed_clause_is_not_named_anywhere_in_it(self) -> None:
        digest = self.digests("REL.1").without["REL.1"]
        for spelling in ("REL.1", "rel-1", "Keep usable state"):
            self.assertNotIn(spelling, digest)

    def test_the_labeling_section_is_untouched_unless_the_first_clause_went(self) -> None:
        digests = self.digests("REL.1", "P0E.1")
        labeling = digests.full[digests.full.index("## Labeling"):]
        self.assertTrue(digests.without["REL.1"].endswith(labeling))
        self.assertIn("`P0E.1` -> `p0e-1`", digests.full)
        self.assertIn("`REL.1` -> `rel-1`", digests.without["P0E.1"])
        self.assertNotIn("P0E.1", digests.without["P0E.1"])

    def test_it_differs_from_the_shipped_digest_by_that_one_line_and_nothing_else(self) -> None:
        digests = self.digests("REL.1", "REL.2")
        for clause in ("REL.1", "REL.2"):
            with self.subTest(clause):
                self.assertEqual(digests.without[clause].splitlines(),
                                 [line for line in digests.full.splitlines() if not line.startswith(f"{clause} | ")])
        self.assertTrue(digests.full.startswith("<!-- generated by house-rules compile from CODING-STYLE.md@"))
        self.assertEqual(digests.without["REL.1"].splitlines()[0], digests.full.splitlines()[0])  # the hash of the rules file is not a difference

    def test_the_real_rules_file_is_not_touched(self) -> None:
        before = (self.root / "CODING-STYLE.md").read_bytes()
        self.digests("REL.1", "REL.2")
        self.assertEqual((self.root / "CODING-STYLE.md").read_bytes(), before)

    def test_with_rule_is_the_digest_the_repo_ships(self) -> None:
        self.assertEqual(self.digests("REL.1").full, (self.root / "CODING-STYLE.digest.md").read_text())

    def test_a_stale_shipped_digest_is_refused(self) -> None:
        path = self.root / "CODING-STYLE.md"
        path.write_text(path.read_text().replace("Keep the guarded block", "Keep the try block"))
        with self.assertRaisesRegex(HouseRulesError, "house-rules compile"):
            self.digests("REL.1")

    def test_a_clause_that_is_not_defined_is_refused(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "ZZ.9 is not defined"):
            self.digests("ZZ.9")

    def test_a_reviewer_rule_is_refused(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "R.1 is a Reviewer rule"):
            self.digests("R.1")

    def test_a_clause_whose_block_holds_the_severity_table_is_refused(self) -> None:
        with self.assertRaisesRegex(HouseRulesError, "changed more than that clause"):
            self.digests("REL.3")

    def test_a_clause_that_takes_another_clause_with_it_is_refused(self) -> None:
        path = self.root / "CODING-STYLE.md"
        path.write_text(path.read_text().replace("   A display-only change is exempt.\n", "   A display-only change is exempt.\n   - [R.9] Guidance nested in the block.\n"))
        self.compile_digest()
        with self.assertRaisesRegex(HouseRulesError, "changed more than that clause"):
            self.digests("REL.2")

    def test_the_digest_check_compares_the_labeling_section_unless_the_first_clause_went(self) -> None:
        full = "<!-- h -->\n# Style digest\n\n## Violations\nA.1 | blocker | a\nA.2 | nice | b\n\n## Labeling\nexample a-1\n"
        second_went = "<!-- h -->\n# Style digest\n\n## Violations\nA.1 | blocker | a\n\n## Labeling\n"
        rc.check_digest_lost_only(full, second_went + "example a-1\n", "A.2")
        with self.assertRaisesRegex(HouseRulesError, "more than that clause's line"):
            rc.check_digest_lost_only(full, second_went + "example other\n", "A.2")
        first_went = "<!-- h -->\n# Style digest\n\n## Violations\nA.2 | nice | b\n\n## Labeling\nexample a-2\n"
        rc.check_digest_lost_only(full, first_went, "A.1")
        with self.assertRaisesRegex(HouseRulesError, "more than that clause's line"):
            rc.check_digest_lost_only(full, first_went.replace("A.2 | nice | b", "A.2 | nice | changed"), "A.1")

    def test_a_clause_another_clause_still_names_is_refused(self) -> None:
        path = self.root / "CODING-STYLE.md"
        path.write_text(path.read_text().replace("(should-fix)\n   A display-only", "(should-fix) <!-- digest: Like REL.1, but for ids -->\n   A display-only"))
        self.compile_digest()
        with self.assertRaisesRegex(HouseRulesError, "still mentions it"):
            self.digests("REL.1")

    def test_remove_clause_block_takes_the_indented_lines_and_the_blank_line_after_it(self) -> None:
        text = "1. **[A.1]** one\n   more\n\n2. **[A.2]** two\n   more\n"
        self.assertEqual(rc.remove_clause_block(text, 1), "2. **[A.2]** two\n   more\n")
        self.assertEqual(rc.remove_clause_block(text, 4), "1. **[A.1]** one\n   more\n\n")

    def test_remove_clause_block_takes_one_line_of_a_table_row(self) -> None:
        text = "| A.1 | nice | one |\n| A.2 | nice | two |\n\nafter\n"
        self.assertEqual(rc.remove_clause_block(text, 1), "| A.2 | nice | two |\n\nafter\n")
        self.assertEqual(rc.remove_clause_block(text, 2), "| A.1 | nice | one |\n\nafter\n")
        self.assertEqual(rc.remove_clause_block("| A.1 | nice | one |\n\nafter\n", 1), "\nafter\n")  # the blank line is not the row's


class LiveTest(CheckCase):
    FAKE = r'''
import json, os, subprocess, sys

args = sys.argv[1:]
if "--version" in args:
    sys.stdout.write("fake-reviewer 9.9.9")
    sys.exit(0)

def git(*git_args):
    return subprocess.run(["git", *git_args], capture_output=True, text=True).stdout

plan = json.load(open(os.environ["FAKE_PLAN"]))
log_path = os.environ["FAKE_LOG"]
number = 1 + (sum(1 for _ in open(log_path)) if os.path.exists(log_path) else 0)
review_range = args[-1].split()[-1]
base, head = review_range.split("..")
digest = git("show", base + ":CODING-STYLE.digest.md")
changed = sorted(git("diff", "--name-only", review_range).split())
if plan.get("stderr_line"):
    sys.stderr.write(plan["stderr_line"] + "\n")  # what a real reviewer says about its settings, on any run
with open(log_path, "a") as log:
    log.write(json.dumps({"n": number, "args": args, "stderr_line": plan.get("stderr_line"), "cwd": os.getcwd(), "child": os.environ.get("HOUSE_RULES_REVIEW_CHILD"),
                          "session": os.environ.get("CLAUDE_CODE_SESSION_ID"), "changed": changed,
                          "base_files": sorted(git("ls-tree", "-r", "--name-only", base).split()),
                          "claude_md": git("show", base + ":CLAUDE.md"), "digest": digest,
                          "at_base": {name: git("show", base + ":" + name) for name in changed},
                          "at_head": {name: git("show", head + ":" + name) for name in changed},
                          "commits": git("rev-list", "--count", review_range).strip()}) + "\n")
if number in plan.get("fail_calls", []):
    sys.stdout.write("partial output")
    sys.stderr.write("".join(f"note {i}\n" for i in range(1, 8)) + "quota exceeded\n")
    sys.exit(1)
if number in plan.get("prose_calls", []):
    sys.stdout.write("No findings, nothing to report.\n")
    sys.exit(0)
findings = []
for file, (clause, label) in plan["violations"].items():
    if file in changed and clause + " | " in digest:  # it names the violation only if the digest teaches the clause
        findings.append({"file": file, "line": 1, "summary": "s", "failure_scenario": "f", "category": label, "severity": "should-fix"})
sys.stdout.write(plan.get("preface", "Reviewed the diff.") + "\n\n```json\n" + json.dumps(findings, indent=2) + "\n```\n")
'''

    def setUp(self) -> None:
        super().setUp()
        self.script = self.tmp / "fake-reviewer"
        self.script.write_text(f"#!{sys.executable}\n{self.FAKE}", encoding="utf-8")
        self.script.chmod(0o755)
        self.log = self.tmp / "fake-calls.jsonl"
        self.plan_path = self.tmp / "fake-plan.json"
        self.set_plan()
        patcher = mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(self.script), "FAKE_LOG": str(self.log),
                                               "FAKE_PLAN": str(self.plan_path), "CLAUDE_CODE_SESSION_ID": "session-of-the-caller"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_plan(self, **plan: object) -> None:
        plan.setdefault("violations", {VIOLATION_1: ["REL.1", "rel-1"], VIOLATION_2: ["REL.2", "rel-2"]})
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def condition_of(self, call: dict) -> str:
        """Which condition a call ran in: whether the digest in the scratch repo teaches the clause of its case."""
        clause = "REL.1" if VIOLATION_1 in call["changed"] else "REL.2"
        return "with_rule" if f"{clause} | " in call["digest"] else "without_rule"

    def test_without_yes_spend_it_makes_no_call_and_exits_2(self) -> None:
        code, out, err = self.check("--live")
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])
        self.assertIn("12 reviewer call(s) planned", out)
        self.assertIn("plus at most 12 more as repeats (one per run at most", out)
        self.assertIn("--yes-spend must equal the 12 planned calls, repeats are covered by it", out)
        self.assertIn("--yes-spend 12", err)
        self.assertFalse(self.layout.recordings_dir.exists())  # not even a digest snapshot before the spending is confirmed

    def test_a_yes_spend_that_is_not_the_planned_number_makes_no_call(self) -> None:
        for wrong in ("0", "11", "13", "24"):
            with self.subTest(wrong):
                self.assertEqual(self.check("--live", "--yes-spend", wrong)[0], 2)
        self.assertEqual(self.calls(), [])

    def test_the_planned_number_follows_the_cases_and_runs_asked_for(self) -> None:
        code, out, _ = self.check("--live", "--case", "fix-2", "--runs", "2")
        self.assertEqual(code, 2)
        self.assertIn("4 reviewer call(s) planned", out)
        self.assertEqual(self.check("--live", "--case", "fix-2", "--runs", "2", "--yes-spend", "4")[0], 0)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual({tuple(call["changed"]) for call in self.calls()}, {tuple(sorted([VIOLATION_2, CONTROL_2]))})

    def test_the_matching_yes_spend_makes_exactly_the_planned_calls_and_writes_every_recording(self) -> None:
        code, out, err = self.check("--live", "--yes-spend", "12")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.calls()), 12)
        self.assertEqual(sorted(path.relative_to(self.layout.recordings_dir).as_posix() for path in self.layout.recordings_dir.glob("*/*")),
                         sorted([f"{case}/{condition}-{run}.{ext}" for case in ("fix-1", "fix-2")
                                 for condition in ("with_rule", "without_rule") for run in (1, 2, 3) for ext in ("txt", "json")]
                                + [f"{case}/{condition}.digest.md" for case in ("fix-1", "fix-2") for condition in ("with_rule", "without_rule")]))
        self.assertIn("recorded 12 run(s): 0 needed a repeat, 0 still failed", out)

    def test_each_call_reviews_exactly_the_two_files_as_a_range_in_a_scratch_repo(self) -> None:
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        for call in self.calls():
            self.assertEqual(call["args"][0], "-p")
            self.assertRegex(call["args"][1], r"^/code-review high [0-9a-f]{40}\.\.[0-9a-f]{40}$")
            self.assertEqual(len(call["args"]), 2)
            self.assertEqual(call["changed"], sorted([VIOLATION_1, CONTROL_1]))
            self.assertEqual(call["base_files"], ["CLAUDE.md", "CODING-STYLE.digest.md"])
            self.assertEqual(call["commits"], "1")
            self.assertEqual(call["claude_md"], "## Code style digest (house-rules)\n@CODING-STYLE.digest.md\n")
            self.assertFalse(Path(call["cwd"]).is_relative_to(self.root))
            self.assertFalse(Path(call["cwd"]).exists(), "the scratch repo is removed afterwards")

    def test_the_stderr_of_a_run_that_worked_is_kept_nowhere(self) -> None:
        secret = "/home/someone/secret-project"
        warning = f"Permission allow rule (settings.json): Bash(find {secret}* -type f) has a wildcard before the rest of the command"
        self.set_plan(stderr_line=warning)
        code, out, err = self.check("--live", "--yes-spend", "12")
        self.assertEqual((code, err), (0, ""))  # nothing is shown on the console for a run that worked
        self.assertEqual({call["stderr_line"] for call in self.calls()}, {warning})  # the reviewer did say it, on every call
        files = [path for path in self.layout.recordings_dir.rglob("*") if path.is_file()]
        self.assertEqual(len(files), 12 * 2 + 4)  # raw and provenance of every run, a snapshot of every case and condition, nothing else
        self.assertEqual([path.name for path in files if "stderr" in path.name], [])
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, text, path.name)
            if path.suffix == ".json":
                self.assertNotIn("stderr", text, path.name)  # no key for it either
        self.assertNotIn(secret, out)

    def test_the_child_marker_is_set_and_the_session_id_is_gone(self) -> None:
        self.assertEqual(os.environ["CLAUDE_CODE_SESSION_ID"], "session-of-the-caller")
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        self.assertEqual([(call["child"], call["session"]) for call in self.calls()], [("1", None), ("1", None)])

    def test_with_rule_gets_the_full_digest_and_without_rule_the_digest_without_the_clause(self) -> None:
        digests = self.digests("REL.1")
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        by_condition = {self.condition_of(call): call["digest"] for call in self.calls()}
        self.assertEqual(by_condition, {"with_rule": digests.full, "without_rule": digests.without["REL.1"]})

    def test_the_conditions_alternate_so_that_drift_falls_on_both(self) -> None:
        self.assertEqual(self.check("--live", "--yes-spend", "12")[0], 0)
        conditions = [self.condition_of(call) for call in self.calls()]
        self.assertEqual(len(conditions), 12)
        for start in range(0, 12, 2):
            self.assertEqual(sorted(conditions[start:start + 2]), ["with_rule", "without_rule"])
        first_of_fix_1 = [conditions[start] for start, call in zip(range(0, 12, 2), self.calls()[::2]) if VIOLATION_1 in call["changed"]]
        self.assertEqual(len(first_of_fix_1), 3)
        self.assertEqual([a != b for a, b in zip(first_of_fix_1, first_of_fix_1[1:])], [True, True])

    def test_the_plan_is_run_by_run(self) -> None:
        jobs = rc.plan_jobs([self.case("fix-1"), self.case("fix-2")], 2)
        self.assertEqual([(job.case.id, job.condition, job.run) for job in jobs],
                         [("fix-1", "without_rule", 1), ("fix-1", "with_rule", 1), ("fix-2", "with_rule", 1), ("fix-2", "without_rule", 1),
                          ("fix-1", "with_rule", 2), ("fix-1", "without_rule", 2), ("fix-2", "without_rule", 2), ("fix-2", "with_rule", 2)])

    def test_the_provenance_of_a_recording_says_what_produced_it(self) -> None:
        self.set_plan(violations={VIOLATION_1: ["REL.1", "rel-1"]}, preface="Reviewed with claude-sonnet-4-5 today.")
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        raw_path, prov_path = self.layout.recording("fix-1", "without_rule", 1)
        prov = json.loads(prov_path.read_text())
        digests = self.digests("REL.1")
        call = next(call for call in self.calls() if self.condition_of(call) == "without_rule")
        self.assertEqual(prov["v"], 1)
        self.assertEqual((prov["case"], prov["condition"], prov["run"], prov["attempt"], prov["level"]), ("fix-1", "without_rule", 1, 1, "high"))
        self.assertEqual(prov["base_sha256"], {})  # a case without base/ names no base files
        self.assertRegex(prov["date_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(prov["command"], [str(self.script), "-p", call["args"][1]])
        self.assertEqual(prov["range"], call["args"][1].split()[-1])
        self.assertEqual(prov["reviewer_version"], "fake-reviewer 9.9.9")
        self.assertEqual(prov["code_sha256"], {rel: hashlib.sha256((self.layout.cases_dir / "fix-1" / rel).read_bytes()).hexdigest()
                                               for rel in (VIOLATION_1, CONTROL_1)})
        self.assertEqual(prov["digest_sha256"], hashlib.sha256(digests.without["REL.1"].encode()).hexdigest())
        self.assertEqual(prov["clause_removed"], "REL.1")
        self.assertEqual((prov["exit_code"], prov["error"], prov["status"], prov["failure"]), (0, None, "ok", None))
        self.assertEqual(prov["model_id"], "claude-sonnet-4-5")
        self.assertEqual(prov["raw_sha256"], hashlib.sha256(raw_path.read_bytes()).hexdigest())
        with_prov = json.loads(self.layout.recording("fix-1", "with_rule", 1)[1].read_text())
        self.assertEqual((with_prov["clause_removed"], with_prov["digest_sha256"]), (None, hashlib.sha256(digests.full.encode()).hexdigest()))

    def test_a_failed_call_is_repeated_once_and_both_attempts_are_kept(self) -> None:
        self.set_plan(fail_calls=[2])  # the second call is the with_rule review of fix-1 (see plan_jobs)
        code, out, err = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 0)
        self.assertEqual([self.condition_of(call) for call in self.calls()], ["without_rule", "with_rule", "with_rule"])  # the repeat is at once
        self.assertIn("recorded 2 run(s): 1 needed a repeat, 0 still failed", out)
        first_raw, first_prov = self.layout.recording("fix-1", "with_rule", 1, failed_first_attempt=True)
        first = json.loads(first_prov.read_text())
        self.assertEqual(first_raw.read_text(), "partial output")
        self.assertEqual((first["status"], first["exit_code"], first["attempt"]), ("failed", 1, 1))
        self.assertIn("exited with 1", first["failure"])
        self.assertNotIn("stderr_tail", first)  # the provenance holds no stderr, of a failed run either
        self.assertNotIn("quota exceeded", first_prov.read_text())
        self.assert_failed_stderr_shown_and_kept(err, "with_rule-1.attempt1.stderr.txt", attempt=1)
        self.assertEqual(sorted(path.name for path in self.layout.recordings_dir.glob("fix-1/*.stderr.txt")), ["with_rule-1.attempt1.stderr.txt"])
        last_raw, last_prov = self.layout.recording("fix-1", "with_rule", 1)
        last = json.loads(last_prov.read_text())
        self.assertEqual((last["status"], last["attempt"], last["exit_code"]), ("ok", 2, 0))
        self.assertNotEqual(last_raw.read_text(), "partial output")
        self.assertEqual([path.exists() for path in self.layout.recording("fix-1", "without_rule", 1, failed_first_attempt=True)], [False, False])
        run = self.score("fix-1", 1)["conditions"]["with_rule"]["runs"][0]  # replay scores the second attempt
        self.assertEqual((run["status"], run["attempts"], run["violation_labels"]), ("ok", 2, ["rel-1"]))
        self.assertIn("exited with 1", run["first_attempt_failure"])

    def assert_failed_stderr_shown_and_kept(self, err: str, file_name: str, *, attempt: int) -> None:
        """The console got the last 5 lines of the stderr of the failed call, and `file_name` next to the recording holds all 8."""
        self.assertIn(f"fix-1/with_rule-1 attempt {attempt} failed", err)
        self.assertIn(file_name, err)
        for line in ("note 4", "note 5", "note 6", "note 7", "quota exceeded"):
            self.assertIn(line, err)
        self.assertNotIn("note 3", err)  # only the end of it goes to the console
        side = (self.layout.recordings_dir / "fix-1" / file_name).read_text()
        self.assertEqual(side, "".join(f"note {i}\n" for i in range(1, 8)) + "quota exceeded\n")  # the file has all of it

    def test_a_run_that_fails_twice_stays_failed_and_makes_the_case_incomplete(self) -> None:
        self.set_plan(fail_calls=[2, 3])
        code, out, err = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(path.name for path in self.layout.recordings_dir.glob("fix-1/*.stderr.txt")),
                         ["with_rule-1.attempt1.stderr.txt", "with_rule-1.stderr.txt"])  # one for each failed attempt
        self.assertIn("attempt 1 failed", err)
        self.assertIn("attempt 2 failed", err)
        for path in self.layout.recordings_dir.glob("fix-1/*.json"):
            self.assertNotIn("quota exceeded", path.read_text())
        self.assertEqual(len(self.calls()), 3)
        self.assertIn("recorded 2 run(s): 1 needed a repeat, 1 still failed", out)
        for failed_first_attempt in (True, False):
            _, prov_path = self.layout.recording("fix-1", "with_rule", 1, failed_first_attempt=failed_first_attempt)
            self.assertEqual(json.loads(prov_path.read_text())["status"], "failed")
        code, out, _ = self.check("--case", "fix-1", "--runs", "1")
        self.assertEqual(code, 0)
        self.assertIn("INCOMPLETE 1", out)

    def test_no_run_is_repeated_more_than_once(self) -> None:
        self.set_plan(fail_calls=list(range(1, 20)))
        code, out, _ = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls()), 4)  # two run slots, two attempts each
        self.assertIn("recorded 2 run(s): 2 needed a repeat, 2 still failed", out)

    def test_a_run_that_did_not_fail_is_never_repeated(self) -> None:
        self.assertEqual(self.check("--live", "--yes-spend", "12")[0], 0)
        self.assertEqual(len(self.calls()), 12)
        self.assertEqual(list(self.layout.recordings_dir.glob("*/*.attempt1.*")), [])
        self.assertEqual({json.loads(path.read_text())["attempt"] for path in self.layout.recordings_dir.glob("*/*.json")}, {1})

    def test_output_without_json_findings_is_a_failed_run_and_is_repeated_but_an_empty_array_is_not(self) -> None:
        self.set_plan(prose_calls=[1])
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        self.assertEqual(len(self.calls()), 3)
        first = rc.plan_jobs([self.case("fix-1")], 1)[0]
        raw_path, prov_path = self.layout.recording("fix-1", first.condition, 1, failed_first_attempt=True)
        self.assertEqual(raw_path.read_text(), "No findings, nothing to report.\n")
        self.assertEqual(json.loads(prov_path.read_text())["status"], "failed")
        self.assertEqual(list(self.layout.recordings_dir.glob("*/*.stderr.txt")), [])  # it said nothing on stderr: no empty file
        calls_so_far = len(self.calls())
        self.set_plan(violations={})  # a review that finds nothing prints an empty array: a run with 0 findings
        self.assertEqual(self.check("--live", "--case", "fix-2", "--runs", "1", "--yes-spend", "2")[0], 0)
        self.assertEqual(len(self.calls()) - calls_so_far, 2)
        self.assertEqual(list(self.layout.recordings_dir.glob("fix-2/*.attempt1.*")), [])

    def test_a_run_left_with_only_its_failed_first_attempt_gets_just_the_repeat(self) -> None:
        self.record("fix-1", "with_rule", 1, "partial output", exit_code=1, failed_first_attempt=True)
        code, _, err = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertIn("recordings already exist: fix-1/with_rule-1", err)
        self.assertEqual(self.calls(), [])
        code, out, _ = self.check("--live", "--case", "fix-1", "--runs", "1", "--skip-existing")
        self.assertEqual(code, 2)
        self.assertIn("2 reviewer call(s) planned", out)
        self.assertIn("plus at most 1 more as repeats", out)  # the run that has its first attempt already is not given another repeat
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--skip-existing", "--yes-spend", "2")[0], 0)
        self.assertEqual(len(self.calls()), 2)
        first_raw, first_prov = self.layout.recording("fix-1", "with_rule", 1, failed_first_attempt=True)
        self.assertEqual(first_raw.read_text(), "partial output")
        self.assertEqual(json.loads(first_prov.read_text())["attempt"], 1)
        self.assertEqual(json.loads(self.layout.recording("fix-1", "with_rule", 1)[1].read_text())["attempt"], 2)
        self.assertEqual(self.score("fix-1", 1)["conditions"]["with_rule"]["runs"][0]["attempts"], 2)

    def test_a_reviewer_that_cannot_be_started_is_a_failed_run(self) -> None:
        self.script.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")  # executable, so the PATH check passes; exec fails
        code, out, _ = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        prov = json.loads(self.layout.recording("fix-1", "with_rule", 1)[1].read_text())
        self.assertEqual((prov["status"], prov["exit_code"], prov["raw_sha256"]), ("failed", None, hashlib.sha256(b"").hexdigest()))
        self.assertIn("cannot run", prov["error"])
        self.assertIn("cannot run", prov["reviewer_version_error"])

    def test_a_command_that_is_not_installed_refuses_before_any_spending(self) -> None:
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": "/nonexistent/claude"}):
            code, _, err = self.check("--live", "--yes-spend", "12")
        self.assertEqual(code, 1)
        self.assertIn("not found on PATH", err)
        self.assertFalse(self.layout.recordings_dir.exists())

    def test_existing_recordings_are_never_overwritten(self) -> None:
        self.record("fix-1", "with_rule", 1, NOTHING)
        before = self.layout.recording("fix-1", "with_rule", 1)[0].read_bytes()
        code, _, err = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertIn("fix-1/with_rule-1", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.layout.recording("fix-1", "with_rule", 1)[0].read_bytes(), before)

    def test_skip_existing_records_only_the_runs_that_have_none(self) -> None:
        self.record("fix-1", "with_rule", 1, NOTHING)
        code, out, _ = self.check("--live", "--case", "fix-1", "--runs", "1", "--skip-existing", "--yes-spend", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual([self.condition_of(call) for call in self.calls()], ["without_rule"])
        self.assertEqual(self.layout.recording("fix-1", "with_rule", 1)[0].read_text(), NOTHING)

    def test_with_everything_recorded_skip_existing_has_nothing_to_do(self) -> None:
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        calls = len(self.calls())
        code, out, _ = self.check("--live", "--case", "fix-1", "--runs", "1", "--skip-existing")
        self.assertEqual((code, len(self.calls())), (0, calls))
        self.assertIn("nothing to record", out)

    def test_recordings_made_live_replay_to_a_pass_for_with_rule_only(self) -> None:
        self.assertEqual(self.check("--live", "--yes-spend", "12")[0], 0)
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("PASS 2  FAIL 0  INCOMPLETE 0", out)
        results = json.loads(self.layout.results_json.read_text())
        self.assertEqual(results["reviewer_versions"], ["fake-reviewer 9.9.9"])
        for case in results["cases"]:
            self.assertEqual(case["conditions"]["with_rule"]["violation_labeled_runs"], 3)
            self.assertEqual(case["conditions"]["without_rule"]["violation_flagged_runs"], 0)

    def test_a_with_rule_call_that_fails_again_makes_that_case_incomplete_after_a_live_run(self) -> None:
        self.set_plan(fail_calls=[2, 3])
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 1)
        self.assertEqual(self.check("--case", "fix-1", "--runs", "1")[1].count("INCOMPLETE 1"), 1)

    def test_a_case_with_base_files_is_reviewed_as_a_modification_of_them(self) -> None:
        self.assertEqual(self.check("--live", "--case", "fix-2", "--runs", "1", "--yes-spend", "2")[0], 0)
        case_dir = self.layout.cases_dir / "fix-2"
        self.assertEqual(len(self.calls()), 2)
        for call in self.calls():
            self.assertEqual(call["changed"], sorted([VIOLATION_2, CONTROL_2]))
            self.assertEqual(call["base_files"], ["CLAUDE.md", "CODING-STYLE.digest.md", VIOLATION_2, CONTROL_2])
            self.assertEqual(call["commits"], "1")
            self.assertEqual(call["at_base"], {VIOLATION_2: (case_dir / "base" / "ids_a.py").read_text(),
                                               CONTROL_2: (case_dir / "base" / "ids_b.py").read_text()})
            self.assertEqual(call["at_head"], {VIOLATION_2: (case_dir / VIOLATION_2).read_text(), CONTROL_2: (case_dir / CONTROL_2).read_text()})
            self.assertEqual(call["claude_md"], "## Code style digest (house-rules)\n@CODING-STYLE.digest.md\n")

    def test_a_case_with_a_base_for_one_file_only_adds_the_other(self) -> None:
        (self.layout.cases_dir / "fix-2" / "base" / "ids_b.py").unlink()
        self.assertEqual(self.check("--live", "--case", "fix-2", "--runs", "1", "--yes-spend", "2")[0], 0)
        for call in self.calls():
            self.assertEqual(call["changed"], sorted([VIOLATION_2, CONTROL_2]))
            self.assertEqual(call["base_files"], ["CLAUDE.md", "CODING-STYLE.digest.md", VIOLATION_2])
            self.assertEqual(call["at_base"][CONTROL_2], "")

    def test_the_provenance_names_the_base_files_and_their_hashes(self) -> None:
        self.assertEqual(self.check("--live", "--case", "fix-2", "--runs", "1", "--yes-spend", "2")[0], 0)
        case_dir = self.layout.cases_dir / "fix-2"
        prov = json.loads(self.layout.recording("fix-2", "with_rule", 1)[1].read_text())
        self.assertEqual(prov["base_sha256"], {f"base/{name}": hashlib.sha256((case_dir / "base" / name).read_bytes()).hexdigest()
                                               for name in ("ids_a.py", "ids_b.py")})
        self.assertEqual(self.check("--case", "fix-2", "--runs", "1")[0], 0)  # and replay finds them unchanged

    def test_every_case_and_condition_keeps_the_exact_digest_its_runs_used(self) -> None:
        self.assertEqual(self.check("--live", "--yes-spend", "12")[0], 0)
        for case_id, clause in (("fix-1", "REL.1"), ("fix-2", "REL.2")):
            digests = self.digests(clause)
            self.assertEqual(self.layout.snapshot(case_id, "with_rule").read_text(), digests.full)
            self.assertEqual(self.layout.snapshot(case_id, "without_rule").read_text(), digests.without[clause])
        for prov_path in self.layout.recordings_dir.glob("*/*.json"):
            prov = json.loads(prov_path.read_text())
            snapshot = self.layout.snapshot(prov["case"], prov["condition"]).read_bytes()
            self.assertEqual(prov["digest_sha256"], hashlib.sha256(snapshot).hexdigest(), prov_path.name)

    def test_runs_added_after_the_rules_changed_are_refused_before_any_call(self) -> None:
        self.assertEqual(self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")[0], 0)
        calls_so_far = len(self.calls())
        path = self.root / "CODING-STYLE.md"
        path.write_text(path.read_text().replace("Keep usable state until the replacement is ready", "Keep the old copy until the new one is ready"))
        self.compile_digest()
        code, _, err = self.check("--live", "--case", "fix-1", "--runs", "2", "--skip-existing", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertIn("the digest now is not the one its earlier runs were recorded with", err)
        self.assertEqual(len(self.calls()), calls_so_far)

    def test_recordings_made_live_still_replay_after_the_rules_change(self) -> None:
        self.assertEqual(self.check("--live", "--yes-spend", "12")[0], 0)
        path = self.root / "CODING-STYLE.md"
        path.write_text(path.read_text().replace("Keep usable state until the replacement is ready", "Keep the old copy until the new one is ready"))
        self.compile_digest()
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("PASS 2  FAIL 0  INCOMPLETE 0", out)
        self.assertIn("fix-1: the rules changed since recording", out)
        self.assertNotIn("fix-2: the rules changed", out)

    def test_a_scratch_directory_inside_the_repo_is_refused(self) -> None:
        inside = self.root / "scratch"
        inside.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(inside)):
            code, _, err = self.check("--live", "--case", "fix-1", "--runs", "1", "--yes-spend", "2")
        self.assertEqual(code, 1)
        self.assertIn("inside the repo", err)
        self.assertEqual(self.calls(), [])


class RealCasesTest(unittest.TestCase):
    cases_dir = REPO_ROOT / "samples" / "generalization" / "cases"

    def test_there_are_at_least_four_cases(self) -> None:
        self.assertGreaterEqual(len(list(self.cases_dir.glob("*/oracle.py"))), MIN_REAL_CASES)

    def test_every_case_has_an_oracle_that_exits_0(self) -> None:
        oracles = sorted(self.cases_dir.glob("*/oracle.py"))
        self.assertGreaterEqual(len(oracles), MIN_REAL_CASES, "the cases are not written yet")
        for oracle in oracles:
            with self.subTest(case=oracle.parent.name):
                proc = subprocess.run([sys.executable, "-I", "oracle.py"], cwd=oracle.parent, capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_case_with_earlier_versions_holds_one_for_each_of_its_two_files(self) -> None:
        case = next(case for case in rc.load_cases(self.cases_dir, []) if case.base)
        self.assertEqual(case.id, "rel-2")
        self.assertEqual(sorted(case.base), sorted(f"base/{rel.removeprefix('code/')}" for rel in case.code))

    def test_every_case_is_one_the_harness_can_read_about_a_clause_the_rules_define(self) -> None:
        cases = rc.load_cases(self.cases_dir, [])
        self.assertGreaterEqual(len(cases), MIN_REAL_CASES)
        defined = parse_rules_text((REPO_ROOT / "CODING-STYLE.md").read_text(encoding="utf-8"), "CODING-STYLE.md", DEFAULT_CLAUSE_ID_PATTERN)
        for case in cases:
            with self.subTest(case=case.id):
                self.assertIn(case.clause, defined)
                self.assertTrue(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", case.label))


if __name__ == "__main__":
    unittest.main()
