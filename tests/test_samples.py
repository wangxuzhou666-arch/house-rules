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


HELPER = SAMPLE / "make_answers.py"
RECORDING_PATH = SAMPLE / RECORDING


def run_helper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(HELPER), *args], capture_output=True, text=True)


def sample_rules_without_rel1() -> str:
    """The starting corpus of the workshop (`sed '6,9d'`): the sample rules minus REL.1 and the three lines that explain it."""
    lines = (SAMPLE / "CODING-STYLE.md").read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[:5] + lines[9:])


class WorkshopHelperTest(RepoCase):
    """`make_answers.py` stands in for the model and the person in the workshop's decision step; what it writes must be accepted
    by the real commands, and what is not the workshop's input must be refused."""

    def setUp(self) -> None:
        super().setUp()
        self.files = self.tmp / "files"
        self.files.mkdir()

    def adopt(self, rules: str) -> None:
        """A repo with `rules`, committed, and the recorded review imported over the files it saw."""
        (self.repo / "CODING-STYLE.md").write_text(rules, encoding="utf-8")
        self.write_config(rules_files=["CODING-STYLE.md"])
        self.cli_ok("compile")
        self.commit("baseline")
        for source in (SAMPLE / "reviewed").glob("*.py"):
            shutil.copy2(source, self.repo / source.name)
        self.cli_ok("review", "--from-file", str(RECORDING_PATH), "--run", "live-r3")

    def saved(self, name: str, text: str) -> Path:
        path = self.files / name
        path.write_text(text, encoding="utf-8")
        return path

    def helper_ok(self, *args: str) -> dict:
        proc = run_helper(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)

    def refused(self, *args: str) -> str:
        """The message of a refusal; a refusal prints nothing on stdout, so a redirect cannot leave a half-made file behind."""
        proc = run_helper(*args)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.startswith("make_answers.py: error: "), proc.stderr)
        return proc.stderr

    def context_file(self, **change: object) -> Path:
        context = json.loads(self.cli_ok("decision-context"))
        context.update(change)
        return self.saved("context.json", json.dumps(context))

    def test_the_workshop_corpus_is_the_sample_without_rel1_and_its_notes(self) -> None:
        rules = sample_rules_without_rel1()
        self.assertIn("[ERR.1]", rules)
        self.assertIn("[R.1]", rules)
        self.assertNotIn("REL.1", rules)
        self.assertNotIn("Scope:", rules)
        self.assertNotIn("Prepared means", rules)
        self.assertNotIn("power-loss", rules)

    def test_the_clause_it_proposes_is_the_one_the_sample_ships(self) -> None:
        lines = (SAMPLE / "CODING-STYLE.md").read_text(encoding="utf-8").splitlines()
        self.adopt(sample_rules_without_rel1())
        change = self.helper_ok("assessment", str(self.context_file()))["assessments"][0]["changes"][0]
        self.assertEqual(change["after"].splitlines(), lines[5:7])

    def test_help_names_both_commands(self) -> None:
        proc = run_helper("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("assessment", proc.stdout)
        self.assertIn("answers", proc.stdout)
        self.assertIn("--confirmed-by", run_helper("answers", "--help").stdout)

    def test_the_files_it_writes_are_accepted_and_the_rule_is_added(self) -> None:
        self.adopt(sample_rules_without_rel1())
        self.assertEqual([(f["file"], f["rule_id"]) for f in self.events() if f["kind"] == "finding"],
                         [("heldout_unsafe.py", "unmapped"), ("config_store.py", "unmapped")])
        digest_before = (self.repo / "CODING-STYLE.digest.md").read_text(encoding="utf-8")

        made = self.helper_ok("assessment", str(self.context_file()))
        self.assertEqual([item["action"] for item in made["assessments"]], ["add", "keep"])
        assessment = self.saved("assessment.json", json.dumps(made))
        self.cli_ok("decision-context", "--assessment", str(assessment))
        prepared = json.loads(self.cli_ok("prepare-decisions", "--assessment", str(assessment)))
        self.assertEqual((prepared["status"], [group["id"] for group in prepared["groups"]]), ("prepared", ["a1", "a2"]))

        answers_path = self.saved("answers.json", json.dumps(
            self.helper_ok("answers", str(self.saved("prepared.json", json.dumps(prepared))), "--confirmed-by", "Ada")))
        before = (self.repo / "CODING-STYLE.md").read_text(encoding="utf-8")
        preview = json.loads(self.cli_ok("apply-decisions", "--dry-run", "--input", str(answers_path)))
        self.assertEqual(preview["status"], "preview")
        self.assertEqual((self.repo / "CODING-STYLE.md").read_text(encoding="utf-8"), before)
        applied = json.loads(self.cli_ok("apply-decisions", "--input", str(answers_path)))
        self.assertEqual((applied["status"], applied["accepted"], applied["answer_counts"]),
                         ("applied", 1, {"accept": 1, "defer": 1}))

        rules = (self.repo / "CODING-STYLE.md").read_text(encoding="utf-8")
        self.assertIn("- **[REL.1]** Preserve the existing usable configuration", rules)
        self.assertGreater(len((self.repo / "CODING-STYLE.digest.md").read_bytes()), len(digest_before.encode("utf-8")))
        self.assertIn("REL.1 | should-fix | Preserve usable configuration", (self.repo / "CODING-STYLE.digest.md").read_text(encoding="utf-8"))
        self.cli_ok("compile", "--check", "--strict")
        changelog = (self.repo / "CODING-STYLE.changelog.md").read_text(encoding="utf-8")
        self.assertIn("### add REL.1", changelog)
        self.assertIn('Decision: accept by Ada — "Yes, add REL.1 as written."', changelog)
        summary = json.loads(self.cli_ok("decision-summary"))["groups"]["initial"]
        self.assertEqual((summary["change"], summary["no_change"]), ({"accept": 1}, {"defer": 1}))
        # accepting a rule does not close the finding it came from
        self.assertEqual([event for event in self.events() if event["kind"] == "outcome"], [])

    def test_the_person_is_named_workshop_learner_unless_told_otherwise(self) -> None:
        self.adopt(sample_rules_without_rel1())
        assessment = self.saved("assessment.json", json.dumps(self.helper_ok("assessment", str(self.context_file()))))
        prepared = self.saved("prepared.json", self.cli_ok("prepare-decisions", "--assessment", str(assessment)))
        self.assertEqual(self.helper_ok("answers", str(prepared))["confirmed_by"], "workshop learner")

    def test_a_missing_file_is_refused(self) -> None:
        self.assertIn("cannot read", self.refused("assessment", str(self.files / "nope.json")))
        self.assertIn("cannot read", self.refused("answers", str(self.files / "nope.json")))

    def test_text_that_is_not_json_is_refused(self) -> None:
        path = self.saved("context.json", "error: not a git repository\n")
        self.assertIn("is not valid JSON", self.refused("assessment", str(path)))
        self.assertIn("is not valid JSON", self.refused("answers", str(path)))

    def test_json_that_is_not_an_object_is_refused(self) -> None:
        self.assertIn("is not a JSON object", self.refused("assessment", str(self.saved("list.json", "[]"))))

    def test_a_context_without_an_id_or_findings_is_refused(self) -> None:
        self.adopt(sample_rules_without_rel1())
        self.assertIn("no usable `context_id`", self.refused("assessment", str(self.context_file(context_id=""))))
        self.assertIn("no usable `findings`", self.refused("assessment", str(self.context_file(findings=[]))))

    def test_a_finding_without_an_id_is_refused(self) -> None:
        self.adopt(sample_rules_without_rel1())
        context = json.loads(self.cli_ok("decision-context"))
        del context["findings"][0]["finding"]["id"]
        self.assertIn("lacks `id` or `file`", self.refused("assessment", str(self.saved("context.json", json.dumps(context)))))

    def test_a_context_that_is_not_the_recorded_review_is_refused(self) -> None:
        self.adopt(sample_rules_without_rel1())
        context = json.loads(self.cli_ok("decision-context"))
        one = dict(context, findings=context["findings"][:1])
        message = self.refused("assessment", str(self.saved("one.json", json.dumps(one))))
        self.assertIn("knows only the two findings of recorded-review.txt", message)
        self.assertIn("heldout_unsafe.py:7", message)
        other = json.loads(json.dumps(context))
        other["findings"][1]["finding"]["file"] = "other.py"
        self.assertIn("other.py", self.refused("assessment", str(self.saved("other.json", json.dumps(other)))))
        twice = dict(context, findings=context["findings"] + context["findings"][:1])
        self.assertIn("knows only the two findings", self.refused("assessment", str(self.saved("twice.json", json.dumps(twice)))))

    def test_a_finding_the_rules_already_cover_is_refused(self) -> None:
        self.adopt((SAMPLE / "CODING-STYLE.md").read_text(encoding="utf-8"))
        message = self.refused("assessment", str(self.context_file()))
        self.assertIn("already labeled REL.1", message)
        self.assertIn("without REL.1", message)

    def test_a_batch_that_is_not_prepared_is_refused(self) -> None:
        group = {"id": "a1", "assessment": {"action": "add"}}
        good = {"status": "prepared", "batch_id": "b" * 64, "groups": [group]}
        self.assertEqual(self.helper_ok("answers", str(self.saved("good.json", json.dumps(good))))["answers"][0]["decision"], "accept")
        for name, change, expected in (
                ("status", {"status": "preview"}, "not 'prepared'"),
                ("no-status", {"status": None}, "not 'prepared'"),
                ("batch", {"batch_id": ""}, "no usable `batch_id`"),
                ("groups", {"groups": []}, "no usable `groups`"),
                ("action", {"groups": [{"id": "a1", "assessment": {"action": "rewrite"}}]}, "no answer to give"),
                ("no-assessment", {"groups": [{"id": "a1"}]}, "no answer to give"),
                ("no-id", {"groups": [{"assessment": {"action": "add"}}]}, "no answer to give"),
                ("not-a-group", {"groups": ["a1"]}, "no answer to give")):
            with self.subTest(name):
                self.assertIn(expected, self.refused("answers", str(self.saved(f"{name}.json", json.dumps({**good, **change})))))

    def test_a_group_the_model_would_keep_is_deferred_not_accepted(self) -> None:
        batch = {"status": "prepared", "batch_id": "b" * 64, "groups": [
            {"id": "a1", "assessment": {"action": "add"}}, {"id": "a2", "assessment": {"action": "keep"}}]}
        answers = self.helper_ok("answers", str(self.saved("batch.json", json.dumps(batch))))
        self.assertEqual([(a["id"], a["decision"]) for a in answers["answers"]], [("a1", "accept"), ("a2", "defer")])
        self.assertTrue(all(a["response"] for a in answers["answers"]))
        self.assertEqual((answers["v"], answers["batch_id"]), (1, "b" * 64))

    def test_no_command_is_an_error(self) -> None:
        self.assertEqual(run_helper().returncode, 2)
