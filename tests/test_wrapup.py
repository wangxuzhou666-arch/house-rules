"""`ratchet wrapup`: the base, what counts as a change, the thresholds, the review it runs and what it reports."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import STYLE_RULES, RepoCase, git, review_finding, review_item, review_round

from ratchet.git import run_git
from ratchet.review import round_key
from ratchet.review_evidence import snippet_around
from ratchet.state_tree import MAX_UNTRACKED_BYTES, OVERSIZE_STAND_IN, numstat_since, state_tree, tree_exists
from ratchet.wrapup import code_window, round_key_branch, run_blind_spots


def numbered(count: int, prefix: str = "line") -> str:
    return "".join(f"{prefix} {number}\n" for number in range(1, count + 1))


def review_text(*items: dict) -> str:
    """What the built-in code review prints: prose, then the findings in a json fence."""
    return f"Reviewed the diff.\n\n```json\n{json.dumps(list(items), indent=2)}\n```\n"


def blind_spot(summary: str, **overrides: object) -> dict:
    """A review item no clause covers."""
    item = review_item(category="unmapped", severity="should-fix", summary=summary)
    item.update(overrides)
    return item


class WrapupCase(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls_path = self.tmp / "claude-calls.jsonl"
        self.last_err = ""
        patcher = mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": "/nonexistent/claude"})  # no test reaches the real claude
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, seeds: dict[str, str | bytes] | None = None, *, rules_files: tuple[str, ...] = ("rules.md",), **config: object) -> str:
        """Commit the rules, the config and a 20-line app.py (plus `seeds`) on main; the sha of that commit."""
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=list(rules_files), **config)
        self.put("app.py", numbered(20, "L"))
        for name, content in (seeds or {}).items():
            self.put(name, content)
        return self.commit_all("base")

    def put(self, name: str, content: str | bytes, root: Path | None = None) -> Path:
        path = (root or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit_all(self, subject: str, root: Path | None = None) -> str:
        top = root or self.repo
        git(top, "add", "-A")
        git(top, "-c", "commit.gpgsign=false", "commit", "-q", "-m", subject)
        return git(top, "rev-parse", "HEAD")

    def commit_file(self, name: str, line_count: int, root: Path | None = None) -> str:
        self.put(name, numbered(line_count), root)
        return self.commit_all(f"add {name}", root)

    def more_work(self, name: str = "more.py", line_count: int = 250) -> None:
        """New work big enough for the next wrap-up to review: what an earlier review read is no longer counted."""
        self.put(name, numbered(line_count))

    def adopt_a_rule(self, root: Path | None = None, *, rules: str = "rules.md", digest: str = "CODING-STYLE.digest.md") -> None:
        """What adopting a rule writes: its text in the rules file, the digest compiled from it, an entry in the changelog.
        12 lines each, so three files and 36 lines count as work if nothing leaves them out."""
        for name in (rules, digest, "CODING-STYLE.changelog.md"):
            path = (root or self.repo) / name
            self.put(name, (path.read_text(encoding="utf-8") if path.exists() else "") + numbered(12, "adopted"), root)

    def wrap(self, *argv: str, expect: int = 0) -> dict:
        """Run `wrapup --json`: the object it printed. Its stdout must be that one object and nothing else."""
        code, out, err = self.cli("wrapup", "--json", *argv)
        self.last_err = err
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def measure(self, *argv: str) -> dict:
        return self.wrap("--dry-run", *argv)

    def write_review(self, *items: dict, name: str = "review.txt") -> Path:
        path = self.tmp / name
        path.write_text(review_text(*items), encoding="utf-8")
        return path

    def fake_claude(self, stdout: str) -> Path:
        """An executable that records each review call and prints canned output; `--version` is answered apart."""
        script = self.tmp / "fake-claude"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    sys.stdout.write('2.1.0 (Claude Code)')\n"
            "    sys.exit(0)\n"
            f"open({str(self.calls_path)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"sys.stdout.write({stdout!r})\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def with_claude(self, script: Path, *argv: str, expect: int = 0) -> dict:
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(script)}):
            return self.wrap(*argv, expect=expect)

    def claude_calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]

    def review_runs(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "review_run"]

    def findings(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "finding"]

    def round_on(self, branch: str, run: str, **overrides: object) -> dict:
        """A recorded review of `branch`. Only a review of the branch a wrap-up runs on can be its base."""
        return review_round(key=f"{branch}@0a1b2c3d4e5f", run=run, **overrides)

    def objects(self) -> dict[str, int]:
        """Every file in the repo's object store with its size: what a command that writes an object changes."""
        root = self.repo / ".git" / "objects"
        return {str(path.relative_to(root)): path.stat().st_size for path in sorted(root.rglob("*")) if path.is_file()}


class BaseRuleTest(WrapupCase):
    """The first rule that holds picks the base: --base, the last review of this branch, the fork point, HEAD."""

    def feature(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "feature")

    def test_an_explicit_base_wins_over_the_ledger_and_the_fork_point(self) -> None:
        base = self.start()
        self.feature()
        reviewed = self.commit_file("one.txt", 3)
        self.append_raw(self.round_on("feature", "r1", head=reviewed))
        self.assertEqual(self.measure()["base_reason"], "last-review")  # without the flag the ledger decides
        data = self.measure("--base", base)
        self.assertEqual((data["base"], data["base_reason"]), (base, "--base"))
        self.assertEqual(self.measure("--base", "main")["base"], base)  # any ref, not only a sha

    def test_a_base_that_is_not_a_commit_is_refused(self) -> None:
        self.start()
        code, _, err = self.cli("wrapup", "--base", "no-such-ref")
        self.assertEqual(code, 1)
        self.assertIn("--base 'no-such-ref' is not a commit", err)

    def test_the_newest_review_whose_head_is_an_ancestor_is_the_base(self) -> None:
        self.start()
        self.feature()
        first, second = self.commit_file("one.txt", 3), self.commit_file("two.txt", 3)
        self.append_raw(self.round_on("feature", "r1", head=first), self.round_on("feature", "r2", head=second))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (second, "last-review"))
        _, out, _ = self.cli("wrapup", "--dry-run")
        self.assertIn(f"since {second[:12]} (HEAD when review r2 started)", out)

    def test_reviews_without_a_usable_head_are_passed_over(self) -> None:
        self.start()
        self.feature()
        first = self.commit_file("one.txt", 3)
        self.commit_file("two.txt", 3)
        self.append_raw(self.round_on("feature", "r1", head=first), self.round_on("feature", "r2", head="none"),
                        self.round_on("feature", "r3"))  # r2 ran on a branch with no commit, r3 is from before the field existed
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (first, "last-review"))

    def test_a_review_on_another_branch_falls_back_to_the_fork_point(self) -> None:
        base = self.start()
        git(self.repo, "checkout", "-q", "-b", "other")
        elsewhere = self.commit_file("other.txt", 2)
        git(self.repo, "checkout", "-q", "main")
        self.feature()
        self.commit_file("one.txt", 2)
        self.append_raw(self.round_on("other", "r1", head=elsewhere))  # a commit this branch does not descend from
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (base, "merge-base"))

    def test_a_review_that_is_not_an_ancestor_is_passed_over_for_an_older_one_that_is(self) -> None:
        self.start()
        git(self.repo, "checkout", "-q", "-b", "other")
        elsewhere = self.commit_file("other.txt", 2)
        git(self.repo, "checkout", "-q", "main")
        self.feature()
        reviewed = self.commit_file("one.txt", 2)
        self.append_raw(self.round_on("feature", "r1", head=reviewed), self.round_on("feature", "r2", head=elsewhere))  # r2: history was rewritten
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (reviewed, "last-review"))

    def test_a_head_of_none_is_never_looked_up_as_a_ref(self) -> None:
        base = self.start()
        self.feature()
        git(self.repo, "tag", "none", self.commit_file("one.txt", 2))  # a ref that happens to be called none
        self.append_raw(self.round_on("feature", "r1", head="none"))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (base, "merge-base"))

    def test_a_head_this_clone_does_not_have_is_passed_over_and_does_not_fail(self) -> None:
        base = self.start()
        self.feature()
        self.commit_file("one.txt", 2)
        self.append_raw(self.round_on("feature", "r1", head="a" * 40))  # pruned after a rebase, or from another clone
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (base, "merge-base"))

    def test_only_a_review_of_the_branch_head_is_on_can_be_the_last_review(self) -> None:
        base = self.start()
        self.feature()
        first, second = self.commit_file("one.txt", 3), self.commit_file("two.txt", 3)
        self.append_raw(self.round_on("main", "r1", head=second))  # its head is an ancestor of this HEAD, but it reviewed main
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (base, "merge-base"))
        self.append_raw(self.round_on("feature", "r2", head=first), self.round_on("main", "r3", head=second))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (first, "last-review"))  # the newest review of feature, not the newest review

    def test_a_review_of_a_target_names_no_branch_and_is_never_the_last_review(self) -> None:
        base = self.start()
        self.feature()
        first, second = self.commit_file("one.txt", 3), self.commit_file("two.txt", 3)
        self.append_raw(review_round(key="target:feature", run="r1", head=second))
        self.assertEqual(self.measure()["base"], base)
        self.append_raw(self.round_on("feature", "r2", head=first), review_round(key="target:feature", run="r3", head=second))
        self.assertEqual(self.measure()["base"], first)  # and it does not hide an older review of the branch

    def test_on_a_detached_head_no_review_is_the_last_one_and_the_fork_point_decides(self) -> None:
        base = self.start()
        self.feature()
        reviewed = self.commit_file("one.txt", 3)
        git(self.repo, "checkout", "-q", "--detach")
        self.append_raw(self.round_on("feature", "r1", head=reviewed), review_round(key="target:feature", run="r2", head=reviewed))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"]), (base, "merge-base", 3))

    def test_a_branch_name_with_an_at_sign_is_not_taken_for_a_shorter_one(self) -> None:
        self.start()
        git(self.repo, "checkout", "-q", "-b", "feat")
        reviewed = self.commit_file("one.txt", 3)
        self.append_raw(self.round_on("feat@2", "r1", head=reviewed))  # the key feat@2@0a1b2c3d4e5f: a review of the branch feat@2
        self.assertEqual(self.measure()["base_reason"], "merge-base")
        git(self.repo, "checkout", "-q", "-b", "feat@2")
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"]), (reviewed, "last-review"))

    def test_the_fork_point_counts_the_branch_work_committed_and_not(self) -> None:
        base = self.start()
        self.feature()
        self.commit_file("one.txt", 10)
        self.commit_file("two.txt", 20)
        self.put("three.txt", numbered(5))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"], data["changed_files"]), (base, "merge-base", 35, 3))
        _, out, _ = self.cli("wrapup", "--dry-run")
        self.assertIn(f"since {base[:12]} (where this branch forked from main)", out)

    def test_on_the_default_branch_only_uncommitted_work_counts(self) -> None:
        self.start()
        head = self.commit_file("one.txt", 10)
        self.put("two.txt", numbered(4))
        data = self.measure()
        self.assertEqual((data["base"], data["changed_lines"], data["changed_files"]), (head, 4, 1))

    def test_the_configured_default_branch_decides_the_fork_point(self) -> None:
        self.start(default_branch="develop")
        git(self.repo, "branch", "develop")
        git(self.repo, "checkout", "-q", "-b", "feature", "develop")
        self.commit_file("one.txt", 7)
        data = self.measure()
        self.assertEqual((data["base_reason"], data["changed_lines"]), ("merge-base", 7))
        self.assertIn("forked from develop", self.cli("wrapup", "--dry-run")[1])

    def test_with_nothing_to_measure_from_it_counts_the_uncommitted_work_and_says_how_to_widen_it(self) -> None:
        self.start()
        git(self.repo, "branch", "-m", "main", "trunk")  # no origin/HEAD, main or master: no fork point to find
        head = self.commit_file("one.txt", 10)
        self.put("two.txt", numbered(3))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"], data["changed_files"]), (head, "head", 3, 1))
        _, out, _ = self.cli("wrapup", "--dry-run")
        self.assertIn("only uncommitted changes count; pass --base REF to count committed work too", out)

    def test_a_branch_with_no_commit_counts_every_file(self) -> None:
        self.put("a.txt", numbered(4))
        git(self.repo, "add", "a.txt")
        self.put("b.txt", numbered(3))
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"], data["changed_files"]), (None, "head", 7, 2))
        self.assertIn("since the start (no commit yet", self.cli("wrapup", "--dry-run")[1])


class ChangeCountTest(WrapupCase):
    """Lines added plus deleted, and distinct paths, from the base to the working tree."""

    def test_committed_staged_unstaged_and_untracked_work_all_count(self) -> None:
        base = self.start({"u.txt": numbered(10, "U")})
        self.commit_file("c1.txt", 10)                               # committed: +10
        lines = (self.repo / "app.py").read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if line != "L 3"]           # unstaged edit of a tracked file: -1
        lines[9:9] = ["new a", "new b", "new c"]                    # +3
        self.put("app.py", "\n".join(lines) + "\n")
        self.put("s.txt", numbered(7))                              # staged new file: +7
        git(self.repo, "add", "s.txt")
        self.put("u.txt", "changed 1\nchanged 2\n" + numbered(10, "U").split("\n", 2)[2])  # two lines replaced: +2 -2
        self.put("n.txt", "a\nb\nc\nd\ne")                          # untracked, no newline after the last line: 5
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (10 + 4 + 7 + 4 + 5, 5))

    def test_a_deleted_file_counts_its_lines(self) -> None:
        base = self.start({"gone.txt": numbered(12)})
        (self.repo / "gone.txt").unlink()
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (12, 1))

    def test_a_pure_rename_is_one_file_and_no_lines_and_an_edit_counts_only_the_edit(self) -> None:
        base = self.start({"old.txt": numbered(10)})
        git(self.repo, "mv", "old.txt", "new.txt")
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (0, 1))
        self.put("new.txt", numbered(10).replace("line 1\n", "edited\n", 1))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (2, 1))

    def test_a_binary_file_is_one_file_and_no_lines_tracked_or_not(self) -> None:
        base = self.start({"img.bin": b"\x00\x01\x02" * 100})
        self.put("img.bin", b"\x00\x09\x08" * 100)
        self.put("new.bin", b"abc\x00" + b"x\n" * 500)
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (0, 2))

    def test_what_git_ignores_does_not_count(self) -> None:
        base = self.start({".gitignore": "*.log\nbuild/\n"})
        self.put("debug.log", numbered(300))
        self.put("build/out.txt", numbered(300))
        self.put("kept.txt", numbered(2))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (2, 1))

    def test_the_files_ratchet_itself_writes_do_not_count_even_when_nothing_ignores_them(self) -> None:
        base = self.start()
        self.cli_ok("review", "--from-file", str(self.write_review(blind_spot("No rule covers this"))))  # ledger and .ratchet/
        self.assertTrue((self.repo / ".ratchet" / "reviews").is_dir())
        self.put("mine.txt", numbered(3))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (3, 1))

    def test_adopting_a_rule_is_no_work_the_rules_file_the_digest_and_the_changelog_are_left_out(self) -> None:
        base = self.start({"CODING-STYLE.digest.md": numbered(3, "d"), "CODING-STYLE.changelog.md": numbered(3, "c")})
        self.adopt_a_rule()  # three tracked files, 12 added lines each
        self.put("mine.txt", numbered(3))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (3, 1))

    def test_the_same_when_those_files_are_new_and_untracked_and_named_by_the_config(self) -> None:
        base = self.start({"project/rules.md": "1. **[PRJ.1]** A project rule.\n"}, rules_files=("rules.md", "project/rules.md"),
                          digest="docs/style.digest.md")
        self.adopt_a_rule(rules="project/rules.md", digest="docs/style.digest.md")  # the changelog and the digest are untracked
        self.put("CODING-STYLE.digest.md", numbered(2))  # the default digest name is nobody's digest here: work
        self.put("mine.txt", numbered(3))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (3 + 2, 2))

    def test_only_those_files_at_those_places_are_left_out(self) -> None:
        base = self.start()
        for name, count in (("docs/CODING-STYLE.changelog.md", 4), ("sub/rules.md", 5), ("rules.md.bak", 2), ("CODING-STYLE.digest.md.bak", 1)):
            self.put(name, numbered(count))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (4 + 5 + 2 + 1, 4))

    def test_a_rules_file_outside_the_repo_is_no_trouble_and_leaves_out_nothing_in_it(self) -> None:
        outside = self.tmp / "shared-rules.md"
        outside.write_text(STYLE_RULES, encoding="utf-8")
        base = self.start(rules_files=(str(outside),))
        self.put("rules.md", STYLE_RULES + numbered(2))  # not a configured rules file now
        self.put("mine.txt", numbered(3))
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (3 + 2, 2))

    def test_an_empty_file_a_symlink_and_a_last_line_without_a_newline(self) -> None:
        base = self.start()
        self.put("empty.txt", "")
        os.symlink("app.py", self.repo / "link")
        self.put("one.txt", "just one line")
        data = self.measure("--base", base)
        self.assertEqual((data["changed_lines"], data["changed_files"]), (1, 3))

    def test_nothing_changed_is_nothing(self) -> None:
        self.start()
        data = self.wrap()
        self.assertEqual((data["status"], data["changed_lines"], data["changed_files"]), ("skipped", 0, 0))


class ThresholdTest(WrapupCase):
    """Either number reached is enough; both missed means no review and no cost."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start()

    def test_below_both_thresholds_nothing_is_reviewed_and_the_reason_is_in_numbers(self) -> None:
        self.put("a.txt", numbered(12))
        self.put("b.txt", "x\n")
        claude = self.fake_claude(review_text())
        before = self.ledger_bytes()
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(claude)}):
            code, out, err = self.cli("wrapup")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("wrapup: skipped, changed lines 13 < wrapup_min_changed_lines 200 and changed files 2 < wrapup_min_files 5; no review run", out)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".ratchet").exists())

    def test_the_lines_threshold_alone_triggers_at_exactly_the_number(self) -> None:
        self.put("big.txt", numbered(199))
        self.assertEqual(self.wrap()["status"], "skipped")
        self.put("big.txt", numbered(200))
        data = self.measure()
        self.assertEqual((data["status"], data["reason"]), ("dry-run", "changed lines 200 >= wrapup_min_changed_lines 200"))

    def test_the_files_threshold_alone_triggers_at_exactly_the_number(self) -> None:
        for number in range(4):
            self.put(f"f{number}.txt", "x\n")
        self.assertEqual(self.wrap()["status"], "skipped")
        self.put("f4.txt", "x\n")
        data = self.measure()
        self.assertEqual((data["status"], data["reason"]), ("dry-run", "changed files 5 >= wrapup_min_files 5"))

    def test_both_reached_says_both(self) -> None:
        for number in range(6):
            self.put(f"f{number}.txt", numbered(40))
        data = self.measure()
        self.assertEqual(data["reason"], "changed lines 240 >= wrapup_min_changed_lines 200; changed files 6 >= wrapup_min_files 5")

    def test_the_config_sets_both_thresholds(self) -> None:
        self.write_config(rules_files=["rules.md"], wrapup_min_changed_lines=10, wrapup_min_files=2)
        git(self.repo, "add", "-A")
        self.commit("config")
        base = git(self.repo, "rev-parse", "HEAD")
        self.put("a.txt", numbered(9))
        data = self.measure("--base", base)
        self.assertEqual((data["status"], data["thresholds"]), ("skipped", {"min_changed_lines": 10, "min_files": 2}))
        self.put("b.txt", "x\n")
        self.assertEqual(self.measure("--base", base)["status"], "dry-run")  # 10 lines in 2 files: both reached
        (self.repo / "b.txt").unlink()
        self.put("a.txt", numbered(10))
        self.assertEqual(self.measure("--base", base)["status"], "dry-run")  # the lines alone
        self.put("a.txt", numbered(1))
        self.put("c.txt", "x\n")
        self.assertEqual(self.measure("--base", base)["status"], "dry-run")  # the files alone


class ReviewRunTest(WrapupCase):
    """Above the threshold the review is `ratchet review` itself, and what it found is read back from the ledger."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start()
        self.put("big.py", numbered(250))

    def test_a_saved_review_is_recorded_as_ratchet_review_records_it(self) -> None:
        saved = self.write_review(blind_spot("Retry has no limit", file="big.py", line=4),
                                  review_item(category="p1-1", file="big.py", line=9, summary="Deep nesting"))
        data = self.wrap("--from-file", str(saved))
        self.assertEqual((data["status"], data["run"]), ("reviewed", "r1"))
        self.assertEqual(len(self.findings()), 2)
        (run,) = self.review_runs()
        self.assertEqual((run["run"], run["source"], run["head"]), ("r1", "file", self.base))
        self.assertEqual([(item["display_id"], item["file"], item["line"]) for item in data["new_unmapped"]], [("r1-1", "big.py", 4)])
        self.assertEqual(data["open_unmapped"], 1)
        self.assertIn("review round 1/3 for main@", self.last_err)

    def test_the_text_report_lists_the_new_blind_spots_and_the_next_step(self) -> None:
        saved = self.write_review(blind_spot("Retry has no limit", file="big.py", line=4))
        code, out, err = self.cli("wrapup", "--from-file", str(saved))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("wrapup: changed lines 250 >= wrapup_min_changed_lines 200; running a review", out)
        self.assertIn("review round 1/3 for main@", out)
        self.assertIn("wrapup: 1 new unmapped finding(s) from run r1, which no rule covers:\n"
                      "  r1-1  big.py:4  should-fix  Retry has no limit\n", out)
        self.assertIn("/review-ratchet:maintain drafts rule proposals from them (at most 3 per wrap-up)", out)
        self.assertTrue(out.rstrip().endswith("wrapup: open unmapped findings: 1"))

    def test_a_review_that_found_nothing_new_says_so(self) -> None:
        data = self.wrap("--from-file", str(self.write_review(review_item(category="p1-1", summary="Deep nesting"))))
        self.assertEqual((data["status"], data["new_unmapped"], data["open_unmapped"]), ("reviewed", [], 0))
        self.more_work()
        out = self.cli("wrapup", "--from-file", str(self.write_review(review_item(category="p1-1", line=77, summary="Deep"))))[1]
        self.assertIn("wrapup: no new unmapped findings from run r2", out)

    def test_new_means_this_run_and_open_counts_every_run(self) -> None:
        first = self.wrap("--from-file", str(self.write_review(blind_spot("Old blind spot", line=1))))
        self.more_work()
        second = self.wrap("--from-file", str(self.write_review(blind_spot("New blind spot", line=2, file="other.py"), name="second.txt")))
        self.assertEqual([item["summary"] for item in first["new_unmapped"]], ["Old blind spot"])
        self.assertEqual((second["run"], [item["summary"] for item in second["new_unmapped"]]), ("r2", ["New blind spot"]))
        self.assertEqual(second["open_unmapped"], 2)

    def test_a_blind_spot_that_got_an_outcome_is_no_longer_open(self) -> None:
        first = self.wrap("--from-file", str(self.write_review(blind_spot("Old blind spot", line=1), blind_spot("Other", line=2))))
        closed = first["new_unmapped"][0]["id"]
        self.append_raw({"v": 1, "kind": "outcome", "id": closed, "outcome": "false_positive", "outcome_by": "human",
                         "evidence": "wrap-up popup", "ts": "2026-01-07T12:00:00"})
        self.more_work()
        second = self.wrap("--from-file", str(self.write_review(blind_spot("Fresh", line=3), name="second.txt")))
        self.assertEqual(second["open_unmapped"], 2)  # Other and Fresh; the one with an outcome is not counted

    def test_new_blind_spots_come_most_severe_first_and_carry_what_a_question_needs(self) -> None:
        saved = self.write_review(blind_spot("A nice one", line=1, severity="nice"), blind_spot("A blocker", line=2, severity="blocker"),
                                  blind_spot("A should-fix", line=3))
        data = self.wrap("--from-file", str(saved))
        self.assertEqual([item["severity"] for item in data["new_unmapped"]], ["blocker", "should-fix", "nice"])
        item = data["new_unmapped"][0]
        self.assertEqual(set(item), {"id", "display_id", "file", "line", "severity", "summary", "failure_scenario"})
        self.assertEqual(item["id"], "demo-repo:l1:r1:r1-2")

    def test_the_real_path_runs_claude_once_at_the_given_level(self) -> None:
        claude = self.fake_claude(review_text(blind_spot("Retry has no limit", file="big.py", line=4)))
        data = self.with_claude(claude, "--level", "max")
        self.assertEqual((data["status"], len(data["new_unmapped"])), ("reviewed", 1))
        self.assertEqual(self.claude_calls(), [["-p", "/code-review max"]])
        (run,) = self.review_runs()
        self.assertEqual((run["source"], run["level"], run["reviewer"]), ("claude", "max", "claude-code"))

    def test_the_default_level_is_the_one_ratchet_review_uses(self) -> None:
        self.with_claude(self.fake_claude(review_text()))
        self.assertEqual(self.claude_calls(), [["-p", "/code-review high"]])

    def test_the_next_wrapup_is_measured_from_the_working_tree_the_review_found(self) -> None:
        self.wrap("--from-file", str(self.write_review()))  # big.py is uncommitted while the review reads it
        self.commit_file("more.txt", 30)                    # then it is committed with 30 new lines
        data = self.measure()
        self.assertEqual(data["base_reason"], "last-review-tree")
        self.assertEqual((data["changed_lines"], data["changed_files"]), (30, 1))      # only what came after the review
        self.assertEqual(data["base"], self.review_runs()[0]["tree"])
        self.assertEqual(self.measure("--base", self.base)["changed_lines"], 250 + 30)  # a commit as the base cannot tell

    def test_level_means_nothing_for_a_saved_review(self) -> None:
        code, _, err = self.cli("wrapup", "--from-file", str(self.write_review()), "--level", "max")
        self.assertEqual(code, 1)
        self.assertIn("--level chooses what to review; it does nothing with --from-file", err)
        self.assertEqual(self.review_runs(), [])


class RoundCapTest(WrapupCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.start(max_review_rounds=1)
        self.put("big.py", numbered(250))
        self.claude = self.fake_claude(review_text(blind_spot("Retry has no limit", file="big.py", line=4)))

    def test_at_the_cap_the_review_is_refused_with_exit_3_and_claude_is_not_run(self) -> None:
        self.assertEqual(self.with_claude(self.claude)["status"], "reviewed")
        self.more_work()
        before = self.ledger_bytes()
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(self.claude)}):
            code, out, err = self.cli("wrapup")
        self.assertEqual(code, 3)
        self.assertIn("round cap reached for main@", err)
        self.assertIn("1/1 rounds used", err)
        self.assertIn("ratchet wrapup has no --over-cap: `ratchet review --over-cap` runs one more round, recorded", err)
        self.assertEqual(len(self.claude_calls()), 1)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertNotIn("wrapup: no new unmapped", out)

    def test_with_json_the_refusal_is_a_capped_object_and_the_exit_code_is_still_3(self) -> None:
        self.with_claude(self.claude)
        self.more_work()
        data = self.with_claude(self.claude, expect=3)
        self.assertEqual((data["status"], data["run"], data["new_unmapped"], data["open_unmapped"]), ("capped", None, [], 1))
        self.assertIn("round cap reached", data["reason"])
        self.assertEqual((data["changed_lines"], data["changed_files"]), (250, 1))
        self.assertIn("round cap reached", self.last_err)
        self.assertEqual(len(self.claude_calls()), 1)

    def test_below_the_threshold_the_cap_is_never_consulted(self) -> None:
        self.with_claude(self.claude)
        data = self.wrap()  # nothing new since the review read big.py: below both thresholds, so the cap is not asked
        self.assertEqual((data["status"], data["changed_lines"], data["changed_files"]), ("skipped", 0, 0))


class JsonAndDryRunTest(WrapupCase):
    KEYS = {"status", "base", "base_reason", "changed_lines", "changed_files", "thresholds", "run", "new_unmapped",
            "repeated_unmapped", "repeat_of", "open_unmapped", "reason"}

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start()

    def test_stdout_is_one_json_object_and_the_review_output_is_on_stderr(self) -> None:
        self.put("big.py", numbered(250))
        code, out, err = self.cli("wrapup", "--json", "--from-file", str(self.write_review(blind_spot("Retry", file="big.py", line=4))))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "reviewed")
        self.assertTrue(out.startswith("{\n") and out.rstrip().endswith("}"))
        for human in ("wrapup: 250 changed line(s)", "review round 1/3", "saved raw review output", "wrapup: 1 new unmapped"):
            self.assertIn(human, err)
            self.assertNotIn(human, out)

    def test_the_fields_of_a_reviewed_a_skipped_and_a_dry_run_object(self) -> None:
        skipped = self.wrap()
        self.assertEqual(set(skipped), self.KEYS)
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual((skipped["base"], skipped["base_reason"], skipped["run"], skipped["new_unmapped"]), (self.base, "merge-base", None, []))
        self.assertEqual(skipped["thresholds"], {"min_changed_lines": 200, "min_files": 5})
        self.assertIn("< wrapup_min_changed_lines 200", skipped["reason"])
        self.put("big.py", numbered(250))
        self.assertEqual(set(self.measure()), self.KEYS)
        reviewed = self.wrap("--from-file", str(self.write_review()))
        self.assertEqual(set(reviewed), self.KEYS)
        self.assertEqual((reviewed["status"], reviewed["run"], reviewed["changed_lines"], reviewed["changed_files"]), ("reviewed", "r1", 250, 1))
        self.assertEqual((skipped["repeated_unmapped"], skipped["repeat_of"], reviewed["repeated_unmapped"], reviewed["repeat_of"]),
                         ([], {}, [], {}))  # present on every object, empty where nothing repeats

    def test_a_dry_run_measures_and_says_what_it_would_do_and_writes_nothing(self) -> None:
        self.put("big.py", numbered(250))
        claude = self.fake_claude(review_text())
        before = self.ledger_bytes()
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(claude)}):
            code, out, err = self.cli("wrapup", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[dry-run] wrapup: changed lines 250 >= wrapup_min_changed_lines 200; "
                      f"would run `{claude} -p '/code-review high'`; nothing run or written", out)  # RATCHET_CLAUDE_CMD is honored
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.ledger_bytes(), before)
        self.assertFalse((self.repo / ".ratchet").exists())

    def test_a_dry_run_of_a_saved_review_says_which_file_and_still_writes_nothing(self) -> None:
        self.put("big.py", numbered(250))
        saved = self.write_review(blind_spot("Retry"))
        code, out, _ = self.cli("wrapup", "--dry-run", "--from-file", str(saved))
        self.assertEqual(code, 0)
        self.assertIn(f"would ingest {saved} as a review; nothing run or written", out)
        self.assertFalse(self.ledger.exists())

    def test_a_dry_run_below_the_threshold_is_a_skip(self) -> None:
        self.assertEqual(self.measure()["status"], "skipped")

    def test_the_other_commands_flags_are_taken(self) -> None:
        elsewhere = self.tmp / "elsewhere.jsonl"
        self.put("big.py", numbered(250))
        saved = self.write_review(blind_spot("Retry"))
        self.wrap("--from-file", str(saved), "--ledger", str(elsewhere))
        self.assertTrue(elsewhere.exists())
        self.assertFalse(self.ledger.exists())


class ReportOnlyTest(WrapupCase):
    def setUp(self) -> None:
        super().setUp()
        self.start()
        self.put("big.py", numbered(250))

    def test_it_reports_the_open_blind_spots_of_the_latest_run_and_measures_nothing(self) -> None:
        self.wrap("--from-file", str(self.write_review(blind_spot("Old", line=1))))
        self.more_work()
        self.wrap("--from-file", str(self.write_review(blind_spot("Newer", line=2), blind_spot("Newest", line=3), name="second.txt")))
        data = self.wrap("--report-only")
        self.assertEqual(data["status"], "reported")
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"], data["changed_files"]), (None, None, None, None))
        self.assertEqual((data["run"], [item["summary"] for item in data["new_unmapped"]], data["open_unmapped"]), ("r2", ["Newer", "Newest"], 3))
        self.assertEqual(len(self.review_runs()), 2)  # nothing was reviewed

    def test_a_blind_spot_with_an_outcome_is_not_reported(self) -> None:
        first = self.wrap("--from-file", str(self.write_review(blind_spot("Real", line=1), blind_spot("Not real", line=2))))
        self.append_raw({"v": 1, "kind": "outcome", "id": first["new_unmapped"][1]["id"], "outcome": "false_positive",
                         "outcome_by": "human", "evidence": "wrap-up popup", "ts": "2026-01-07T12:00:00"})
        data = self.wrap("--report-only")
        self.assertEqual([item["summary"] for item in data["new_unmapped"]], ["Real"])

    def test_a_finding_under_a_legacy_catch_all_clause_is_a_blind_spot_when_the_config_says_so(self) -> None:
        self.append_raw(review_round(run="R1"), review_finding("R1-001", rule_id="R.1"))
        self.assertEqual(self.wrap("--report-only")["new_unmapped"], [])
        self.write_config(rules_files=["rules.md"], legacy_unmapped_alias=["R.1"])
        self.assertEqual([item["display_id"] for item in self.wrap("--report-only")["new_unmapped"]], ["R1-001"])

    def test_without_a_recorded_review_there_is_nothing_to_report(self) -> None:
        code, _, err = self.cli("wrapup", "--report-only")
        self.assertEqual(code, 1)
        self.assertIn("no review run is recorded yet", err)

    def test_it_takes_no_measuring_or_review_options(self) -> None:
        for option in (["--base", "HEAD"], ["--level", "low"], ["--from-file", "x"], ["--dry-run"]):
            with self.subTest(option=option):
                code, _, err = self.cli("wrapup", "--report-only", *option)
                self.assertEqual(code, 1)
                self.assertIn("--report-only measures and reviews nothing", err)


class LinkedWorktreeTest(WrapupCase):
    """The tree measured and reviewed is the one wrapup runs in; the ledger and config stay with the main worktree."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start()
        self.linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(self.linked), "-b", "feature")

    def cli_in(self, directory: Path, *argv: str) -> dict:
        code, out, err = self.cli("wrapup", "--json", *argv, "--repo", str(directory), with_repo=False)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_it_measures_the_worktree_it_is_run_in(self) -> None:
        self.put("main_only.txt", numbered(50))
        self.put("linked_only.txt", numbered(7), self.linked)
        in_linked = self.cli_in(self.linked, "--dry-run")
        self.assertEqual((in_linked["changed_lines"], in_linked["changed_files"], in_linked["base"]), (7, 1, self.base))
        in_main = self.cli_in(self.repo, "--dry-run")
        self.assertEqual((in_main["changed_lines"], in_main["changed_files"]), (50, 1))

    def test_adopting_a_rule_in_a_linked_worktree_is_no_work_there_either(self) -> None:
        self.adopt_a_rule(self.linked)  # the same relative names as in the main worktree, which is where the config lives
        self.put("linked_only.txt", numbered(7), self.linked)
        in_linked = self.cli_in(self.linked, "--dry-run")
        self.assertEqual((in_linked["changed_lines"], in_linked["changed_files"]), (7, 1))

    def test_the_current_directory_is_used_when_repo_is_not_given(self) -> None:
        self.put("main_only.txt", numbered(50))
        self.put("linked_only.txt", numbered(7), self.linked)
        previous = os.getcwd()
        os.chdir(self.linked)
        try:
            code, out, err = self.cli("wrapup", "--json", "--dry-run", with_repo=False)
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["changed_lines"], 7)

    def test_a_review_started_there_is_recorded_with_that_worktrees_head(self) -> None:
        linked_head = self.commit_file("linked_big.txt", 250, self.linked)
        data = self.cli_in(self.linked, "--from-file", str(self.write_review(blind_spot("Retry"))))
        self.assertEqual((data["status"], data["changed_lines"]), ("reviewed", 250))
        (run,) = self.review_runs()
        self.assertEqual(run["head"], linked_head)
        self.assertTrue(self.ledger.exists())
        self.assertFalse((self.linked / ".style-findings.jsonl").exists())
        again = self.cli_in(self.linked, "--dry-run")
        self.assertEqual((again["base"], again["base_reason"], again["changed_lines"]),
                         (git(self.linked, "rev-parse", "HEAD^{tree}"), "last-review-tree", 0))  # a clean tree at review time is HEAD's tree

    def test_a_linked_worktree_does_not_measure_from_the_main_worktrees_review(self) -> None:
        self.put("main_big.txt", numbered(250))
        self.put("linked_only.txt", numbered(7), self.linked)
        self.assertEqual(self.cli_in(self.repo, "--from-file", str(self.write_review()))["status"], "reviewed")  # main's tree holds main_big.txt
        in_linked = self.cli_in(self.linked, "--dry-run")  # the review's head is the linked HEAD itself, so an ancestor of it
        self.assertEqual((in_linked["base"], in_linked["base_reason"], in_linked["changed_lines"], in_linked["changed_files"]),
                         (self.base, "merge-base", 7, 1))

    def test_the_main_worktree_does_not_measure_from_a_linked_worktrees_review(self) -> None:
        self.put("linked_big.txt", numbered(250), self.linked)
        self.put("main_only.txt", numbered(7))
        self.assertEqual(self.cli_in(self.linked, "--from-file", str(self.write_review()))["status"], "reviewed")
        in_main = self.cli_in(self.repo, "--dry-run")
        self.assertEqual((in_main["base"], in_main["base_reason"], in_main["changed_lines"], in_main["changed_files"]),
                         (self.base, "merge-base", 7, 1))

    def test_each_worktree_measures_from_the_newest_review_of_its_own_branch_whoever_reviewed_later(self) -> None:
        self.put("linked_big.txt", numbered(250), self.linked)
        self.cli_in(self.linked, "--from-file", str(self.write_review()))
        self.put("main_big.txt", numbered(250))
        self.cli_in(self.repo, "--from-file", str(self.write_review(name="main.txt")))  # the later review is of main
        linked_run, main_run = self.review_runs()
        self.assertEqual(self.cli_in(self.linked, "--dry-run")["base"], linked_run["tree"])
        self.assertEqual(self.cli_in(self.repo, "--dry-run")["base"], main_run["tree"])


class RoundKeyBranchTest(WrapupCase):
    """The branch is read out of the key `round_key` makes, so the two are checked against each other."""

    def test_the_branch_is_what_round_key_puts_before_the_fork_point(self) -> None:
        self.assertEqual(round_key_branch(round_key(self.repo, None, None)), "main")  # main@unborn: no commit yet
        self.start()
        git(self.repo, "checkout", "-q", "-b", "feat@2")
        self.commit_file("one.txt", 2)
        key = round_key(self.repo, None, None)
        self.assertRegex(key, r"^feat@2@[0-9a-f]{12}$")
        self.assertEqual(round_key_branch(key), "feat@2")

    def test_a_target_key_names_no_branch(self) -> None:
        self.start()
        for target in ("feature/x", "123", "a@b", "feat@2@0a1b2c3d4e5f"):
            with self.subTest(target=target):
                key = round_key(self.repo, target, None)
                self.assertEqual((key, round_key_branch(key)), (f"target:{target}", None))

    def test_what_is_no_key_at_all_names_no_branch(self) -> None:
        for key in ("", "no-at-sign", "@", "target:", "target:x@y"):
            with self.subTest(key=key):
                self.assertIsNone(round_key_branch(key))


class SnapshotBaseTest(WrapupCase):
    """The base is the working tree as the last review found it, so reviewed work is not counted again once committed."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start()

    def review_uncommitted_work(self) -> dict:
        self.put("big.py", numbered(250))
        return self.wrap("--from-file", str(self.write_review()))

    def test_work_reviewed_while_uncommitted_is_not_counted_again_once_committed(self) -> None:
        first = self.review_uncommitted_work()
        self.assertEqual((first["base_reason"], first["changed_lines"]), ("merge-base", 250))
        self.commit_all("big.py")
        self.put("more.py", numbered(30))
        data = self.measure()
        self.assertEqual((data["base_reason"], data["changed_lines"], data["changed_files"]), ("last-review-tree", 30, 1))
        self.assertIn("(the working tree as review r1 found it)", self.cli("wrapup", "--dry-run")[1])

    def test_committing_what_a_review_read_is_no_change_and_a_file_it_read_untracked_stays_uncounted(self) -> None:
        self.review_uncommitted_work()
        self.assertEqual(self.measure()["changed_lines"], 0)
        self.commit_all("big.py")
        data = self.wrap()
        self.assertEqual((data["status"], data["changed_lines"], data["changed_files"]), ("skipped", 0, 0))

    def test_an_edit_after_the_review_counts_and_so_does_a_deletion(self) -> None:
        self.review_uncommitted_work()
        self.put("big.py", numbered(250).replace("line 1\n", "edited\n", 1))
        self.assertEqual(self.measure()["changed_lines"], 2)
        (self.repo / "big.py").unlink()
        self.assertEqual(self.measure()["changed_lines"], 250)

    def test_the_files_ratchet_writes_after_the_snapshot_do_not_count(self) -> None:
        self.review_uncommitted_work()  # the ledger and .ratchet/ are written after the snapshot, and nothing here ignores them
        data = self.measure()
        self.assertEqual((data["changed_lines"], data["changed_files"]), (0, 0))

    def test_adopting_a_rule_is_no_work_against_a_tree_either(self) -> None:
        self.review_uncommitted_work()
        self.adopt_a_rule()
        self.put("mine.txt", numbered(3))
        data = self.measure()
        self.assertEqual((data["base_reason"], data["changed_lines"], data["changed_files"]), ("last-review-tree", 3, 1))
        self.commit_all("adopt a rule")  # committing them is no work either: they are in the tree already
        self.assertEqual(self.measure()["changed_files"], 1)

    def test_adopting_a_rule_is_no_work_against_a_commit_whether_the_fork_point_or_the_head_of_a_review(self) -> None:
        self.adopt_a_rule()
        self.put("mine.txt", numbered(3))
        fork = self.measure()
        self.assertEqual((fork["base_reason"], fork["changed_lines"], fork["changed_files"]), ("merge-base", 3, 1))
        self.append_raw(review_round(run="r1", head=self.base))  # a review that recorded no tree: its HEAD is the base
        head = self.measure()
        self.assertEqual((head["base_reason"], head["changed_lines"], head["changed_files"]), ("last-review", 3, 1))

    def test_neither_the_review_nor_the_measuring_touches_the_index_or_the_files(self) -> None:
        self.put("app.py", numbered(20, "L") + "staged\n")
        git(self.repo, "add", "app.py")
        self.put("big.py", numbered(250))
        git(self.repo, "status", "--porcelain")  # git refreshes its own index here; what follows must leave it as it is
        index, big = self.repo / ".git" / "index", self.repo / "big.py"
        status, cached = git(self.repo, "status", "--porcelain", "-uno"), git(self.repo, "diff", "--cached")  # -uno: the review writes .ratchet/ and the ledger
        index_before, big_before = index.read_bytes(), (big.read_bytes(), big.stat().st_mtime_ns)
        self.wrap("--from-file", str(self.write_review()))
        self.measure()
        self.assertEqual(index.read_bytes(), index_before)
        self.assertEqual((big.read_bytes(), big.stat().st_mtime_ns), big_before)
        self.assertEqual((git(self.repo, "status", "--porcelain", "-uno"), git(self.repo, "diff", "--cached")), (status, cached))

    def test_a_linked_worktree_snapshots_with_its_own_index(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        git(linked, "update-index", "--skip-worktree", "app.py")  # in the linked index only: git ignores edits to this file
        self.put("app.py", "edited\n", linked)
        self.assertEqual(state_tree(linked), git(linked, "rev-parse", "HEAD^{tree}"))  # the main index would have picked the edit up

    def test_a_tree_git_gc_removed_falls_back_to_the_head_and_says_so(self) -> None:
        self.review_uncommitted_work()
        tree = self.review_runs()[0]["tree"]
        self.assertTrue(tree_exists(self.repo, tree))
        (self.repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        data = self.measure()
        self.assertEqual((data["base"], data["base_reason"], data["changed_lines"]), (self.base, "last-review", 250))
        self.assertIn("is no longer in this repo", self.cli("wrapup", "--dry-run")[1])

    def test_a_review_without_a_tree_falls_back_to_the_head_and_says_what_that_costs(self) -> None:
        self.append_raw(review_round(run="r1", head=self.base))
        out = self.cli("wrapup", "--dry-run")[1]
        self.assertIn(f"since {self.base[:12]} (HEAD when review r1 started)", out)
        self.assertIn("wrapup: note: review r1 recorded no tree, so work it left uncommitted counts again", out)

    def test_the_newest_review_decides_even_when_it_recorded_no_tree(self) -> None:
        self.review_uncommitted_work()
        self.append_raw(review_round(run="r2", head=self.base))
        self.assertEqual(self.measure()["base_reason"], "last-review")

    def test_an_untracked_file_over_the_limit_is_one_file_with_no_lines_and_never_written_to_the_repo(self) -> None:
        self.review_uncommitted_work()
        self.put("huge.txt", "x\n" * (MAX_UNTRACKED_BYTES // 2 + 1))
        blob = git(self.repo, "hash-object", "huge.txt")
        data = self.measure()
        self.assertEqual((data["changed_lines"], data["changed_files"]), (0, 1))
        with self.assertRaises(AssertionError):
            git(self.repo, "cat-file", "-e", blob)  # not in the object database

    def test_a_file_of_exactly_the_limit_is_read_like_any_other(self) -> None:
        self.review_uncommitted_work()
        self.put("edge.txt", "x\n" * (MAX_UNTRACKED_BYTES // 2))
        self.assertEqual(self.measure()["changed_lines"], MAX_UNTRACKED_BYTES // 2)


class MeasuringWritesNothingTest(WrapupCase):
    """The tree of now, which a wrap-up only measures with, is built in a temporary object directory. Only the review's own
    snapshot, the one it leaves for the next comparison, is written to the repo."""

    def setUp(self) -> None:
        super().setUp()
        self.base = self.start({"tracked.txt": numbered(30)})
        self.put("big.py", numbered(250))
        self.assertEqual(self.wrap("--from-file", str(self.write_review()))["status"], "reviewed")  # records the tree to compare with
        self.put("tracked.txt", numbered(30).replace("line 1\n", "edited\n", 1))  # work since: an edit (+1 -1), a new file (240 lines)
        self.put("fresh.py", numbered(240))
        self.put("huge.txt", "x\n" * (MAX_UNTRACKED_BYTES // 2 + 1))  # and one over the size limit, which has a stand-in blob of its own

    def test_a_dry_run_measuring_against_the_review_tree_adds_nothing_to_the_object_store(self) -> None:
        before = self.objects()
        data = self.measure()
        self.assertEqual((data["status"], data["base_reason"], data["changed_lines"], data["changed_files"]),
                         ("dry-run", "last-review-tree", 242, 3))
        self.assertEqual(self.objects(), before)

    def test_a_wrapup_below_the_thresholds_adds_nothing_either(self) -> None:
        (self.repo / "fresh.py").unlink()
        before = self.objects()
        data = self.wrap()
        self.assertEqual((data["status"], data["base_reason"], data["changed_lines"], data["changed_files"]),
                         ("skipped", "last-review-tree", 2, 2))
        self.assertEqual(self.objects(), before)

    def test_a_linked_worktree_measures_into_the_one_object_store_without_writing_to_it(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", str(linked), "-b", "feature")
        self.put("linked_big.py", numbered(250), linked)
        code, out, err = self.cli("wrapup", "--json", "--from-file", str(self.write_review(name="linked.txt")), "--repo", str(linked), with_repo=False)
        self.assertEqual((code, json.loads(out)["status"]), (0, "reviewed"), err)
        self.put("linked_more.py", numbered(240), linked)
        before = self.objects()
        code, out, err = self.cli("wrapup", "--json", "--dry-run", "--repo", str(linked), with_repo=False)
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["base_reason"], data["changed_lines"], data["changed_files"]),
                         (0, "dry-run", "last-review-tree", 240, 1), err)
        self.assertEqual(self.objects(), before)

    def test_the_temporary_object_directory_is_deleted_afterwards(self) -> None:
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(scratch)):
            self.assertEqual(self.measure()["base_reason"], "last-review-tree")
        self.assertEqual(list(scratch.iterdir()), [])

    def test_the_snapshot_a_review_keeps_is_in_the_repo_and_measuring_does_not_remove_it(self) -> None:
        tree = self.review_runs()[0]["tree"]
        self.assertIn(f"{tree[:2]}/{tree[2:]}", self.objects())
        self.measure()
        self.assertTrue(tree_exists(self.repo, tree))
        self.assertIn(f"{tree[:2]}/{tree[2:]}", self.objects())

    def test_measuring_counts_what_the_object_store_alone_would_have_given(self) -> None:
        tree = self.review_runs()[0]["tree"]
        kept = numstat_since(self.repo, tree)
        written = git(self.repo, "diff", "--numstat", "-z", tree, state_tree(self.repo))  # the same diff, with the tree of now in the repo
        self.assertEqual(sorted(kept.split("\0")), sorted(written.split("\0")))


class StateTreeTest(WrapupCase):
    """The tree `state_tree` makes: every file under its own name, however it is spelled, and the cases with no commit."""

    SPELLINGS = ("with space.txt", "multi\nline.txt", "测试 αβγ.txt", "tab\there.txt", "dir with space/nested file.txt")

    def blobs_in(self, tree: str) -> dict[str, str]:
        """{path: blob id} of every file in `tree`, read NUL-separated so that no name needs unquoting."""
        entries = [entry.split("\t", 1) for entry in git(self.repo, "ls-tree", "-r", "-z", tree).split("\0") if entry]
        return {path: meta.split()[2] for meta, path in entries}

    def files_in(self, tree: str) -> dict[str, str]:
        return {path: git(self.repo, "cat-file", "-p", blob) for path, blob in self.blobs_in(tree).items()}

    def on_disk(self) -> dict[str, str]:
        return {str(path.relative_to(self.repo)): path.read_text(encoding="utf-8").strip() for path in self.repo.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.repo).parts}

    def test_every_file_is_in_the_tree_under_its_own_name_edited_deleted_untracked_or_not(self) -> None:
        self.start({name: f"tracked {number}" for number, name in enumerate(self.SPELLINGS)} | {"gone.txt": "to be deleted"})
        self.put(self.SPELLINGS[0], "edited after the commit")
        (self.repo / "gone.txt").unlink()
        for number, name in enumerate(self.SPELLINGS):
            self.put(f"untracked {name}", f"loose {number}")
        self.assertEqual(self.files_in(state_tree(self.repo)), self.on_disk())
        self.assertNotIn("gone.txt", self.on_disk())

    def test_the_same_names_are_counted_right_from_a_commit_and_from_a_tree(self) -> None:
        names = self.SPELLINGS[:3]
        base = self.start({name: numbered(10) for name in names})
        for name in names:
            self.put(name, numbered(10).replace("line 1\n", "edited\n", 1))  # +1 -1 in each of three tracked files
            self.put(f"new {name}", numbered(4))  # and four lines in each of three new ones
        data = self.measure("--base", base)
        self.assertEqual((data["base_reason"], data["changed_lines"], data["changed_files"]), ("--base", 3 * 2 + 3 * 4, 6))
        self.cli_ok("review", "--from-file", str(self.write_review()))  # records the tree of this work
        for name in names:
            for spelling in (name, f"new {name}"):
                path = self.repo / spelling
                path.write_text(path.read_text(encoding="utf-8") + "one more line\n", encoding="utf-8")
        data = self.measure()
        self.assertEqual((data["base_reason"], data["changed_lines"], data["changed_files"]), ("last-review-tree", 6, 6))

    def test_oversize_files_are_staged_in_batches_under_names_that_need_care(self) -> None:
        self.start()
        names = ["a b.bin", "c\nd.bin", "测试.bin", "e[1].bin", "f*.bin"]
        for number, name in enumerate(names):
            self.put(name, f"{'x' * 50}{number}")  # a content of its own, so that any one of them reaching the object store shows
        self.put("small.txt", "small")
        self.put("e1.bin", "e1")  # small files the glob readings of `e[1].bin` and `f*.bin` would match
        self.put("fx.bin", "fx")
        with mock.patch("ratchet.state_tree.MAX_UNTRACKED_BYTES", 10), mock.patch("ratchet.state_tree.INDEX_ENTRIES_PER_CALL", 2), \
                mock.patch("ratchet.state_tree.run_git", wraps=run_git) as spy:
            tree = state_tree(self.repo)
        calls = [call.args[0] for call in spy.call_args_list if call.args[0][0] == "update-index"]
        self.assertEqual([call.count("--cacheinfo") for call in calls], [2, 2, 1])  # five entries, two to a call
        stand_in_file = self.tmp / "stand-in"
        stand_in_file.write_bytes(OVERSIZE_STAND_IN)
        stand_in = git(self.repo, "hash-object", str(stand_in_file))
        blobs = self.blobs_in(tree)
        self.assertEqual({name: blobs[name] for name in names}, {name: stand_in for name in names})
        files = self.files_in(tree)
        self.assertEqual((files["small.txt"], files["e1.bin"], files["fx.bin"]), ("small", "e1", "fx"))  # a name is not a pattern that takes them out too
        for name in names:  # an oversize file is never read into the store, whatever its name looks like to a glob
            with self.assertRaises(AssertionError, msg=name):
                git(self.repo, "cat-file", "-e", git(self.repo, "hash-object", name))

    def test_a_tree_of_a_branch_with_no_commit_holds_what_is_staged_and_what_is_untracked(self) -> None:
        empty = git(self.repo, "hash-object", "-t", "tree", os.devnull)
        self.assertEqual(state_tree(self.repo), empty)  # no index and no file: the empty tree, which every repo has
        self.assertTrue(tree_exists(self.repo, empty))
        self.put("staged.txt", "staged v1")
        git(self.repo, "add", "staged.txt")
        self.put("staged.txt", "staged v2")  # edited after it was staged: the tree has the file as it is on disk
        self.put("untracked.txt", "loose")
        index = (self.repo / ".git" / "index").read_bytes()
        self.assertEqual(self.files_in(state_tree(self.repo)), {"staged.txt": "staged v2", "untracked.txt": "loose"})
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), index)

    def test_a_repo_path_with_the_list_separator_a_quote_or_a_backslash_in_it_is_measured_all_the_same(self) -> None:
        odd = self.tmp / 'we:ird"dir\\x' / "repo"  # git splits GIT_ALTERNATE_OBJECT_DIRECTORIES at `:`
        odd.mkdir(parents=True)
        git(odd, "init", "-q", "-b", "main")
        (odd / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(odd, "add", "-A")
        git(odd, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
        (odd / "new.txt").write_text("a\nb\n", encoding="utf-8")
        before = sorted(path.name for path in (odd / ".git" / "objects").rglob("*") if path.is_file())
        self.assertEqual(numstat_since(odd, git(odd, "rev-parse", "HEAD^{tree}")), "2\t0\tnew.txt\0")
        self.assertEqual(sorted(path.name for path in (odd / ".git" / "objects").rglob("*") if path.is_file()), before)

    def test_a_branch_with_no_commit_is_measured_from_the_empty_tree_without_writing_to_the_repo(self) -> None:
        empty = git(self.repo, "hash-object", "-t", "tree", os.devnull)
        self.put("a.txt", numbered(3))
        self.put("b b.txt", numbered(2))
        before = self.objects()
        records = sorted(numstat_since(self.repo, empty).split("\0"))
        self.assertEqual(records, ["", "2\t0\tb b.txt", "3\t0\ta.txt"])
        self.assertEqual(self.objects(), before)


class CodeWindowTest(unittest.TestCase):
    def window_at(self, lines: list[str], line: int) -> tuple[str, ...] | None:
        return code_window({"line": line, "snippet": snippet_around(lines, line), "snippet_source": "review"})

    def test_the_same_code_has_the_same_window_wherever_it_stands_cut_to_the_budget_or_not(self) -> None:
        for width in (1, 5, 60, 200):  # characters per line: short lines fit the budget, long ones are cut
            code = [f"{number:>3} " + "w" * width for number in range(1, 13)]
            moved = ["# a new header line"] * 5 + code
            for line in range(4, 10):  # far from both ends of the file: the whole window is there
                with self.subTest(width=width, line=line):
                    window = self.window_at(code, line)
                    self.assertEqual(window, self.window_at(moved, line + 5))
                    self.assertEqual("[snippet truncated]" in window, width == 200)

    def test_a_window_is_every_line_of_the_snippet_not_the_flagged_line_alone(self) -> None:
        code = ["a", "b", "return None", "c", "d"]
        self.assertEqual(self.window_at(code, 3), ("a", "b", "return None", "c", "d"))
        for edited in range(5):  # a change to any line of the window is a change to the window
            other = list(code)
            other[edited] = "x"
            with self.subTest(edited=edited):
                self.assertNotEqual(self.window_at(other, 3), self.window_at(code, 3))

    def test_a_window_clipped_by_the_start_of_the_file_is_not_the_window_of_the_same_code_further_down(self) -> None:
        code = [f"line {number}" for number in range(1, 10)]
        self.assertNotEqual(self.window_at(code, 1), self.window_at(["# header"] * 5 + code, 6))  # asked about again, not guessed at

    def test_whitespace_is_collapsed_line_by_line_and_nothing_else(self) -> None:
        window = code_window({"line": 2, "snippet": "a\n  if   x:\n\tc ", "snippet_source": "worktree"})
        self.assertEqual(window, ("a", "if x:", "c"))
        self.assertNotEqual(window, ("a", "if  x:", "c"))
        self.assertNotEqual(window, ("a", "ifx:", "c"))
        self.assertNotEqual(window, ("a", "", "if x:", "c"))  # a blank line is part of the window's shape

    def test_no_code_to_compare_in_these_cases(self) -> None:
        window = snippet_around(["", "   ", "", "\t"], 2)
        self.assertIsNone(code_window({"line": 2, "snippet": window, "snippet_source": "review"}))  # blank lines say nothing
        self.assertIsNone(code_window({"line": 3, "snippet": "a\nb\nc", "snippet_source": "unavailable"}))
        self.assertIsNone(code_window({"line": 3, "snippet": "typed by hand"}))  # `add` records any text, in no known shape
        self.assertIsNone(code_window({"line": 3}))


class RepeatedBlindSpotTest(WrapupCase):
    """A blind spot an earlier run left open is not new when a later review reports it again."""

    def setUp(self) -> None:
        super().setUp()
        self.start({"svc.py": "".join(f"code_{number} = {number}\n" for number in range(1, 81))})

    def review(self, line: int, summary: str, name: str = "review.txt", file: str = "svc.py") -> dict:
        return self.wrap("--from-file", str(self.write_review(blind_spot(summary, file=file, line=line), name=name)))

    def test_the_same_code_reported_again_at_a_new_line_is_a_repeat_in_other_words(self) -> None:
        self.more_work("a.py")
        first = self.review(40, "Retry has no limit")
        self.assertEqual((len(first["new_unmapped"]), first["repeated_unmapped"], first["repeat_of"]), (1, [], {}))
        self.put("svc.py", "inserted = 0\n" * 3 + (self.repo / "svc.py").read_text(encoding="utf-8"))  # the code moves down
        self.more_work("b.py")
        second = self.review(43, "The loop in retry never ends", name="second.txt")
        self.assertEqual((second["new_unmapped"], second["repeated_unmapped"]), ([], ["demo-repo:l1:r2:r2-1"]))
        self.assertEqual(second["repeat_of"], {"demo-repo:l1:r2:r2-1": "demo-repo:l1:r1:r1-1"})
        self.assertEqual(second["open_unmapped"], 2)  # the ledger keeps the repeat: it is an observation, and still open
        self.assertIn("1 of run r2's unmapped finding(s) repeat a blind spot already known from an earlier run "
                      "(still open there, or marked false_positive or dismissed; same file, same code around the flagged line); "
                      "not asked about again:\n  r2-1  svc.py:43  is a repeat of r1-1\n", self.last_err)
        self.assertIn("wrapup: no new unmapped findings from run r2", self.last_err)

    def test_other_code_in_the_same_file_is_new_and_report_only_splits_the_same_way(self) -> None:
        self.more_work("a.py")
        self.review(40, "Retry has no limit")
        self.more_work("b.py")
        second = self.review(41, "Another problem")
        self.assertEqual((len(second["new_unmapped"]), second["repeated_unmapped"]), (1, []))
        self.put("svc.py", (self.repo / "svc.py").read_text(encoding="utf-8").replace("code_70 = 70", "code_70 = 700"))  # far from both windows
        self.more_work("c.py")
        third = self.review(40, "Retry has no limit, again", name="third.txt")
        self.assertEqual(third["repeated_unmapped"], ["demo-repo:l1:r3:r3-1"])
        again = self.wrap("--report-only")
        self.assertEqual((again["new_unmapped"], again["repeated_unmapped"], again["repeat_of"]),
                         (third["new_unmapped"], third["repeated_unmapped"], third["repeat_of"]))
        self.assertIn("  r3-1  svc.py:40  is a repeat of r1-1\n", self.last_err)  # report-only says it the same way

    def test_code_edited_next_to_the_flagged_line_is_other_code_and_is_asked_about_again(self) -> None:
        self.more_work("a.py")
        self.review(40, "Retry has no limit")
        self.put("svc.py", (self.repo / "svc.py").read_text(encoding="utf-8").replace("code_41 = 41", "code_41 = 410"))  # one line below it
        self.more_work("b.py")
        second = self.review(40, "Retry has no limit, again", name="second.txt")
        self.assertEqual(([item["display_id"] for item in second["new_unmapped"]], second["repeated_unmapped"], second["repeat_of"]),
                         (["r2-1"], [], {}))
        self.assertIn("wrapup: 1 new unmapped finding(s) from run r2", self.last_err)
        self.assertNotIn("is a repeat of", self.last_err)

    def test_the_same_line_of_text_in_other_code_of_the_file_is_not_a_repeat(self) -> None:
        # Two `return None` in one file: no such user, and an order that was never paid. Turning the first down says nothing about the second.
        self.put("lookup.py", "def find_user(users, name):\n    for user in users:\n        if user.name == name:\n            return user\n"
                              "    return None\n\n\ndef unpaid_total(orders):\n    total = 0\n    for order in orders:\n"
                              "        if order.paid:\n            continue\n        total += order.amount\n    if total == 0:\n"
                              "        return None\n    return total\n")
        self.more_work("a.py")
        first = self.review(5, "A missing user is returned as None, so nobody can tell it from a failed lookup", file="lookup.py")
        self.cli_ok("label", first["new_unmapped"][0]["id"], "--outcome", "false_positive", "--note", "wrap-up: not a real problem")
        self.more_work("b.py")
        second = self.review(15, "An order nobody paid for gets None, not a total", name="second.txt", file="lookup.py")
        self.assertEqual(([item["display_id"] for item in second["new_unmapped"]], second["repeated_unmapped"]), (["r2-1"], []))
        self.assertEqual(second["open_unmapped"], 1)  # the first was turned down; this one is open and will be asked about
        self.more_work("c.py")
        third = self.review(5, "None for a missing user again", name="third.txt", file="lookup.py")
        self.assertEqual((third["new_unmapped"], third["repeat_of"]), ([], {"demo-repo:l1:r3:r3-1": first["new_unmapped"][0]["id"]}))

    def test_what_counts_as_near_is_three_lines_each_side_the_window_a_review_keeps(self) -> None:
        code = [f"code_{number} = {number}" for number in range(1, 21)]

        def spot(display_id: str, run: str, lines: list[str]) -> dict:
            return review_finding(display_id, run=run, rule_id="unmapped", file="x.py", line=10,
                                  snippet=snippet_around(lines, 10), snippet_source="review")

        old = spot("r1-1", "r1", code)
        for offset in range(-6, 7):
            edited = list(code)
            edited[9 + offset] = "edited = 0"
            with self.subTest(offset=offset):
                spots = run_blind_spots([old, spot("r2-1", "r2", edited)], frozenset(), "r2")
                self.assertEqual([f["display_id"] for f in spots.repeated], ["r2-1"] if abs(offset) > 3 else [])
        spaced = [line.replace(" = ", "    =  ") for line in code]  # whitespace alone is not an edit
        self.assertEqual([f["display_id"] for f in run_blind_spots([old, spot("r2-1", "r2", spaced)], frozenset(), "r2").repeated], ["r2-1"])

    def test_a_repeat_names_the_earliest_finding_it_repeats(self) -> None:
        def unmapped(display_id: str, run: str) -> dict:
            return review_finding(display_id, run=run, rule_id="unmapped", file="x.py", line=5, snippet="a\nb\nc\nd\ne", snippet_source="review")

        first, again, latest = unmapped("r1-1", "r1"), unmapped("r2-1", "r2"), unmapped("r3-1", "r3")
        spots = run_blind_spots([first, again, latest], frozenset(), "r3")  # r2-1 is itself a repeat of r1-1, and still open
        self.assertEqual(spots.repeat_of, {latest["id"]: first})
        self.assertEqual(run_blind_spots([again, first, latest], frozenset(), "r3").repeat_of, {latest["id"]: again})  # ledger order decides
        self.assertEqual(run_blind_spots([first, again], frozenset(), "r2").repeat_of, {again["id"]: first})

    def test_an_earlier_blind_spot_counts_as_known_while_it_is_open_or_turned_down_but_not_once_fixed(self) -> None:
        old = review_finding("r1-1", run="r1", rule_id="unmapped", file="x.py", line=5)
        new = review_finding("r2-1", run="r2", rule_id="unmapped", file="x.py", line=5)

        def outcome(kind: str) -> dict:
            return {"v": 1, "kind": "outcome", "id": old["id"], "outcome": kind, "outcome_by": "human", "evidence": "e",
                    "ts": "2026-01-07T12:00:00"}

        def split(*events: dict) -> tuple[list[str], list[str]]:
            spots = run_blind_spots(list(events), frozenset(), "r2")
            return [f["display_id"] for f in spots.new], [f["display_id"] for f in spots.repeated]

        self.assertEqual(split(old, new), ([], ["r2-1"]))  # still open
        for turned_down in ("false_positive", "dismissed"):
            with self.subTest(outcome=turned_down):
                self.assertEqual(split(old, outcome(turned_down), new), ([], ["r2-1"]))
        self.assertEqual(split(old, outcome("fixed"), new), (["r2-1"], []))  # the same code reported again may not be fixed

    def test_a_blind_spot_the_user_marked_not_real_is_not_asked_about_again_and_the_repeat_gets_no_outcome(self) -> None:
        self.more_work("a.py")
        first = self.review(40, "Retry has no limit")
        first_id = first["new_unmapped"][0]["id"]
        self.cli_ok("label", first_id, "--outcome", "false_positive", "--note", "wrap-up: not a real problem")
        self.more_work("b.py")
        second = self.review(40, "Retry has no limit, said again", name="second.txt")
        self.assertEqual((second["new_unmapped"], second["repeated_unmapped"]), ([], ["demo-repo:l1:r2:r2-1"]))
        self.assertEqual(second["open_unmapped"], 1)  # the ledger took the repeat in, open
        self.assertEqual([event["id"] for event in self.events() if event["kind"] == "outcome"], [first_id])  # and wrote no outcome for it
        self.more_work("c.py")
        third = self.review(40, "And again", name="third.txt")
        self.assertEqual((third["new_unmapped"], len(third["repeated_unmapped"])), ([], 1))

    def test_a_blind_spot_that_was_fixed_is_asked_about_when_the_same_code_is_reported_again(self) -> None:
        self.more_work("a.py")
        first = self.review(40, "Retry has no limit")
        self.cli_ok("label", first["new_unmapped"][0]["id"], "--outcome", "fixed", "--note", "fixed in a commit")
        self.more_work("b.py")
        second = self.review(40, "Retry has no limit", name="second.txt")
        self.assertEqual(([item["display_id"] for item in second["new_unmapped"]], second["repeated_unmapped"]), (["r2-1"], []))

    def test_with_a_window_on_one_side_only_the_line_number_does_not_make_a_repeat(self) -> None:
        def unmapped(display_id: str, run: str, **extra: object) -> dict:
            return review_finding(display_id, run=run, rule_id="unmapped", file="x.py", line=2, **extra)

        coded = {"snippet": "a\nb\nc", "snippet_source": "review"}  # the code around line 2
        blank = {"snippet": "\n  \n", "snippet_source": "review"}  # lines that say nothing about the code
        for earlier, later in ((coded, {}), ({}, coded), (coded, blank), (blank, coded)):
            spots = run_blind_spots([unmapped("r1-1", "r1", **earlier), unmapped("r2-1", "r2", **later)], frozenset(), "r2")
            self.assertEqual(([f["display_id"] for f in spots.new], spots.repeated), (["r2-1"], []), (earlier, later))
        both = run_blind_spots([unmapped("r1-1", "r1", **coded), unmapped("r2-1", "r2", **coded)], frozenset(), "r2")
        self.assertEqual([f["display_id"] for f in both.repeated], ["r2-1"])  # the same code on both sides is a repeat

    def test_blank_lines_on_both_sides_say_nothing_so_the_line_number_stands_in(self) -> None:
        def blank(display_id: str, run: str, line: int) -> dict:
            return review_finding(display_id, run=run, rule_id="unmapped", file="x.py", line=line, snippet="\n  \n", snippet_source="review")

        old = blank("r1-1", "r1", 5)
        same_line = run_blind_spots([old, blank("r2-1", "r2", 5)], frozenset(), "r2")
        other_line = run_blind_spots([old, blank("r2-1", "r2", 9)], frozenset(), "r2")  # two empty stretches of one file are not the same code
        self.assertEqual(([f["display_id"] for f in same_line.repeated], [f["display_id"] for f in other_line.repeated]), (["r2-1"], []))

    def test_only_an_unmapped_finding_of_another_run_makes_a_repeat_and_a_legacy_alias_reads_as_unmapped(self) -> None:
        covered = review_finding("r1-1", run="r1", rule_id="P1.1", file="x.py", line=5)  # a rule covers this one
        catch_all = review_finding("r1-2", run="r1", rule_id="R.1", file="y.py", line=5)
        first = review_finding("r2-1", run="r2", rule_id="unmapped", file="x.py", line=5)
        second = review_finding("r2-2", run="r2", rule_id="unmapped", file="y.py", line=5)
        events = [covered, catch_all, first, second]

        def split(alias: frozenset[str]) -> tuple[list[str], list[str]]:
            spots = run_blind_spots(events, alias, "r2")
            return [f["display_id"] for f in spots.new], [f["display_id"] for f in spots.repeated]

        self.assertEqual(split(frozenset()), (["r2-1", "r2-2"], []))  # R.1 is a rule when the config says nothing else
        self.assertEqual(split(frozenset({"R.1"})), (["r2-1"], ["r2-2"]))  # and the catch-all it was is a blind spot already known

    def test_two_findings_of_one_run_on_the_same_code_are_both_new_when_no_earlier_run_has_it(self) -> None:
        twin = review_finding("r1-1", run="r1", rule_id="unmapped", file="z.py", line=9)
        again = review_finding("r1-2", run="r1", rule_id="unmapped", file="z.py", line=9)
        self.assertEqual([f["display_id"] for f in run_blind_spots([twin, again], frozenset(), "r1").new], ["r1-1", "r1-2"])

    def test_without_a_snippet_the_same_file_and_line_stand_in_and_findings_of_one_run_never_repeat_each_other(self) -> None:
        old = review_finding("r1-1", run="r1", rule_id="unmapped", file="x.py", line=5)
        same = review_finding("r2-1", run="r2", rule_id="unmapped", file="x.py", line=5)
        moved = review_finding("r2-2", run="r2", rule_id="unmapped", file="x.py", line=6)
        other_file = review_finding("r2-3", run="r2", rule_id="unmapped", file="y.py", line=5)
        spots = run_blind_spots([old, same, moved, other_file], frozenset(), "r2")
        self.assertEqual(([f["display_id"] for f in spots.new], [f["display_id"] for f in spots.repeated]), (["r2-2", "r2-3"], ["r2-1"]))
        later = review_finding("r3-1", run="r3", rule_id="unmapped", file="x.py", line=5)
        self.assertEqual(run_blind_spots([old, same, later], frozenset(), "r2").repeated, [same])  # a later run is not "earlier"
        alone = run_blind_spots([same, later], frozenset(), "r2")  # and it makes no repeat of its own when it is the only other run with the code
        self.assertEqual(([f["display_id"] for f in alone.new], alone.repeated), (["r2-1"], []))


if __name__ == "__main__":
    unittest.main()
