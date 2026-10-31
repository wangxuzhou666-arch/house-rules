"""Linked worktrees: what every worktree shares and what each one has of its own.

The ledger, the skips file, `.house-rules/` and `.house-rules.json` live in the main worktree and all of them use those. The files git
tracks are each worktree's own: the rules, the digest, the inject targets, the changelog and the code under review. Every test
makes the two worktrees differ in the file it is about, so reading the wrong one shows.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import STYLE_RULES, RepoCase, git, report_item, review_finding, review_item

from house_rules import api
from house_rules.config import load_config, tracked_name, tracked_path
from house_rules.decision_context import build_context
from house_rules.errors import HouseRulesError
from house_rules.gate import finding_hash
from house_rules.native_decisions import apply_answers, prepare
from house_rules.proposal_store import JOURNAL, encoded, replace_local
from house_rules.proposals import CHANGELOG, STATE, VERSION
from house_rules.review import save_review
from house_rules.review_evidence import capture, save

DIGEST = "CODING-STYLE.digest.md"
REVIEWER = "reviewer.md"
EDITED_RULES = STYLE_RULES.replace("Fail fast at the boundary.", "Fail fast at every boundary.")
WITH_NEW_CLAUSE = STYLE_RULES.replace("1. **[P3.1]** Explain why, not what.\n",
                                      "1. **[P3.1]** Explain why, not what.\n2. **[P3.9]** Name the invariant a comment protects.\n")
REWRITTEN = "1. **[P1.1]** Nesting depth of four or more is a violation."
SOURCE = "".join(f"line {number}\n" for number in range(1, 9))
TREE_SOURCE = SOURCE.replace("line 3\n", "tree line 3\n")
GIT_IDENTITY = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


class LinkedCase(RepoCase):
    """The main worktree and a linked one on branch `feature`, both holding the committed rules, digest, reviewer file and source."""

    def setUp(self) -> None:
        super().setUp()
        self.write_rules(STYLE_RULES)
        (self.repo / REVIEWER).write_text("Before\n<!-- style-digest:begin -->\nold\n<!-- style-digest:end -->\nAfter\n", encoding="utf-8")
        self.write_config(rules_files=["rules.md"], inject_targets=[REVIEWER])
        self.cli_ok("compile")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text(SOURCE, encoding="utf-8")
        git(self.repo, "add", "-A")
        self.base = self.commit("base")
        self.tree = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(self.tree))
        patcher = mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": "/nonexistent/claude"})  # no test reaches the real claude
        patcher.start()
        self.addCleanup(patcher.stop)

    def in_tree(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        return self.cli(*argv, "--repo", str(self.tree), stdin=stdin, with_repo=False)

    def ok_in_tree(self, *argv: str, stdin: str = "") -> str:
        code, out, err = self.in_tree(*argv, stdin=stdin)
        self.assertEqual(code, 0, f"house-rules {argv} failed in the linked worktree: {err}")
        return out

    def main_text(self, name: str) -> str:
        return (self.repo / name).read_text(encoding="utf-8")

    def tree_text(self, name: str) -> str:
        return (self.tree / name).read_text(encoding="utf-8")

    def commit_in_tree(self, *message: str) -> str:
        args = [part for text in message for part in ("-m", text)]
        git(self.tree, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", *args)
        return git(self.tree, "rev-parse", "HEAD")

    def main_files(self, *names: str) -> dict[str, str]:
        return {name: self.main_text(name) for name in names}


class ConfigPathsTest(LinkedCase):
    """`load_config` puts the shared state under the main worktree and the tracked files under the worktree being worked in."""

    def test_tracked_files_are_the_worktrees_and_shared_state_is_the_mains(self) -> None:
        self.write_config(rules_files=["rules.md"], inject_targets=[REVIEWER, {"path": "docs/x.md", "sections": ["labeling"]}],
                          skips_file="skips.jsonl")
        config = load_config(self.tree, None, None, self.tree)
        self.assertEqual((config.root, config.worktree), (self.repo, self.tree))
        self.assertEqual(config.rules_files, (self.tree / "rules.md",))
        self.assertEqual(config.digest_path, self.tree / DIGEST)
        self.assertEqual([target.path for target in config.inject_targets], [self.tree / REVIEWER, self.tree / "docs" / "x.md"])
        self.assertEqual(config.ledger_path, self.repo / ".style-findings.jsonl")
        self.assertEqual(config.skips_path, self.repo / "skips.jsonl")

    def test_in_the_main_worktree_both_are_the_main_worktree(self) -> None:
        config = load_config(self.repo, None, None, self.repo)
        self.assertEqual((config.root, config.worktree), (self.repo, self.repo))
        self.assertEqual(config.rules_files, (self.repo / "rules.md",))

    def test_the_worktree_is_found_from_a_subdirectory_and_from_the_current_directory(self) -> None:
        self.assertEqual(load_config(self.tree / "src", None, None, self.repo).worktree, self.tree)
        self.assertEqual(load_config(None, None, None, self.tree / "src").worktree, self.tree)
        self.assertEqual(load_config(None, None, None, self.repo / "src").worktree, self.repo)

    def test_the_config_of_the_main_worktree_decides_whatever_the_linked_copy_says(self) -> None:
        (self.tree / ".house-rules.json").write_text(json.dumps({"rules_files": ["absent.md"]}), encoding="utf-8")
        self.assertEqual(self.in_tree("compile", "--check")[0], 0)
        self.assertEqual(load_config(self.tree, None, None, self.tree).rules_files, (self.tree / "rules.md",))

    def test_a_name_outside_the_repo_is_looked_up_from_the_main_worktree(self) -> None:
        root, tree = Path("/main/repo"), Path("/elsewhere/linked")
        self.assertEqual(tracked_path(root, tree, "docs/rules.md"), tree / "docs" / "rules.md")
        self.assertEqual(tracked_path(root, tree, "docs/../rules.md"), tree / "rules.md")
        self.assertEqual(tracked_path(root, tree, "../shared.md"), root / "../shared.md")
        self.assertEqual(tracked_path(root, tree, "/etc/shared.md"), Path("/etc/shared.md"))
        self.assertEqual(tracked_path(root, root, "docs/../rules.md"), root / "docs" / ".." / "rules.md")  # the main worktree: as written

    def test_a_tracked_name_reads_the_same_from_every_worktree(self) -> None:
        root, tree = Path("/main/repo"), Path("/elsewhere/linked")
        for name in ("rules.md", "docs/rules.md", "../shared.md"):
            with self.subTest(name=name):
                self.assertEqual(tracked_name(root, tree, tracked_path(root, tree, name)), tracked_name(root, root, tracked_path(root, root, name)))
        self.assertEqual(tracked_name(root, tree, tracked_path(root, tree, "../shared.md")), "../shared.md")


class CompileTest(LinkedCase):
    def test_compile_writes_the_digest_and_the_injected_block_of_the_worktree_it_is_run_in(self) -> None:
        in_main = self.main_files("rules.md", DIGEST, REVIEWER)
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        out = self.ok_in_tree("compile")
        self.assertIn(f"wrote {DIGEST} (digest)", out)
        self.assertIn(f"wrote {REVIEWER} (injected block)", out)
        self.assertIn(f"digest {DIGEST}: ", out)  # the summary line names the digest from the worktree too
        self.assertIn("Fail fast at every boundary", self.tree_text(DIGEST))
        self.assertIn("Fail fast at every boundary", self.tree_text(REVIEWER))
        self.assertEqual(self.main_files("rules.md", DIGEST, REVIEWER), in_main)

    def test_an_inject_target_without_its_markers_is_named_from_the_worktree(self) -> None:
        (self.tree / REVIEWER).write_text("no markers here\n", encoding="utf-8")
        code, _, err = self.in_tree("compile")
        self.assertEqual(code, 1)
        self.assertIn(f"house-rules: error: {REVIEWER}: marker", err)  # anchored: `../linked/reviewer.md: marker` contains the bare form

    def test_compile_check_compares_the_files_of_the_worktree_it_is_run_in(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")  # its rules moved on, its digest did not
        code, _, err = self.in_tree("compile", "--check")
        self.assertEqual(code, 1)
        self.assertIn(f"stale: {DIGEST} (digest, out of date)", err)
        self.assertEqual(self.cli("compile", "--check")[0], 0)  # the main worktree's pair is still consistent
        self.ok_in_tree("compile")
        self.assertEqual(self.in_tree("compile", "--check")[0], 0)

    def test_the_main_worktree_is_checked_against_its_own_files_while_a_linked_one_differs(self) -> None:
        (self.repo / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        self.assertEqual(self.cli("compile", "--check")[0], 1)
        self.assertEqual(self.in_tree("compile", "--check")[0], 0)

    def test_compile_dry_run_names_the_worktrees_files_and_writes_nothing(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        before = self.tree_text(DIGEST), self.tree_text(REVIEWER), self.main_text(DIGEST)
        out = self.ok_in_tree("compile", "--dry-run")
        self.assertIn(f"[dry-run] compile: would write {DIGEST} (digest)", out)
        self.assertIn(f"+++ b/{DIGEST}", out)
        self.assertEqual((self.tree_text(DIGEST), self.tree_text(REVIEWER), self.main_text(DIGEST)), before)

    def test_the_same_rules_compile_to_the_same_bytes_in_every_worktree(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        (self.repo / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        self.ok_in_tree("compile")
        self.cli_ok("compile")
        for name in (DIGEST, REVIEWER):
            self.assertEqual(self.tree_text(name), self.main_text(name), name)

    def test_the_current_directory_decides_the_worktree_when_repo_is_not_given(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        previous = os.getcwd()
        os.chdir(self.tree / "src")
        self.addCleanup(os.chdir, previous)
        code, out, err = self.cli("compile", with_repo=False)
        self.assertEqual(code, 0, err)
        self.assertIn("Fail fast at every boundary", self.tree_text(DIGEST))
        self.assertNotIn("every boundary", self.main_text(DIGEST))

    def test_a_staged_check_follows_the_index_and_files_of_the_linked_worktree(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        self.ok_in_tree("compile")
        git(self.tree, "add", "rules.md")
        code, _, err = self.in_tree("compile", "--check", "--staged")
        self.assertEqual(code, 1, err)
        git(self.tree, "add", DIGEST, REVIEWER)
        self.assertEqual(self.in_tree("compile", "--check", "--staged")[0], 0)

    def test_a_rules_file_shared_outside_the_repo_is_the_same_file_from_every_worktree(self) -> None:
        (self.tmp / "shared-rules.md").write_text(STYLE_RULES, encoding="utf-8")
        self.write_config(rules_files=["../shared-rules.md"])
        self.cli_ok("compile")
        self.assertIn("../shared-rules.md@", self.main_text(DIGEST))
        git(self.repo, "add", "-A")
        self.commit("share the rules")
        (self.tmp / "deep").mkdir()
        deep = self.tmp / "deep" / "linked"  # a level deeper than the main worktree: `../` leads somewhere else from here
        git(self.repo, "worktree", "add", "-q", "-b", "deeper", str(deep))
        code, out, err = self.cli("compile", "--check", "--repo", str(deep), with_repo=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((deep / DIGEST).read_text(encoding="utf-8"), self.main_text(DIGEST))


class HookTest(LinkedCase):
    """The hooks `init` installs run in the worktree a commit is made in, and see its files."""

    def setUp(self) -> None:
        super().setUp()
        self.cli_ok("init", "--yes")  # the hooks go to the shared .git/hooks

    def commit_in_tree_with_hooks(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", *args], cwd=self.tree,
                              capture_output=True, text=True, env={**os.environ, **GIT_IDENTITY})

    def test_the_pre_commit_hook_passes_a_commit_made_after_compiling_in_the_linked_worktree(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        self.ok_in_tree("compile")
        git(self.tree, "add", "rules.md", DIGEST, REVIEWER)
        proc = self.commit_in_tree_with_hooks("-m", "rule: every boundary")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_pre_commit_hook_blocks_a_linked_worktrees_commit_whose_digest_is_stale(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        git(self.tree, "add", "rules.md")
        proc = self.commit_in_tree_with_hooks("-m", "rule without its digest")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"stale: {DIGEST}", proc.stderr)

    def test_the_post_commit_hook_closes_the_finding_a_trailer_names_in_a_linked_worktree(self) -> None:
        finding = review_finding(display_id="r1-1", run="r1")
        self.append_raw(finding)
        (self.tree / "note.txt").write_text("fixed\n", encoding="utf-8")
        git(self.tree, "add", "note.txt")
        proc = self.commit_in_tree_with_hooks("-m", "fix: the thing\n\nFixes-Finding: r1-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (outcome,) = [event for event in self.events() if event["kind"] == "outcome"]
        self.assertEqual((outcome["id"], outcome["outcome"], outcome["outcome_by"]), (finding["id"], "fixed", "auto:commit"))
        self.assertTrue(outcome["evidence"].startswith(f"commit {git(self.tree, 'rev-parse', '--short=8', 'HEAD')} "))


class InferTest(LinkedCase):
    def setUp(self) -> None:
        super().setUp()
        self.finding = review_finding(display_id="r1-1", run="r1")
        self.append_raw(self.finding)

    def outcomes(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "outcome"]

    def test_a_trailer_committed_in_a_linked_worktree_is_found_from_that_worktree(self) -> None:
        sha = self.commit_in_tree("fix: the thing", "Fixes-Finding: r1-1")
        self.assertIn("+0 fixed", self.cli_ok("infer"))  # the main worktree's HEAD carries no trailer
        self.assertEqual(self.outcomes(), [])
        self.assertIn("+1 fixed (auto:commit)", self.ok_in_tree("infer"))
        (outcome,) = self.outcomes()
        self.assertEqual((outcome["id"], outcome["outcome_by"]), (self.finding["id"], "auto:commit"))
        self.assertTrue(outcome["evidence"].startswith(f"commit {sha[:8]} "))

    def test_a_trailer_committed_in_the_main_worktree_is_not_read_from_a_linked_one(self) -> None:
        self.commit("fix: the thing", "Fixes-Finding: r1-1")
        self.assertIn("+0 fixed", self.ok_in_tree("infer"))
        self.assertEqual(self.outcomes(), [])
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer"))

    def test_all_reads_the_branches_of_every_worktree_from_any_of_them(self) -> None:
        self.commit_in_tree("fix: the thing", "Fixes-Finding: r1-1")
        self.assertIn("+1 fixed (auto:commit)", self.cli_ok("infer", "--all"))

    def test_quiet_prints_nothing_in_a_linked_worktree_with_nothing_to_close(self) -> None:
        self.assertEqual(self.ok_in_tree("infer", "--quiet"), "")


class RecordedInTest(LinkedCase):
    """What is recorded in the ledger from a linked worktree names that worktree's HEAD and reads its files."""

    def setUp(self) -> None:
        super().setUp()
        (self.tree / "src" / "a.py").write_text(TREE_SOURCE, encoding="utf-8")
        git(self.tree, "add", "src")
        self.tree_sha = self.commit_in_tree("feature work")[:8]
        self.main_sha = git(self.repo, "rev-parse", "--short=8", "HEAD")
        self.assertNotEqual(self.tree_sha, self.main_sha)

    def shas(self, kind: str) -> set[str]:
        return {event["sha"] for event in self.events() if event["kind"] == kind}

    def test_add_stamps_the_head_of_the_worktree_it_is_run_in(self) -> None:
        self.ok_in_tree("add", "--source", "l1", "--run", "R1", stdin=json.dumps([report_item(rule_id="P0E.1")]))
        self.assertEqual(self.shas("finding"), {self.tree_sha})
        self.cli_ok("add", "--source", "l1", "--run", "R2", stdin=json.dumps([report_item(id="R2-001", rule_id="P0E.1")]))
        self.assertEqual(self.shas("finding"), {self.tree_sha, self.main_sha})

    def test_ingest_code_review_stamps_the_head_of_the_worktree_it_is_run_in(self) -> None:
        text = json.dumps([review_item(category="p0e-1")])
        self.ok_in_tree("ingest-code-review", "--source", "l1", "--run", "R1", stdin=text)
        self.assertEqual(self.shas("finding"), {self.tree_sha})

    def test_a_gate_finding_and_a_gate_pass_stamp_the_head_of_the_worktree_they_are_run_in(self) -> None:
        item = {"file": "src/a.py", "line": 3, "code": "P0E.1", "message": "gate says no"}
        self.ok_in_tree("gate", "record", stdin=json.dumps([item]))
        self.ok_in_tree("gate", "pass", "--files", "src/a.py")
        self.assertEqual((self.shas("finding"), self.shas("gate_pass")), ({self.tree_sha}, {self.tree_sha}))

    def test_the_python_api_stamps_the_head_of_the_worktree_it_is_given(self) -> None:
        item = {"file": "src/a.py", "line": 3, "code": "P0E.1", "message": "gate says no"}
        api.record_gate_findings([item], repo=self.tree)
        api.record_gate_pass(["src/a.py"], repo=self.tree)
        self.assertEqual((self.shas("finding"), self.shas("gate_pass")), ({self.tree_sha}, {self.tree_sha}))

    def test_a_gate_finding_is_hashed_and_cut_from_the_code_of_the_worktree(self) -> None:
        item = {"file": "src/a.py", "line": 3, "code": "P0E.1", "message": "gate says no"}
        self.ok_in_tree("gate", "record", stdin=json.dumps([item]))
        self.cli_ok("gate", "record", stdin=json.dumps([item]))
        in_tree, in_main = [event for event in self.events() if event["kind"] == "finding"]
        self.assertEqual(in_tree["display_id"], finding_hash("src/a.py", "P0E.1", "tree line 3"))
        self.assertIn("tree line 3", in_tree["snippet"])
        self.assertEqual(in_main["display_id"], finding_hash("src/a.py", "P0E.1", "line 3"))
        self.assertNotIn("tree line 3", in_main["snippet"])

    def test_a_gate_file_named_by_an_absolute_path_must_be_inside_the_worktree_it_is_run_in(self) -> None:
        item = {"file": str(self.tree / "src" / "a.py"), "line": 3, "code": "P0E.1", "message": "gate says no"}
        self.ok_in_tree("gate", "record", stdin=json.dumps([item]))
        self.ok_in_tree("gate", "pass", "--files", str(self.tree / "src" / "a.py"))
        self.assertEqual({event["file"] for event in self.events() if event["kind"] == "finding"}, {"src/a.py"})
        code, _, err = self.cli("gate", "record", stdin=json.dumps([item]))  # the main worktree does not contain it
        self.assertEqual(code, 1)
        self.assertIn("is outside the repo", err)

    def test_rule_ids_are_checked_against_the_rules_of_the_worktree(self) -> None:
        (self.tree / "rules.md").write_text(WITH_NEW_CLAUSE, encoding="utf-8")  # P3.9 exists in the linked worktree only
        items = json.dumps([report_item(rule_id="P3.9")])
        self.ok_in_tree("add", "--source", "l1", "--run", "R1", stdin=items)
        code, _, err = self.cli("add", "--source", "l1", "--run", "R2", stdin=items)
        self.assertEqual(code, 1)
        self.assertIn("unknown rule_id 'P3.9'", err)

    def test_a_gate_code_is_checked_against_the_rules_of_the_worktree(self) -> None:
        (self.tree / "rules.md").write_text(WITH_NEW_CLAUSE, encoding="utf-8")
        item = json.dumps([{"file": "src/a.py", "line": 3, "code": "P3.9", "message": "gate says no"}])
        self.ok_in_tree("gate", "record", stdin=item)
        self.assertEqual(self.cli("gate", "record", stdin=item)[0], 1)


class ReviewRulesTest(LinkedCase):
    """`review` labels findings with, and saves as evidence, the rules of the worktree it reviews."""

    def fake_claude(self, stdout: str) -> Path:
        script = self.tmp / "fake-claude"
        script.write_text(f"#!{sys.executable}\nimport sys\nif '--version' in sys.argv[1:]:\n    sys.stdout.write('2.1.0 (Claude Code)')\n    sys.exit(0)\n"
                          f"sys.stdout.write({stdout!r})\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_findings_are_labeled_and_the_evidence_is_saved_with_the_rules_of_the_reviewed_worktree(self) -> None:
        (self.tree / "rules.md").write_text(WITH_NEW_CLAUSE, encoding="utf-8")
        text = "```json\n" + json.dumps([review_item(category="p3-9", severity="nice")]) + "\n```\n"
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(self.fake_claude(text))}):
            code, _, err = self.cli("review", "--run", "R1", "--repo", str(self.tree), with_repo=False)
        self.assertEqual((code, err), (0, ""))
        (finding,) = [event for event in self.events() if event["kind"] == "finding"]
        self.assertEqual((finding["rule_id"], finding["rule_id_source"]), ("P3.9", "model"))
        evidence = json.loads((self.repo / ".house-rules" / "reviews" / "R1.evidence.json").read_text(encoding="utf-8"))
        (rules,) = evidence["rules"]
        self.assertEqual(rules["absolute_path"], str(self.tree / "rules.md"))
        self.assertIn("Name the invariant a comment protects", rules["text"])

    def test_the_main_worktree_does_not_know_a_clause_only_the_linked_one_has(self) -> None:
        (self.tree / "rules.md").write_text(WITH_NEW_CLAUSE, encoding="utf-8")
        text = "```json\n" + json.dumps([review_item(category="p3-9", severity="nice")]) + "\n```\n"
        with mock.patch.dict(os.environ, {"HOUSE_RULES_CLAUDE_CMD": str(self.fake_claude(text))}):
            self.cli_ok("review", "--run", "R1")
        (finding,) = [event for event in self.events() if event["kind"] == "finding"]
        self.assertEqual(finding["rule_id"], "unmapped")


class DecisionContextTest(LinkedCase):
    def setUp(self) -> None:
        super().setUp()
        (self.tree / "src" / "a.py").write_text(TREE_SOURCE, encoding="utf-8")
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        self.finding = review_finding(file="src/a.py", line=3, rule_id="unmapped")
        self.append_raw(self.finding)

    def packet(self, *repo: str) -> dict:
        out = self.cli_ok("decision-context", *repo) if not repo else self.ok_in_tree("decision-context")
        return json.loads(out)

    def test_the_code_and_the_rules_are_read_from_the_worktree_it_is_run_in(self) -> None:
        in_tree = self.packet("tree")
        source = in_tree["findings"][0]["source"]
        self.assertEqual(source["absolute_path"], str(self.tree / "src" / "a.py"))
        self.assertIn("tree line 3", source["text"])
        (rules,) = in_tree["rules"]
        self.assertEqual(rules["absolute_path"], str(self.tree / "rules.md"))
        self.assertIn("every boundary", rules["text"])
        in_main = self.packet()
        self.assertEqual(in_main["findings"][0]["source"]["absolute_path"], str(self.repo / "src" / "a.py"))
        self.assertNotIn("tree line 3", in_main["findings"][0]["source"]["text"])
        self.assertNotEqual(in_tree["context_id"], in_main["context_id"])

    def test_the_raw_review_comes_from_the_shared_state_of_the_main_worktree(self) -> None:
        reviews = self.repo / ".house-rules" / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "R1.txt").write_text("the raw review\n", encoding="utf-8")
        raw = self.packet("tree")["findings"][0]["raw_review"]
        self.assertEqual((raw["status"], raw["absolute_path"]), ("available", str(reviews / "R1.txt")))

    def test_an_absolute_path_in_the_ledger_is_read_in_the_worktree_that_contains_it(self) -> None:
        self.append_raw(review_finding(display_id="R1-002", file=str(self.tree / "src" / "a.py"), line=3, rule_id="unmapped"))
        sources = {item["finding"]["display_id"]: item["source"] for item in self.packet("tree")["findings"]}
        self.assertEqual(sources["R1-002"]["status"], "available")
        self.assertIn("tree line 3", sources["R1-002"]["text"])
        self.assertEqual(self.packet()["findings"][1]["source"]["status"], "unavailable")

    def test_the_saved_evidence_of_a_file_named_by_an_absolute_path_is_found_in_the_worktree(self) -> None:
        tree_config = load_config(self.tree, None, None, self.tree)
        save_review(self.repo, "R1", "raw review")
        save(tree_config, self.tree, "R1", capture(tree_config, self.tree, None), "raw review")
        self.append_raw(review_finding(display_id="R1-002", file=str(self.tree / "src" / "a.py"), line=3, rule_id="unmapped"))
        by_id = {item["finding"]["display_id"]: item for item in self.packet("tree")["findings"]}
        self.assertEqual(by_id["R1-002"]["review_source"]["status"], "available")
        self.assertEqual(by_id["R1-002"]["review_source"]["file"], "src/a.py")


class ProposalCase(LinkedCase):
    """A rewrite of P1.1 grounded in a finding; `batch_in` prepares it in one worktree, `apply_in` applies it there."""

    def setUp(self) -> None:
        super().setUp()
        self.finding = review_finding(rule_id="P1.1", rule_code="P1.1", summary="The depth rule flags a clear nesting.")
        self.append_raw(self.finding)
        self.drafts = self.tmp / "drafts.json"
        self.drafts.write_text(json.dumps([{"action": "rewrite", "rule_id": "P1.1", "file": "rules.md", "after": REWRITTEN,
                                            "rationale": "Four levels already hide the failure path.",
                                            "evidence_ids": [self.finding["id"]]}]), encoding="utf-8")

    def run_in(self, where: Path, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        return self.cli(*argv, "--repo", str(where), stdin=stdin, with_repo=False)

    def batch_in(self, where: Path) -> str:
        code, out, err = self.run_in(where, "distill", "--drafts", str(self.drafts))
        self.assertEqual(code, 0, err)
        return out.splitlines()[0].split(": ", 1)[1]

    def decision(self, batch_id: str) -> str:
        return json.dumps({"v": 1, "batch_id": batch_id, "confirmed_by": "Synthetic test operator",
                           "decisions": [{"id": "p1", "decision": "accept"}]})

    def apply_in(self, where: Path, batch_id: str) -> tuple[int, str, str]:
        return self.run_in(where, "apply-proposals", "--input", "-", stdin=self.decision(batch_id))

    def applied_in(self, where: Path) -> str:
        batch_id = self.batch_in(where)
        code, _, err = self.apply_in(where, batch_id)
        self.assertEqual(code, 0, err)
        return batch_id

    def receipt(self, batch_id: str) -> dict:
        return json.loads((self.repo / STATE / batch_id / "receipt.json").read_text(encoding="utf-8"))

    def write_receipt(self, batch_id: str, receipt: dict) -> None:
        (self.repo / STATE / batch_id / "receipt.json").write_text(encoded(receipt), encoding="utf-8")

    def tracked(self, where: Path) -> dict[str, str]:
        return {name: (where / name).read_text(encoding="utf-8") if (where / name).exists() else ""
                for name in ("rules.md", DIGEST, REVIEWER, CHANGELOG)}


class ApplyTest(ProposalCase):
    def test_a_batch_prepared_and_applied_in_a_linked_worktree_changes_that_worktrees_files_only(self) -> None:
        in_main = self.tracked(self.repo)
        batch_id = self.applied_in(self.tree)
        for name in ("rules.md", DIGEST, REVIEWER):
            self.assertIn("four or more", self.tree_text(name), name)
        self.assertIn("Revision 1", self.tree_text(CHANGELOG))
        self.assertEqual(self.tracked(self.repo), in_main)
        self.assertEqual(self.in_tree("compile", "--check")[0], 0)
        self.assertEqual(self.cli("compile", "--check")[0], 0)
        self.assertEqual(sorted(path.name for path in (self.repo / STATE).iterdir()), sorted(["version.json", "write.lock", batch_id]))

    def test_the_state_of_a_batch_applied_in_a_linked_worktree_is_kept_in_the_main_one(self) -> None:
        batch_id = self.applied_in(self.tree)
        self.assertTrue((self.repo / STATE / batch_id / "batch.json").is_file())
        self.assertEqual(json.loads((self.repo / VERSION).read_text(encoding="utf-8"))["batch_id"], batch_id)
        self.assertFalse((self.tree / ".house-rules").exists())

    def test_the_receipt_names_the_worktree_the_batch_was_applied_in(self) -> None:
        self.assertEqual(self.receipt(self.applied_in(self.tree))["worktree"], str(self.tree))

    def test_the_receipt_of_a_batch_applied_in_the_main_worktree_names_it(self) -> None:
        self.assertEqual(self.receipt(self.applied_in(self.repo))["worktree"], str(self.repo))

    def test_a_dry_run_shows_the_worktrees_diff_and_writes_nothing(self) -> None:
        batch_id = self.batch_in(self.tree)
        before = self.tracked(self.tree), self.tracked(self.repo)
        code, out, err = self.run_in(self.tree, "apply-proposals", "--input", "-", "--dry-run", stdin=self.decision(batch_id))
        self.assertEqual(code, 0, err)
        self.assertIn(f"+++ b/{DIGEST}", out)
        self.assertEqual((self.tracked(self.tree), self.tracked(self.repo)), before)
        self.assertFalse((self.repo / STATE / batch_id / "receipt.json").exists())

    def test_the_baseline_is_the_current_worktrees_files_so_a_batch_is_stale_where_they_differ(self) -> None:
        batch_id = self.batch_in(self.repo)
        (self.tree / "rules.md").write_text(STYLE_RULES + "\nMoved on in the linked worktree.\n", encoding="utf-8")
        code, _, err = self.apply_in(self.tree, batch_id)
        self.assertEqual(code, 1)
        self.assertIn("create a fresh proposal batch", err)
        self.assertEqual(self.tracked(self.repo)["rules.md"], STYLE_RULES)
        self.assertFalse((self.repo / STATE / batch_id / "receipt.json").exists())

    def test_a_batch_prepared_in_a_linked_worktree_is_stale_in_the_main_one_when_the_files_differ(self) -> None:
        (self.tree / "rules.md").write_text(STYLE_RULES + "\nMoved on in the linked worktree.\n", encoding="utf-8")
        batch_id = self.batch_in(self.tree)
        code, _, err = self.apply_in(self.repo, batch_id)
        self.assertEqual(code, 1)
        self.assertIn("create a fresh proposal batch", err)
        self.assertEqual(self.main_text("rules.md"), STYLE_RULES)

    def test_a_batch_applies_in_any_worktree_whose_files_match_its_baseline_and_changes_only_that_one(self) -> None:
        batch_id = self.batch_in(self.repo)
        in_main = self.tracked(self.repo)
        code, _, err = self.apply_in(self.tree, batch_id)
        self.assertEqual(code, 0, err)
        self.assertIn("four or more", self.tree_text("rules.md"))
        self.assertEqual(self.tracked(self.repo), in_main)
        self.assertEqual(self.receipt(batch_id)["worktree"], str(self.tree))

    def test_applying_again_in_the_worktree_that_applied_it_is_a_no_op(self) -> None:
        batch_id = self.applied_in(self.tree)
        code, out, err = self.apply_in(self.tree, batch_id)
        self.assertEqual(code, 0, err)
        self.assertIn("already_applied", out)

    def test_applying_a_batch_in_another_worktree_after_it_was_applied_is_refused_and_names_where(self) -> None:
        batch_id = self.applied_in(self.tree)
        in_main = self.tracked(self.repo)
        code, _, err = self.apply_in(self.repo, batch_id)
        self.assertEqual(code, 1)
        self.assertIn(f"applied in {self.tree}", err)
        self.assertIn("commit it there and merge that commit here", err)
        self.assertNotIn("create a new batch", err)  # a second batch would make a second revision of the same change
        self.assertEqual(self.tracked(self.repo), in_main)

    def test_the_change_reaches_the_other_worktree_by_a_commit_and_a_merge(self) -> None:
        self.applied_in(self.tree)
        git(self.tree, "add", "rules.md", DIGEST, REVIEWER, CHANGELOG)
        self.commit_in_tree("rule: P1.1 counts four levels")
        git(self.repo, "merge", "-q", "--ff-only", "feature")
        self.assertEqual(self.tracked(self.repo), self.tracked(self.tree))
        self.assertEqual(self.cli("compile", "--check")[0], 0)

    def test_a_batch_prepared_in_a_linked_worktree_whose_rules_differ_from_the_main_ones_applies_there(self) -> None:
        (self.tree / "rules.md").write_text(STYLE_RULES + "\nMoved on in the linked worktree.\n", encoding="utf-8")
        in_main = self.tracked(self.repo)
        self.applied_in(self.tree)  # the files it checks and writes are the linked worktree's, not the main one's
        self.assertIn("four or more", self.tree_text("rules.md"))
        self.assertIn("Moved on in the linked worktree.", self.tree_text("rules.md"))
        self.assertEqual(self.tracked(self.repo), in_main)


class RollbackTest(ProposalCase):
    def test_rollback_in_the_worktree_that_applied_restores_its_files_and_the_shared_state(self) -> None:
        in_tree, in_main = self.tracked(self.tree), self.tracked(self.repo)
        batch_id = self.applied_in(self.tree)
        code, out, err = self.run_in(self.tree, "rollback-proposals", "--batch", batch_id)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.tracked(self.tree), self.tracked(self.repo)), (in_tree, in_main))
        self.assertFalse((self.repo / VERSION).exists())
        self.assertEqual(self.receipt(batch_id)["status"], "rolled_back")

    def test_rollback_in_another_worktree_is_refused_and_names_the_one_to_use(self) -> None:
        batch_id = self.applied_in(self.tree)
        after, in_main = self.tracked(self.tree), self.tracked(self.repo)
        code, _, err = self.run_in(self.repo, "rollback-proposals", "--batch", batch_id)
        self.assertEqual(code, 1)
        self.assertIn(f"applied in {self.tree}", err)
        self.assertIn(f"rollback-proposals --batch {batch_id} there", err)
        self.assertEqual((self.tracked(self.tree), self.tracked(self.repo)), (after, in_main))
        self.assertEqual(self.receipt(batch_id)["status"], "applied")
        self.assertEqual(self.run_in(self.tree, "rollback-proposals", "--batch", batch_id)[0], 0)

    def test_a_dry_run_rollback_in_another_worktree_is_refused_too(self) -> None:
        batch_id = self.applied_in(self.tree)
        code, _, err = self.run_in(self.repo, "rollback-proposals", "--batch", batch_id, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn(f"applied in {self.tree}", err)

    def test_bytes_that_match_do_not_make_another_worktree_the_one_to_roll_back_in(self) -> None:
        batch_id = self.applied_in(self.repo)  # the main worktree's files now hold the change
        for name in ("rules.md", DIGEST, REVIEWER, CHANGELOG):  # as if the change had been merged into the linked branch
            (self.tree / name).write_text(self.main_text(name), encoding="utf-8")
        merged = self.tracked(self.tree)
        code, _, err = self.run_in(self.tree, "rollback-proposals", "--batch", batch_id)
        self.assertEqual(code, 1)
        self.assertIn(f"applied in {self.repo}", err)
        self.assertEqual(self.tracked(self.tree), merged)
        self.assertEqual(self.receipt(batch_id)["status"], "applied")
        self.assertEqual(self.run_in(self.repo, "rollback-proposals", "--batch", batch_id)[0], 0)

    def test_a_receipt_from_before_worktrees_were_told_apart_belongs_to_the_main_worktree(self) -> None:
        batch_id = self.applied_in(self.repo)
        receipt = self.receipt(batch_id)
        del receipt["worktree"]
        self.write_receipt(batch_id, receipt)
        for name in ("rules.md", DIGEST, REVIEWER, CHANGELOG):
            (self.tree / name).write_text(self.main_text(name), encoding="utf-8")
        code, _, err = self.run_in(self.tree, "rollback-proposals", "--batch", batch_id)
        self.assertEqual(code, 1)
        self.assertIn(f"applied in {self.repo}", err)
        self.assertEqual(self.run_in(self.repo, "rollback-proposals", "--batch", batch_id)[0], 0)
        self.assertEqual(self.main_text("rules.md"), STYLE_RULES)

    def test_a_later_edit_in_the_worktree_still_blocks_its_rollback(self) -> None:
        batch_id = self.applied_in(self.tree)
        (self.tree / "rules.md").write_text(self.tree_text("rules.md") + "\nUser edit\n", encoding="utf-8")
        code, _, err = self.run_in(self.tree, "rollback-proposals", "--batch", batch_id)
        self.assertEqual(code, 1)
        self.assertIn("rollback would overwrite a later edit: rules.md", err)


class RecoverTest(ProposalCase):
    """An interrupted transaction is the worktree's: its journal is shared state, so every worktree is blocked, and only one can undo it."""

    def interrupt_in_tree(self) -> str:
        batch_id = self.batch_in(self.tree)
        original = replace_local

        def failing(root: Path, name: str, text: str | None) -> None:
            if name == DIGEST:
                raise OSError("synthetic disk failure")
            return original(root, name, text)

        with mock.patch("house_rules.proposal_store.replace_local", side_effect=failing):
            code, _, err = self.apply_in(self.tree, batch_id)
        self.assertEqual(code, 1)
        self.assertIn("interrupted", err)
        return batch_id

    def test_the_journal_names_the_worktree_and_is_kept_in_the_main_one(self) -> None:
        self.interrupt_in_tree()
        self.assertEqual(json.loads((self.repo / JOURNAL).read_text(encoding="utf-8"))["tree"], str(self.tree))
        self.assertFalse((self.tree / ".house-rules").exists())

    def test_another_worktree_is_blocked_and_told_where_to_recover(self) -> None:
        batch_id = self.interrupt_in_tree()
        code, _, err = self.apply_in(self.repo, batch_id)
        self.assertEqual(code, 1)
        self.assertIn(f"interrupted proposal transaction in {self.tree}; run house-rules recover-proposals there first", err)
        code, _, err = self.run_in(self.repo, "recover-proposals")
        self.assertEqual(code, 1)
        self.assertIn(f"was writing files in {self.tree}; run house-rules recover-proposals there", err)

    def test_the_worktree_that_was_interrupted_recovers_its_own_files(self) -> None:
        batch_id = self.interrupt_in_tree()
        self.assertIn("four or more", self.tree_text("rules.md"))  # the first file was written before the failure
        code, out, err = self.run_in(self.tree, "recover-proposals")
        self.assertEqual(code, 0, err)
        self.assertIn("restored", out)
        self.assertEqual(self.tree_text("rules.md"), STYLE_RULES)
        self.assertFalse((self.repo / JOURNAL).exists())
        self.assertEqual(self.apply_in(self.tree, batch_id)[0], 0)  # and the batch can be applied again

    def test_an_interrupted_worktree_that_is_asked_again_is_told_to_recover_here(self) -> None:
        batch_id = self.interrupt_in_tree()
        code, _, err = self.apply_in(self.tree, batch_id)
        self.assertEqual(code, 1)
        self.assertIn("interrupted proposal transaction; run house-rules recover-proposals first", err)

    def test_a_later_edit_in_the_worktree_blocks_its_recovery(self) -> None:
        self.interrupt_in_tree()
        (self.tree / "rules.md").write_text(self.tree_text("rules.md") + "\nUser edit\n", encoding="utf-8")
        code, _, err = self.run_in(self.tree, "recover-proposals")
        self.assertEqual(code, 1)
        self.assertIn("recovery would overwrite a later edit: rules.md", err)

    def test_every_command_that_waits_for_an_idle_journal_says_where_to_recover(self) -> None:
        batch_id = self.interrupt_in_tree()
        commands = {"distill": ["distill", "--drafts", str(self.drafts)], "apply-proposals": ["apply-proposals", "--input", "-"],
                    "rollback-proposals": ["rollback-proposals", "--batch", batch_id]}
        here = "interrupted proposal transaction; run house-rules recover-proposals first"
        there = f"interrupted proposal transaction in {self.tree}; run house-rules recover-proposals there first"
        for command, argv in commands.items():
            for where, expected in ((self.tree, here), (self.repo, there)):
                with self.subTest(command=command, where=where.name):
                    code, _, err = self.run_in(where, *argv, stdin=self.decision(batch_id))
                    self.assertEqual(code, 1)
                    self.assertIn(expected, err)

    def test_a_journal_from_before_worktrees_were_told_apart_is_recovered_in_the_main_worktree(self) -> None:
        replace_local(self.repo, JOURNAL, encoded({"v": 1, "changes": {"rules.md": [STYLE_RULES, "partial"]}}))
        (self.repo / "rules.md").write_text("partial", encoding="utf-8")
        code, _, err = self.run_in(self.tree, "recover-proposals")
        self.assertEqual(code, 1)
        self.assertIn(f"was writing files in {self.repo}", err)
        self.assertEqual(self.run_in(self.repo, "recover-proposals")[0], 0)
        self.assertEqual(self.main_text("rules.md"), STYLE_RULES)


class NativeDecisionTest(ProposalCase):
    def setUp(self) -> None:
        super().setUp()
        self.reported = review_finding(display_id="R1-002", file="src/a.py", line=2, rule_id="unmapped")
        self.append_raw(self.reported)
        self.tree_config = load_config(self.tree, None, None, self.tree)
        self.main_config = load_config(self.repo, None, None, self.repo)

    def batch(self) -> dict:
        context = build_context(self.tree_config, self.events(), [self.reported["id"]])
        change = {"action": "add", "rule_id": "P3.9", "file": "rules.md", "after": "- [P3.9] Name the invariant a comment protects. (nice)"}
        assessment = {"finding_ids": [self.reported["id"]], "verification": {"status": "supported", "basis": "Synthetic inspected example."},
                      "action": "add", "rule_ids": [], "reason": "A distinct failure needs a rule.", "changes": [change]}
        return prepare(self.tree_config, self.events(), {"v": 1, "context_id": context["context_id"], "assessments": [assessment]},
                       [self.reported["id"]])

    def answers(self, batch: dict) -> dict:
        return {"v": 1, "batch_id": batch["id"], "confirmed_by": "Synthetic test human",
                "answers": [{"id": "a1", "decision": "accept", "response": "Synthetic explicit accept"}]}

    def test_a_batch_prepared_from_one_worktrees_code_is_not_applied_in_another_even_when_the_files_match(self) -> None:
        batch = self.batch()
        with self.assertRaisesRegex(HouseRulesError, "decision evidence or source code changed"):
            apply_answers(self.main_config, self.events(), self.answers(batch))
        self.assertEqual(self.main_text("rules.md"), STYLE_RULES)
        self.assertFalse((self.repo / STATE / batch["id"] / "receipt.json").exists())

    def test_it_applies_in_the_worktree_it_was_prepared_in(self) -> None:
        batch = self.batch()
        result = apply_answers(self.tree_config, self.events(), self.answers(batch))
        self.assertEqual(result["status"], "applied")
        self.assertIn("[P3.9]", self.tree_text("rules.md"))
        self.assertNotIn("[P3.9]", self.main_text("rules.md"))
        self.assertEqual(self.receipt(batch["id"])["worktree"], str(self.tree))


class InitTest(LinkedCase):
    def test_init_sets_up_the_linked_worktree_and_says_which_it_is(self) -> None:
        (self.tree / DIGEST).unlink()
        out = self.ok_in_tree("init", "--yes")
        self.assertEqual(out.splitlines()[0], f"house-rules init: {self.tree}")
        self.assertIn(f"stay shared with {self.repo}", out.splitlines()[1])
        self.assertIn(f"@{DIGEST}", self.tree_text("CLAUDE.md"))
        self.assertTrue((self.tree / DIGEST).is_file())
        self.assertIn(".house-rules/", self.tree_text(".gitignore"))
        for name in ("CLAUDE.md", ".gitignore"):
            self.assertFalse((self.repo / name).exists(), name)  # the main worktree's tracked files are not touched
        self.assertEqual(self.main_text(".house-rules.json"), json.dumps({"rules_files": ["rules.md"], "inject_targets": [REVIEWER]}))

    def test_the_commit_it_asks_for_is_made_of_the_linked_worktrees_files(self) -> None:
        (self.tree / DIGEST).unlink()
        out = self.ok_in_tree("init", "--yes")
        (staged,) = [line for line in out.splitlines() if line.strip().startswith("git add ")]
        self.assertEqual(staged.split(" && ")[0].strip(), "git add CLAUDE.md .gitignore")  # the digest it rewrote is byte-identical to the committed one

    def test_a_dry_run_in_the_linked_worktree_names_it_and_writes_nothing(self) -> None:
        (self.tree / DIGEST).unlink()
        out = self.ok_in_tree("init", "--dry-run")
        self.assertEqual(out.splitlines()[0], f"[dry-run] house-rules init: {self.tree}")
        self.assertIn("+++ b/CLAUDE.md", out)  # the diff labels are the worktree's paths
        self.assertNotIn("+++ b/../", out)
        self.assertFalse((self.tree / DIGEST).exists())
        self.assertFalse((self.tree / "CLAUDE.md").exists())

    def test_init_compiles_the_linked_worktrees_own_rules_and_reads_the_config_names(self) -> None:
        (self.tree / "rules.md").write_text(EDITED_RULES, encoding="utf-8")
        (self.tree / DIGEST).unlink()
        out = self.ok_in_tree("init", "--yes")
        self.assertIn("already lists rules_files ['rules.md']", out)
        self.assertIn("Fail fast at every boundary", self.tree_text(DIGEST))
        self.assertNotIn("every boundary", self.main_text(DIGEST))

    def test_the_main_worktree_heading_is_the_main_worktree_and_has_no_second_line(self) -> None:
        out = self.cli_ok("init", "--yes")
        self.assertEqual(out.splitlines()[0], f"house-rules init: {self.repo}")
        self.assertNotIn("linked worktree", out)


class HooksPathTest(LinkedCase):
    def test_a_hooks_directory_inside_the_repo_is_the_linked_worktrees_own(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")  # repo-wide, resolved against each worktree's top
        out = self.ok_in_tree("init", "--yes")
        self.assertTrue((self.tree / ".githooks" / "pre-commit").is_file())
        self.assertFalse((self.repo / ".githooks" / "pre-commit").exists())
        self.assertIn("portable: safe to commit", out)


class InitWithoutConfigTest(RepoCase):
    def test_init_will_not_create_the_shared_config_from_a_linked_worktree(self) -> None:
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        self.commit("base")
        tree = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(tree))
        for flag in ("--yes", "--dry-run"):
            with self.subTest(flag=flag):
                code, _, err = self.cli("init", flag, "--repo", str(tree), with_repo=False)
                self.assertEqual(code, 1)
                self.assertIn(f"{self.repo / '.house-rules.json'} lists no rules_files", err)
                self.assertIn("run `house-rules init` in the main worktree first", err)
                self.assertFalse((self.repo / ".house-rules.json").exists())
                self.assertFalse((tree / ".house-rules.json").exists())
                self.assertFalse((tree / "CODING-STYLE.md").exists())


class SharedStateTest(LinkedCase):
    def test_the_label_page_is_written_next_to_the_shared_ledger(self) -> None:
        self.append_raw(review_finding())
        out = self.ok_in_tree("label-page")
        self.assertIn(str(self.repo / ".house-rules-label-page.html"), out)
        self.assertTrue((self.repo / ".house-rules-label-page.html").is_file())
        self.assertTrue((self.repo / ".house-rules-label-batch.json").is_file())
        self.assertFalse((self.tree / ".house-rules-label-page.html").exists())

    def test_the_proposal_batch_page_is_written_into_the_shared_state(self) -> None:
        finding = review_finding(rule_id="P1.1", rule_code="P1.1")
        self.append_raw(finding)
        drafts = self.tmp / "drafts.json"
        drafts.write_text(json.dumps([{"action": "rewrite", "rule_id": "P1.1", "file": "rules.md", "after": REWRITTEN,
                                       "rationale": "Why.", "evidence_ids": [finding["id"]]}]), encoding="utf-8")
        out = self.ok_in_tree("distill", "--drafts", str(drafts))
        batch_id = out.splitlines()[0].split(": ", 1)[1]
        self.assertIn(f"review page: {self.repo / STATE / batch_id / 'review.html'}", out)
        self.assertFalse((self.tree / ".house-rules").exists())


class SkipsTest(LinkedCase):
    """The skips file is shared state like the ledger whose finding ids it lists: every worktree reads the main worktree's copy."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(rules_files=["rules.md"], inject_targets=[REVIEWER], skips_file="skips.jsonl")
        self.ok_in_tree("gate", "record", stdin=json.dumps([{"file": "src/a.py", "line": 3, "code": "P0E.1", "message": "gate says no"}]))
        (self.finding,) = [event for event in self.events() if event["kind"] == "finding"]

    def closed(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "outcome"]

    def test_a_finding_the_main_worktrees_skips_file_names_is_not_closed_by_a_pass_in_a_linked_one(self) -> None:
        (self.repo / "skips.jsonl").write_text(json.dumps({"finding_ids": [self.finding["id"]]}) + "\n", encoding="utf-8")
        self.ok_in_tree("gate", "pass", "--files", "src/a.py")
        self.ok_in_tree("infer")
        self.assertEqual(self.closed(), [])

    def test_a_copy_of_the_skips_file_in_a_linked_worktree_is_not_read(self) -> None:
        (self.tree / "skips.jsonl").write_text(json.dumps({"finding_ids": [self.finding["id"]]}) + "\n", encoding="utf-8")
        self.ok_in_tree("gate", "pass", "--files", "src/a.py")
        self.ok_in_tree("infer")
        (outcome,) = self.closed()
        self.assertEqual((outcome["id"], outcome["outcome_by"]), (self.finding["id"], "auto:gate_pass"))


if __name__ == "__main__":
    unittest.main()
