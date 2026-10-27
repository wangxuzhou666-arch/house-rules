import contextlib
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path)
from support import _GIT_ENV, STYLE_RULES, RepoCase, git, review_item

from ratchet.blocks import HASH_BEGIN, HASH_END, HTML_BEGIN, HTML_END, upsert_block
from ratchet.cli import main
from ratchet.errors import RatchetError
from ratchet.hooks import ExitScan, Placement, UnclearExit, hook_body, place_block, scan_for_top_level_exit
from ratchet.onboard import CLAUDE_MD_HEADING, EXECUTABLE_PATH, TEMPLATE_PATH

DIGEST = "CODING-STYLE.digest.md"
QUOTED_EXECUTABLE = shlex.quote(str(EXECUTABLE_PATH))
QUOTED_INTERPRETER = shlex.quote(sys.executable)


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class InitCase(RepoCase):
    def files(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.repo)): p.read_bytes()
                for p in sorted(self.repo.rglob("*")) if p.is_file() and ".git/objects" not in p.as_posix()
                and not p.as_posix().endswith(".git/index") and ".git/logs" not in p.as_posix()}

    def init(self, *argv: str) -> str:
        code, out, err = self.cli("init", *argv)
        self.assertEqual(code, 0, err)
        return out

    def ask(self, answers: str, *argv: str) -> tuple[int, str, str]:
        """Run init as if typed at a terminal; `answers` is one line per question."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", TtyInput(answers)):
            code = main(["init", "--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def text(self, name: str) -> str:
        return (self.repo / name).read_text(encoding="utf-8")

    def hook(self, name: str) -> Path:
        return self.repo / ".git" / "hooks" / name

    def statuses(self, out: str) -> dict[str, str]:
        """{step name: status words} from the printed report."""
        # Unanchored: at a prompt the answer's newline is typed by the user, so the next report line can follow the question text.
        pattern = (r"  (config|rules file|compile|CLAUDE\.md|hook [a-z-]+|git hooks|\.gitignore) +"
                   r"(added|updated|already ok|skipped|MANUAL|FAILED|would add|would update) ")
        return {match.group(1): match.group(2) for match in re.finditer(pattern, out)}


class FreshRepoTest(InitCase):
    def test_yes_sets_everything_up_from_the_template(self) -> None:
        out = self.init("--yes")
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": ["CODING-STYLE.md"]})
        self.assertEqual(self.text("CODING-STYLE.md"), TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertTrue((self.repo / DIGEST).is_file())
        self.assertEqual(self.text("CLAUDE.md"), f"{HTML_BEGIN}\n{CLAUDE_MD_HEADING}\n@{DIGEST}\n{HTML_END}\n")
        self.assertEqual(set(self.statuses(out).values()), {"added"})
        self.assertEqual(len(self.statuses(out)), 7)

    def test_the_digest_it_wrote_is_current(self) -> None:
        self.init("--yes")
        self.assertEqual(self.cli("compile", "--check")[0], 0)

    def test_hooks_are_executable_shell_scripts_that_record_the_absolute_executable(self) -> None:
        self.init("--yes")
        for name, command in (("pre-commit", "compile --check --staged"), ("post-commit", "infer --quiet")):
            path = self.hook(name)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"), name)
            self.assertTrue(os.access(path, os.X_OK), name)
            self.assertIn(f"RATCHET_BIN={QUOTED_EXECUTABLE}\n", text)
            self.assertIn(f"RATCHET_PY={QUOTED_INTERPRETER}\n", text)
            self.assertIn(f'"$RATCHET_PY" "$RATCHET_BIN" {command}', text)
            self.assertEqual(text.count(HASH_BEGIN), 1)
            self.assertEqual(text.count(HASH_END), 1)

    def test_the_report_says_which_python_the_hooks_run_with(self) -> None:
        out = self.init("--yes")
        self.assertIn(f"(runs with {sys.executable})", out)

    def test_the_pre_commit_hook_blocks_and_the_post_commit_hook_never_does(self) -> None:
        self.init("--yes")
        self.assertIn("|| exit 1", self.text(".git/hooks/pre-commit"))
        self.assertIn("|| true", self.text(".git/hooks/post-commit"))

    def test_gitignore_lists_the_ledger_the_label_files_and_the_saved_reviews(self) -> None:
        self.init("--yes")
        self.assertEqual(self.text(".gitignore"), "\n".join([
            HASH_BEGIN, ".style-findings.jsonl", ".ratchet-label-page.html", ".ratchet-label-batch.json",
            ".ratchet/reviews/", HASH_END, ""]))

    def test_it_ends_with_three_next_steps(self) -> None:
        out = self.init("--yes")
        steps = [line for line in out.splitlines() if line.startswith("next ")]
        self.assertEqual([line[:7] for line in steps], ["next 1:", "next 2:", "next 3:"])
        self.assertIn("review --level high", steps[0])
        self.assertIn("count --by-rule", steps[1])
        self.assertIn("label-page", steps[2])
        self.assertEqual(out.splitlines()[-1], steps[-1])

    def test_a_directory_that_is_not_a_git_repo_is_an_error(self) -> None:
        code, _, err = self.cli("init", "--yes", "--repo", str(self.tmp), with_repo=False)
        self.assertEqual(code, 1)
        self.assertIn("not a git repository", err)


class IdempotenceTest(InitCase):
    def test_a_second_run_changes_nothing_and_reports_already_ok(self) -> None:
        self.init("--yes")
        before = self.files()
        out = self.init("--yes")
        self.assertEqual(self.files(), before)
        self.assertEqual(set(self.statuses(out).values()), {"already ok"})
        self.assertEqual(len(self.statuses(out)), 7)

    def test_a_rerun_fills_only_what_was_deleted(self) -> None:
        self.init("--yes")
        (self.repo / DIGEST).unlink()
        self.hook("post-commit").unlink()
        out = self.init("--yes")
        statuses = self.statuses(out)
        self.assertEqual(statuses["compile"], "added")
        self.assertEqual(statuses["hook post-commit"], "added")
        self.assertEqual(statuses["config"], "already ok")
        self.assertEqual(statuses["hook pre-commit"], "already ok")

    def test_a_stale_digest_is_regenerated_as_an_update(self) -> None:
        self.init("--yes")
        (self.repo / DIGEST).write_text("hand edited\n", encoding="utf-8")
        self.assertEqual(self.statuses(self.init("--yes"))["compile"], "updated")
        self.assertEqual(self.cli("compile", "--check")[0], 0)

    def test_a_hook_path_that_moved_is_refreshed_in_place(self) -> None:
        self.init("--yes")
        path = self.hook("pre-commit")
        path.write_text(path.read_text(encoding="utf-8").replace(QUOTED_EXECUTABLE, "/old/place/ratchet")
                        .replace(QUOTED_INTERPRETER, "/old/python3"), encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "updated")
        self.assertIn(QUOTED_EXECUTABLE, path.read_text(encoding="utf-8"))
        self.assertIn(QUOTED_INTERPRETER, path.read_text(encoding="utf-8"))
        self.assertEqual(path.read_text(encoding="utf-8").count(HASH_BEGIN), 1)


class DryRunTest(InitCase):
    def test_dry_run_writes_nothing_and_prints_the_plan(self) -> None:
        before = self.files()
        out = self.init("--dry-run")
        self.assertEqual(self.files(), before)
        self.assertFalse(self.hook("pre-commit").exists())
        statuses = self.statuses(out)
        self.assertEqual(len(statuses), 7)
        self.assertEqual(set(statuses.values()), {"would add"})
        self.assertIn("[dry-run] nothing written", out)
        self.assertNotIn("next 1:", out)

    def test_dry_run_needs_no_yes_and_no_terminal(self) -> None:
        self.assertEqual(self.cli("init", "--dry-run")[0], 0)

    def test_dry_run_with_existing_rules_reports_the_digest_and_does_not_write_it(self) -> None:
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        out = self.init("--dry-run")
        self.assertEqual(self.statuses(out)["compile"], "would add")
        self.assertFalse((self.repo / DIGEST).exists())
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        self.assertFalse((self.repo / ".gitignore").exists())

    def test_dry_run_on_an_initialized_repo_says_already_ok(self) -> None:
        self.init("--yes")
        out = self.init("--dry-run")
        self.assertEqual(set(self.statuses(out).values()), {"already ok"})

    def test_dry_run_still_fails_on_a_bad_input(self) -> None:
        code, _, err = self.cli("init", "--dry-run", "--rules-file", str(self.tmp / "absent.md"))
        self.assertEqual(code, 1)
        self.assertIn("rules file not found", err)


class RulesFileTest(InitCase):
    def test_an_existing_rules_file_is_used_and_the_template_is_not_copied(self) -> None:
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        self.init("--yes")
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": ["docs/CODING-STYLE.md"]})
        self.assertFalse((self.repo / "CODING-STYLE.md").exists())

    def test_template_never_overwrites_an_existing_file(self) -> None:
        (self.repo / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        out = self.init("--yes", "--template")
        self.assertEqual(self.text("CODING-STYLE.md"), STYLE_RULES)
        self.assertIn("already exists; left untouched", out)

    def test_rules_file_inside_the_repo_is_stored_relative(self) -> None:
        mine = self.repo / "mine.md"
        mine.write_text(STYLE_RULES, encoding="utf-8")
        self.init("--yes", "--rules-file", str(mine))
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": ["mine.md"]})
        self.assertFalse((self.repo / "CODING-STYLE.md").exists())

    def test_rules_file_outside_the_repo_is_stored_absolute(self) -> None:
        shared = self.tmp / "shared.md"
        shared.write_text(STYLE_RULES, encoding="utf-8")
        self.init("--yes", "--rules-file", str(shared))
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": [str(shared)]})
        self.assertTrue((self.repo / DIGEST).is_file())

    def test_a_missing_rules_file_is_an_error_and_nothing_is_written(self) -> None:
        before = self.files()
        code, _, err = self.cli("init", "--yes", "--rules-file", str(self.tmp / "absent.md"))
        self.assertEqual(code, 1)
        self.assertIn("rules file not found", err)
        self.assertEqual(self.files(), before)

    def test_rules_file_and_template_exclude_each_other(self) -> None:
        with self.assertRaises(SystemExit):
            self.cli("init", "--yes", "--template", "--rules-file", "x.md")

    def test_a_rules_file_without_clauses_stops_at_compile_and_a_rerun_resumes(self) -> None:
        (self.repo / "CODING-STYLE.md").write_text("# nothing here\n", encoding="utf-8")
        code, _, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no clause definitions", err)
        (self.repo / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        self.assertEqual(self.statuses(self.init("--yes"))["config"], "already ok")


class ConfigFileTest(InitCase):
    def test_other_config_keys_are_kept_when_rules_files_is_added(self) -> None:
        self.write_config(label_page_threshold=5, repo_slug="mine")
        out = self.init("--yes")
        self.assertEqual(json.loads(self.text(".ratchet.json")),
                         {"label_page_threshold": 5, "repo_slug": "mine", "rules_files": ["CODING-STYLE.md"]})
        self.assertEqual(self.statuses(out)["config"], "updated")

    def test_a_config_with_rules_files_is_left_byte_for_byte(self) -> None:
        self.write_rules(STYLE_RULES)
        (self.repo / ".ratchet.json").write_text('{"rules_files":["rules.md"],   "scenarios": {}}', encoding="utf-8")
        self.init("--yes")
        self.assertEqual(self.text(".ratchet.json"), '{"rules_files":["rules.md"],   "scenarios": {}}')
        self.assertFalse((self.repo / "CODING-STYLE.md").exists())

    def test_a_configured_rules_file_that_is_missing_is_an_error(self) -> None:
        self.write_config(rules_files=["gone.md"])
        code, _, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("cannot read rules file", err)

    def test_a_custom_digest_path_drives_the_import_line(self) -> None:
        self.write_config(digest="docs/style.digest.md")
        (self.repo / "docs").mkdir()
        self.init("--yes")
        self.assertIn("@docs/style.digest.md", self.text("CLAUDE.md"))
        self.assertTrue((self.repo / "docs" / "style.digest.md").is_file())


class ClaudeMdTest(InitCase):
    def test_the_block_is_appended_and_the_existing_text_is_untouched(self) -> None:
        original = "# Project notes\n\nrun the tests with make\n"
        (self.repo / "CLAUDE.md").write_text(original, encoding="utf-8")
        self.init("--yes")
        self.assertEqual(self.text("CLAUDE.md"), f"{original}\n{HTML_BEGIN}\n{CLAUDE_MD_HEADING}\n@{DIGEST}\n{HTML_END}\n")

    def test_an_import_line_the_user_wrote_is_not_duplicated(self) -> None:
        original = f"# Notes\n@{DIGEST}\n"
        (self.repo / "CLAUDE.md").write_text(original, encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.text("CLAUDE.md"), original)
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "already ok")

    def test_a_block_with_another_digest_path_is_updated_in_place(self) -> None:
        original = f"top\n{HTML_BEGIN}\n@old.digest.md\n{HTML_END}\nbottom\n"
        (self.repo / "CLAUDE.md").write_text(original, encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.text("CLAUDE.md"), f"top\n{HTML_BEGIN}\n{CLAUDE_MD_HEADING}\n@{DIGEST}\n{HTML_END}\nbottom\n")
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "updated")

    def test_a_half_written_block_is_an_error_and_nothing_else_is_written_after_it(self) -> None:
        (self.repo / "CLAUDE.md").write_text(f"{HTML_BEGIN}\n@{DIGEST}\n", encoding="utf-8")
        code, _, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("CLAUDE.md", err)
        self.assertFalse(self.hook("pre-commit").exists())

    def test_no_claude_md_leaves_it_alone(self) -> None:
        out = self.init("--yes", "--no-claude-md")
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "skipped")


class HooksTest(InitCase):
    def test_an_existing_hook_keeps_its_content_and_gains_one_block(self) -> None:
        original = "#!/bin/sh\necho 'my own check'\n"
        self.hook("pre-commit").parent.mkdir(parents=True, exist_ok=True)
        self.hook("pre-commit").write_text(original, encoding="utf-8")
        self.init("--yes")
        text = self.text(".git/hooks/pre-commit")
        self.assertTrue(text.startswith(original + "\n" + HASH_BEGIN + "\n"))
        self.init("--yes")
        self.assertEqual(self.text(".git/hooks/pre-commit"), text)
        self.assertEqual(text.count(HASH_BEGIN), 1)

    def test_an_existing_hook_without_the_execute_bit_gets_it(self) -> None:
        path = self.hook("post-commit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
        path.chmod(0o644)
        self.init("--yes")
        self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_a_rerun_restores_a_lost_execute_bit(self) -> None:
        self.init("--yes")
        path = self.hook("pre-commit")
        path.chmod(0o644)
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "updated")
        self.assertTrue(os.access(path, os.X_OK))

    def test_a_hook_in_another_language_is_not_touched_and_is_reported_manual(self) -> None:
        path = self.hook("pre-commit")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = "#!/usr/bin/env python3\nprint('hi')\n"
        path.write_text(original, encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.statuses(out)["hook pre-commit"], "MANUAL")
        self.assertIn(f"{sys.executable} {EXECUTABLE_PATH} compile --check --staged", out)  # the line to add names the interpreter too
        self.assertEqual(self.statuses(out)["hook post-commit"], "added")

    def test_a_hook_without_a_shebang_is_not_touched(self) -> None:
        path = self.hook("post-commit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("echo no shebang\n", encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(path.read_text(encoding="utf-8"), "echo no shebang\n")
        self.assertEqual(self.statuses(out)["hook post-commit"], "MANUAL")

    def test_a_relative_core_hooks_path_is_respected(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        out = self.init("--yes")
        self.assertTrue((self.repo / ".githooks" / "pre-commit").is_file())
        self.assertFalse(self.hook("pre-commit").exists())
        self.assertIn("portable: safe to commit", out)

    def test_an_absolute_core_hooks_path_is_respected(self) -> None:
        shared = self.tmp / "shared-hooks"
        git(self.repo, "config", "core.hooksPath", str(shared))
        out = self.init("--yes")
        self.assertTrue((shared / "post-commit").is_file())
        self.assertNotIn("portable", out)

    def test_a_hooks_path_set_outside_the_repo_is_called_out(self) -> None:
        shared = self.tmp / "shared-hooks"
        config = self.tmp / "global.gitconfig"
        config.write_text(f"[core]\n\thooksPath = {shared}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            out = self.init("--yes")
        self.assertTrue((shared / "pre-commit").is_file())
        self.assertIn(f"core.hooksPath comes from {config}", out)
        self.assertIn("skips repos without .ratchet.json", out)

    def test_a_hooks_path_set_in_the_repo_config_is_not_called_out(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.assertNotIn("comes from", self.init("--yes"))

    def test_no_hooks_installs_none(self) -> None:
        out = self.init("--yes", "--no-hooks")
        self.assertFalse(self.hook("pre-commit").exists())
        self.assertFalse(self.hook("post-commit").exists())
        self.assertEqual(self.statuses(out)["git hooks"], "skipped")

    def test_hooks_in_a_linked_worktree_go_to_the_shared_hooks_dir(self) -> None:
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        self.commit("base")
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(linked))
        code, _, err = self.cli("init", "--yes", "--repo", str(linked), with_repo=False)
        self.assertEqual(code, 0, err)
        self.assertTrue(self.hook("pre-commit").is_file())


class HookScriptTest(unittest.TestCase):
    """Run the generated block with sh, against fake executables."""

    def run_block(self, hook: str, executable: Path, with_config: bool = True,
                  interpreter: Path = Path(sys.executable)) -> subprocess.CompletedProcess:
        script = "#!/bin/sh\n" + "\n".join(hook_body(hook, (interpreter, executable))) + "\necho reached-the-end\n"
        with support_tmpdir() as repo:
            if with_config:
                (repo / ".ratchet.json").write_text("{}", encoding="utf-8")
            return subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=repo)

    def fake_executable(self, directory: Path, exit_code: int) -> Path:
        """A Python script with an `env python3` shebang, like bin/ratchet: the hook must not depend on that line."""
        path = directory / "with space" / "ratchet"
        path.parent.mkdir(parents=True)
        path.write_text(f'#!/usr/bin/env python3\nimport sys\nprint("args:", *sys.argv[1:])\nsys.exit({exit_code})\n', encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_a_path_with_spaces_is_quoted_and_the_arguments_arrive(self) -> None:
        with support_tmpdir() as tmp:
            proc = self.run_block("post-commit", self.fake_executable(tmp, 0))
        self.assertIn("args: infer --quiet", proc.stdout)

    def test_pre_commit_stops_the_script_when_the_check_fails(self) -> None:
        with support_tmpdir() as tmp:
            proc = self.run_block("pre-commit", self.fake_executable(tmp, 1))
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("reached-the-end", proc.stdout)

    def test_pre_commit_lets_the_script_continue_when_the_check_passes_and_is_quiet(self) -> None:
        with support_tmpdir() as tmp:
            proc = self.run_block("pre-commit", self.fake_executable(tmp, 0))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("reached-the-end", proc.stdout)
        self.assertNotIn("args:", proc.stdout)

    def test_post_commit_survives_a_failing_infer(self) -> None:
        with support_tmpdir() as tmp:
            proc = self.run_block("post-commit", self.fake_executable(tmp, 3))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("reached-the-end", proc.stdout)

    def test_a_repo_without_a_config_is_left_alone_even_in_a_shared_hooks_directory(self) -> None:
        with support_tmpdir() as tmp:
            for hook in ("pre-commit", "post-commit"):
                with self.subTest(hook=hook):
                    proc = self.run_block(hook, self.fake_executable(tmp / hook, 1), with_config=False)
                    self.assertEqual(proc.returncode, 0)
                    self.assertEqual((proc.stdout.strip(), proc.stderr), ("reached-the-end", ""))

    def test_a_missing_executable_warns_loudly_and_does_not_fail(self) -> None:
        for hook in ("pre-commit", "post-commit"):
            with self.subTest(hook=hook):
                proc = self.run_block(hook, Path("/nonexistent/place/ratchet"))
                self.assertEqual(proc.returncode, 0)
                self.assertIn("reached-the-end", proc.stdout)
                self.assertIn("WARNING", proc.stderr)
                self.assertIn("/nonexistent/place/ratchet", proc.stderr)
                self.assertIn(f"{hook} is NOT running", proc.stderr)

    def test_a_missing_interpreter_warns_loudly_and_does_not_fail(self) -> None:
        for hook in ("pre-commit", "post-commit"):
            with self.subTest(hook=hook), support_tmpdir() as tmp:
                proc = self.run_block(hook, self.fake_executable(tmp, 1), interpreter=Path("/nonexistent/python3.99"))
                self.assertEqual(proc.returncode, 0)
                self.assertIn("reached-the-end", proc.stdout)
                self.assertNotIn("args:", proc.stdout)
                self.assertIn("WARNING", proc.stderr)
                self.assertIn("/nonexistent/python3.99", proc.stderr)
                self.assertIn(f"{hook} is NOT running", proc.stderr)

    def test_an_interpreter_path_with_spaces_is_quoted(self) -> None:
        with support_tmpdir() as tmp:
            wrapper = tmp / "py dir" / "python 3"
            wrapper.parent.mkdir()
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            proc = self.run_block("post-commit", self.fake_executable(tmp, 0), interpreter=wrapper)
        self.assertIn("args: infer --quiet", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_the_hook_uses_the_pinned_interpreter_not_whatever_python3_is_on_the_path(self) -> None:
        with support_tmpdir() as tmp:
            decoy = tmp / "bin" / "python3"
            decoy.parent.mkdir()
            decoy.write_text("#!/bin/sh\necho decoy-python3-ran\nexit 7\n", encoding="utf-8")
            decoy.chmod(0o755)
            script = "#!/bin/sh\n" + "\n".join(hook_body("post-commit", (Path(sys.executable), self.fake_executable(tmp, 0)))) + "\n"
            (tmp / ".ratchet.json").write_text("{}", encoding="utf-8")
            proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=tmp,
                                  env={**os.environ, "PATH": f"{decoy.parent}:{os.environ['PATH']}"})
        self.assertNotIn("decoy", proc.stdout)
        self.assertIn("args: infer --quiet", proc.stdout)

    def test_pre_commit_still_blocks_when_the_pinned_interpreter_runs_and_the_check_fails(self) -> None:
        with support_tmpdir() as tmp:
            proc = self.run_block("pre-commit", self.fake_executable(tmp, 1), interpreter=Path(sys.executable))
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("WARNING", proc.stderr)


@contextlib.contextmanager
def support_tmpdir():
    import tempfile
    with tempfile.TemporaryDirectory() as name:
        yield Path(os.path.realpath(name))


class GitignoreTest(InitCase):
    def test_an_entry_already_listed_outside_the_block_is_not_repeated(self) -> None:
        (self.repo / ".gitignore").write_text("node_modules/\n.style-findings.jsonl\n", encoding="utf-8")
        self.init("--yes")
        text = self.text(".gitignore")
        self.assertEqual(text.count(".style-findings.jsonl"), 1)
        self.assertIn(".ratchet-label-page.html", text)
        self.assertTrue(text.startswith("node_modules/\n.style-findings.jsonl\n\n" + HASH_BEGIN))

    def test_a_root_anchored_entry_counts_as_listed(self) -> None:
        (self.repo / ".gitignore").write_text("/.style-findings.jsonl\n", encoding="utf-8")
        self.init("--yes")
        self.assertEqual(self.text(".gitignore").count(".style-findings.jsonl"), 1)

    def test_everything_already_listed_means_no_block(self) -> None:
        listed = "\n".join([".style-findings.jsonl", ".ratchet-label-page.html", ".ratchet-label-batch.json", ".ratchet/reviews/", ""])
        (self.repo / ".gitignore").write_text(listed, encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.text(".gitignore"), listed)
        self.assertEqual(self.statuses(out)[".gitignore"], "already ok")

    def test_a_custom_ledger_path_moves_the_label_files_with_it(self) -> None:
        self.write_config(ledger="data/ledger.jsonl")
        self.init("--yes")
        lines = self.text(".gitignore").splitlines()
        for entry in ("data/ledger.jsonl", "data/.ratchet-label-page.html", "data/.ratchet-label-batch.json"):
            self.assertIn(entry, lines)
        self.assertNotIn(".style-findings.jsonl", lines)

    def test_a_ledger_outside_the_repo_is_not_listed(self) -> None:
        self.write_config(ledger=str(self.tmp / "ledger.jsonl"))
        self.init("--yes")
        self.assertEqual(self.text(".gitignore").splitlines(), [HASH_BEGIN, ".ratchet/reviews/", HASH_END])

    def test_a_half_written_block_is_an_error(self) -> None:
        (self.repo / ".gitignore").write_text(f"{HASH_END}\n{HASH_BEGIN}\n", encoding="utf-8")
        code, _, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(".gitignore", err)


class InteractiveTest(InitCase):
    def test_without_yes_and_without_a_terminal_it_refuses_and_writes_nothing(self) -> None:
        before = self.files()
        code, _, err = self.cli("init")
        self.assertEqual(code, 1)
        self.assertIn("stdin is not a terminal", err)
        self.assertIn("--yes", err)
        self.assertEqual(self.files(), before)

    def test_pressing_enter_everywhere_equals_yes(self) -> None:
        code, out, err = self.ask("\n\n\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(set(self.statuses(out).values()), {"added"})
        self.assertTrue(self.hook("pre-commit").is_file())

    def test_the_default_offered_is_an_existing_rules_file(self) -> None:
        (self.repo / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        _, out, _ = self.ask("\n\n\n")
        self.assertIn("[CODING-STYLE.md]", out)
        self.assertEqual(self.text("CODING-STYLE.md"), STYLE_RULES)

    def test_typing_a_path_uses_that_file(self) -> None:
        mine = self.repo / "mine.md"
        mine.write_text(STYLE_RULES, encoding="utf-8")
        code, _, err = self.ask(f"{mine}\n\n\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": ["mine.md"]})

    def test_typing_template_copies_the_template_even_when_a_file_is_detected(self) -> None:
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        self.ask("template\n\n\n")
        self.assertEqual(self.text("CODING-STYLE.md"), TEMPLATE_PATH.read_text(encoding="utf-8"))

    def test_declining_claude_md_skips_it(self) -> None:
        code, out, _ = self.ask("\nn\n\n")
        self.assertEqual(code, 0)
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "skipped")
        self.assertTrue(self.hook("pre-commit").is_file())

    def test_declining_the_hooks_skips_both(self) -> None:
        code, out, _ = self.ask("\n\nn\n")
        self.assertEqual(code, 0)
        self.assertFalse(self.hook("pre-commit").exists())
        self.assertFalse(self.hook("post-commit").exists())
        self.assertEqual(self.statuses(out)["git hooks"], "skipped")

    def test_an_initialized_repo_asks_nothing(self) -> None:
        self.init("--yes")
        code, _, err = self.ask("")  # no answers available: any question would be an error
        self.assertEqual(code, 0, err)

    def test_running_out_of_answers_is_an_error(self) -> None:
        code, _, err = self.ask("")
        self.assertEqual(code, 1)
        self.assertIn("end of input", err)


class UpsertBlockTest(unittest.TestCase):
    def test_add_to_empty_text(self) -> None:
        self.assertEqual(upsert_block("", "<b>", "<e>", ["x"], "f"), ("<b>\nx\n<e>\n", "add"))

    def test_add_keeps_the_text_and_separates_with_one_blank_line(self) -> None:
        self.assertEqual(upsert_block("a\n", "<b>", "<e>", ["x"], "f"), ("a\n\n<b>\nx\n<e>\n", "add"))
        self.assertEqual(upsert_block("a\n\n", "<b>", "<e>", ["x"], "f"), ("a\n\n<b>\nx\n<e>\n", "add"))
        self.assertEqual(upsert_block("a", "<b>", "<e>", ["x"], "f"), ("a\n\n<b>\nx\n<e>\n", "add"))

    def test_same_block_is_ok_and_a_different_body_updates_in_place(self) -> None:
        text = "top\n<b>\nx\n<e>\nbottom\n"
        self.assertEqual(upsert_block(text, "<b>", "<e>", ["x"], "f"), (text, "ok"))
        self.assertEqual(upsert_block(text, "<b>", "<e>", ["y", "z"], "f"), ("top\n<b>\ny\nz\n<e>\nbottom\n", "update"))

    def test_crlf_lines_outside_the_block_survive(self) -> None:
        text = "top\r\n<b>\r\nx\r\n<e>\r\nbottom\r\n"
        new_text, status = upsert_block(text, "<b>", "<e>", ["y"], "f")
        self.assertEqual(status, "update")
        self.assertTrue(new_text.startswith("top\r\n"))
        self.assertTrue(new_text.endswith("<e>\nbottom\r\n"))

    def test_marker_problems_are_errors_that_name_the_file(self) -> None:
        for text in ("<b>\nx\n", "x\n<e>\n", "<e>\n<b>\n", "<b>\n<b>\n<e>\n", "<b>\n<e>\n<e>\n"):
            with self.subTest(text=text), self.assertRaisesRegex(RatchetError, "^CLAUDE.md: expected exactly one"):
                upsert_block(text, "<b>", "<e>", ["x"], "CLAUDE.md")


class EndToEndTest(InitCase):
    """Real git, real hooks, the real executable."""

    def git_commit(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", *args], cwd=self.repo,
                              capture_output=True, text=True, env=_GIT_ENV)

    def test_a_commit_with_a_fixes_trailer_closes_the_finding_through_the_post_commit_hook(self) -> None:
        self.init("--yes")
        self.cli_ok("ingest-code-review", "--source", "l1", "--run", "R1", stdin=json.dumps([review_item(category="p0e-1")]))
        self.assertIn("1 open", self.cli_ok("count"))
        git(self.repo, "add", "-A")
        committed = self.git_commit("-m", "fix: narrow the guarded block", "-m", "Fixes-Finding: R1-1")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertIn("infer: +1 fixed (auto:commit), +0 fixed (auto:gate_pass); open 0", committed.stderr)  # git sends a hook's stdout to stderr
        self.assertIn("0 open", self.cli_ok("count"))

    def test_a_commit_with_nothing_to_close_is_silent_from_both_hooks(self) -> None:
        self.init("--yes")
        git(self.repo, "add", "-A")
        committed = self.git_commit("-m", "chore: first commit")
        self.assertEqual((committed.returncode, committed.stdout, committed.stderr), (0, "", ""))

    def test_a_trailer_naming_no_finding_warns_but_the_commit_goes_through(self) -> None:
        self.init("--yes")
        git(self.repo, "add", "-A")
        committed = self.git_commit("-m", "fix: x", "-m", "Fixes-Finding: R9-009")
        self.assertEqual(committed.returncode, 0)
        self.assertIn("matches no finding", committed.stderr)

    def test_editing_the_rules_without_the_digest_is_stopped_by_the_pre_commit_hook(self) -> None:
        self.init("--yes")
        git(self.repo, "add", "-A")
        self.assertEqual(self.git_commit("-m", "chore: initial").returncode, 0)
        (self.repo / "CODING-STYLE.md").write_text(self.text("CODING-STYLE.md") + "\n", encoding="utf-8")
        git(self.repo, "add", "CODING-STYLE.md")
        blocked = self.git_commit("-m", "docs: touch the rules")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn(f"stale: {DIGEST}", blocked.stderr)
        self.cli_ok("compile")
        git(self.repo, "add", DIGEST)
        self.assertEqual(self.git_commit("-m", "docs: touch the rules").returncode, 0)


class HookScanTest(unittest.TestCase):
    """Which line of a shell script ends it at top level. Only a column-0 `exit` / `exec cmd` outside every group counts."""

    def scan(self, text: str) -> ExitScan:
        return scan_for_top_level_exit(text.splitlines())

    def test_the_forms_of_a_final_exit(self) -> None:
        for last in ("exit", "exit 0", "exit 1", "exit $rc", 'exit "$status"', "exit $?", "exit 0  # done", "exit;", "exit 0 || true",
                     "exec ./real-hook", 'exec /usr/bin/env sh "$@"', "exec -a name cmd"):
            with self.subTest(last=last):
                self.assertEqual(self.scan(f"#!/bin/sh\necho a\n{last}\n"), ExitScan(2, None))

    def test_lines_that_only_look_like_an_exit(self) -> None:
        for line in ("  exit 0", "\texit 0", "echo exit", "# exit 0", "exitcode=1", "exit_now() { :; }", "exec >/dev/null 2>&1", "exec 3<input",
                     "exec 3>&1", "exec", "trap cleanup EXIT", '[ -z "$x" ] && exit 0', "true || exit 1", "echo a; exit 1"):
            with self.subTest(line=line):
                self.assertEqual(self.scan(f"#!/bin/sh\n{line}\n"), ExitScan(None, None))

    def test_an_exit_inside_a_group_is_not_top_level_even_at_column_0(self) -> None:
        groups = {
            "function": "f() {\nexit 1\n}",
            "function keyword": "function f {\nexit 1\n}",
            "brace group": "{\nexit 1\n}",
            "if": "if true; then\nexit 1\nfi",
            "if else": "if true; then\n:\nelif false; then\n:\nelse\nexit 1\nfi",
            "for": "for f in a b; do\nexit 1\ndone",
            "while": "while true; do\nexit 1\ndone",
            "until": "until false; do\nexit 1\ndone",
            "case": 'case "$x" in\n  a) echo a ;;\n  *)\nexit 1\n  ;;\nesac',
            "nested": "f() {\nif true; then\nfor i in 1; do\nexit 1\ndone\nfi\n}",
        }
        for label, text in groups.items():
            with self.subTest(label):
                self.assertEqual(self.scan(f"#!/bin/sh\n{text}\n"), ExitScan(None, None))

    def test_an_exit_after_a_closed_group_is_top_level(self) -> None:
        closed = {
            "function": "f() {\n  exit 1\n}",
            "if": "if true; then\n  echo a\nfi",
            "for": "for f in a b; do\n  echo $f\ndone",
            "case": 'case "$x" in\n  a) echo a ;;\nesac',
            "one-line if": "if true; then exit 1; fi",
            "one-line loop": "for f in a b; do echo $f; done",
            "one-line brace group": "[ -f x ] && { echo a; }",
            "one-line function": "f() { exit 1; }",
            "loop with a redirect": "while read -r line; do\n  echo $line\ndone < /dev/null",
        }
        for label, text in closed.items():
            with self.subTest(label):
                lines = text.count("\n") + 1
                self.assertEqual(self.scan(f"{text}\nexit 0\n"), ExitScan(lines, None))

    def test_keywords_in_strings_comments_and_arguments_are_not_groups(self) -> None:
        for line in ('echo "if"', "echo 'case'", "echo if", "echo fi", "echo done", 'echo "fi"', "# if", "echo a # fi", "x=${#y}", "n=$((1+2))",
                     "echo $(echo fi)", "echo `echo if`", "echo it\\'s", "# don't"):
            with self.subTest(line=line):
                self.assertEqual(self.scan(f"{line}\nexit 0\n"), ExitScan(1, None))

    def test_a_here_document_makes_a_later_exit_a_guess(self) -> None:
        scan = self.scan("cat <<EOF >/dev/null\nnote\nEOF\nexit 0\n")
        self.assertEqual(scan, ExitScan(3, "a here-document follows"))

    def test_other_text_the_scan_cannot_follow_makes_a_later_exit_a_guess(self) -> None:
        unreadable = {
            "a string over several lines": ('echo "a\nb"\nexit 0\n', "a quoted string continues on the next line"),
            "a substitution over several lines": ("x=$(\n  date\n)\nexit 0\n", "a command substitution continues on the next line"),
            "a line continuation": ("echo a \\\n  b\nexit 0\n", "a line continuation follows"),
            "a subshell over several lines": ("(\n  cd x\n)\nexit 0\n", "a subshell spans lines"),
            "a closing keyword with no opener": ("fi\nexit 0\n", "a closing keyword has no opener"),
        }
        for label, (text, reason) in unreadable.items():
            with self.subTest(label):
                scan = self.scan(text)
                self.assertEqual(scan.doubt, reason)
                self.assertIsNotNone(scan.index)

    def test_an_exit_before_the_unreadable_part_is_certain(self) -> None:
        self.assertEqual(self.scan("echo a\nexit 0\ncat <<EOF\nx\nEOF\n"), ExitScan(1, None))

    def test_unreadable_text_with_no_exit_after_it_is_nothing(self) -> None:
        self.assertEqual(self.scan("cat <<EOF >/dev/null\nx\nEOF\necho done\n"), ExitScan(None, None))


class PlaceBlockTest(unittest.TestCase):
    BODY = ["echo block"]

    def place(self, base: str) -> Placement:
        return place_block(base, self.BODY, "pre-commit")

    def block(self) -> str:
        return f"{HASH_BEGIN}\necho block\n{HASH_END}\n"

    def test_no_exit_appends_after_a_blank_line(self) -> None:
        self.assertEqual(self.place("#!/bin/sh\necho a\n"), Placement(f"#!/bin/sh\necho a\n\n{self.block()}", "add"))

    def test_a_final_exit_gets_the_block_before_it(self) -> None:
        self.assertEqual(self.place("#!/bin/sh\necho a\nexit 0\n"), Placement(f"#!/bin/sh\necho a\n\n{self.block()}\nexit 0\n", "add"))

    def test_a_blank_line_above_the_exit_is_reused(self) -> None:
        self.assertEqual(self.place("#!/bin/sh\necho a\n\nexit 0\n"), Placement(f"#!/bin/sh\necho a\n\n{self.block()}\nexit 0\n", "add"))

    def test_the_block_alone_before_the_exit_is_ok_and_stays(self) -> None:
        text = f"#!/bin/sh\necho a\n\n{self.block()}\nexit 0\n"
        self.assertEqual(self.place(text), Placement(text, "ok"))

    def test_a_block_before_the_exit_is_refreshed_in_place(self) -> None:
        text = f"#!/bin/sh\n{HASH_BEGIN}\nold\n{HASH_END}\nexit 0\n"
        self.assertEqual(self.place(text), Placement(f"#!/bin/sh\n{self.block()}exit 0\n", "update"))

    def test_a_block_after_the_exit_moves_before_it_and_leaves_no_stray_blank_line(self) -> None:
        text = f"#!/bin/sh\necho a\nexit 0\n\n{HASH_BEGIN}\nold\n{HASH_END}\n"
        self.assertEqual(self.place(text), Placement(f"#!/bin/sh\necho a\n\n{self.block()}\nexit 0\n", "update"))

    def test_a_block_after_the_exit_with_text_after_it_keeps_that_text(self) -> None:
        text = f"#!/bin/sh\nexit 0\n\n{HASH_BEGIN}\nold\n{HASH_END}\n\n# tail\n"
        self.assertEqual(self.place(text).text, f"#!/bin/sh\n\n{self.block()}\nexit 0\n\n# tail\n")

    def test_an_exit_inside_a_function_is_ignored(self) -> None:
        text = "#!/bin/sh\ndie() {\nexit 1\n}\necho a\n"
        self.assertEqual(self.place(text), Placement(f"{text}\n{self.block()}", "add"))

    def test_an_exit_that_cannot_be_placed_safely_raises_with_the_line(self) -> None:
        with self.assertRaises(UnclearExit) as caught:
            self.place("#!/bin/sh\ncat <<EOF >/dev/null\nx\nEOF\nexit 0\n")
        self.assertEqual((caught.exception.index, caught.exception.line, caught.exception.doubt), (4, "exit 0", "a here-document follows"))

    def test_a_block_that_precedes_an_unclear_exit_is_left_alone(self) -> None:
        text = f"#!/bin/sh\n{self.block()}cat <<EOF >/dev/null\nx\nEOF\nexit 0\n"
        self.assertEqual(self.place(text), Placement(text, "ok"))

    def test_a_block_after_an_unclear_exit_raises(self) -> None:
        with self.assertRaises(UnclearExit):
            self.place(f"#!/bin/sh\ncat <<EOF >/dev/null\nx\nEOF\nexit 0\n{self.block()}")

    def test_crlf_lines_outside_the_block_survive(self) -> None:
        placed = self.place("#!/bin/sh\r\necho a\r\nexit 0\r\n")
        self.assertTrue(placed.text.startswith("#!/bin/sh\r\necho a\r\n"))
        self.assertTrue(placed.text.endswith("exit 0\r\n"))

    def test_a_file_without_a_final_newline_after_the_exit(self) -> None:
        self.assertEqual(self.place("#!/bin/sh\nexit 0").text, f"#!/bin/sh\n\n{self.block()}\nexit 0")


class HookPlacementTest(InitCase):
    """init on a hook script that already ends with an exit: the block must be somewhere the hook reaches."""

    def install(self, text: str, hook: str = "pre-commit") -> Path:
        path = self.hook(hook)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def probe(self, path: Path) -> list[str]:
        proc = subprocess.run([str(path)], cwd=self.repo, capture_output=True, text=True, env={**os.environ, "RATCHET_HOOK_PROBE": "1"})
        return proc.stdout.splitlines()

    def test_a_final_exit_0_gets_the_block_before_it_and_the_guard_runs(self) -> None:
        path = self.install("#!/bin/sh\necho mine\nexit 0\n")
        out = self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "added")
        self.assertTrue(text.startswith(f"#!/bin/sh\necho mine\n\n{HASH_BEGIN}\n"))
        self.assertTrue(text.endswith(f"{HASH_END}\n\nexit 0\n"))
        self.assertIn("ratchet-hook-probe pre-commit", self.probe(path))

    def test_it_is_idempotent_and_a_rerun_says_already_ok(self) -> None:
        path = self.install("#!/bin/sh\necho mine\nexit 0\n")
        self.init("--yes")
        before = path.read_text(encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "already ok")
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_final_exit_with_a_variable(self) -> None:
        path = self.install("#!/bin/sh\nrc=0\necho mine\nexit $rc\n")
        self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index(HASH_BEGIN), text.index("exit $rc"))
        self.assertIn("ratchet-hook-probe pre-commit", self.probe(path))

    def test_a_top_level_exec_ends_the_script_too(self) -> None:
        path = self.install("#!/bin/sh\necho mine\nexec ./real-hook\n")
        self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index(HASH_BEGIN), text.index("exec ./real-hook"))

    def test_an_exec_that_only_redirects_does_not(self) -> None:
        original = "#!/bin/sh\nexec 3>&1\necho mine\n"
        path = self.install(original)
        self.init("--yes")
        self.assertTrue(path.read_text(encoding="utf-8").startswith(original + "\n" + HASH_BEGIN))

    def test_an_exit_inside_a_function_does_not_count(self) -> None:
        for label, original in (("indented", '#!/bin/sh\ndie() {\n  echo "$1" >&2\n  exit 1\n}\necho mine\n'),
                                ("column 0", '#!/bin/sh\ndie() {\necho "$1" >&2\nexit 1\n}\necho mine\n')):
            with self.subTest(label):
                path = self.install(original)
                self.init("--yes")
                self.assertTrue(path.read_text(encoding="utf-8").startswith(original + "\n" + HASH_BEGIN))
                path.unlink()

    def test_an_exit_inside_an_if_and_a_guard_do_not_count(self) -> None:
        original = '#!/bin/sh\nif [ -n "$SKIP" ]; then\nexit 0\nfi\n[ -n "$X" ] && exit 0\necho mine\n'
        path = self.install(original)
        self.init("--yes")
        self.assertTrue(path.read_text(encoding="utf-8").startswith(original + "\n" + HASH_BEGIN))

    def test_a_block_an_earlier_version_appended_after_the_exit_is_moved_before_it(self) -> None:
        old_block = "\n".join([HASH_BEGIN, *hook_body("pre-commit", (Path("/old/python3"), Path("/old/ratchet"))), HASH_END])
        path = self.install(f"#!/bin/sh\necho mine\nexit 0\n\n{old_block}\n")
        out = self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "updated")
        self.assertLess(text.index(HASH_BEGIN), text.index("\nexit 0"))
        self.assertEqual(text.count(HASH_BEGIN), 1)
        self.assertNotIn("/old/python3", text)
        self.assertTrue(text.startswith("#!/bin/sh\necho mine\n\n" + HASH_BEGIN))
        self.assertTrue(text.endswith(f"{HASH_END}\n\nexit 0\n"))  # no blank line left where the old block was
        self.assertEqual(self.statuses(self.init("--yes"))["hook pre-commit"], "already ok")

    def test_a_set_e_script_with_a_final_exit(self) -> None:
        path = self.install("#!/bin/sh\nset -e\necho mine\nexit 0\n")
        self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index(HASH_BEGIN), text.index("exit 0"))
        self.assertIn("ratchet-hook-probe pre-commit", self.probe(path))

    def test_a_script_the_scan_cannot_read_is_left_alone_and_the_output_says_what_to_paste_where(self) -> None:
        original = "#!/bin/sh\ncat <<EOF >/dev/null\nnote\nEOF\nexit 0\n"
        path = self.install(original)
        out = self.init("--yes")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.statuses(out)["hook pre-commit"], "MANUAL")
        self.assertEqual(self.statuses(out)["hook post-commit"], "added")
        self.assertIn("line 5 (`exit 0`)", out)
        self.assertIn("a here-document follows", out)
        self.assertIn("Paste this block above that line", out)
        lines = out.splitlines()
        pasted = [line[10:] for line in lines[lines.index(f"{' ' * 10}{HASH_BEGIN}"):lines.index(f"{' ' * 10}{HASH_END}") + 1]]
        self.assertEqual(pasted[0], HASH_BEGIN)
        self.assertIn("if [ -f .ratchet.json ]; then", pasted)
        # Doing what it says makes the next run pass: the pasted block sits before the exit and answers the probe.
        path.write_text(original.replace("exit 0\n", "\n".join(pasted) + "\nexit 0\n"), encoding="utf-8")
        rerun = self.init("--yes")
        self.assertEqual(self.statuses(rerun)["hook pre-commit"], "already ok")

    def test_a_multi_line_string_is_unreadable_too(self) -> None:
        original = '#!/bin/sh\necho "first\nsecond"\nexit 0\n'
        path = self.install(original)
        out = self.init("--yes")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.statuses(out)["hook pre-commit"], "MANUAL")
        self.assertIn("a quoted string continues on the next line", out)

    def test_unreadable_text_with_no_exit_after_it_still_appends(self) -> None:
        original = "#!/bin/sh\ncat <<EOF >/dev/null\nnote\nEOF\n"
        path = self.install(original)
        self.init("--yes")
        self.assertTrue(path.read_text(encoding="utf-8").startswith(original + "\n" + HASH_BEGIN))


class HookProbeTest(InitCase):
    """The live check: every installed block is run once, and a hook that never reaches it fails init."""

    def install(self, text: str, hook: str = "pre-commit") -> Path:
        path = self.hook(hook)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def fails(self, *argv: str) -> tuple[str, str]:
        code, out, err = self.cli("init", "--yes", *argv)
        self.assertEqual(code, 1, out)
        return out, err

    def test_a_block_the_hook_never_reaches_fails_init_loudly(self) -> None:
        # No top-level exit for the scan to find: the exit is inside a function that the script calls.
        path = self.install('#!/bin/sh\nmain() {\n  echo mine\n  exit 0\n}\nmain "$@"\n')
        out, err = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")
        self.assertIn("never reached", out)
        self.assertIn(f"Move the `{HASH_BEGIN}` ... `{HASH_END}` block above that point", out)
        self.assertIn("mine", out)  # the hook's own output is quoted back
        self.assertIn("FAILED step(s): hook pre-commit", err)
        self.assertEqual(self.statuses(out)["hook post-commit"], "added")  # the other hook is still installed
        self.assertEqual(self.statuses(out)[".gitignore"], "added")  # and so are the steps after it
        self.assertEqual(path.read_text(encoding="utf-8").count(HASH_BEGIN), 1)

    def test_a_rerun_does_not_turn_the_failure_into_already_ok(self) -> None:
        self.install("#!/bin/sh\nmain() {\n  exit 0\n}\nmain\n")
        self.fails()
        out, _ = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")

    def test_a_failed_run_prints_no_next_steps(self) -> None:
        self.install("#!/bin/sh\nmain() {\n  exit 0\n}\nmain\n")
        out, _ = self.fails()
        self.assertNotIn("next 1:", out)
        self.assertNotIn("review-ratchet is set up", out)
        self.assertNotIn("commit the files", out)

    def test_a_block_placed_after_an_exit_by_hand_is_caught(self) -> None:
        # The scan only trusts column 0, so a user who indents an exit above the block defeats it; the live check does not care.
        path = self.install("#!/bin/sh\necho mine\n")
        self.init("--yes")
        path.write_text(path.read_text(encoding="utf-8").replace("echo mine\n", "echo mine\n  exit 0\n", 1), encoding="utf-8")
        out, _ = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")
        self.assertIn("status 0", out)

    def test_a_hook_that_exits_early_when_nothing_is_staged_fails_and_moving_the_block_up_fixes_it(self) -> None:
        # The check runs with nothing staged, so this script leaves before the block: the message says where the block belongs.
        user_part = '[ -z "$(git diff --cached --name-only)" ] && exit 0\necho work\n'
        path = self.install("#!/bin/sh\n" + user_part)
        out, _ = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")
        text = path.read_text(encoding="utf-8")
        block = text[text.index(HASH_BEGIN):text.index(HASH_END) + len(HASH_END)]
        path.write_text(f"#!/bin/sh\n{block}\n{user_part}", encoding="utf-8")
        self.assertEqual(self.statuses(self.init("--yes"))["hook pre-commit"], "already ok")

    def test_the_user_part_of_the_hook_runs_with_nothing_staged_and_cannot_change_the_real_index(self) -> None:
        (self.repo / "x.txt").write_text("x", encoding="utf-8")
        git(self.repo, "add", "x.txt")
        self.install('#!/bin/sh\ngit diff --cached --name-only > "$PWD/seen-by-hook.txt"\ngit add -A\n')
        self.init("--yes")
        self.assertEqual((self.repo / "seen-by-hook.txt").read_text(encoding="utf-8"), "")
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "x.txt")

    def test_with_a_commit_the_probe_index_matches_head(self) -> None:
        (self.repo / "tracked.txt").write_text("a", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        self.commit("base")
        (self.repo / "tracked.txt").write_text("b", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        self.install('#!/bin/sh\ngit diff --cached --name-only > "$PWD/seen-by-hook.txt"\n')
        self.init("--yes")
        self.assertEqual((self.repo / "seen-by-hook.txt").read_text(encoding="utf-8"), "")  # not "tracked.txt", and not every file deleted
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "tracked.txt")

    def test_a_dry_run_never_runs_the_hook_and_a_real_run_runs_it_once(self) -> None:
        self.install('#!/bin/sh\necho x >> "$PWD/ran.txt"\n')
        self.init("--dry-run")
        self.assertFalse((self.repo / "ran.txt").exists())
        self.init("--yes")
        self.assertEqual((self.repo / "ran.txt").read_text(encoding="utf-8"), "x\n")

    def test_a_hook_that_answers_with_a_failing_status_is_not_a_pass(self) -> None:
        self.install("#!/bin/sh\necho ratchet-hook-probe pre-commit\n  exit 3\n")  # indented, so not top-level: the block lands after it; the token alone is not enough
        out, _ = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")
        self.assertIn("status 3", out)

    def test_a_hook_that_takes_too_long_before_the_block_fails_with_the_time_limit(self) -> None:
        self.install("#!/bin/sh\n  exec sleep 30\n")
        with mock.patch("ratchet.hooks.PROBE_TIMEOUT_SECONDS", 1):
            out, _ = self.fails()
        self.assertEqual(self.statuses(out)["hook pre-commit"], "FAILED")
        self.assertIn("did not finish within 1s", out)

    def test_a_script_in_another_language_is_never_run(self) -> None:
        self.install("#!/usr/bin/env python3\nopen('ran.txt', 'w').write('x')\n")
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "MANUAL")
        self.assertFalse((self.repo / "ran.txt").exists())

    def test_the_probe_variable_and_token_are_in_the_block(self) -> None:
        self.init("--yes")
        text = self.text(".git/hooks/pre-commit")
        self.assertIn('if [ -n "${RATCHET_HOOK_PROBE:-}" ]; then', text)
        self.assertIn('echo "ratchet-hook-probe pre-commit"', text)


class PortableHookTest(InitCase):
    """A hooks directory inside the work tree is committed, so its block holds no path of this machine."""

    def setUp(self) -> None:
        super().setUp()
        git(self.repo, "config", "core.hooksPath", ".githooks")

    def hook_texts(self) -> dict[str, str]:
        return {name: self.text(f".githooks/{name}") for name in ("pre-commit", "post-commit")}

    def as_machine(self, machine: str) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("ratchet.onboard.sys", SimpleNamespace(executable=f"/{machine}/bin/python3.12")))
        stack.enter_context(mock.patch("ratchet.onboard.EXECUTABLE_PATH", Path(f"/{machine}/review-ratchet/bin/ratchet")))
        return stack

    def test_the_block_holds_no_path_of_this_machine(self) -> None:
        self.init("--yes")
        for name, text in self.hook_texts().items():
            with self.subTest(name):
                for machine_specific in (sys.executable, str(EXECUTABLE_PATH), str(EXECUTABLE_PATH.parent), str(self.repo), str(self.tmp)):
                    self.assertNotIn(machine_specific, text)
                self.assertNotIn("RATCHET_PY=/", text)
                self.assertNotIn("RATCHET_BIN=/", text)

    def test_the_report_calls_it_portable_and_does_not_say_to_keep_it_out_of_git(self) -> None:
        out = self.init("--yes")
        self.assertIn("portable: safe to commit", out)
        self.assertNotIn("do not commit", out)
        self.assertNotIn("records this machine", out)

    def test_a_rerun_on_another_machine_changes_no_tracked_hook_file(self) -> None:
        with self.as_machine("machine-a"):
            self.init("--yes")
        before = self.hook_texts()
        with self.as_machine("machine-b"):
            out = self.init("--yes")
        self.assertEqual(self.hook_texts(), before)
        self.assertEqual(self.statuses(out)["hook pre-commit"], "already ok")
        self.assertEqual(self.statuses(out)["hook post-commit"], "already ok")

    def test_the_same_two_machines_do_change_a_pinned_hook(self) -> None:
        git(self.repo, "config", "--unset", "core.hooksPath")  # control: .git/hooks is not tracked, so it keeps the absolute paths
        with self.as_machine("machine-a"), mock.patch("ratchet.onboard.probe_hook", return_value=None):  # those paths do not exist
            self.init("--yes")
        first = self.text(".git/hooks/pre-commit")
        with self.as_machine("machine-b"), mock.patch("ratchet.onboard.probe_hook", return_value=None):
            out = self.init("--yes")
        self.assertNotEqual(self.text(".git/hooks/pre-commit"), first)
        self.assertEqual(self.statuses(out)["hook pre-commit"], "updated")
        self.assertIn("/machine-b/bin/python3.12", self.text(".git/hooks/pre-commit"))

    def test_a_hooks_directory_outside_the_work_tree_keeps_the_absolute_paths(self) -> None:
        shared = self.tmp / "shared-hooks"
        git(self.repo, "config", "core.hooksPath", str(shared))
        self.init("--yes")
        text = (shared / "pre-commit").read_text(encoding="utf-8")
        self.assertIn(f"RATCHET_BIN={QUOTED_EXECUTABLE}\n", text)
        self.assertIn(f"RATCHET_PY={QUOTED_INTERPRETER}\n", text)

    def test_dot_git_hooks_keeps_the_absolute_paths(self) -> None:
        git(self.repo, "config", "--unset", "core.hooksPath")
        self.init("--yes")
        self.assertIn(f"RATCHET_BIN={QUOTED_EXECUTABLE}\n", self.text(".git/hooks/pre-commit"))

    def test_a_copy_of_the_tool_inside_the_repo_is_pointed_at_through_bin(self) -> None:
        inside = self.repo / "third_party" / "ratchet" / "bin" / "ratchet"
        with mock.patch("ratchet.onboard.EXECUTABLE_PATH", inside):
            out = self.init("--yes")
        self.assertIn('set "bin": "third_party/ratchet/bin/ratchet" in .ratchet.json', out)

    def test_the_real_tool_outside_the_repo_gets_no_such_hint(self) -> None:
        self.assertNotIn('set "bin"', self.init("--yes"))

    def test_an_existing_script_with_a_final_exit_gets_a_portable_block_before_it(self) -> None:
        (self.repo / ".githooks").mkdir()
        path = self.repo / ".githooks" / "pre-commit"
        path.write_text("#!/bin/sh\nset -e\necho my-check\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        out = self.init("--yes")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(self.statuses(out)["hook pre-commit"], "added")
        self.assertLess(text.index(HASH_BEGIN), text.index("exit 0"))
        self.assertNotIn(sys.executable, text)


class PortableBlockRuntimeTest(unittest.TestCase):
    """Run the portable block with sh and see how it finds the tool and the Python."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / ".ratchet.json").write_text("{}", encoding="utf-8")
        self.path_dir = self.tmp / "path-bin"
        self.path_dir.mkdir()
        self.python3_log = self.tmp / "python3.log"
        # `python3` on PATH is a recording shim around the interpreter running the tests (3.10+, unlike a system python3).
        shim = self.path_dir / "python3"
        shim.write_text(f'#!/bin/sh\necho used >> "{self.python3_log}"\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)

    def fake_tool(self, path: Path, label: str, exit_code: int = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'import sys\nprint("{label}:", *sys.argv[1:])\nsys.exit({exit_code})\n', encoding="utf-8")
        return path

    def run_block(self, hook: str, prologue: str = "", **env: str) -> subprocess.CompletedProcess:
        script = f"#!/bin/sh\n{prologue}" + "\n".join(hook_body(hook, None)) + "\necho reached-the-end\n"
        full_env = {"PATH": f"{self.path_dir}:/usr/bin:/bin", "HOME": str(self.tmp), **env}
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=self.repo, env=full_env)

    def test_ratchet_bin_from_the_environment_is_used(self) -> None:
        tool = self.fake_tool(self.tmp / "elsewhere" / "tool.py", "from-env")
        proc = self.run_block("post-commit", RATCHET_BIN=str(tool))
        self.assertIn("from-env: infer --quiet", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_the_environment_beats_the_config_and_the_config_beats_the_path(self) -> None:
        self.fake_tool(self.repo / "tools" / "ratchet.py", "from-config")
        self.fake_tool(self.path_dir / "ratchet", "from-path")
        (self.repo / ".ratchet.json").write_text(json.dumps({"bin": "tools/ratchet.py"}), encoding="utf-8")
        env_tool = self.fake_tool(self.tmp / "env.py", "from-env")
        self.assertIn("from-env:", self.run_block("post-commit", RATCHET_BIN=str(env_tool)).stdout)
        self.assertIn("from-config:", self.run_block("post-commit").stdout)

    def test_bin_in_the_config_is_relative_to_the_repo_root(self) -> None:
        self.fake_tool(self.repo / "third_party" / "ratchet" / "bin" / "ratchet", "vendored")
        (self.repo / ".ratchet.json").write_text(json.dumps({"bin": "third_party/ratchet/bin/ratchet"}), encoding="utf-8")
        proc = self.run_block("post-commit")
        self.assertIn("vendored: infer --quiet", proc.stdout)

    def test_without_either_the_ratchet_on_the_path_is_used(self) -> None:
        self.fake_tool(self.path_dir / "ratchet", "from-path").chmod(0o755)  # `command -v` only finds executables
        proc = self.run_block("post-commit")
        self.assertIn("from-path: infer --quiet", proc.stdout)

    def test_a_bin_that_does_not_exist_warns_instead_of_falling_back_to_the_path(self) -> None:
        self.fake_tool(self.path_dir / "ratchet", "from-path")
        (self.repo / ".ratchet.json").write_text(json.dumps({"bin": "tools/gone.py"}), encoding="utf-8")
        proc = self.run_block("pre-commit")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("from-path", proc.stdout)
        self.assertIn("WARNING: cannot find the ratchet executable", proc.stderr)
        self.assertIn("pre-commit is NOT running", proc.stderr)
        self.assertIn("reached-the-end", proc.stdout)

    def test_nothing_to_run_warns_and_does_not_fail_even_under_set_e(self) -> None:
        for flags in ("", "set -e\n", "set -eu\n"):
            for hook in ("pre-commit", "post-commit"):
                with self.subTest(flags=flags, hook=hook):
                    proc = self.run_block(hook, prologue=flags)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("reached-the-end", proc.stdout)
                    self.assertIn("WARNING: cannot find the ratchet executable (set RATCHET_BIN", proc.stderr)
                    self.assertIn(f"{hook} is NOT running", proc.stderr)

    def test_a_config_that_cannot_be_read_does_not_end_a_set_e_hook(self) -> None:
        (self.repo / ".ratchet.json").write_text("{not json", encoding="utf-8")
        self.fake_tool(self.path_dir / "ratchet", "from-path").chmod(0o755)
        proc = self.run_block("post-commit", prologue="set -e\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("from-path: infer --quiet", proc.stdout)
        self.assertIn("reached-the-end", proc.stdout)

    def test_ratchet_py_from_the_environment_is_used_and_python3_on_the_path_is_the_default(self) -> None:
        tool = self.fake_tool(self.tmp / "tool.py", "ran")
        self.run_block("post-commit", RATCHET_BIN=str(tool), RATCHET_PY=sys.executable)
        self.assertFalse(self.python3_log.exists())  # the shim was not used
        self.run_block("post-commit", RATCHET_BIN=str(tool))
        self.assertTrue(self.python3_log.exists())

    def test_a_python_that_is_too_old_warns_and_runs_nothing(self) -> None:
        old = self.tmp / "python-old"
        old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # what `sys.exit(version < (3, 10))` does on 3.9
        old.chmod(0o755)
        tool = self.fake_tool(self.tmp / "tool.py", "ran")
        for hook in ("pre-commit", "post-commit"):
            with self.subTest(hook=hook):
                proc = self.run_block(hook, RATCHET_PY=str(old), RATCHET_BIN=str(tool))
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn("ran:", proc.stdout)
                self.assertIn(f"WARNING: {old} is missing or older than Python 3.10", proc.stderr)
                self.assertIn(f"{hook} is NOT running", proc.stderr)
                self.assertIn("reached-the-end", proc.stdout)

    def test_a_missing_python_warns_and_runs_nothing(self) -> None:
        tool = self.fake_tool(self.tmp / "tool.py", "ran")
        proc = self.run_block("pre-commit", RATCHET_PY="/nonexistent/python3.99", RATCHET_BIN=str(tool))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("WARNING: /nonexistent/python3.99 is missing or older than Python 3.10", proc.stderr)
        self.assertIn("reached-the-end", proc.stdout)

    def test_a_failing_check_blocks_pre_commit_and_never_post_commit(self) -> None:
        tool = self.fake_tool(self.tmp / "tool.py", "ran", exit_code=1)
        blocked = self.run_block("pre-commit", RATCHET_BIN=str(tool))
        self.assertEqual(blocked.returncode, 1)
        self.assertNotIn("reached-the-end", blocked.stdout)
        survived = self.run_block("post-commit", RATCHET_BIN=str(tool))
        self.assertEqual(survived.returncode, 0)
        self.assertIn("reached-the-end", survived.stdout)

    def test_a_failing_check_blocks_under_set_e_too(self) -> None:
        tool = self.fake_tool(self.tmp / "tool.py", "ran", exit_code=1)
        self.assertEqual(self.run_block("pre-commit", prologue="set -e\n", RATCHET_BIN=str(tool)).returncode, 1)

    def test_a_repo_without_a_config_is_left_alone(self) -> None:
        (self.repo / ".ratchet.json").unlink()
        for hook in ("pre-commit", "post-commit"):
            with self.subTest(hook=hook):
                proc = self.run_block(hook)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                self.assertEqual(proc.stdout.strip(), "reached-the-end")

    def test_the_probe_answers_before_anything_else_in_both_kinds_of_block(self) -> None:
        (self.repo / ".ratchet.json").unlink()  # not even a config is needed
        for label, pinned in (("portable", None), ("pinned", (Path("/nonexistent/python3"), Path("/nonexistent/ratchet")))):
            for hook in ("pre-commit", "post-commit"):
                with self.subTest(kind=label, hook=hook):
                    script = "#!/bin/sh\n" + "\n".join(hook_body(hook, pinned)) + "\necho reached-the-end\n"
                    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=self.repo,
                                          env={"PATH": "/usr/bin:/bin", "RATCHET_HOOK_PROBE": "1"})
                    self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"ratchet-hook-probe {hook}\n", ""))

    def test_an_empty_probe_variable_is_not_a_probe(self) -> None:
        proc = self.run_block("pre-commit", RATCHET_HOOK_PROBE="")
        self.assertIn("reached-the-end", proc.stdout)
        self.assertNotIn("ratchet-hook-probe", proc.stdout)


class CommitHintTest(InitCase):
    """After init the onboarding files must be committed together: init says which and how."""

    def hint(self, out: str) -> list[str]:
        lines = out.splitlines()
        return lines[next(i for i, line in enumerate(lines) if line.startswith("commit the files init created")):][:2]

    def run_line(self, line: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-c", line], cwd=self.repo, capture_output=True, text=True, env=_GIT_ENV)

    def commit_other(self) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "unrelated"], cwd=self.repo,
                              capture_output=True, text=True, env=_GIT_ENV)

    def test_init_prints_the_files_to_stage_and_a_ready_to_run_commit_line(self) -> None:
        out = self.init("--yes")
        header, line = self.hint(out)
        self.assertIn("until these are committed together every other commit is blocked", header)
        self.assertEqual(line, '  git add .ratchet.json CODING-STYLE.md CODING-STYLE.digest.md CLAUDE.md .gitignore '
                               '&& git commit -m "chore: adopt review-ratchet"')

    def test_the_commit_hint_comes_before_the_next_steps_which_stay_last(self) -> None:
        lines = self.init("--yes").splitlines()
        self.assertLess(next(i for i, line in enumerate(lines) if line.startswith("commit the files")),
                        next(i for i, line in enumerate(lines) if line.startswith("next 1:")))
        self.assertTrue(lines[-1].startswith("next 3:"))

    def test_hooks_inside_the_work_tree_are_part_of_the_commit(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        _, line = self.hint(self.init("--yes"))
        self.assertIn(".gitignore .githooks/pre-commit .githooks/post-commit &&", line)

    def test_the_trap_is_real_and_the_printed_line_gets_out_of_it(self) -> None:
        out = self.init("--yes")
        (self.repo / "other.txt").write_text("unrelated", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        blocked = self.commit_other()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("not found in the git index", blocked.stderr)
        git(self.repo, "rm", "-q", "--cached", "other.txt")
        adopted = self.run_line(self.hint(out)[1].strip())
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual(git(self.repo, "log", "--format=%s"), "chore: adopt review-ratchet")
        git(self.repo, "add", "other.txt")
        later = self.commit_other()
        self.assertEqual(later.returncode, 0, later.stderr)

    def test_once_everything_is_committed_a_rerun_prints_one_line(self) -> None:
        out = self.init("--yes")
        self.run_line(self.hint(out)[1].strip())
        rerun = self.init("--yes")
        self.assertEqual(rerun.splitlines()[-1], "review-ratchet is set up")
        self.assertNotIn("commit the files", rerun)
        self.assertNotIn("next 1:", rerun)

    def test_an_all_ok_rerun_with_nothing_committed_still_points_at_the_commit(self) -> None:
        self.init("--yes")
        rerun = self.init("--yes")
        self.assertIn("review-ratchet is set up", rerun)
        self.assertIn("commit the files init created", rerun)
        self.assertNotIn("next 1:", rerun)

    def test_a_step_the_user_opted_out_of_still_counts_as_set_up(self) -> None:
        self.init("--yes", "--no-claude-md")
        rerun = self.init("--yes", "--no-claude-md")
        self.assertIn("review-ratchet is set up", rerun)
        self.assertNotIn("next 1:", rerun)

    def test_an_ignored_file_is_not_listed_because_git_add_would_refuse_it(self) -> None:
        (self.repo / ".gitignore").write_text("CODING-STYLE.digest.md\n", encoding="utf-8")
        _, line = self.hint(self.init("--yes"))
        self.assertNotIn("CODING-STYLE.digest.md", line)

    def test_a_claude_md_the_user_already_wired_is_not_listed(self) -> None:
        (self.repo / "CLAUDE.md").write_text(f"@{DIGEST}\n", encoding="utf-8")
        _, line = self.hint(self.init("--yes"))
        self.assertNotIn("CLAUDE.md", line)

    def test_a_rules_file_outside_the_repo_is_not_listed(self) -> None:
        shared = self.tmp / "shared.md"
        shared.write_text(STYLE_RULES, encoding="utf-8")
        _, line = self.hint(self.init("--yes", "--rules-file", str(shared)))
        self.assertNotIn("shared.md", line)
        self.assertIn(".ratchet.json", line)

    def test_names_with_spaces_are_quoted(self) -> None:
        (self.repo / "docs dir").mkdir()
        (self.repo / "docs dir" / "style rules.md").write_text(STYLE_RULES, encoding="utf-8")
        self.write_config(rules_files=["docs dir/style rules.md"])
        _, line = self.hint(self.init("--yes"))
        self.assertIn("'docs dir/style rules.md'", line)
        self.assertEqual(self.run_line(line.strip()).returncode, 0)

    def test_a_dry_run_prints_no_commit_line(self) -> None:
        self.assertNotIn("commit the files", self.init("--dry-run"))

    def test_without_hooks_there_is_still_a_commit_line_but_no_blocked_commit_warning(self) -> None:
        header, line = self.hint(self.init("--yes", "--no-hooks"))
        self.assertIn("git commit -m", line)
        self.assertNotIn("blocked", header)


class DryRunDiffTest(InitCase):
    def test_every_file_a_real_run_would_write_is_shown_as_a_diff(self) -> None:
        out = self.init("--dry-run")
        for name in (".ratchet.json", "CODING-STYLE.digest.md", "CLAUDE.md", ".gitignore", ".git/hooks/pre-commit", ".git/hooks/post-commit"):
            with self.subTest(name):
                self.assertIn(f"+++ b/{name}", out)
        self.assertIn("--- /dev/null", out)

    def test_the_claude_md_diff_is_the_exact_text_and_says_where_it_goes(self) -> None:
        (self.repo / "CLAUDE.md").write_text("# Project notes\n\nrun make\n", encoding="utf-8")
        out = self.init("--dry-run")
        self.assertIn("appended at the end of CLAUDE.md", out)
        self.assertIn("--- a/CLAUDE.md", out)
        for line in (f"+{HTML_BEGIN}", f"+{CLAUDE_MD_HEADING}", f"+@{DIGEST}", f"+{HTML_END}", " run make"):
            self.assertIn(f"      {line}\n", out)
        self.assertEqual(self.text("CLAUDE.md"), "# Project notes\n\nrun make\n")

    def test_an_existing_hook_diff_shows_its_own_lines_as_context_and_only_the_block_as_added(self) -> None:
        path = self.hook("pre-commit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho 'my own check'\nexit 0\n", encoding="utf-8")
        out = self.init("--dry-run")
        self.assertIn("--- a/.git/hooks/pre-commit", out)
        self.assertIn("       echo 'my own check'\n", out)
        self.assertIn(f"      +{HASH_BEGIN}\n", out)
        self.assertNotIn("-echo", out)
        self.assertIn("       exit 0\n", out)

    def test_the_digest_diff_and_its_size_come_from_the_template_that_is_not_copied_yet(self) -> None:
        out = self.init("--dry-run")
        size = int(re.search(r"digest CODING-STYLE\.digest\.md: (\d+) bytes", out).group(1))
        self.assertIn("+# Style digest", out)
        self.init("--yes")
        self.assertEqual(len((self.repo / DIGEST).read_bytes()), size)

    def test_an_updated_digest_shows_only_the_changed_lines(self) -> None:
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        self.cli_ok("compile")
        self.write_rules(STYLE_RULES.replace("Fail fast at the boundary.", "Fail fast at every boundary."))
        out = self.init("--dry-run")
        self.assertIn("-P0F.1 | blocker | Fail fast at the boundary", out)
        self.assertIn("+P0F.1 | blocker | Fail fast at every boundary", out)
        self.assertNotIn("+P0E.2", out)

    def test_an_injected_block_is_listed_with_the_digest_and_the_size_names_the_digest(self) -> None:
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"], inject_targets=["AGENTS.md"])
        (self.repo / "AGENTS.md").write_text("notes\n<!-- style-digest:begin -->\n<!-- style-digest:end -->\n", encoding="utf-8")
        out = self.init("--dry-run")
        self.assertRegex(out, rf"digest {re.escape(DIGEST)}: \d+ bytes, AGENTS.md \(injected block\)")
        self.assertIn("+++ b/AGENTS.md", out)

    def test_nothing_changing_prints_no_diff(self) -> None:
        self.init("--yes")
        out = self.init("--dry-run")
        self.assertNotIn("+++ b/", out)
        self.assertNotIn("--- a/", out)

    def test_a_real_run_prints_no_diff(self) -> None:
        self.assertNotIn("+++ b/", self.init("--yes"))

    def test_a_dry_run_still_writes_nothing(self) -> None:
        before = self.files()
        self.init("--dry-run")
        self.assertEqual(self.files(), before)


class ClaudeMdHeadingTest(InitCase):
    def test_the_block_has_its_own_heading_above_the_import(self) -> None:
        self.init("--yes")
        self.assertEqual(self.text("CLAUDE.md").splitlines(), [HTML_BEGIN, "## Code style digest (review-ratchet)", f"@{DIGEST}", HTML_END])

    def test_a_block_from_an_older_run_without_the_heading_gets_it_in_place(self) -> None:
        (self.repo / "CLAUDE.md").write_text(f"top\n{HTML_BEGIN}\n@{DIGEST}\n{HTML_END}\nbottom\n", encoding="utf-8")
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "updated")
        self.assertEqual(self.text("CLAUDE.md"), f"top\n{HTML_BEGIN}\n{CLAUDE_MD_HEADING}\n@{DIGEST}\n{HTML_END}\nbottom\n")
        self.assertEqual(self.statuses(self.init("--yes"))["CLAUDE.md"], "already ok")

    def test_the_heading_goes_after_the_users_text_not_before_it(self) -> None:
        (self.repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
        self.init("--yes")
        self.assertTrue(self.text("CLAUDE.md").startswith("# Mine\n\n" + HTML_BEGIN))


class ShadowedHooksTest(InitCase):
    def custom_hook(self, name: str, executable: bool = True) -> None:
        path = self.repo / ".git" / "hooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)

    def warnings(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if "warning: core.hooksPath" in line]

    def test_hooks_that_core_hooks_path_now_hides_are_named_once(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.custom_hook("pre-push")
        self.custom_hook("commit-msg")
        self.custom_hook("pre-commit.sample")
        out = self.init("--yes")
        (warning,) = self.warnings(out)
        self.assertIn("commit-msg, pre-push", warning)
        self.assertNotIn("sample", warning)
        self.assertIn("no longer run", warning)
        self.assertIn(".githooks", warning)

    def test_no_warning_when_core_hooks_path_is_not_set(self) -> None:
        self.custom_hook("pre-push")
        self.assertEqual(self.warnings(self.init("--yes")), [])

    def test_no_warning_when_only_samples_or_non_executable_files_are_there(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.custom_hook("pre-push.sample")
        self.custom_hook("commit-msg", executable=False)
        self.assertEqual(self.warnings(self.init("--yes")), [])

    def test_no_warning_without_hooks(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.custom_hook("pre-push")
        self.assertEqual(self.warnings(self.init("--yes", "--no-hooks")), [])

    def test_the_warning_is_part_of_a_dry_run_and_a_rerun(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.custom_hook("pre-push")
        self.assertEqual(len(self.warnings(self.init("--dry-run"))), 1)
        self.init("--yes")
        self.assertEqual(len(self.warnings(self.init("--yes"))), 1)


class MissingTemplateTest(InitCase):
    """A vendored copy made before templates/ was included has no template file."""

    def missing(self):
        return mock.patch("ratchet.onboard.TEMPLATE_PATH", self.tmp / "no-templates" / "CODING-STYLE.template.md")

    def test_template_without_the_file_is_a_clean_error_that_names_rules_file_and_writes_nothing(self) -> None:
        before = self.files()
        with self.missing():
            code, _, err = self.cli("init", "--yes", "--template")
        self.assertEqual(code, 1)
        self.assertIn("not in this copy of review-ratchet", err)
        self.assertIn("--rules-file", err)
        self.assertIn("ratchet vendor", err)
        self.assertEqual(self.files(), before)

    def test_the_default_with_no_rules_file_found_is_the_same_error(self) -> None:
        with self.missing():
            code, _, err = self.cli("init", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--rules-file", err)

    def test_a_dry_run_reports_it_too(self) -> None:
        with self.missing():
            code, _, err = self.cli("init", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("--rules-file", err)

    def test_an_existing_rules_file_needs_no_template(self) -> None:
        (self.repo / "CODING-STYLE.md").write_text(STYLE_RULES, encoding="utf-8")
        with self.missing():
            code, _, err = self.cli("init", "--yes", "--template")
        self.assertEqual(code, 0, err)

    def test_rules_file_works_without_the_template(self) -> None:
        mine = self.repo / "mine.md"
        mine.write_text(STYLE_RULES, encoding="utf-8")
        with self.missing():
            code, _, err = self.cli("init", "--yes", "--rules-file", str(mine))
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
