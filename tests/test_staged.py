import os
import subprocess
import unittest
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import STYLE_RULES, RepoCase, git

DIGEST_NAME = "CODING-STYLE.digest.md"
EDITED_RULES = STYLE_RULES.replace("Fail fast at the boundary.", "Fail fast at every boundary.")


class StagedCase(RepoCase):
    """A repo whose rules, config and digest are committed and consistent."""

    def setUp(self) -> None:
        super().setUp()
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        self.cli_ok("compile")
        git(self.repo, "add", "-A")
        self.commit("initial")

    def stage(self, *paths: str) -> None:
        git(self.repo, "add", "--", *paths)

    def check_staged(self) -> tuple[int, str, str]:
        return self.cli("compile", "--check", "--staged")

    def edit_rules(self, text: str = EDITED_RULES) -> None:
        self.write_rules(text)

    def regenerate(self) -> None:
        self.cli_ok("compile")


class StagedCheckTest(StagedCase):
    def test_a_consistent_commit_passes(self) -> None:
        code, out, err = self.check_staged()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("compile --check --staged: 1 output(s) up to date", out)

    def test_staged_rules_without_the_regenerated_digest_fail(self) -> None:
        self.edit_rules()
        self.stage("rules.md")
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST_NAME} (digest, the STAGED version is out of date)", err)
        self.assertIn(f"`git add {DIGEST_NAME}`", err)
        self.assertIn("reads what is staged, not the working tree", err)

    def test_a_regenerated_but_unstaged_digest_still_fails_where_plain_check_passes(self) -> None:
        self.edit_rules()
        self.regenerate()
        self.stage("rules.md")
        self.assertEqual(self.cli("compile", "--check")[0], 0)  # the working tree is consistent
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST_NAME}", err)

    def test_staging_both_makes_it_pass(self) -> None:
        self.edit_rules()
        self.regenerate()
        self.stage("rules.md", DIGEST_NAME)
        self.assertEqual(self.check_staged()[0], 0)

    def test_an_unstaged_rules_edit_is_not_a_problem_for_the_commit(self) -> None:
        self.edit_rules()  # edited in the working tree only: the index still holds a consistent pair
        self.assertEqual(self.cli("compile", "--check")[0], 1)
        self.assertEqual(self.check_staged()[0], 0)

    def test_a_staged_digest_without_its_rules_change_fails(self) -> None:
        self.edit_rules()
        self.regenerate()
        self.stage(DIGEST_NAME)
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST_NAME} (digest, the STAGED version is out of date)", err)

    def test_only_the_staged_part_of_a_partly_staged_file_counts(self) -> None:
        self.edit_rules()
        self.stage("rules.md")
        self.write_rules(STYLE_RULES)  # the working tree goes back to the old text; the index keeps the edit
        self.assertEqual(self.cli("compile", "--check")[0], 0)
        self.assertEqual(self.check_staged()[0], 1)

    def test_a_digest_that_is_not_in_the_index_is_reported_as_not_staged(self) -> None:
        git(self.repo, "rm", "-q", "--cached", DIGEST_NAME)
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST_NAME} (digest, not staged)", err)

    def test_rules_that_are_not_in_the_index_are_an_error_naming_git_add(self) -> None:
        git(self.repo, "rm", "-q", "--cached", "rules.md")
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("cannot read rules file", err)
        self.assertIn("not found in the git index", err)
        self.assertIn("git add", err)

    def test_the_config_is_read_from_the_working_tree(self) -> None:
        self.write_config(rules_files=["rules.md"], digest="other.digest.md")  # not staged
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("stale: other.digest.md (digest, not staged)", err)

    def test_staged_applies_to_injected_blocks(self) -> None:
        (self.repo / "CLAUDE.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        self.regenerate()
        self.stage("CLAUDE.md", DIGEST_NAME, ".house-rules.json")
        self.commit("add the block")
        self.assertEqual(self.check_staged()[0], 0)
        self.edit_rules()
        self.regenerate()
        self.stage("rules.md", DIGEST_NAME)  # CLAUDE.md's block is regenerated but not staged
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("stale: CLAUDE.md (injected block, the STAGED version is out of date)", err)
        self.assertIn("`git add CLAUDE.md`", err)
        self.assertNotIn(f"stale: {DIGEST_NAME}", err)

    def test_every_stale_file_is_named_in_the_git_add_suggestion(self) -> None:
        (self.repo / "CLAUDE.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        self.regenerate()
        self.stage("CLAUDE.md", DIGEST_NAME, ".house-rules.json")
        self.commit("add the block")
        self.edit_rules()
        self.stage("rules.md")  # both outputs are now stale as staged
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn(f"`git add {DIGEST_NAME} CLAUDE.md`", err)

    def test_a_staged_target_without_markers_says_the_working_tree_has_them(self) -> None:
        (self.repo / "CLAUDE.md").write_text("notes\n", encoding="utf-8")  # committed without markers
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        self.stage("CLAUDE.md", ".house-rules.json")
        (self.repo / "CLAUDE.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("staged copy of CLAUDE.md has no style-digest markers (the working tree copy does); stage it with `git add CLAUDE.md`", err)

    def test_a_staged_target_without_markers_anywhere_gets_the_plain_marker_error(self) -> None:
        (self.repo / "CLAUDE.md").write_text("notes\n", encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        self.stage("CLAUDE.md", ".house-rules.json")
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("staged copy of CLAUDE.md: marker <!-- style-digest:begin --> not found", err)
        self.assertNotIn("the working tree copy does", err)

    def test_other_marker_problems_in_the_staged_copy_say_staged(self) -> None:
        marker_pair = "<!-- style-digest:begin -->\n<!-- style-digest:end -->\n"
        (self.repo / "CLAUDE.md").write_text(marker_pair * 2, encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        self.stage("CLAUDE.md", ".house-rules.json")
        (self.repo / "CLAUDE.md").write_text(marker_pair, encoding="utf-8")  # the working tree is fine; the staged copy is not
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("staged copy of CLAUDE.md: marker <!-- style-digest:begin --> appears 2 times", err)

    def test_a_working_tree_marker_error_does_not_mention_staging(self) -> None:
        (self.repo / "CLAUDE.md").write_text("notes\n", encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=["CLAUDE.md"])
        code, _, err = self.cli("compile", "--check")
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md: marker <!-- style-digest:begin --> not found", err)
        self.assertNotIn("staged", err)

    def test_a_rules_file_outside_the_repo_is_read_from_disk(self) -> None:
        outside = self.tmp / "shared.md"
        outside.write_text(STYLE_RULES, encoding="utf-8")
        self.write_config(rules_files=["../shared.md"])
        self.regenerate()
        self.stage(DIGEST_NAME, ".house-rules.json")
        self.commit("use a shared rules file")
        self.assertEqual(self.check_staged()[0], 0)
        outside.write_text(EDITED_RULES, encoding="utf-8")
        self.assertEqual(self.check_staged()[0], 1)

    def test_a_rules_file_name_with_glob_characters_is_a_name(self) -> None:
        (self.repo / "rules[1].md").write_text(STYLE_RULES, encoding="utf-8")
        self.write_config(rules_files=["rules[1].md"])
        self.regenerate()
        self.stage("rules[1].md", DIGEST_NAME, ".house-rules.json")
        self.assertEqual(self.check_staged()[0], 0)

    def test_a_name_with_glob_characters_is_not_matched_as_a_pattern(self) -> None:
        (self.repo / "rules1.md").write_text(STYLE_RULES, encoding="utf-8")  # what the pattern `rules[1].md` would match
        (self.repo / "rules[1].md").write_text(STYLE_RULES, encoding="utf-8")
        self.write_config(rules_files=["rules[1].md"])
        self.stage("rules1.md")
        code, _, err = self.check_staged()
        self.assertEqual(code, 1)
        self.assertIn("not found in the git index", err)

    def test_staged_never_writes(self) -> None:
        self.edit_rules()
        self.stage("rules.md")
        before = {path.name: path.read_bytes() for path in self.repo.iterdir() if path.is_file()}
        self.check_staged()
        self.assertEqual({path.name: path.read_bytes() for path in self.repo.iterdir() if path.is_file()}, before)

    def test_staged_needs_check(self) -> None:
        code, _, err = self.cli("compile", "--staged")
        self.assertEqual(code, 1)
        self.assertIn("--staged only applies to --check", err)

    def test_plain_check_messages_are_unchanged(self) -> None:
        self.edit_rules()
        code, _, err = self.cli("compile", "--check")
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST_NAME} (digest, out of date)", err)
        self.assertIn("run `house-rules compile` to regenerate", err)


class StagedWorktreeTest(StagedCase):
    def test_a_linked_worktree_is_checked_against_its_own_index(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(linked))
        (linked / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        git(linked, "add", "rules.md")
        code, _, err = self.cli("compile", "--check", "--staged", "--repo", str(linked), with_repo=False)
        self.assertEqual(code, 1, err)
        self.assertIn(f"stale: {DIGEST_NAME}", err)
        self.assertEqual(self.check_staged()[0], 0)  # the main worktree's index is untouched


class StagedHookEnvironmentTest(StagedCase):
    """A partial commit runs the hook with GIT_INDEX_FILE pointing at a temporary index: that is the one to read."""

    def test_an_explicit_path_commit_is_checked_against_the_index_it_will_build(self) -> None:
        self.cli_ok("init", "--yes")
        self.edit_rules()
        self.regenerate()  # rules and digest are both edited in the working tree
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        partial = subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "rules only", "rules.md"],
                                 cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertNotEqual(partial.returncode, 0, "the digest was not part of the commit; the hook must refuse it")
        self.assertIn(f"stale: {DIGEST_NAME}", partial.stderr)
        both = subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "both", "rules.md", DIGEST_NAME],
                              cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertEqual(both.returncode, 0, both.stderr)


class IndexBlobEnvTest(unittest.TestCase):
    def test_index_blob_keeps_the_hooks_index_file(self) -> None:
        from house_rules import git as house_rules_git
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": "/nonexistent/index", "GIT_DIR": "/nonexistent/dir"}):
            self.assertEqual(house_rules_git._git_env(keep_index_file=True).get("GIT_INDEX_FILE"), "/nonexistent/index")
            self.assertNotIn("GIT_DIR", house_rules_git._git_env(keep_index_file=True))
            self.assertNotIn("GIT_INDEX_FILE", house_rules_git._git_env(keep_index_file=False))


if __name__ == "__main__":
    unittest.main()
