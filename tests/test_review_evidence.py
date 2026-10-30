import json
import subprocess
from pathlib import Path

from support import _GIT_ENV, RepoCase, git, review_finding
from ratchet.config import load_config
from ratchet.errors import RatchetError
from ratchet.git import commit_sha, committed_blob
from ratchet.proposal_store import local_path, read_local, replace_local
from ratchet.review import save_review
from ratchet.review_evidence import capture, save, snapshot_snippets, source_for
from ratchet.source_evidence import MAX_SOURCE_BYTES, read_committed_source, read_source


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
        snapshot = capture(self.config, self.repo, target)
        save_review(self.repo, "R1", "synthetic review")
        save(self.config, self.repo, "R1", snapshot, "synthetic review")
        return snapshot

    def test_pre_review_text_survives_later_working_tree_edits(self):
        self.capture_and_save()
        (self.repo / "source.py").write_text("later fix\n")
        source = source_for(self.config, self.finding)
        self.assertEqual(source["text"], "reviewed change")
        self.assertTrue(source["stable_during_review"])
        self.assertEqual(source["basis"], "local_pre_review")

    def test_a_source_modified_during_review_is_flagged(self):
        snapshot = capture(self.config, self.repo, None)
        (self.repo / "source.py").write_text("changed during review\n")
        save_review(self.repo, "R1", "synthetic review")
        save(self.config, self.repo, "R1", snapshot, "synthetic review")
        self.assertFalse(source_for(self.config, self.finding)["stable_during_review"])

    def test_import_and_unrelated_target_do_not_claim_an_exact_review_snapshot(self):
        self.assertEqual(source_for(self.config, self.finding)["status"], "unavailable")
        self.capture_and_save("42")
        self.assertEqual(source_for(self.config, self.finding)["basis"], "local_pre_review_target_unverified")

    def test_changed_raw_review_and_reused_snapshot_are_rejected(self):
        snapshot = self.capture_and_save()
        with self.assertRaisesRegex(RatchetError, "already exists"):
            save(self.config, self.repo, "R1", snapshot, "synthetic review")
        (self.repo / ".ratchet/reviews/R1.txt").write_text("different review")
        with self.assertRaisesRegex(RatchetError, "no longer matches"):
            source_for(self.config, self.finding)


class LinkedTreeEvidenceTest(RepoCase):
    """The evidence is about the worktree being reviewed; where it is kept is the main worktree's business.

    The rules are tracked files, so they are the tree's own.
    """

    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        (self.repo / "source.py").write_text("base\n")
        git(self.repo, "add", "source.py", "rules.md", ".ratchet.json")
        self.commit("base")
        self.tree = self.tmp / "feature-tree"
        git(self.repo, "worktree", "add", "-q", "-b", "feature", str(self.tree))
        self.commit("main moves on")  # the two worktrees stand on different commits
        (self.repo / "source.py").write_text("main edit\n")
        (self.tree / "source.py").write_text("feature edit\n")
        (self.tree / "only_in_feature.py").write_text("new\n")
        self.config = load_config(self.repo, None, None, self.repo)

    def test_the_snapshot_holds_the_head_and_the_files_of_the_tree_it_was_given(self):
        snapshot = capture(self.config, self.tree, None)
        self.assertEqual(snapshot["head"], commit_sha(self.tree, "HEAD"))
        self.assertNotEqual(snapshot["head"], commit_sha(self.repo, "HEAD"))
        self.assertEqual(sorted(snapshot["files"]), ["only_in_feature.py", "source.py"])
        self.assertEqual(snapshot["files"]["source.py"]["text"], "feature edit")
        main = capture(self.config, self.repo, None)
        self.assertEqual(main["head"], commit_sha(self.repo, "HEAD"))
        self.assertEqual(main["files"]["source.py"]["text"], "main edit")
        self.assertNotIn("only_in_feature.py", main["files"])

    def test_the_evidence_is_kept_in_the_main_worktree_and_checked_against_the_tree(self):
        snapshot = capture(self.config, self.tree, None)
        save_review(self.repo, "R1", "synthetic review")
        (self.tree / "source.py").write_text("changed during review\n")
        save(self.config, self.tree, "R1", snapshot, "synthetic review")
        self.assertFalse((self.tree / ".ratchet").exists())
        record = json.loads((self.repo / ".ratchet" / "reviews" / "R1.evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(record["source_stable_during_review"], {"only_in_feature.py": True, "source.py": False})

    def test_the_rules_are_those_of_the_tree_being_reviewed(self):
        (self.tree / "rules.md").write_text("tree rules\n")  # the feature branch edited its rules
        tree_config = load_config(self.tree, None, None, self.tree)
        (rules,) = capture(tree_config, self.tree, None)["rules"]
        self.assertEqual(Path(rules["absolute_path"]), self.tree / "rules.md")
        self.assertEqual(rules["text"], "tree rules")
        (main_rules,) = capture(self.config, self.repo, None)["rules"]
        self.assertEqual(Path(main_rules["absolute_path"]), self.repo / "rules.md")
        self.assertNotEqual(main_rules["text"], "tree rules")


class RangeEvidenceTest(RepoCase):
    """A review of a commit range read those commits, so its evidence is the files the range changed as HEAD holds them,
    though the working tree has edits to them that shift every line."""

    def setUp(self):
        super().setUp()
        self.with_sample_rules()
        self.config = load_config(self.repo, None, None, self.repo)
        (self.repo / "a.py").write_text("old\n")
        (self.repo / "outside.py").write_text("outside one\noutside two\n")
        (self.repo / "gone.py").write_text("gone\n")
        git(self.repo, "add", "-A")
        self.start = self.commit("start")
        (self.repo / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
        (self.repo / "b.py").write_text("b one\n")
        (self.repo / "gone.py").unlink()
        git(self.repo, "add", "-A")
        self.head = self.commit("the reviewed range")
        self.range = f"{self.start}..{self.head}"
        (self.repo / "a.py").write_text("inserted\none\ntwo\nthree\nfour\nfive\n")  # a file of the range, edited since
        (self.repo / "outside.py").write_text("edited\noutside one\noutside two\n")  # a file the range left alone, edited too
        (self.repo / "c.py").write_text("untracked\n")

    def test_the_evidence_is_the_files_the_range_changed_as_head_holds_them(self):
        snapshot = capture(self.config, self.repo, None, self.range)
        self.assertEqual(snapshot["committed_range"], self.range)
        self.assertEqual(sorted(snapshot["files"]), ["a.py", "b.py", "gone.py"])
        self.assertEqual(snapshot["files"]["a.py"]["text"], "one\ntwo\nthree\nfour\nfive")  # not the edited file
        self.assertEqual(snapshot["files"]["a.py"]["version"], f"commit:{self.head}")
        self.assertEqual(snapshot["files"]["gone.py"]["status"], "unavailable")  # the range deleted it
        self.assertEqual(snapshot["omitted_files"], [])

    def test_work_that_is_not_committed_is_no_part_of_it(self):
        files = capture(self.config, self.repo, None, self.range)["files"]
        self.assertTrue({"c.py", "outside.py"}.isdisjoint(files), sorted(files))

    def test_a_review_of_the_local_changes_keeps_them_as_they_are_on_disk(self):
        snapshot = capture(self.config, self.repo, None)
        self.assertIsNone(snapshot["committed_range"])
        self.assertEqual(sorted(snapshot["files"]), ["a.py", "c.py", "outside.py"])
        self.assertEqual(snapshot["files"]["a.py"]["text"], "inserted\none\ntwo\nthree\nfour\nfive")
        self.assertEqual(snapshot["files"]["a.py"]["version"], "current_workspace")

    def test_the_rules_are_still_read_from_disk(self):
        (self.repo / "rules.md").write_text("edited rules\n")  # the reviewer loads its rules from the working tree
        (rules,) = capture(self.config, self.repo, None, self.range)["rules"]
        self.assertEqual(rules["text"], "edited rules")

    def test_a_snippet_is_cut_from_the_commit_for_any_file_the_working_tree_edited(self):
        locate = snapshot_snippets(self.config, self.repo, capture(self.config, self.repo, None, self.range))
        self.assertEqual(locate("a.py", 1), ("one\ntwo\nthree\nfour", "review"))  # the edited file would give "inserted\none\ntwo\nthree"
        self.assertEqual(locate("outside.py", 1), ("outside one\noutside two", "review"))
        self.assertEqual(locate("c.py", 1), (None, "unavailable"))  # a file no commit holds

    def saved_stability(self, snapshot):
        save_review(self.repo, "R1", "synthetic review")
        save(self.config, self.repo, "R1", snapshot, "synthetic review")
        record = json.loads((self.repo / ".ratchet" / "reviews" / "R1.evidence.json").read_text(encoding="utf-8"))
        return record["source_stable_during_review"]

    def test_a_file_the_working_tree_edits_while_the_review_runs_is_not_unstable(self):
        snapshot = capture(self.config, self.repo, None, self.range)
        (self.repo / "a.py").write_text("changed during the review\n")
        stable = self.saved_stability(snapshot)
        self.assertEqual((stable["a.py"], stable["b.py"]), (True, True))  # the commit did not change, whatever the file did
        source = source_for(self.config, review_finding(file="a.py", line=2))
        self.assertEqual((source["text"], source["stable_during_review"]), ("one\ntwo\nthree\nfour\nfive", True))

    def test_a_commit_made_while_the_review_runs_does_not_make_the_evidence_unstable(self):
        snapshot = capture(self.config, self.repo, None, self.range)
        (self.repo / "a.py").write_text("committed during the review\n")
        git(self.repo, "add", "a.py")
        self.commit("made while the review ran")  # HEAD moves on; the evidence is still the commit it was captured from
        self.assertEqual(self.saved_stability(snapshot)["a.py"], True)


class CommittedSourceTest(RepoCase):
    """`read_committed_source` reads a file as a commit holds it, and refuses what `read_source` refuses."""

    def setUp(self):
        super().setUp()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("one\ntwo\nthree\n")
        (self.repo / ".env").write_text("TOKEN=secret\n")
        (self.repo / "link.py").symlink_to("src/a.py")
        (self.repo / "binary.bin").write_bytes(b"ab\0cd")
        (self.repo / "latin.txt").write_bytes(b"caf\xe9\n")
        (self.repo / "big.txt").write_text("x" * (MAX_SOURCE_BYTES + 1))
        git(self.repo, "add", "-A")
        self.head = self.commit("c1")
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.head},vendor/sub")  # a submodule entry
        self.head = self.commit("c2")

    def read(self, name: str) -> dict:
        return read_committed_source(self.repo, self.head, name)

    def test_the_file_is_what_the_commit_holds_whatever_the_working_tree_became(self):
        self.assertEqual(self.read("src/a.py")["text"], "one\ntwo\nthree")
        (self.repo / "src" / "a.py").write_text("changed\n")
        self.assertEqual(self.read("src/a.py")["text"], "one\ntwo\nthree")
        (self.repo / "src" / "a.py").unlink()
        self.assertEqual(self.read("src/a.py")["text"], "one\ntwo\nthree")
        self.assertEqual(self.read(str(self.repo / "src" / "a.py"))["text"], "one\ntwo\nthree")  # named by absolute path

    def test_what_the_working_tree_reader_refuses_is_refused_for_the_commit_too(self):
        refused = (".env", "../outside.txt", str(self.tmp / "outside.txt"), ".git/config", "link.py", "binary.bin", "latin.txt",
                   "big.txt", "src", "absent.py", ".")
        for name in refused:
            with self.subTest(name=name):
                self.assertEqual(read_source(self.repo, name, None)["status"], "unavailable")
                self.assertEqual(self.read(name)["status"], "unavailable")

    def test_the_commit_must_hold_a_regular_file_there(self):
        for name in ("vendor/sub", "vendor", "src/a.py/deeper", "not-in-the-commit.py"):
            with self.subTest(name=name):
                self.assertEqual(self.read(name)["status"], "unavailable")

    def test_a_symlink_in_the_commit_is_refused_after_the_working_tree_copy_stopped_being_one(self):
        (self.repo / "link.py").unlink()
        (self.repo / "link.py").write_text("plain now\n")
        self.assertEqual(read_source(self.repo, "link.py", None)["status"], "available")  # the working tree no longer minds
        self.assertEqual(self.read("link.py")["status"], "unavailable")  # the commit holds a symlink, whose text would be its target

    def test_a_commit_git_cannot_find_is_unavailable_not_an_error(self):
        source = read_committed_source(self.repo, "0" * 40, "src/a.py")
        self.assertEqual(source["status"], "unavailable")
        self.assertIn("failed", source["reason"])

    def test_a_name_git_reads_as_pathspec_magic_for_a_secret_file_does_not_read_it(self):
        # `local_path` refuses a first segment that starts with `.env`; git reads each of these as the pathspec `.env`.
        for name in (":.env", ":(top).env", ":(literal).env"):
            with self.subTest(name=name):
                source = self.read(name)
                self.assertEqual(source["status"], "unavailable")
                self.assertNotIn("secret", str(source))


class CommittedBlobTest(RepoCase):
    def setUp(self):
        super().setUp()
        (self.repo / "dir").mkdir()
        (self.repo / "dir" / "file.txt").write_bytes(b"0123456789")
        (self.repo / "link.txt").symlink_to("dir/file.txt")
        git(self.repo, "add", "-A")
        self.head = self.commit("c1")

    def test_a_regular_file_is_its_bytes(self):
        self.assertEqual(committed_blob(self.repo, self.head, "dir/file.txt", 100), b"0123456789")

    def test_a_file_over_the_limit_is_not_read(self):
        self.assertEqual(committed_blob(self.repo, self.head, "dir/file.txt", 10), b"0123456789")
        self.assertIsNone(committed_blob(self.repo, self.head, "dir/file.txt", 9))

    def test_a_symlink_a_directory_and_a_missing_path_are_none(self):
        for name in ("link.txt", "dir", "missing.txt", "dir/file.txt/deeper"):
            with self.subTest(name=name):
                self.assertIsNone(committed_blob(self.repo, self.head, name, 100))

    def test_a_commit_git_does_not_know_is_an_error(self):
        with self.assertRaisesRegex(RatchetError, "failed"):
            committed_blob(self.repo, "0" * 40, "dir/file.txt", 100)

    def test_pathspec_magic_in_a_path_is_part_of_a_name_that_no_file_has(self):
        (self.repo / ".env").write_bytes(b"TOKEN=secret\n")
        git(self.repo, "add", "-f", ".env")
        head = self.commit("c2")
        self.assertEqual(committed_blob(self.repo, head, ".env", 100), b"TOKEN=secret\n")
        magic = (":.env", ":(top).env", ":(literal).env", ":/.env", ":(top)dir/file.txt", ":/dir/file.txt", ":/",
                 ":(icase)DIR/FILE.TXT", ":(glob)dir/*.txt", ":!dir", ":^dir", ":(exclude)dir", ":(attr:text)dir/file.txt")
        for name in magic:
            with self.subTest(name=name):
                self.assertIsNone(committed_blob(self.repo, head, name, 100))

    def test_a_file_whose_own_name_looks_like_pathspec_magic_is_read_by_that_name(self):
        (self.repo / ":(top)note.txt").write_bytes(b"magic-looking\n")
        (self.repo / ":plain.txt").write_bytes(b"colon\n")
        (self.repo / "dir" / "a[1].txt").write_bytes(b"bracket\n")
        git(self.repo, "add", "-A")
        head = self.commit("c2")
        self.assertEqual(committed_blob(self.repo, head, ":(top)note.txt", 100), b"magic-looking\n")
        self.assertEqual(committed_blob(self.repo, head, ":plain.txt", 100), b"colon\n")
        self.assertEqual(committed_blob(self.repo, head, "dir/a[1].txt", 100), b"bracket\n")
        self.assertEqual(committed_blob(self.repo, head, "dir/file.txt", 100), b"0123456789")


# The names `local_path` protects, spelled the way a case-insensitive file system (macOS APFS, Windows) still opens them.
PROTECTED_SPELLINGS = (".ENV", ".Env", ".eNv", ".ENV.local", ".Env.Production", ".ENVRC", "sub/.ENV", "sub/.Env.local",
                       ".GIT/config", ".Git/config", ".git/CONFIG", ".GIT/CONFIG", "sub/.GIT/config", "src/.Git")


class ProtectedNameCaseTest(RepoCase):
    """`.git` and `.env*` are protected however they are cased.

    A case-insensitive file system opens `.ENV` as the file `.env` and `.GIT/config` as `.git/config`, whose remote URL can
    carry a token. The name check is string work done before any file is touched, so the first two tests need no such file
    system; the others read real files under the other spelling and must be refused for the guard's reason, which a
    case-sensitive file system's "no such file" is not.
    """

    def test_every_spelling_of_a_protected_name_is_refused_by_name(self):
        for name in PROTECTED_SPELLINGS:
            with self.subTest(name=name), self.assertRaisesRegex(RatchetError, "unsafe proposal path"):
                local_path(self.repo, name)

    def test_names_that_only_resemble_a_protected_one_are_not_refused(self):
        for name in (".gitignore", ".GITIGNORE", ".gitattributes", ".github/workflows/ci.yml", ".GitHub/PULL_REQUEST_TEMPLATE.md",
                     "environment.md", "src/envelope.py", "Env/readme.md", "git/config", "dot.env", "src/.gitkeep"):
            with self.subTest(name=name):
                self.assertEqual(local_path(self.repo, name), self.repo / name)

    def test_a_secret_is_not_read_under_another_spelling(self):
        (self.repo / "sub").mkdir()
        for name in (".env", ".env.local", "sub/.env"):
            (self.repo / name).write_text("TOKEN=secret\n")
        git(self.repo, "remote", "add", "origin", "https://user:secret@example.invalid/org/repo.git")  # `.git/config` holds a token now
        for name in (*PROTECTED_SPELLINGS, str(self.repo / ".ENV"), str(self.repo / ".GIT" / "config")):
            with self.subTest(name=name):
                source = read_source(self.repo, name, 1)
                self.assertEqual(source["status"], "unavailable")
                self.assertIn("unsafe proposal path", source["reason"])
                self.assertNotIn("secret", str(source))

    def test_a_protected_name_in_another_case_is_neither_read_nor_overwritten_through_the_store(self):
        (self.repo / ".env").write_text("TOKEN=secret\n")
        with self.assertRaisesRegex(RatchetError, "unsafe proposal path"):
            read_local(self.repo, ".ENV")
        with self.assertRaisesRegex(RatchetError, "unsafe proposal path"):
            replace_local(self.repo, ".ENV", "overwritten\n")
        self.assertEqual((self.repo / ".env").read_text(), "TOKEN=secret\n")

    def test_ordinary_dot_files_are_still_read(self):
        (self.repo / ".github").mkdir()
        files = {".github/ci.yml": "on: push", ".gitignore": "*.pyc", "environment.md": "notes"}
        for name, text in files.items():
            (self.repo / name).write_text(text + "\n")
        for name, text in files.items():
            with self.subTest(name=name):
                self.assertEqual(read_source(self.repo, name, None)["text"], text)

    def test_the_review_evidence_does_not_keep_an_untracked_secret_named_in_another_case(self):
        self.with_sample_rules()
        (self.repo / ".ENV").write_text("TOKEN=secret\n")  # untracked and not ignored, so git lists it and capture reads it
        (self.repo / "app.py").write_text("print('hi')\n")
        snapshot = capture(load_config(self.repo, None, None, self.repo), self.repo, None)
        self.assertEqual(snapshot["files"]["app.py"]["status"], "available")
        self.assertEqual(snapshot["files"][".ENV"]["status"], "unavailable")
        self.assertIn("unsafe proposal path", snapshot["files"][".ENV"]["reason"])
        self.assertFalse("secret" in json.dumps(snapshot), "a secret reached the evidence")


def commit_holding_dot_git_dir(repo: Path) -> str:
    """A commit whose tree has `.GIT/config`. `git add` refuses that path, so the tree is built with plumbing."""
    def plumbing(*args: str, stdin: str = "") -> str:
        proc = subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True, text=True, env=_GIT_ENV, check=True)
        return proc.stdout.strip()

    blob = plumbing("hash-object", "-w", "--stdin", stdin='[remote "origin"]\n\turl = https://user:secret@example.invalid/x.git\n')
    inner = plumbing("mktree", stdin=f"100644 blob {blob}\tconfig\n")
    outer = plumbing("mktree", stdin=f"040000 tree {inner}\t.GIT\n")
    return plumbing("commit-tree", "-m", "a .GIT directory", outer)


class CommittedProtectedNameCaseTest(RepoCase):
    """A commit that holds a file under a protected name in another case is no more readable than the working tree is."""

    def setUp(self):
        super().setUp()
        (self.repo / "sub").mkdir()
        for name in (".ENV", ".Env.local", "sub/.ENV"):
            (self.repo / name).write_text("TOKEN=secret\n")
        (self.repo / "plain.txt").write_text("plain\n")
        git(self.repo, "add", "-A")
        self.head = self.commit("c1")
        self.git_dir_commit = commit_holding_dot_git_dir(self.repo)
        self.held = [(self.head, name) for name in (".ENV", ".Env.local", "sub/.ENV")] + [(self.git_dir_commit, ".GIT/config")]

    def test_the_commits_do_hold_the_files_under_those_spellings(self):
        # Without this the refusals below could be a missing file, and they would pass for the wrong reason.
        for commit, name in self.held:
            with self.subTest(name=name):
                self.assertIn(b"secret", committed_blob(self.repo, commit, name, 1000))

    def test_they_are_refused_for_the_name_not_read(self):
        for commit, name in self.held:
            with self.subTest(name=name):
                source = read_committed_source(self.repo, commit, name)
                self.assertEqual(source["status"], "unavailable")
                self.assertIn("unsafe proposal path", source["reason"])
                self.assertNotIn("secret", str(source))
        self.assertEqual(read_committed_source(self.repo, self.head, "plain.txt")["text"], "plain")
