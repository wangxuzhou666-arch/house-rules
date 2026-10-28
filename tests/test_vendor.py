import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import REPO_ROOT, git

from ratchet.cli import main
from ratchet.errors import RatchetError
from ratchet.vendor import MARKER_NAME, SOURCE_ROOT, UNCOMMITTED, plan_vendor, source_commit, tree_files


def independent_hash(base: Path) -> str:
    """The documented recipe, written out again: sha256 of `path NUL sha256(bytes) LF` lines in path order."""
    digest = hashlib.sha256()
    for rel in sorted(str(p.relative_to(base).as_posix()) for entry in ("ratchet", "bin/ratchet", "schemas", "templates", "LICENSE")
                      for p in ([base / entry] if (base / entry).is_file() else sorted((base / entry).rglob("*")))
                      if p.is_file() and "__pycache__" not in p.parts):
        digest.update(f"{rel}\0{hashlib.sha256((base / rel).read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


class VendorCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.source = self.tmp / "source"
        for rel, text in {"ratchet/__init__.py": "VERSION = 1\n", "ratchet/cli.py": "print('cli')\n", "ratchet/page.html": "<p>page</p>\n",
                          "bin/ratchet": "#!/usr/bin/env python3\nprint('bin')\n", "schemas/finding.json": "{}\n",
                          "LICENSE": "MIT\n", "README.md": "not vendored\n", "tests/test_x.py": "not vendored\n",
                          "templates/t.md": "# template\n"}.items():
            (self.source / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.source / rel).write_text(text, encoding="utf-8")
        (self.source / "bin/ratchet").chmod(0o755)
        self.target = self.tmp / "app" / "third_party" / "ratchet"

    def run_cli(self, *argv: str, source: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("ratchet.cli.SOURCE_ROOT", source or self.source):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def vendor(self, *extra: str) -> str:
        code, out, err = self.run_cli("vendor", "--into", str(self.target), *extra)
        self.assertEqual(code, 0, err)
        return out

    def marker(self) -> dict:
        return json.loads((self.target / MARKER_NAME).read_text(encoding="utf-8"))

    def check(self, *extra: str) -> tuple[int, str, str]:
        return self.run_cli("vendor", "--check", "--into", str(self.target), *extra)

    def commit_source(self) -> str:
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "add", "-A")
        git(self.source, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial")
        return git(self.source, "rev-parse", "HEAD")


class CopyTest(VendorCase):
    def test_it_copies_exactly_the_tool_and_writes_the_marker(self) -> None:
        out = self.vendor()
        copied = sorted(str(p.relative_to(self.target).as_posix()) for p in self.target.rglob("*") if p.is_file())
        self.assertEqual(copied, ["LICENSE", MARKER_NAME, "bin/ratchet", "ratchet/__init__.py", "ratchet/cli.py", "ratchet/page.html",
                                  "schemas/finding.json", "templates/t.md"])
        self.assertIn("vendor: 7 file(s)", out)
        self.assertEqual((self.target / "ratchet/cli.py").read_bytes(), (self.source / "ratchet/cli.py").read_bytes())

    def test_the_marker_has_the_commit_and_the_hash_of_the_copied_files(self) -> None:
        self.vendor()
        marker = self.marker()
        self.assertEqual(set(marker), {"source_commit", "content_sha256"})
        self.assertEqual(marker["source_commit"], UNCOMMITTED)  # the source is not a repo
        self.assertEqual(marker["content_sha256"], independent_hash(self.target))
        self.assertEqual(marker["content_sha256"], independent_hash(self.source))

    def test_the_marker_names_the_head_commit_of_a_clean_source_repo(self) -> None:
        head = self.commit_source()
        self.vendor()
        self.assertEqual(self.marker()["source_commit"], head)

    def test_the_hash_moves_with_a_byte_a_name_or_the_file_set(self) -> None:
        base = independent_hash(self.source)
        (self.source / "ratchet/cli.py").write_text("print('cli!')\n", encoding="utf-8")
        edited = independent_hash(self.source)
        (self.source / "ratchet/extra.py").write_text("", encoding="utf-8")
        added = independent_hash(self.source)
        self.assertEqual(len({base, edited, added}), 3)
        (self.source / "ratchet/extra.py").rename(self.source / "ratchet/extra2.py")
        self.assertNotEqual(independent_hash(self.source), added)

    def test_the_execute_bit_of_bin_ratchet_survives(self) -> None:
        self.vendor()
        self.assertTrue(os.access(self.target / "bin/ratchet", os.X_OK))

    def test_bytecode_and_finder_files_are_not_copied_or_hashed(self) -> None:
        (self.source / "ratchet/__pycache__").mkdir()
        (self.source / "ratchet/__pycache__/cli.cpython-312.pyc").write_bytes(b"\0")
        (self.source / "ratchet/stray.pyc").write_bytes(b"\0")
        (self.source / "schemas/.DS_Store").write_bytes(b"\0")
        self.vendor()
        self.assertEqual(self.marker()["content_sha256"], independent_hash(self.target))
        self.assertEqual([p.name for p in self.target.rglob("*") if p.suffix == ".pyc" or p.name in {"__pycache__", ".DS_Store"}], [])

    def test_it_creates_the_target_directory_and_nothing_else_outside_it(self) -> None:
        self.vendor()
        self.assertEqual(sorted(p.name for p in (self.tmp / "app").iterdir()), ["third_party"])

    def test_dry_run_writes_nothing(self) -> None:
        out = self.vendor("--dry-run")
        self.assertIn("[dry-run] vendor: 7 file(s)", out)
        self.assertIn("nothing written", out)
        self.assertFalse((self.tmp / "app").exists())

    def test_a_source_without_the_tool_in_it_is_refused(self) -> None:
        (self.source / "LICENSE").unlink()
        code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("nothing to vendor at ['LICENSE']", err)
        self.assertFalse(self.target.exists())

    def test_the_real_tree_has_everything_the_vendor_list_asks_for(self) -> None:
        plan = plan_vendor(SOURCE_ROOT)
        self.assertIn("LICENSE", plan.files)
        self.assertIn("bin/ratchet", plan.files)
        self.assertIn("ratchet/cli.py", plan.files)
        self.assertIn("ratchet/label_page.html", plan.files)
        self.assertIn("schemas/finding.v1.schema.json", plan.files)
        self.assertIn("templates/CODING-STYLE.template.md", plan.files)  # `init --template` reads it
        self.assertEqual([f for f in plan.files if f.startswith(("tests", "docs", "skills", ".claude-plugin"))], [])

    def test_a_vendored_copy_of_the_real_tool_runs_by_itself(self) -> None:
        code, _, err = self.run_cli("vendor", "--into", str(self.target), source=SOURCE_ROOT)
        self.assertEqual(code, 0, err)
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        proc = subprocess.run([sys.executable, str(self.target / "bin" / "ratchet"), "count", "--repo", str(repo)],
                              capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("0 total", proc.stdout)
        code, out, err = self.run_cli("vendor", "--check", "--into", str(self.target), source=SOURCE_ROOT)
        self.assertEqual((code, err), (0, ""), out)  # running it left a __pycache__ behind; that is not an edit


class CommitTest(VendorCase):
    def test_a_dirty_tracked_file_makes_it_uncommitted(self) -> None:
        self.commit_source()
        (self.source / "ratchet/cli.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertEqual(source_commit(self.source), UNCOMMITTED)

    def test_a_new_untracked_file_in_a_vendored_directory_makes_it_uncommitted(self) -> None:
        self.commit_source()
        (self.source / "schemas/new.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(source_commit(self.source), UNCOMMITTED)

    def test_changes_outside_the_vendored_files_do_not(self) -> None:
        head = self.commit_source()
        (self.source / "README.md").write_text("edited\n", encoding="utf-8")
        (self.source / "docs").mkdir()
        (self.source / "docs/new.md").write_text("new\n", encoding="utf-8")
        self.assertEqual(source_commit(self.source), head)

    def test_a_repo_with_no_commit_yet_is_uncommitted(self) -> None:
        git(self.source, "init", "-q", "-b", "main")
        self.assertEqual(source_commit(self.source), UNCOMMITTED)

    def test_a_source_that_is_not_in_a_repo_is_uncommitted(self) -> None:
        self.assertEqual(source_commit(self.source), UNCOMMITTED)

    def test_a_source_nested_in_another_repo_does_not_borrow_that_repos_head(self) -> None:
        outer = self.tmp / "outer"
        outer.mkdir()
        git(outer, "init", "-q", "-b", "main")
        git(outer, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "outer")
        nested = outer / "vendor" / "ratchet"
        nested.parent.mkdir()
        self.source.rename(nested)
        self.assertEqual(source_commit(nested), UNCOMMITTED)


    def test_a_clean_source_that_an_outer_repo_tracks_does_not_borrow_that_repos_head(self) -> None:
        # The files are committed in the outer repo, so nothing looks dirty: only the top-level check can refuse the outer HEAD.
        outer = self.tmp / "outer"
        nested = outer / "vendor" / "ratchet"
        nested.parent.mkdir(parents=True)
        self.source.rename(nested)
        git(outer, "init", "-q", "-b", "main")
        git(outer, "add", "-A")
        git(outer, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "outer")
        self.assertEqual(source_commit(nested), UNCOMMITTED)

    def test_an_unborn_branch_is_uncommitted_even_when_git_sees_no_change(self) -> None:
        # Every vendored path is ignored, so `git status` is empty: only the HEAD check can refuse to name a commit.
        git(self.source, "init", "-q", "-b", "main")
        (self.source / ".gitignore").write_text("ratchet/\nbin/\nschemas/\ntemplates/\nLICENSE\n", encoding="utf-8")
        self.assertEqual(source_commit(self.source), UNCOMMITTED)


class VendoredInitTest(unittest.TestCase):
    """`init --template` runs from a vendored copy of the real tool."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.target = self.tmp / "third_party" / "ratchet"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["vendor", "--into", str(self.target)]), 0, err.getvalue())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")

    def init_with_copy(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.target / "bin" / "ratchet"), "init", "--yes", "--template", "--no-hooks",
                               "--repo", str(self.repo)], capture_output=True, text=True, cwd=self.tmp)

    def test_the_copy_carries_the_template_and_init_template_works_from_it(self) -> None:
        proc = self.init_with_copy()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.repo / "CODING-STYLE.md").read_text(encoding="utf-8"),
                         (REPO_ROOT / "templates" / "CODING-STYLE.template.md").read_text(encoding="utf-8"))

    def test_a_copy_without_the_template_fails_cleanly_and_names_the_way_out(self) -> None:
        shutil.rmtree(self.target / "templates")
        proc = self.init_with_copy()
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("not in this copy of review-ratchet", proc.stderr)
        self.assertIn("--rules-file", proc.stderr)
        self.assertFalse((self.repo / ".ratchet.json").exists())  # nothing was half-written


class CheckTest(VendorCase):
    def setUp(self) -> None:
        super().setUp()
        self.vendor()

    def test_an_untouched_copy_passes(self) -> None:
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("matches VENDORED_FROM.json", out)

    def test_editing_a_file_fails_with_exit_1_and_says_so(self) -> None:
        (self.target / "ratchet/cli.py").write_text("print('hacked')\n", encoding="utf-8")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("was edited since it was vendored", err)

    def test_adding_deleting_or_renaming_a_file_counts_as_an_edit(self) -> None:
        (self.target / "ratchet/extra.py").write_text("", encoding="utf-8")
        self.assertEqual(self.check()[0], 1)
        (self.target / "ratchet/extra.py").unlink()
        self.assertEqual(self.check()[0], 0)
        (self.target / "schemas/finding.json").rename(self.target / "schemas/renamed.json")
        self.assertEqual(self.check()[0], 1)
        (self.target / "schemas/renamed.json").rename(self.target / "schemas/finding.json")
        (self.target / "LICENSE").unlink()
        self.assertEqual(self.check()[0], 1)

    def test_a_changed_license_or_launcher_counts_as_an_edit(self) -> None:
        (self.target / "LICENSE").write_text("Apache\n", encoding="utf-8")
        self.assertEqual(self.check()[0], 1)

    def test_files_next_to_the_copy_and_bytecode_inside_it_are_not_edits(self) -> None:
        (self.target / "NOTES.md").write_text("mine\n", encoding="utf-8")
        (self.target / "tests").mkdir()
        (self.target / "ratchet/__pycache__").mkdir()
        (self.target / "ratchet/__pycache__/cli.cpython-312.pyc").write_bytes(b"\0")
        (self.target / "schemas/.DS_Store").write_bytes(b"\0")
        self.assertEqual(self.check()[0], 0)

    def test_it_writes_nothing(self) -> None:
        (self.target / "ratchet/cli.py").write_text("print('hacked')\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        self.check()
        self.assertEqual({p: p.read_bytes() for p in self.target.rglob("*") if p.is_file()}, before)

    def test_the_source_moving_on_is_only_noticed_with_against_source(self) -> None:
        (self.source / "ratchet/cli.py").write_text("print('v2')\n", encoding="utf-8")
        self.assertEqual(self.check()[0], 0)
        code, _, err = self.check("--against-source")
        self.assertEqual(code, 1)
        self.assertIn("is stale", err)
        self.assertNotIn("was edited", err)

    def test_against_source_passes_while_the_source_is_unchanged(self) -> None:
        code, out, err = self.check("--against-source")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("and the current source", out)

    def test_an_edited_and_stale_copy_reports_both(self) -> None:
        (self.source / "ratchet/cli.py").write_text("print('v2')\n", encoding="utf-8")
        (self.target / "ratchet/cli.py").write_text("print('hacked')\n", encoding="utf-8")
        _, _, err = self.check("--against-source")
        self.assertIn("was edited", err)
        self.assertIn("is stale", err)

    def test_revendoring_makes_a_stale_or_edited_copy_pass_again(self) -> None:
        (self.source / "ratchet/cli.py").write_text("print('v2')\n", encoding="utf-8")
        (self.target / "ratchet/cli.py").write_text("print('hacked')\n", encoding="utf-8")
        self.vendor()
        self.assertEqual(self.check("--against-source")[0], 0)
        self.assertEqual((self.target / "ratchet/cli.py").read_text(encoding="utf-8"), "print('v2')\n")

    def test_a_directory_that_was_never_vendored_is_an_error(self) -> None:
        code, _, err = self.run_cli("vendor", "--check", "--into", str(self.tmp / "nowhere"))
        self.assertEqual(code, 1)
        self.assertIn("was not made by `ratchet vendor`", err)

    def test_a_damaged_marker_is_an_error(self) -> None:
        for text in ("{not json", "[]", json.dumps({"source_commit": "x"}), json.dumps({"source_commit": "x", "content_sha256": 5})):
            with self.subTest(text=text):
                (self.target / MARKER_NAME).write_text(text, encoding="utf-8")
                code, _, err = self.check()
                self.assertEqual(code, 1)
                self.assertIn(MARKER_NAME, err)

    def test_against_source_needs_check(self) -> None:
        code, _, err = self.run_cli("vendor", "--into", str(self.target), "--against-source")
        self.assertEqual(code, 1)
        self.assertIn("add --check", err)


class RefreshTest(VendorCase):
    def snapshot(self):
        return {p.relative_to(self.target).as_posix(): (p.read_bytes(), p.stat().st_mode)
                for p in self.target.rglob("*") if p.is_file()}

    def test_failed_copy_keeps_the_old_tool_marker_and_host_files(self):
        self.vendor()
        (self.target / "bin/host-tool").write_text("host data\n")
        before = self.snapshot()
        with mock.patch("ratchet.vendor.shutil.copy2", side_effect=OSError("disk full")):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.check()[0], 0)

    def test_failed_marker_preparation_keeps_the_old_tool(self):
        self.vendor()
        before = self.snapshot()
        with mock.patch("ratchet.vendor.write_text", side_effect=OSError("marker write failed")):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.snapshot(), before)

    def test_copy_that_no_longer_matches_the_plan_is_refused_before_replacement(self):
        self.vendor()
        before = self.snapshot()
        copy_file = shutil.copy2

        def corrupt_copy(source, destination):
            copied = copy_file(source, destination)
            if Path(source).name == "LICENSE":
                Path(destination).write_text("incomplete copy\n")
            return copied

        with mock.patch("ratchet.vendor.shutil.copy2", side_effect=corrupt_copy):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.snapshot(), before)

    def test_failed_activation_restores_every_old_entry_and_marker(self):
        self.vendor()
        before = self.snapshot()
        (self.source / "ratchet/cli.py").write_text("print('new version')\n")
        replace = os.replace

        def fail_activation(source, destination):
            if Path(destination) == self.target / "schemas" and "new" in Path(source).parts:
                raise OSError("activation failed")
            return replace(source, destination)

        with mock.patch("ratchet.vendor.os.replace", side_effect=fail_activation):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.check()[0], 0)

    def test_failed_restoration_keeps_backup_and_names_recovery_location(self):
        self.vendor()
        old_package = (self.target / "ratchet/cli.py").read_bytes()
        replace = os.replace

        def fail_activation_and_restore(source, destination):
            source, destination = Path(source), Path(destination)
            if destination == self.target / "schemas" and "new" in source.parts:
                raise OSError("activation failed")
            if destination == self.target / "ratchet" and "old" in source.parts:
                raise OSError("restore failed")
            return replace(source, destination)

        with mock.patch("ratchet.vendor.os.replace", side_effect=fail_activation_and_restore):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        recovery, = self.target.parent.glob(".ratchet-vendor-*")
        self.assertIn(str(recovery), err)
        self.assertEqual((recovery / "old/ratchet/cli.py").read_bytes(), old_package)

    def test_first_install_failure_does_not_leave_a_partial_tool(self):
        replace = os.replace

        def fail_activation(source, destination):
            if Path(destination) == self.target / "schemas":
                raise OSError("activation failed")
            return replace(source, destination)

        with mock.patch("ratchet.vendor.os.replace", side_effect=fail_activation):
            code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(list(self.target.parent.glob(".ratchet-vendor-*")), [])

    def test_vendoring_again_replaces_the_copy_and_drops_files_the_source_no_longer_has(self) -> None:
        self.vendor()
        (self.target / "ratchet/leftover.py").write_text("old\n", encoding="utf-8")
        (self.source / "ratchet/cli.py").write_text("print('v2')\n", encoding="utf-8")
        (self.source / "ratchet/page.html").unlink()
        self.vendor()
        self.assertFalse((self.target / "ratchet/leftover.py").exists())
        self.assertFalse((self.target / "ratchet/page.html").exists())
        self.assertEqual((self.target / "ratchet/cli.py").read_text(encoding="utf-8"), "print('v2')\n")
        self.assertEqual(self.marker()["content_sha256"], independent_hash(self.source))

    def test_files_of_the_hosting_repo_in_the_same_directory_are_kept(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "NOTES.md").write_text("mine\n", encoding="utf-8")
        self.vendor()
        self.vendor()
        self.assertEqual((self.target / "NOTES.md").read_text(encoding="utf-8"), "mine\n")

    def test_a_directory_with_a_ratchet_folder_of_its_own_is_refused_untouched(self) -> None:
        (self.target / "ratchet").mkdir(parents=True)
        (self.target / "ratchet/mine.py").write_text("x = 1\n", encoding="utf-8")
        code, _, err = self.run_cli("vendor", "--into", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual([p.name for p in self.target.rglob("*") if p.is_file()], ["mine.py"])

    def test_into_the_source_itself_or_a_folder_of_it_is_refused(self) -> None:
        for target in (self.source, self.source / "vendored"):
            with self.subTest(target=target):
                code, _, err = self.run_cli("vendor", "--into", str(target))
                self.assertEqual(code, 1)
                self.assertIn("inside the source checkout", err)
        self.assertFalse((self.source / "vendored").exists())
        self.assertFalse((self.source / MARKER_NAME).exists())

    def test_it_needs_no_git_repository_to_run_in(self) -> None:
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "ratchet"), "vendor", "--dry-run", "--into", str(self.target)],
                              capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[dry-run] vendor:", proc.stdout)


class TreeFilesTest(VendorCase):
    def test_missing_entries_contribute_nothing(self) -> None:
        self.assertEqual(tree_files(self.tmp / "empty"), [])

    def test_paths_are_sorted_and_slash_separated(self) -> None:
        files = tree_files(self.source)
        self.assertEqual(files, sorted(files))
        self.assertIn("ratchet/__init__.py", files)

    def test_plan_vendor_raises_ratchet_error_not_a_traceback(self) -> None:
        with self.assertRaises(RatchetError):
            plan_vendor(self.tmp / "empty")


if __name__ == "__main__":
    unittest.main()
