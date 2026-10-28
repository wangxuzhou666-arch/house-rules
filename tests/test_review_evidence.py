from pathlib import Path

from support import RepoCase, git, review_finding
from ratchet.config import load_config
from ratchet.errors import RatchetError
from ratchet.review import save_review
from ratchet.review_evidence import capture, save, source_for


class ReviewEvidenceTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        self.config = load_config(self.repo, None, None, self.repo)
        (self.repo / "source.py").write_text("original\n")
        git(self.repo, "add", "source.py", "rules.md", ".ratchet.json")
        self.commit("baseline")
        (self.repo / "source.py").write_text("reviewed change\n")
        self.finding = review_finding(file="source.py", line=1)

    def capture_and_save(self, target=None):
        snapshot = capture(self.config, target)
        save_review(self.repo, "R1", "synthetic review")
        save(self.config, "R1", snapshot, "synthetic review")
        return snapshot

    def test_pre_review_text_survives_later_working_tree_edits(self):
        self.capture_and_save()
        (self.repo / "source.py").write_text("later fix\n")
        source = source_for(self.config, self.finding)
        self.assertEqual(source["text"], "reviewed change")
        self.assertTrue(source["stable_during_review"])
        self.assertEqual(source["basis"], "local_pre_review")

    def test_a_source_modified_during_review_is_flagged(self):
        snapshot = capture(self.config, None)
        (self.repo / "source.py").write_text("changed during review\n")
        save_review(self.repo, "R1", "synthetic review")
        save(self.config, "R1", snapshot, "synthetic review")
        self.assertFalse(source_for(self.config, self.finding)["stable_during_review"])

    def test_import_and_unrelated_target_do_not_claim_an_exact_review_snapshot(self):
        self.assertEqual(source_for(self.config, self.finding)["status"], "unavailable")
        self.capture_and_save("42")
        self.assertEqual(source_for(self.config, self.finding)["basis"], "local_pre_review_target_unverified")

    def test_changed_raw_review_and_reused_snapshot_are_rejected(self):
        snapshot = self.capture_and_save()
        with self.assertRaisesRegex(RatchetError, "already exists"):
            save(self.config, "R1", snapshot, "synthetic review")
        (self.repo / ".ratchet/reviews/R1.txt").write_text("different review")
        with self.assertRaisesRegex(RatchetError, "no longer matches"):
            source_for(self.config, self.finding)
