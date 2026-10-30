"""Rules layers outside the repo: shared with other repos, read here and never written.

`rules_files` is ordered, the global layer first and the project layer last. The global one often lives beside the repo
(`../shared/CODING-STYLE.md`) or in the home directory (`~/rules/CODING-STYLE.md`). A proposal changes only the project
layer, a machine without the shared file refuses to record anything against an incomplete rule set (`wrapup --dry-run`
foresees that refusal), and `compile --check` (the pre-commit hook) goes on without it. Every test uses real temp git repos
with the shared file beside them.
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, git, make_config, report_item, review_finding, review_item, review_round

from ratchet import api
from ratchet.config import is_outer, load_config, rules_name, tracked_name, tracked_path
from ratchet.decision_context import assess, build_context
from ratchet.errors import RatchetError, SharedLayerMissing
from ratchet.ledger import read_events
from ratchet.proposals import CHANGELOG, STATE, VERSION, apply_batch, create_batch
from ratchet.review import (
    REFUSE_CLAUDE, REFUSE_ROUND_CAP, REFUSE_ROUND_KEY, REFUSE_RULES, Refusal, review_refusal, round_key,
)
from ratchet.review_evidence import capture
from ratchet.rules import load_rules, shared_layers
from ratchet.sources import DiskReader, IndexReader

DIGEST = "CODING-STYLE.digest.md"
PROJECT = "CODING-STYLE.md"
SHARED_NAME = "../shared/CODING-STYLE.md"
GLOBAL_RULES = """\
# Global rules

## Structure

1. **[G1.1]** Keep a function to one job.
2. **[G1.2]** Name the failure a branch guards against.

## Libraries

1. **[G2.1]** Prefer the standard library.

| family | severity |
|---|---|
| G1 | blocker |
| G2 | should-fix |
"""
PROJECT_RULES = """\
# Project rules

## Replay

1. **[PRJ.1]** Import without execution is replay. (should-fix)
2. **[PRJ.2]** Reused IDs never skip the round check. (should-fix)
"""
GIT_IDENTITY = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
THE_LAYER_IS_GONE = "rules layer ../shared/CODING-STYLE.md is not on this machine"


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


class SharedCase(RepoCase):
    """A repo whose rules_files lists a shared layer beside it, then its own project layer. The ledger holds one finding."""

    def setUp(self) -> None:
        super().setUp()
        self.shared_dir = self.tmp / "shared"
        self.shared_dir.mkdir()
        self.shared = self.shared_dir / PROJECT
        self.shared.write_text(GLOBAL_RULES, encoding="utf-8")
        self.write_rules(PROJECT_RULES, PROJECT)
        self.write_config(rules_files=[SHARED_NAME, PROJECT])
        patcher = mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": "/nonexistent/claude"})  # no test reaches the real claude
        patcher.start()
        self.addCleanup(patcher.stop)
        self.finding = review_finding(rule_id="PRJ.1", rule_code="PRJ.1", summary="The replay rule flags a clean import.")
        self.append_raw(self.finding)

    def config(self):
        return load_config(self.repo, None, None, self.repo)

    def project_text(self) -> str:
        return (self.repo / PROJECT).read_text(encoding="utf-8")

    def lose_the_layer(self) -> None:
        self.shared.unlink()

    def stderr_of(self, run) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run()
        return err.getvalue()

    def refused(self, *argv: str, stdin: str = "") -> str:
        """The stderr of a command that must fail because the shared layer is not on this machine."""
        code, _, err = self.cli(*argv, stdin=stdin)
        self.assertEqual(code, 1, err)
        self.assertIn(THE_LAYER_IS_GONE, err)
        self.assertNotIn("; skipped", err)  # the warning of a command that went on without the layer says the same words, then fails for another reason
        return err


class ConfigNamesTest(SharedCase):
    """Which files are outside the repo, and what the config calls them."""

    def test_a_file_beside_the_repo_is_outer_and_a_file_in_it_is_not(self) -> None:
        config = self.config()
        self.assertEqual([is_outer(config, path) for path in config.rules_files], [True, False])

    def test_the_path_as_written_by_tracked_path_is_normalized_before_it_is_judged(self) -> None:
        config = self.config()
        self.assertTrue(config.rules_files[0].is_relative_to(self.repo))  # `repo/../shared/x.md` starts with the repo as written
        self.assertTrue(is_outer(config, config.rules_files[0]))

    def test_a_file_of_the_repo_reached_by_a_detour_is_not_outer(self) -> None:
        config = make_config(root=self.repo)
        self.assertFalse(is_outer(config, self.repo / "docs" / ".." / PROJECT))

    def test_an_absolute_path_elsewhere_is_outer(self) -> None:
        self.assertTrue(is_outer(make_config(root=self.repo), Path("/somewhere/else/rules.md")))

    def test_a_linked_worktree_judges_by_its_own_top(self) -> None:
        git(self.repo, "add", "-A")
        self.commit("base")
        tree = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(tree))
        config = load_config(tree, None, None, tree)
        self.assertEqual(config.rules_files, (self.repo / SHARED_NAME, tree / PROJECT))
        self.assertEqual([is_outer(config, path) for path in config.rules_files], [True, False])

    def test_the_config_keeps_the_names_as_written(self) -> None:
        self.assertEqual(self.config().rules_names, (SHARED_NAME, PROJECT))

    def test_the_name_of_a_layer_is_the_same_from_every_worktree(self) -> None:
        git(self.repo, "add", "-A")
        self.commit("base")
        tree = self.tmp / "deep" / "linked"
        tree.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(tree))
        names = {rules_name(config, path) for config in (self.config(), load_config(tree, None, None, tree))
                 for path in config.rules_files[:1]}
        self.assertEqual(names, {SHARED_NAME})


class HomeSpellingTest(RepoCase):
    """A name that starts with `~` is the home of whoever runs the tool, and stays `~/...` in the digest on every machine."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / "rules").mkdir(parents=True)
        self.layer = self.home / "rules" / PROJECT
        self.layer.write_text(GLOBAL_RULES, encoding="utf-8")
        self.write_rules(PROJECT_RULES, PROJECT)
        self.write_config(rules_files=["~/rules/CODING-STYLE.md", PROJECT])
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tilde_is_expanded_when_the_config_is_resolved(self) -> None:
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual(config.rules_files, (self.layer, self.repo / PROJECT))
        self.assertEqual(config.rules_names, ("~/rules/CODING-STYLE.md", PROJECT))
        self.assertTrue(is_outer(config, config.rules_files[0]))

    def test_tracked_path_expands_tilde_for_a_linked_worktree_too(self) -> None:
        tree = self.tmp / "linked"
        self.assertEqual(tracked_path(self.repo, tree, "~/rules/CODING-STYLE.md"), self.layer)
        self.assertEqual(tracked_path(self.repo, self.repo, "~/rules/CODING-STYLE.md"), self.layer)

    def test_a_name_with_a_tilde_is_given_back_as_written_and_any_other_is_relative(self) -> None:
        tree = self.tmp / "linked"
        self.assertEqual(tracked_name(self.repo, tree, self.layer, "~/rules/CODING-STYLE.md"), "~/rules/CODING-STYLE.md")
        relative = tracked_name(self.repo, tree, self.layer, "../home/rules/CODING-STYLE.md")
        self.assertEqual(relative, "../home/rules/CODING-STYLE.md")
        self.assertEqual(tracked_name(self.repo, tree, self.layer), "../home/rules/CODING-STYLE.md")
        # a spelling that is not a ~ is not kept: it is the name every worktree computes, as it was before spellings were recorded
        self.assertEqual(tracked_name(self.repo, tree, self.layer, "./../home/rules/CODING-STYLE.md"), "../home/rules/CODING-STYLE.md")

    def test_the_rules_load_and_compile_from_the_home_directory(self) -> None:
        self.assertEqual(set(load_rules(load_config(self.repo, None, None, self.repo)).clauses),
                         {"G1.1", "G1.2", "G2.1", "PRJ.1", "PRJ.2"})
        self.cli_ok("compile")
        self.assertIn("G1.1 | blocker |", (self.repo / DIGEST).read_text(encoding="utf-8"))

    def test_the_digest_header_names_the_layer_by_its_tilde(self) -> None:
        self.cli_ok("compile")
        header = (self.repo / DIGEST).read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f"~/rules/CODING-STYLE.md@{sha12(self.layer)}", header)
        self.assertNotIn("..", header)
        self.assertNotIn(str(self.home), header)

    def test_the_digest_is_the_same_bytes_on_a_machine_that_keeps_everything_elsewhere(self) -> None:
        self.cli_ok("compile")
        machine = self.tmp / "other-machine"
        home = machine / "users" / "someone-else"
        (home / "rules").mkdir(parents=True)
        (home / "rules" / PROJECT).write_text(GLOBAL_RULES, encoding="utf-8")
        repo = machine / "work" / "deeper" / "demo-repo"  # another depth: a path relative to the repo would differ
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        (repo / PROJECT).write_text(PROJECT_RULES, encoding="utf-8")
        (repo / ".ratchet.json").write_text(json.dumps({"rules_files": ["~/rules/CODING-STYLE.md", PROJECT]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(self.cli("compile", "--repo", str(repo), with_repo=False)[0], 0)
            self.assertEqual(self.cli("compile", "--check", "--repo", str(repo), with_repo=False)[0], 0)
        self.assertEqual((repo / DIGEST).read_bytes(), (self.repo / DIGEST).read_bytes())

    def test_compile_check_passes_from_a_linked_worktree(self) -> None:
        self.cli_ok("compile")
        git(self.repo, "add", "-A")
        self.commit("base")
        tree = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(tree))
        code, _, err = self.cli("compile", "--check", "--repo", str(tree), with_repo=False)
        self.assertEqual((code, err), (0, ""))

    def test_init_shows_the_name_as_written(self) -> None:
        self.write_config(rules_files=["~/rules/CODING-STYLE.md", PROJECT], wrapup_auto=True)  # nothing of the config left for init to ask
        out = self.cli_ok("init", "--yes", "--no-hooks")
        self.assertIn("already lists rules_files ['~/rules/CODING-STYLE.md', 'CODING-STYLE.md']", out)
        self.assertIn("using ~/rules/CODING-STYLE.md", out)
        self.assertEqual(self.cli("compile", "--check")[0], 0)  # the digest init wrote is the one compile writes

    def test_a_missing_file_under_the_home_directory_is_a_missing_layer(self) -> None:
        self.layer.unlink()
        code, _, err = self.cli("add", "--source", "l1", "--run", "R1", stdin=json.dumps([report_item()]))
        self.assertEqual(code, 1, err)
        self.assertIn("rules layer ~/rules/CODING-STYLE.md is not on this machine", err)
        self.assertIn(f"looked for {self.layer}", err)


class LoadRulesTest(SharedCase):
    def test_a_shared_layer_is_read_like_any_other_in_config_order(self) -> None:
        rules = load_rules(self.config())
        self.assertEqual(list(rules.clauses), ["G1.1", "G1.2", "G2.1", "PRJ.1", "PRJ.2"])
        self.assertEqual([source.path.name for source in rules.sources], [PROJECT, PROJECT])
        self.assertEqual([source.sha12 for source in rules.sources], [sha12(self.shared), sha12(self.repo / PROJECT)])
        self.assertEqual(rules.sources[0].text, GLOBAL_RULES)
        self.assertEqual(rules.severity_of("G1.1"), "blocker")  # the shared table covers the project's clauses of that family too
        self.assertEqual(rules.missing, ())

    def test_a_missing_shared_layer_refuses_and_names_the_layer_and_the_place(self) -> None:
        self.lose_the_layer()
        with self.assertRaises(SharedLayerMissing) as caught:
            load_rules(self.config())
        message = str(caught.exception)
        self.assertIn(f"{THE_LAYER_IS_GONE} (looked for {self.shared})", message)
        self.assertIn("outside this repository", message)
        self.assertIn("incomplete rule set", message)
        self.assertEqual(caught.exception.name, SHARED_NAME)

    def test_a_missing_file_of_the_repo_is_still_the_plain_error(self) -> None:
        (self.repo / PROJECT).unlink()
        with self.assertRaisesRegex(RatchetError, "cannot read rules file .*CODING-STYLE.md: not found in the working tree") as caught:
            load_rules(self.config())
        self.assertNotIsInstance(caught.exception, SharedLayerMissing)

    def test_skipping_says_so_on_stderr_and_goes_on_without_the_layer(self) -> None:
        self.lose_the_layer()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rules = load_rules(self.config(), skip_missing=True)
        self.assertEqual(err.getvalue(), f"warning: {THE_LAYER_IS_GONE} (looked for {self.shared}); skipped\n")
        self.assertEqual(rules.missing, (SHARED_NAME,))
        self.assertEqual(list(rules.clauses), ["PRJ.1", "PRJ.2"])

    def test_skipping_does_not_excuse_a_file_of_the_repo(self) -> None:
        self.lose_the_layer()
        (self.repo / PROJECT).unlink()
        with self.assertRaisesRegex(RatchetError, "cannot read rules file"), contextlib.redirect_stderr(io.StringIO()):
            load_rules(self.config(), skip_missing=True)

    def test_a_reference_to_the_missing_layer_is_not_judged_when_it_is_skipped(self) -> None:
        self.write_config(rules_files=[SHARED_NAME, PROJECT], tool_rule_map={"ruff:F811": "G1.1"})
        self.assertEqual(load_rules(self.config()).tool_rule_map, {"ruff:F811": "G1.1"})
        self.lose_the_layer()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(load_rules(self.config(), skip_missing=True).missing, (SHARED_NAME,))
        self.write_config(rules_files=[SHARED_NAME, PROJECT], tool_rule_map={"ruff:F811": "G9.9"})
        self.shared.write_text(GLOBAL_RULES, encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "no rules file defines"):  # the layer is there: a wrong reference is still wrong
            load_rules(self.config(), skip_missing=True)

    def test_the_shared_layers_are_only_the_outer_ones(self) -> None:
        (layer,) = shared_layers(self.config())
        self.assertEqual((layer.path, layer.sha12), (self.config().rules_files[0], sha12(self.shared)))

    def test_a_shared_layer_behind_a_symlinked_directory_is_read(self) -> None:
        link = self.tmp / "linked-shared"
        link.symlink_to(self.shared_dir)
        self.write_config(rules_files=["../linked-shared/CODING-STYLE.md", PROJECT])
        self.assertIn("G1.1", load_rules(self.config()).clauses)


class CompileTest(SharedCase):
    def test_the_digest_lists_the_shared_clauses_first_and_names_both_files(self) -> None:
        self.cli_ok("compile")
        digest = (self.repo / DIGEST).read_text(encoding="utf-8")
        self.assertIn(f"from {SHARED_NAME}@{sha12(self.shared)}, {PROJECT}@{sha12(self.repo / PROJECT)};", digest)
        self.assertLess(digest.index("G1.1 | blocker"), digest.index("PRJ.1 | should-fix"))
        self.cli_ok("compile", "--check")

    def test_the_header_names_a_file_the_same_way_whatever_the_config_spells_it(self) -> None:
        self.write_config(rules_files=["../shared/./CODING-STYLE.md", "./CODING-STYLE.md"])
        self.cli_ok("compile")
        header = (self.repo / DIGEST).read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f"from {SHARED_NAME}@{sha12(self.shared)}, {PROJECT}@{sha12(self.repo / PROJECT)};", header)

    def test_check_without_the_layer_exits_zero_with_a_warning_and_compares_nothing(self) -> None:
        self.cli_ok("compile")
        self.lose_the_layer()
        (self.repo / DIGEST).write_text("not even a digest\n", encoding="utf-8")  # stale for sure, but not comparable
        code, out, err = self.cli("compile", "--check")
        self.assertEqual((code, out), (0, ""))
        self.assertIn(f"warning: {THE_LAYER_IS_GONE} (looked for {self.shared}); skipped", err)
        self.assertIn(f"compile --check: digest not compared, it is generated from every rules layer and this machine lacks {SHARED_NAME}", err)
        self.assertNotIn("stale", err)

    def test_staged_check_without_the_layer_exits_zero_too(self) -> None:
        self.cli_ok("compile")
        git(self.repo, "add", "-A")
        self.commit("base")
        self.lose_the_layer()
        (self.repo / DIGEST).write_text("stale\n", encoding="utf-8")
        git(self.repo, "add", DIGEST)
        code, _, err = self.cli("compile", "--check", "--staged")
        self.assertEqual(code, 0, err)
        self.assertIn("compile --check --staged: digest not compared", err)

    def test_check_with_the_layer_still_fails_on_a_stale_digest(self) -> None:
        self.cli_ok("compile")
        (self.repo / DIGEST).write_text("stale\n", encoding="utf-8")
        code, _, err = self.cli("compile", "--check")
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST} (digest, out of date)", err)

    def test_check_still_fails_for_a_rules_file_of_the_repo_that_is_missing(self) -> None:
        self.lose_the_layer()
        (self.repo / PROJECT).unlink()
        code, _, err = self.cli("compile", "--check")
        self.assertEqual(code, 1)
        self.assertIn("cannot read rules file", err)

    def test_a_plain_compile_refuses_to_write_and_names_the_layer(self) -> None:
        self.lose_the_layer()
        err = self.refused("compile")
        self.assertIn("incomplete rule set", err)
        self.assertFalse((self.repo / DIGEST).exists())

    def test_a_dry_run_refuses_too_rather_than_show_a_digest_without_the_layer(self) -> None:
        self.lose_the_layer()
        out = self.cli("compile", "--dry-run")
        self.assertEqual(out[0], 1)
        self.assertIn(THE_LAYER_IS_GONE, out[2])
        self.assertEqual(out[1], "")

    def test_a_plain_compile_leaves_an_existing_digest_alone(self) -> None:
        self.cli_ok("compile")
        before = (self.repo / DIGEST).read_bytes()
        self.lose_the_layer()
        (self.repo / PROJECT).write_text(PROJECT_RULES + "3. **[PRJ.3]** Another one. (nice)\n", encoding="utf-8")
        self.refused("compile")
        self.assertEqual((self.repo / DIGEST).read_bytes(), before)

    def test_an_inject_target_outside_the_repo_is_not_told_to_be_staged(self) -> None:
        (self.shared_dir / "CLAUDE.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        self.write_config(rules_files=[SHARED_NAME, PROJECT], inject_targets=["../shared/CLAUDE.md"])
        (self.shared_dir / "CLAUDE.md").unlink()
        git(self.repo, "add", "-A")
        code, _, err = self.cli("compile", "--check", "--staged")
        self.assertEqual(code, 1)
        self.assertIn("inject target not found in the working tree:", err)
        self.assertIn("it is outside the repository, so it is read from disk and cannot be staged", err)
        self.assertNotIn("git add", err)

    def test_an_inject_target_in_the_repo_is_still_told_to_be_staged(self) -> None:
        self.write_config(rules_files=[SHARED_NAME, PROJECT], inject_targets=["CLAUDE.md"])
        (self.repo / "CLAUDE.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        git(self.repo, "add", ".ratchet.json", PROJECT)
        code, _, err = self.cli("compile", "--check", "--staged")
        self.assertEqual(code, 1)
        self.assertIn("inject target not found in the git index:", err)
        self.assertIn("(stage it with `git add`)", err)

    def test_a_missing_rules_file_of_the_repo_is_still_told_to_be_staged(self) -> None:
        git(self.repo, "add", ".ratchet.json")  # the project layer is on disk but not in the index
        code, _, err = self.cli("compile", "--check", "--staged")
        self.assertEqual(code, 1)
        self.assertIn("cannot read rules file", err)
        self.assertIn("not found in the git index (stage it with `git add`)", err)

    def test_the_readers_say_where_a_missing_file_was_looked_for(self) -> None:
        outside, inside = self.shared_dir / "absent.md", self.repo / "absent.md"
        self.assertEqual(DiskReader().missing_in(inside), ("the working tree", ""))
        index = IndexReader(self.repo)
        self.assertEqual(index.missing_in(inside), ("the git index", " (stage it with `git add`)"))
        self.assertEqual(index.missing_in(outside), ("the working tree", IndexReader.outside_hint))
        self.assertIn("outside the repository", IndexReader.outside_hint)


class PreCommitHookTest(SharedCase):
    """The hook init installs runs `compile --check --staged`: a machine without the shared file must still be able to commit."""

    def setUp(self) -> None:
        super().setUp()
        self.cli_ok("init", "--yes")
        git(self.repo, "add", "-A")
        self.committed = self.commit_with_hooks("-m", "adopt")
        self.assertEqual(self.committed.returncode, 0, self.committed.stderr)

    def commit_with_hooks(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", *args], cwd=self.repo,
                              capture_output=True, text=True, env={**os.environ, **GIT_IDENTITY})

    def edit_the_project_rules(self) -> None:
        (self.repo / PROJECT).write_text(self.project_text() + "3. **[PRJ.3]** Another one. (nice)\n", encoding="utf-8")
        git(self.repo, "add", PROJECT)  # staged without its regenerated digest

    def test_with_the_layer_a_stale_digest_blocks_the_commit(self) -> None:
        self.edit_the_project_rules()
        proc = self.commit_with_hooks("-m", "rule without digest")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("stale:", proc.stderr)

    def test_without_the_layer_the_commit_goes_through_with_a_warning(self) -> None:
        self.edit_the_project_rules()
        self.lose_the_layer()
        proc = self.commit_with_hooks("-m", "rule without digest, on a machine without the shared file")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(THE_LAYER_IS_GONE, proc.stderr)
        self.assertIn("digest not compared", proc.stderr)

    def test_the_post_commit_hook_does_not_need_the_layer(self) -> None:
        self.lose_the_layer()
        proc = self.commit_with_hooks("--allow-empty", "-m", "empty")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class InitTest(SharedCase):
    def test_init_refuses_at_the_compile_step_and_writes_no_digest(self) -> None:
        self.lose_the_layer()
        code, out, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(THE_LAYER_IS_GONE, err)
        self.assertFalse((self.repo / DIGEST).exists())
        self.assertNotIn("compile", out.replace("rules file", ""))  # no compile step reported as done

    def test_init_with_the_layer_compiles_both_layers(self) -> None:
        self.cli_ok("init", "--yes", "--no-hooks")
        self.assertIn("G1.1 | blocker", (self.repo / DIGEST).read_text(encoding="utf-8"))


class RefusalTest(SharedCase):
    """Nothing is recorded or written against a rule set that lacks a layer. Each case first works with the layer."""

    def ledger_unchanged(self, before: bytes | None) -> None:
        self.assertEqual(self.ledger_bytes(), before)

    def test_add_with_the_layer_maps_its_clauses_and_without_it_refuses(self) -> None:
        clauses = ("G1.1", "PRJ.1")  # one of the layer, one of the project: the project's is no excuse, its rule set is incomplete too
        for run, rule_id in zip(("R0", "R4"), clauses):
            self.cli_ok("add", "--source", "l1", "--run", run, stdin=json.dumps([report_item(id=f"{run}-001", rule_id=rule_id)]))
        before = self.ledger_bytes()
        self.lose_the_layer()
        for run, rule_id in zip(("R2", "R3"), clauses):
            with self.subTest(rule_id=rule_id):
                self.refused("add", "--source", "l1", "--run", run, stdin=json.dumps([report_item(id=f"{run}-001", rule_id=rule_id)]))
        self.ledger_unchanged(before)

    def test_ingest_code_review_does_not_file_the_layers_clauses_as_unmapped(self) -> None:
        review = json.dumps([review_item(category="g1-1", severity="blocker")])
        self.cli_ok("ingest-code-review", "--source", "l1", "--run", "r1", "--dry-run", stdin=review)
        out = self.cli_ok("ingest-code-review", "--source", "l1", "--run", "r1", stdin=review)
        self.assertIn("G1.1", out)
        self.assertEqual(self.events()[-1]["rule_id"], "G1.1")
        before = self.ledger_bytes()
        self.lose_the_layer()
        self.refused("ingest-code-review", "--source", "l1", "--run", "r2", stdin=json.dumps([review_item(category="g1-1", file="src/b.py")]))
        self.ledger_unchanged(before)

    def test_review_refuses_before_it_asks_claude_for_anything(self) -> None:
        self.lose_the_layer()
        for argv in (("review",), ("review", "--dry-run"), ("review", "--level", "low")):
            with self.subTest(argv=argv):
                err = self.refused(*argv)
                self.assertNotIn("not found on PATH", err)  # the claude check comes after the rules check
        self.ledger_unchanged(self.ledger_bytes())
        self.assertFalse((self.repo / ".ratchet" / "reviews").exists())

    def test_review_from_a_saved_file_refuses_too(self) -> None:
        saved = self.tmp / "review.json"
        saved.write_text(json.dumps([review_item()]), encoding="utf-8")
        self.cli_ok("review", "--from-file", str(saved), "--dry-run")
        before = self.ledger_bytes()
        self.lose_the_layer()
        self.refused("review", "--from-file", str(saved))
        self.ledger_unchanged(before)
        self.assertFalse((self.repo / ".ratchet" / "reviews").exists())

    def test_wrapup_that_would_review_refuses(self) -> None:
        self.write_config(rules_files=[SHARED_NAME, PROJECT], wrapup_min_files=1)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        saved = self.tmp / "review.json"
        saved.write_text(json.dumps([review_item()]), encoding="utf-8")
        self.lose_the_layer()
        before = self.ledger_bytes()
        self.refused("wrapup", "--from-file", str(saved))
        self.ledger_unchanged(before)

    def test_gate_record_refuses_and_so_does_the_api(self) -> None:
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        item = [{"file": "src/a.py", "line": 1, "code": "ruff:F811", "message": "redefinition"}]
        self.write_config(rules_files=[SHARED_NAME, PROJECT], tool_rule_map={"ruff:F811": "G1.1"})
        self.assertEqual(api.record_gate_findings(item, repo=self.repo).recorded[0]["rule_id"], "G1.1")
        before = self.ledger_bytes()
        self.lose_the_layer()
        self.refused("gate", "record", stdin=json.dumps([{**item[0], "line": 1, "message": "again, elsewhere"}]))
        with self.assertRaisesRegex(RatchetError, THE_LAYER_IS_GONE):
            api.record_gate_findings([{**item[0], "message": "another"}], repo=self.repo)
        self.ledger_unchanged(before)

    def test_distill_with_drafts_refuses_and_writes_no_state(self) -> None:
        drafts = self.tmp / "drafts.json"
        drafts.write_text(json.dumps([DraftMixin.draft(self.finding)]), encoding="utf-8")
        self.lose_the_layer()
        self.refused("distill", "--drafts", str(drafts))
        self.assertFalse((self.repo / STATE).exists())

    def test_decision_context_refuses(self) -> None:
        self.lose_the_layer()
        self.refused("decision-context")

    def test_statistics_need_no_rules_and_still_work(self) -> None:
        self.lose_the_layer()
        self.assertEqual(json.loads(self.cli_ok("distill"))["total"], 1)

    def test_labels_and_counts_need_no_rules_and_still_work(self) -> None:
        self.lose_the_layer()
        self.assertIn("findings: 1 total", self.cli_ok("count"))
        self.cli_ok("label", self.finding["id"], "--outcome", "fixed")


class DraftMixin:
    @staticmethod
    def draft(finding: dict, **overrides: object) -> dict:
        return {"action": "add", "rule_id": "PRJ.3", "file": PROJECT, "rationale": "A blind spot the review found.",
                "after": "3. **[PRJ.3]** Name the round that was skipped. (should-fix)",
                "evidence_ids": [finding["id"]], **overrides}


class ProposalTest(DraftMixin, SharedCase):
    """A proposal writes the project layer and nothing else of the rules; the shared layer is read for every check."""

    def setUp(self) -> None:
        super().setUp()
        self.cli_ok("compile")
        self.shared_bytes = self.shared.read_bytes()

    def batch(self, **overrides: object) -> dict:
        return create_batch(self.config(), self.events(), [self.draft(self.finding, **overrides)])

    def decision(self, batch: dict, choice: str = "accept") -> dict:
        return {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test operator",
                "decisions": [{"id": "p1", "decision": choice}]}

    def apply(self, batch: dict) -> dict:
        return apply_batch(self.config(), self.events(), self.decision(batch))

    def digest(self) -> str:
        return (self.repo / DIGEST).read_text(encoding="utf-8")

    def test_a_clause_of_a_project_family_goes_behind_its_siblings_in_the_project_file(self) -> None:
        self.apply(self.batch())
        self.assertIn("2. **[PRJ.2]** Reused IDs never skip the round check. (should-fix)\n3. **[PRJ.3]** Name the round that was skipped.",
                      self.project_text())
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)
        self.assertLess(self.digest().index("G2.1 |"), self.digest().index("PRJ.3 |"))
        self.cli_ok("compile", "--check")

    def test_a_clause_of_a_family_only_the_shared_layer_has_goes_to_the_end_of_the_project_file(self) -> None:
        after = "- [G1.9] Say why a guard exists."
        self.apply(self.batch(rule_id="G1.9", after=after))
        self.assertTrue(self.project_text().endswith(f"\n\n{after}\n"), self.project_text())
        self.assertEqual(self.project_text()[:len(PROJECT_RULES)], PROJECT_RULES)  # the rest of the file did not move
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)
        self.assertIn("G1.9 | blocker | Say why a guard exists", self.digest())  # the severity table of the shared layer covers it
        self.assertLess(self.digest().index("PRJ.2 |"), self.digest().index("G1.9 |"))
        self.cli_ok("compile", "--check")

    def test_a_new_family_in_the_project_file_can_have_its_own_severity(self) -> None:
        self.apply(self.batch(rule_id="Z9.1", after="- [Z9.1] A rule of a family nobody has. (nice)"))
        self.assertIn("Z9.1 | nice |", self.digest())

    def test_the_dry_run_of_an_assessment_does_not_touch_the_layer_either(self) -> None:
        create_batch(self.config(), self.events(), [self.draft(self.finding, rule_id="G1.9", after="- [G1.9] x.")], dry_run=True)
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)
        self.assertFalse((self.repo / STATE).exists())

    def test_an_id_the_shared_layer_defines_cannot_be_added(self) -> None:
        with self.assertRaisesRegex(RatchetError, "clause ID G1.1 has already been used"):
            self.batch(rule_id="G1.1", after="- [G1.1] Again.")

    def test_an_id_the_shared_layer_retired_stays_reserved(self) -> None:
        self.shared.write_text(GLOBAL_RULES + "\n<!-- ratchet-retired: G1.7 -->\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "clause ID G1.7 has already been used"):
            self.batch(rule_id="G1.7", after="- [G1.7] Back from the dead.")

    def test_an_id_the_project_retired_stays_reserved_as_before(self) -> None:
        (self.repo / PROJECT).write_text(PROJECT_RULES + "\n<!-- ratchet-retired: PRJ.7 -->\n", encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "clause ID PRJ.7 has already been used"):
            self.batch(rule_id="PRJ.7", after="- [PRJ.7] Back from the dead.")

    def test_a_clause_the_shared_layer_defines_cannot_be_rewritten_or_retired_here(self) -> None:
        for action, after in (("rewrite", "1. **[G1.1]** Keep a function to one clear job."), ("retire", "")):
            for file in (PROJECT, SHARED_NAME, str(self.shared)):
                with self.subTest(action=action, file=file):
                    with self.assertRaisesRegex(RatchetError, f"repository that owns that file") as caught:
                        self.batch(action=action, rule_id="G1.1", after=after, file=file)
                    self.assertIn("read-only here", str(caught.exception))
        self.assertFalse((self.repo / STATE).exists())
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)

    def test_the_message_names_the_file_that_defines_the_clause(self) -> None:
        with self.assertRaisesRegex(RatchetError, rf"G2.1 is defined in {SHARED_NAME.replace('.', r'[.]')}, outside this repository"):
            self.batch(action="retire", rule_id="G2.1", after="")

    def test_a_clause_added_to_the_shared_file_is_refused_and_pointed_at_the_project_files(self) -> None:
        with self.assertRaisesRegex(RatchetError, rf"{SHARED_NAME.replace('.', r'[.]')} lies outside this repository.*{PROJECT}$"):
            self.batch(rule_id="G1.9", after="- [G1.9] x.", file=SHARED_NAME)

    def test_a_clause_of_the_project_can_still_be_rewritten_and_retired(self) -> None:
        batch = self.batch(action="rewrite", rule_id="PRJ.2", after="2. **[PRJ.2]** Reused IDs never skip a round check. (should-fix)")
        self.apply(batch)
        self.assertIn("never skip a round check", self.project_text())
        self.cli_ok("compile", "--check")

    def test_a_batch_is_stale_when_the_shared_layer_changed_after_it_was_made(self) -> None:
        batch = self.batch()
        self.shared.write_text(GLOBAL_RULES.replace("Keep a function to one job.", "Keep a function to one clear job."), encoding="utf-8")
        with self.assertRaisesRegex(RatchetError, "create a fresh proposal batch"):
            self.apply(batch)
        self.assertNotIn("PRJ.3", self.project_text())

    def test_a_batch_applies_when_the_shared_layer_is_as_it_was(self) -> None:
        batch = self.batch()
        self.assertEqual(self.apply(batch)["status"], "applied")

    def test_the_batch_records_the_name_and_hash_of_each_shared_layer(self) -> None:
        self.assertEqual(self.batch()["config"]["shared"], [[SHARED_NAME, sha12(self.shared)]])

    def test_the_hash_is_part_of_the_batch_identity(self) -> None:
        first = self.batch()["id"]
        self.shared.write_text(GLOBAL_RULES + "\n", encoding="utf-8")
        self.cli_ok("compile")
        self.assertNotEqual(self.batch()["id"], first)

    def test_a_repo_without_a_shared_layer_records_none(self) -> None:
        self.write_config(rules_files=[PROJECT])
        self.cli_ok("compile")
        self.assertNotIn("shared", self.batch()["config"])

    def test_apply_refuses_when_the_layer_is_gone_and_changes_nothing(self) -> None:
        batch = self.batch()
        project = self.project_text()
        self.lose_the_layer()
        with self.assertRaisesRegex(SharedLayerMissing, THE_LAYER_IS_GONE):
            self.apply(batch)
        self.assertEqual(self.project_text(), project)
        self.assertFalse((self.repo / VERSION).exists())
        self.assertFalse((self.repo / CHANGELOG).exists())

    def test_a_repo_whose_rules_are_all_shared_has_nowhere_to_write(self) -> None:
        self.write_config(rules_files=[SHARED_NAME])
        with self.assertRaisesRegex(RatchetError, "every file in rules_files lies outside this repository.*project layer"):
            create_batch(self.config(), self.events(), [self.draft(self.finding, file=SHARED_NAME, rule_id="G1.9", after="- [G1.9] x.")])

    def test_the_cli_round_trip_leaves_the_shared_layer_byte_for_byte(self) -> None:
        drafts = self.tmp / "drafts.json"
        drafts.write_text(json.dumps([self.draft(self.finding)]), encoding="utf-8")
        batch_id = self.cli_ok("distill", "--drafts", str(drafts)).splitlines()[0].split(": ", 1)[1]
        batch = json.loads((self.repo / STATE / batch_id / "batch.json").read_text(encoding="utf-8"))
        self.cli_ok("apply-proposals", "--input", "-", stdin=json.dumps(self.decision(batch)))
        self.cli_ok("compile", "--check")
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)
        self.cli_ok("rollback-proposals", "--batch", batch_id)
        self.assertEqual(self.project_text(), PROJECT_RULES)
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)


class LinkedWorktreeTest(DraftMixin, SharedCase):
    """The shared file is one file for every worktree; the project layer is each worktree's own."""

    def setUp(self) -> None:
        super().setUp()
        self.cli_ok("compile")
        git(self.repo, "add", "-A")
        self.commit("base")
        self.tree = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(self.tree))
        self.shared_bytes = self.shared.read_bytes()

    def in_tree(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        return self.cli(*argv, "--repo", str(self.tree), stdin=stdin, with_repo=False)

    def test_the_linked_worktree_compiles_to_the_same_digest(self) -> None:
        code, _, err = self.in_tree("compile", "--check")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.tree / DIGEST).read_bytes(), (self.repo / DIGEST).read_bytes())

    def test_a_proposal_in_the_linked_worktree_writes_its_project_file_only(self) -> None:
        config = load_config(self.tree, None, None, self.tree)
        batch = create_batch(config, self.events(), [self.draft(self.finding)])
        self.assertEqual(list(batch["baseline"]).count(PROJECT), 1)
        self.assertFalse(any(name.startswith("..") for name in batch["baseline"]))
        decision = {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test operator",
                    "decisions": [{"id": "p1", "decision": "accept"}]}
        apply_batch(config, self.events(), decision)
        self.assertIn("PRJ.3", (self.tree / PROJECT).read_text(encoding="utf-8"))
        self.assertNotIn("PRJ.3", self.project_text())
        self.assertEqual(self.shared.read_bytes(), self.shared_bytes)
        self.assertEqual(self.in_tree("compile", "--check")[0], 0)

    def test_the_linked_worktree_refuses_a_change_to_the_shared_layer_too(self) -> None:
        config = load_config(self.tree, None, None, self.tree)
        with self.assertRaisesRegex(RatchetError, "repository that owns that file"):
            create_batch(config, self.events(), [self.draft(self.finding, action="retire", rule_id="G1.2", after="")])

    def test_without_the_layer_the_check_passes_and_the_rest_refuses_from_the_linked_worktree_too(self) -> None:
        self.lose_the_layer()
        code, _, err = self.in_tree("compile", "--check", "--staged")
        self.assertEqual(code, 0, err)
        self.assertIn("digest not compared", err)
        code, _, err = self.in_tree("add", "--source", "l1", "--run", "R9", stdin=json.dumps([report_item(id="R9-001")]))
        self.assertEqual(code, 1)
        self.assertIn(THE_LAYER_IS_GONE, err)

    def test_a_layer_in_a_worktree_one_level_deeper_is_still_found_from_the_main_worktree(self) -> None:
        deeper = self.tmp / "deep" / "linked"
        deeper.parent.mkdir()
        git(self.repo, "worktree", "add", "-q", "-b", "deeper", str(deeper))
        config = load_config(deeper, None, None, deeper)
        self.assertEqual(load_rules(config).missing, ())
        self.assertEqual([layer.path for layer in shared_layers(config)], [self.repo / SHARED_NAME])


class ContextTest(DraftMixin, SharedCase):
    """decision-context reads the shared layer as a source, with no complaint for being outside the repo."""

    def test_the_context_holds_both_layers_as_available_sources(self) -> None:
        context = build_context(self.config(), self.events())
        shared, project = context["rules"]
        self.assertEqual((shared["status"], shared["file"], shared["absolute_path"]), ("available", SHARED_NAME, str(self.shared)))
        self.assertEqual(shared["text"], GLOBAL_RULES.rstrip("\n"))
        self.assertEqual((project["status"], project["file"]), ("available", PROJECT))

    def test_the_context_changes_with_the_shared_layer(self) -> None:
        before = build_context(self.config(), self.events())["context_id"]
        self.shared.write_text(GLOBAL_RULES + "\n1. **[G3.1]** Added upstream.\n", encoding="utf-8")
        self.assertNotEqual(build_context(self.config(), self.events())["context_id"], before)

    def test_a_clause_the_shared_layer_defines_can_cover_a_finding(self) -> None:
        config, events = self.config(), self.events()
        context = build_context(config, events)
        payload = {"v": 1, "context_id": context["context_id"], "assessments": [
            {"finding_ids": [self.finding["id"]], "verification": {"status": "supported", "basis": "read the rule"},
             "action": "covered", "rule_ids": ["G1.1"], "reason": "The global rule already says it.", "changes": []}]}
        (card,) = assess(config, events, context, payload)["cards"]
        (match,) = card["matched_rules"]
        self.assertEqual((match["rule_id"], match["absolute_path"]), ("G1.1", str(self.shared)))
        self.assertIn("Keep a function to one job", match["text"])

    def test_an_assessment_that_rewrites_a_shared_clause_is_refused_with_the_owner_named(self) -> None:
        config, events = self.config(), self.events()
        context = build_context(config, events)
        payload = {"v": 1, "context_id": context["context_id"], "assessments": [
            {"finding_ids": [self.finding["id"]], "verification": {"status": "supported", "basis": "read the rule"},
             "action": "rewrite", "rule_ids": ["G1.1"], "reason": "It is too narrow.",
             "changes": [{"action": "rewrite", "rule_id": "G1.1", "file": PROJECT, "after": "1. **[G1.1]** Wider."}]}]}
        with self.assertRaisesRegex(RatchetError, f"G1.1 is defined in {SHARED_NAME.replace('.', '[.]')}.*repository that owns that file"):
            assess(config, events, context, payload)

    def test_a_missing_layer_refuses_in_words_not_with_an_errno(self) -> None:
        self.lose_the_layer()
        with self.assertRaisesRegex(SharedLayerMissing, THE_LAYER_IS_GONE):
            build_context(self.config(), self.events())

    def test_the_review_evidence_keeps_the_text_of_the_shared_layer(self) -> None:
        config = self.config()
        shared, project = capture(config, self.repo, None)["rules"]
        self.assertEqual((shared["status"], shared["file"]), ("available", SHARED_NAME))
        self.assertIn("G1.1", shared["text"])
        self.assertEqual((project["status"], project["file"]), ("available", PROJECT))


class ForeseenRefusalTest(SharedCase):
    """A review that cannot start is known beforehand (`review_refusal`, `wrapup --dry-run`). A shared layer this machine lacks
    is the first refusal, ahead of the round key, the round cap and the claude command. The claude here exists and records a
    call, so nothing but the layer can stop a review, and a call would show."""

    def setUp(self) -> None:
        super().setUp()
        git(self.repo, "add", ".ratchet.json", PROJECT)
        self.commit("base")
        (self.repo / "big.py").write_text("".join(f"x{number} = {number}\n" for number in range(250)), encoding="utf-8")  # over wrapup_min_changed_lines
        self.calls = self.tmp / "claude-calls"
        claude = self.tmp / "fake-claude"
        claude.write_text(f"#!/bin/sh\necho \"$@\" >> '{self.calls}'\nexit 99\n", encoding="utf-8")
        claude.chmod(0o755)
        self.use(claude)

    def use(self, command: Path) -> None:
        """RATCHET_CLAUDE_CMD for the rest of the test."""
        patcher = mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(command)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def refusal(self) -> Refusal | None:
        config = self.config()
        return review_refusal(config, read_events(config.ledger_path), target=None, from_file=False, over_cap_allowed=False,
                              replayed=False)

    def kinds(self) -> tuple[str, str]:
        """What refuses the review with the layer gone, then what refuses it once the layer is back."""
        self.lose_the_layer()
        without = self.refusal().kind
        self.shared.write_text(GLOBAL_RULES, encoding="utf-8")
        return without, self.refusal().kind

    def message(self) -> str:
        """The words for the missing layer, wherever it refuses."""
        return (f"rules layer {SHARED_NAME} is not on this machine (looked for {self.shared}). It lies outside this repository, and "
                "without its clauses this command would run on an incomplete rule set: put the file there, or run this on a machine "
                "that has it")

    def test_with_the_layer_there_nothing_stops_the_review(self) -> None:
        self.assertIsNone(self.refusal())
        data = json.loads(self.cli_ok("wrapup", "--dry-run", "--json"))
        self.assertEqual((data["status"], data["would_refuse"]), ("dry-run", None))

    def test_without_it_the_review_is_refused_for_the_rules_in_the_words_of_the_layer(self) -> None:
        self.lose_the_layer()
        self.assertEqual(self.refusal(), Refusal(REFUSE_RULES, None, self.message()))

    def test_it_is_the_first_refusal_whatever_else_would_stop_the_review(self) -> None:
        key = round_key(self.repo, None, None)
        self.use(self.tmp / "no-such-claude")
        self.assertEqual(self.kinds(), (REFUSE_RULES, REFUSE_CLAUDE))
        self.append_raw(*[review_round(key, run=f"past{number}") for number in (1, 2, 3)])  # max_review_rounds is 3
        self.assertEqual(self.kinds(), (REFUSE_RULES, REFUSE_ROUND_CAP))
        git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.kinds(), (REFUSE_RULES, REFUSE_ROUND_KEY))

    def test_wrapup_dry_run_reports_it_in_would_refuse_and_writes_nothing(self) -> None:
        self.lose_the_layer()
        before = self.ledger_bytes()
        code, out, err = self.cli("wrapup", "--dry-run", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["status"], data["would_refuse"]), ("dry-run", {"kind": "rules", "round_key": None, "message": self.message()}))
        self.assertIn(f"a real run would be refused: {self.message()}; nothing run or written", err)  # --json sends the text to stderr
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".ratchet").exists())
        self.assertFalse(self.calls.exists())

    def test_the_text_report_says_the_review_would_be_refused_and_why(self) -> None:
        self.lose_the_layer()
        out = self.cli_ok("wrapup", "--dry-run")
        self.assertIn(f"a real run would be refused: {self.message()}; nothing run or written", out)
        self.assertNotIn("would run `", out)  # the plan of a review that starts, which this is not

    def test_a_saved_review_needs_the_layer_as_well(self) -> None:
        saved = self.tmp / "review.json"
        saved.write_text(json.dumps([review_item()]), encoding="utf-8")
        self.lose_the_layer()
        data = json.loads(self.cli_ok("wrapup", "--dry-run", "--json", "--from-file", str(saved)))
        self.assertEqual(data["would_refuse"], {"kind": "rules", "round_key": None, "message": self.message()})

    def test_the_real_wrapup_is_refused_with_the_words_the_dry_run_gave(self) -> None:
        self.lose_the_layer()
        before = self.ledger_bytes()
        code, _, err = self.cli("wrapup")
        self.assertEqual((code, err), (1, f"ratchet: error: {self.message()}\n"))
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".ratchet" / "reviews").exists())
        self.assertFalse(self.calls.exists())

    def test_a_report_of_the_latest_review_needs_no_rules_and_still_works(self) -> None:
        self.append_raw(review_round(round_key(self.repo, None, None), run="r1"))
        self.lose_the_layer()
        data = json.loads(self.cli_ok("wrapup", "--report-only", "--json"))
        self.assertEqual((data["status"], data["would_refuse"]), ("reported", None))


if __name__ == "__main__":
    unittest.main()
