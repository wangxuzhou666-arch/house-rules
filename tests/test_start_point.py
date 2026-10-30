"""The start point: where ratchet began watching the default branch, kept in `.ratchet/start-points.json`, and the git helpers it uses."""
import json
import subprocess
import unittest
from pathlib import Path

import support  # noqa: F401  (puts the repo root on sys.path)
from support import _GIT_ENV, RepoCase, git, make_config

from ratchet.errors import RatchetError
from ratchet.git import commit_tree, is_ancestor
from ratchet.start_point import START_POINTS_NAME, read_start_points, record_start_point, usable_start_point


class RecordTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = make_config(root=self.repo)
        self.base = self.commit("base")

    def recorded(self) -> Path:
        return self.repo / START_POINTS_NAME

    def test_nothing_recorded_reads_as_no_start_points_and_reading_creates_nothing(self) -> None:
        self.assertEqual(read_start_points(self.repo), {})
        self.assertIsNone(usable_start_point(self.config, "main", self.base))
        self.assertFalse((self.repo / ".ratchet").exists())

    def test_a_start_point_is_written_as_a_flat_json_object_and_read_back(self) -> None:
        record_start_point(self.config, "main", self.base)
        self.assertEqual(json.loads(self.recorded().read_text(encoding="utf-8")), {"main": self.base})
        self.assertEqual(read_start_points(self.repo), {"main": self.base})
        self.assertEqual(usable_start_point(self.config, "main", self.base), self.base)

    def test_recording_the_same_start_point_again_changes_nothing(self) -> None:
        record_start_point(self.config, "main", self.base)
        before = self.recorded().read_bytes()
        record_start_point(self.config, "main", self.base)
        self.assertEqual(self.recorded().read_bytes(), before)

    def test_the_start_points_of_other_branches_are_kept_whether_a_branch_is_added_or_replaced(self) -> None:
        later = self.commit("later")
        record_start_point(self.config, "main", self.base)
        record_start_point(self.config, "develop", self.base)
        record_start_point(self.config, "main", later)
        self.assertEqual(read_start_points(self.repo), {"develop": self.base, "main": later})

    def test_a_linked_worktree_records_into_the_main_worktrees_file_and_keeps_the_branches_it_holds(self) -> None:
        record_start_point(self.config, "develop", self.base)
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(linked))
        record_start_point(make_config(root=self.repo, worktree=linked), "main", self.base)
        self.assertEqual(read_start_points(self.repo), {"develop": self.base, "main": self.base})
        self.assertFalse((linked / ".ratchet").exists())

    def test_the_file_is_moved_into_place_and_no_scratch_file_is_left_beside_it(self) -> None:
        record_start_point(self.config, "main", self.base)
        self.assertEqual([path.name for path in (self.repo / ".ratchet").iterdir()], ["start-points.json"])

    def test_an_abbreviated_sha_is_refused_before_it_makes_the_file_unreadable(self) -> None:
        with self.assertRaisesRegex(AssertionError, "not a full commit sha"):
            record_start_point(self.config, "main", self.base[:8])
        self.assertFalse(self.recorded().exists())

    def test_the_gitignore_init_writes_covers_the_file(self) -> None:
        self.cli_ok("init", "--yes", "--no-hooks")
        record_start_point(self.config, "main", self.base)
        ignored = subprocess.run(["git", "check-ignore", "-q", START_POINTS_NAME], cwd=self.repo, env=_GIT_ENV)
        self.assertEqual(ignored.returncode, 0)


class DamagedFileTest(RepoCase):
    DAMAGED = {"not JSON": "{not json", "a list": "[]", "a string": '"main"', "a number as a commit": '{"main": 5}',
               "a null as a commit": '{"main": null}', "an abbreviated sha": '{"main": "abc1234"}',
               "a sha in capitals": '{"main": "' + "A" * 40 + '"}', "a branch name as a commit": '{"main": "develop"}',
               "a sha with a digit too many": '{"main": "' + "a" * 41 + '"}', "a sha with a newline after it": '{"main": "' + "a" * 40 + '\\n"}'}

    def setUp(self) -> None:
        super().setUp()
        self.config = make_config(root=self.repo)
        self.base = self.commit("base")
        self.path = self.repo / START_POINTS_NAME
        self.path.parent.mkdir()

    def test_a_file_that_is_not_a_json_object_of_full_shas_is_an_error_that_names_it(self) -> None:
        for label, text in self.DAMAGED.items():
            with self.subTest(label):
                self.path.write_text(text, encoding="utf-8")
                for call in (lambda: read_start_points(self.repo), lambda: usable_start_point(self.config, "main", self.base)):
                    with self.assertRaisesRegex(RatchetError, "start-points.json"):
                        call()

    def test_recording_over_a_damaged_file_fails_and_leaves_it_as_it_was(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "start-points.json is not JSON"):
            record_start_point(self.config, "main", self.base)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_an_empty_object_is_a_file_with_no_start_points(self) -> None:
        self.path.write_text("{}", encoding="utf-8")
        self.assertEqual(read_start_points(self.repo), {})


class UsableTest(RepoCase):
    """A start point is usable when the branch's commit now descends from it; HEAD, the branch init happens to run on, has no say."""

    def setUp(self) -> None:
        super().setUp()
        self.config = make_config(root=self.repo)
        self.first = self.commit("first")
        self.second = self.commit("second")

    def side_commit(self) -> str:
        """A commit `main` does not descend from, made on another branch; the checkout is back on main afterwards."""
        git(self.repo, "checkout", "-q", "-b", "side", self.first)
        side = self.commit("side")
        git(self.repo, "checkout", "-q", "main")
        return side

    def test_an_ancestor_of_the_tip_is_usable_and_so_is_the_tip_itself(self) -> None:
        for start in (self.first, self.second):
            with self.subTest(start=start[:8]):
                record_start_point(self.config, "main", start)
                self.assertEqual(usable_start_point(self.config, "main", self.second), start)

    def test_a_commit_the_tip_does_not_descend_from_is_not_usable_and_a_new_one_replaces_it(self) -> None:
        record_start_point(self.config, "main", self.side_commit())
        self.assertIsNone(usable_start_point(self.config, "main", self.second))
        record_start_point(self.config, "main", self.first)
        self.assertEqual(usable_start_point(self.config, "main", self.second), self.first)

    def test_a_commit_this_clone_does_not_have_is_not_usable_and_is_no_error(self) -> None:
        record_start_point(self.config, "main", "a" * 40)  # pruned after a rebase, or recorded in another clone
        self.assertIsNone(usable_start_point(self.config, "main", self.second))

    def test_the_branch_commit_decides_and_not_the_checked_out_head(self) -> None:
        side = self.side_commit()
        git(self.repo, "checkout", "-q", "side")  # HEAD descends from `side`, but main does not
        record_start_point(self.config, "main", side)
        self.assertIsNone(usable_start_point(self.config, "main", self.second))

    def test_only_the_start_point_recorded_for_that_branch_counts(self) -> None:
        record_start_point(self.config, "develop", self.first)
        self.assertIsNone(usable_start_point(self.config, "main", self.second))
        self.assertEqual(usable_start_point(self.config, "develop", self.second), self.first)


class IsAncestorTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.commit("first")
        self.second = self.commit("second")
        git(self.repo, "checkout", "-q", "-b", "side", self.first)
        self.side = self.commit("side")

    def test_a_commit_is_its_own_ancestor_and_an_older_commit_is_an_ancestor(self) -> None:
        self.assertTrue(is_ancestor(self.repo, self.first, self.first))
        self.assertTrue(is_ancestor(self.repo, self.first, self.second))
        self.assertTrue(is_ancestor(self.repo, self.first, "main"))

    def test_a_newer_commit_and_a_commit_of_another_branch_are_not(self) -> None:
        self.assertFalse(is_ancestor(self.repo, self.second, self.first))
        self.assertFalse(is_ancestor(self.repo, self.side, "main"))
        self.assertFalse(is_ancestor(self.repo, self.second, "HEAD"))  # HEAD is on side

    def test_the_tip_may_be_any_ref(self) -> None:
        self.assertTrue(is_ancestor(self.repo, self.side, "HEAD"))
        self.assertTrue(is_ancestor(self.repo, self.first, "side"))

    def test_a_commit_the_clone_does_not_have_is_no_ancestor_and_is_no_error(self) -> None:
        self.assertFalse(is_ancestor(self.repo, "a" * 40, "HEAD"))


class CommitTreeTest(RepoCase):
    def test_it_is_the_tree_of_the_commit_and_writes_no_object(self) -> None:
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        commit = self.commit("with a file")
        (self.repo / "b.txt").write_text("uncommitted\n", encoding="utf-8")
        objects = sorted(path for path in (self.repo / ".git" / "objects").rglob("*") if path.is_file())
        self.assertEqual(commit_tree(self.repo, commit), git(self.repo, "rev-parse", f"{commit}^{{tree}}"))
        self.assertEqual(sorted(path for path in (self.repo / ".git" / "objects").rglob("*") if path.is_file()), objects)
        self.assertEqual(git(self.repo, "ls-tree", "--name-only", commit_tree(self.repo, commit)), "a.txt")  # the uncommitted file is not in it


if __name__ == "__main__":
    unittest.main()
