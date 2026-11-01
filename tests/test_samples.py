import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import REPO_ROOT, RepoCase

from house_rules.ingest import extract_review_items

SAMPLE = REPO_ROOT / "samples/config-replacement"
PROVENANCE = "recorded-review-provenance.json"
RECORDING = "recorded-review.txt"


class SampleTest(RepoCase):
    def test_failure_controls_and_permission_preservation(self):
        command = [sys.executable, "-B", str(REPO_ROOT / "samples/config-replacement/check_examples.py")]
        proc = subprocess.run(command, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["unsafe_config_preserves_after_io_failure"])
        self.assertTrue(result["safe_config_preserves_after_io_failure"])
        self.assertFalse(result["heldout_unsafe_preserves_after_serialization_failure"])
        self.assertTrue(result["heldout_safe_preserves_after_serialization_failure"])
        self.assertTrue(result["existing_private_permissions_preserved"])


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance_problems(sample: Path) -> list[str]:
    """Where the recorded review and its provenance disagree with the files of `sample`; empty when they agree.

    The provenance holds the sha-256 of the raw review and of every source the reviewer saw. Those sources are kept apart, in a
    directory of their own, because the published examples moved on after the review (they gained permission-bit
    preservation): the record must match the reviewed bytes, never the published ones. Every finding must name one of them,
    at a line the file has.
    """
    provenance = json.loads((sample / PROVENANCE).read_text(encoding="utf-8"))
    recording = sample / RECORDING
    sources = provenance["reviewed_sources"]
    source_dir = sample / provenance["reviewed_sources_dir"]
    problems = []
    if sha256_of(recording) != provenance["raw_sha256"]:
        problems.append(f"{RECORDING} does not match raw_sha256")
    for name, recorded_sha in sources.items():
        path = source_dir / name
        if not path.is_file():
            problems.append(f"reviewed source {name} is not in {source_dir.name}/")
        elif sha256_of(path) != recorded_sha:
            problems.append(f"reviewed source {name} does not match its recorded sha-256")
    for path in sorted(source_dir.glob("*.py")):
        if path.name not in sources:
            problems.append(f"{source_dir.name}/{path.name} has no recorded sha-256")
    for item in extract_review_items(recording.read_text(encoding="utf-8")):
        where = f"{item['file']}:{item['line']}"
        path = source_dir / item["file"]
        if item["file"] not in sources or not path.is_file():
            problems.append(f"finding at {where} points at a file that is not a reviewed source")
        elif item["line"] > len(path.read_text(encoding="utf-8").splitlines()):
            problems.append(f"finding at {where} points past the end of the file")
    return problems


class RecordedReviewProvenanceTest(unittest.TestCase):
    """The recording is replayed for free, so the files it names must be there and be the ones it was made from."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.sample = Path(scratch.name) / "sample"
        shutil.copytree(SAMPLE, self.sample, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.provenance = json.loads((SAMPLE / PROVENANCE).read_text(encoding="utf-8"))
        self.source_dir = self.sample / self.provenance["reviewed_sources_dir"]

    def edit_provenance(self, change) -> None:
        path = self.sample / PROVENANCE
        record = json.loads(path.read_text(encoding="utf-8"))
        change(record)
        path.write_text(json.dumps(record), encoding="utf-8")

    def edit_recording(self, old: str, new: str) -> None:
        path = self.sample / RECORDING
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_the_published_sample_agrees_with_its_provenance(self) -> None:
        self.assertEqual(provenance_problems(SAMPLE), [])

    def test_the_check_is_not_vacuous(self) -> None:
        """Emptying the record or the recording would make every loop in `provenance_problems` pass with nothing checked."""
        self.assertTrue(self.provenance["reviewed_sources"])
        self.assertTrue(extract_review_items((SAMPLE / RECORDING).read_text(encoding="utf-8")))

    def test_a_wrong_recorded_sha_is_reported(self) -> None:
        self.edit_provenance(lambda record: record["reviewed_sources"].update({"heldout_safe.py": "0" * 64}))
        self.assertEqual(provenance_problems(self.sample), ["reviewed source heldout_safe.py does not match its recorded sha-256"])

    def test_a_reviewed_file_that_changed_is_reported(self) -> None:
        path = self.source_dir / "config_store.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertEqual(provenance_problems(self.sample), ["reviewed source config_store.py does not match its recorded sha-256"])

    def test_a_missing_reviewed_file_is_reported(self) -> None:
        (self.source_dir / "config_store.py").unlink()
        problems = provenance_problems(self.sample)
        self.assertIn("reviewed source config_store.py is not in reviewed/", problems)
        self.assertIn("finding at config_store.py:18 points at a file that is not a reviewed source", problems)

    def test_a_finding_at_a_file_outside_the_sample_is_reported(self) -> None:
        self.edit_recording('"file": "config_store.py"', '"file": "config_store_v2.py"')
        self.edit_provenance(lambda record: record.update({"raw_sha256": sha256_of(self.sample / RECORDING)}))
        self.assertEqual(provenance_problems(self.sample),
                         ["finding at config_store_v2.py:18 points at a file that is not a reviewed source"])

    def test_a_finding_past_the_end_of_its_file_is_reported(self) -> None:
        self.edit_recording('"line": 18,', '"line": 180,')
        self.edit_provenance(lambda record: record.update({"raw_sha256": sha256_of(self.sample / RECORDING)}))
        self.assertEqual(provenance_problems(self.sample), ["finding at config_store.py:180 points past the end of the file"])

    def test_an_edited_recording_is_reported(self) -> None:
        self.edit_recording("swaps it over the target", "replaces it over the target")
        self.assertEqual(provenance_problems(self.sample), [f"{RECORDING} does not match raw_sha256"])

    def test_a_reviewed_file_without_a_recorded_sha_is_reported(self) -> None:
        (self.source_dir / "extra.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(provenance_problems(self.sample), ["reviewed/extra.py has no recorded sha-256"])

    def test_the_published_examples_may_move_on_without_touching_the_record(self) -> None:
        for name in ("heldout_safe.py", "safe_config.py"):
            path = self.sample / name
            path.write_text(path.read_text(encoding="utf-8") + "# a later change\n", encoding="utf-8")
        self.assertEqual(provenance_problems(self.sample), [])


class RecordedReviewReplayTest(RepoCase):
    """The replay of the sample README: the recording imported over the reviewed files, with no reviewer called."""

    def test_replay_records_the_two_findings_over_the_reviewed_sources(self) -> None:
        provenance = json.loads((SAMPLE / PROVENANCE).read_text(encoding="utf-8"))
        run = provenance["run"]
        recording = SAMPLE / RECORDING
        shutil.copy2(SAMPLE / "CODING-STYLE.md", self.repo / "CODING-STYLE.md")
        self.write_config(rules_files=["CODING-STYLE.md"])
        self.commit("baseline")
        for source in (SAMPLE / provenance["reviewed_sources_dir"]).glob("*.py"):
            shutil.copy2(source, self.repo / source.name)

        self.cli_ok("review", "--from-file", str(recording), "--run", run)
        findings = [event for event in self.events() if event["kind"] == "finding"]
        self.assertEqual([(f["file"], f["line"], f["rule_id"]) for f in findings],
                         [("heldout_unsafe.py", 7, "REL.1"), ("config_store.py", 18, "unmapped")])

        packet = json.loads(self.cli_ok("decision-context"))
        self.assertEqual(len(packet["findings"]), 2)
        for entry in packet["findings"]:
            source = entry["source"]
            self.assertEqual(source["status"], "available", entry["finding"]["file"])
            self.assertEqual(source["sha256"], provenance["reviewed_sources"][entry["finding"]["file"]])

        before = self.ledger_bytes()
        self.cli_ok("review", "--from-file", str(recording), "--run", run)
        self.assertEqual(self.ledger_bytes(), before)
