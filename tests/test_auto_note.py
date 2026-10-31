"""`house-rules auto note`: the git post-commit hook records each commit made inside a Claude Code session in a local pending file.

The commits are real: a repo `house-rules init` set up (hooks installed the pinned way and the portable way) takes `git commit`s with
the session id in their environment, as Claude Code's Bash tool would make them. The cheaper checks run the command in-process.
"""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path, and starts every test from a clean environment)
import test_init
from support import _GIT_ENV, RepoCase, git

from house_rules import auto
from house_rules.blocks import HASH_BEGIN, HASH_END
from house_rules.cli import build_parser, main
from house_rules.hooks import hook_body
from house_rules.onboard import EXECUTABLE_PATH

SESSION = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
REVIEW_CHILD = "HOUSE_RULES_REVIEW_CHILD"
# What tests/support.py takes out of the environment besides CLAUDE*. Spelled out, so that a name dropped from its list is caught.
SCRUBBED_NAMES = (REVIEW_CHILD, "HOUSE_RULES_AUTO_HEADLESS")
# Not safe as a file name: a path, a parent, a blank, a newline, one character too long.
UNSAFE_IDS = ("../escape", "..", "has space", "line\nbreak", "x" * 129, "caf\u00e9", "semi;colon")
HOOK_VARIANTS = {"pinned": None, "portable": ".githooks"}  # name -> core.hooksPath that init is run under
TEMPLATES: dict[str, Path] = {}
HOOK_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 auto note: \w+: .*\(at .+:\d+\)$")


def session_env(state: Path, session: str | None = SESSION, **more: str) -> dict[str, str]:
    """What a commit made by Claude Code's Bash tool carries: the session id, and a state directory of the test's own. The tool and the
    Python are named too, for the portable hook to find them."""
    env = {auto.STATE_DIR_ENV: str(state), "HOUSE_RULES_BIN": str(EXECUTABLE_PATH), "HOUSE_RULES_PY": sys.executable, **more}
    if session is not None:
        env[auto.SESSION_ID_ENV] = session
    return env


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def hook_log(state: Path) -> list[str]:
    path = state / auto.HOOK_LOG_NAME
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def setUpModule() -> None:
    """One repo per hook variant that init has set up and committed; each test works on a copy, as init takes most of a second."""
    holder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(holder.cleanup)
    for name, hooks_path in HOOK_VARIANTS.items():
        repo = Path(os.path.realpath(holder.name)) / name / "demo-repo"
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        if hooks_path:
            git(repo, "config", "core.hooksPath", hooks_path)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["init", "--yes", "--repo", str(repo)])
        if code != 0:
            raise AssertionError(f"init failed for the {name} template: {err.getvalue()}")
        git(repo, "add", "-A")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "set up house-rules")  # no session: nothing noted
        TEMPLATES[name] = repo


# ---- where things are, and what a session id may be ---------------------------------------------------------------

class StateDirTest(unittest.TestCase):
    def resolve(self, **env: str) -> Path:
        """state_dir() with the two variables as given (one not given is unset) and any other change to the environment."""
        with mock.patch.dict(os.environ):
            os.environ.pop(auto.STATE_DIR_ENV, None)
            os.environ.pop(auto.XDG_STATE_ENV, None)
            os.environ.update(env)
            return auto.state_dir()

    def test_house_rules_state_dir_comes_first(self) -> None:
        self.assertEqual(self.resolve(HOUSE_RULES_STATE_DIR="/s/mine", XDG_STATE_HOME="/x"), Path("/s/mine"))

    def test_xdg_state_home_comes_next_with_the_tool_name_under_it(self) -> None:
        self.assertEqual(self.resolve(XDG_STATE_HOME="/x"), Path("/x/house-rules"))

    def test_the_default_is_under_home(self) -> None:
        self.assertEqual(self.resolve(HOME="/h"), Path("/h/.local/state/house-rules"))

    def test_a_variable_that_is_set_but_empty_counts_as_unset(self) -> None:
        self.assertEqual(self.resolve(HOUSE_RULES_STATE_DIR="", XDG_STATE_HOME="/x"), Path("/x/house-rules"))
        self.assertEqual(self.resolve(HOUSE_RULES_STATE_DIR="", XDG_STATE_HOME="", HOME="/h"), Path("/h/.local/state/house-rules"))

    def test_the_directories_the_stop_hook_needs_sit_in_it(self) -> None:
        with mock.patch.dict(os.environ, {auto.STATE_DIR_ENV: "/s"}):
            self.assertEqual((auto.pending_dir(), auto.claimed_dir()), (Path("/s/pending"), Path("/s/claimed")))
            self.assertEqual(auto.pending_path(SESSION), Path(f"/s/pending/{SESSION}.jsonl"))


class SessionIdTest(unittest.TestCase):
    def test_letters_digits_dash_and_underscore_up_to_128_are_ids(self) -> None:
        for value in (SESSION, "abc", "A_b-9", "x", "x" * 128):
            with self.subTest(value=value):
                self.assertTrue(auto.is_session_id(value))

    def test_anything_that_could_not_be_a_plain_file_name_is_not(self) -> None:
        for value in ("", "abc\n", "a.b", ".", "a/b", *UNSAFE_IDS):
            with self.subTest(value=value):
                self.assertFalse(auto.is_session_id(value))

    def test_the_session_comes_from_the_claude_code_variable_and_only_when_it_is_an_id(self) -> None:
        self.assertEqual(auto.SESSION_ID_ENV, "CLAUDE_CODE_SESSION_ID")
        with mock.patch.dict(os.environ, {auto.SESSION_ID_ENV: SESSION}):
            self.assertEqual(auto.session_id(), SESSION)
        for value in ("", *UNSAFE_IDS):
            with self.subTest(value=value), mock.patch.dict(os.environ, {auto.SESSION_ID_ENV: value}):
                self.assertIsNone(auto.session_id())
        with mock.patch.dict(os.environ):
            os.environ.pop(auto.SESSION_ID_ENV, None)
            self.assertIsNone(auto.session_id())

    def test_a_path_is_never_built_from_something_that_is_not_an_id(self) -> None:
        with self.assertRaises(AssertionError):
            auto.pending_path("../escape")


# ---- the record -----------------------------------------------------------------------------------------------------

class PendingRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(os.path.realpath(tmp.name)) / "state"
        patcher = mock.patch.dict(os.environ, {auto.STATE_DIR_ENV: str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_line_holds_the_worktree_the_head_and_the_time_in_that_order(self) -> None:
        line = auto.PendingCommit(Path("/w/tree"), "a" * 40, "2026-10-30T08:15:00+00:00").to_line()
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(list(json.loads(line)), ["worktree", "head", "ts"])
        self.assertEqual(json.loads(line), {"worktree": "/w/tree", "head": "a" * 40, "ts": "2026-10-30T08:15:00+00:00"})

    def test_a_line_reads_back_as_the_commit_it_was_made_from(self) -> None:
        commit = auto.PendingCommit(Path("/w/\u521b\u4e1a/tree with space"), "b" * 40, auto.utc_timestamp())
        self.assertEqual(auto.PendingCommit.from_line(commit.to_line()), commit)

    def test_what_is_not_a_record_raises_value_error(self) -> None:
        for line in ("", "not json", "[]", "3", '{"worktree": "/w", "head": "a"}', '{"worktree": "/w", "head": 1, "ts": "t"}',
                     '{"worktree": null, "head": "a", "ts": "t"}'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                auto.PendingCommit.from_line(line)

    def test_the_timestamp_is_utc_iso_8601_to_the_second(self) -> None:
        stamp = auto.utc_timestamp()
        parsed = datetime.fromisoformat(stamp)  # 3.10 reads the +00:00 form, not a trailing Z
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parsed.microsecond, 0)
        self.assertLess(abs(datetime.now(timezone.utc) - parsed), timedelta(seconds=60))

    def test_the_reader_returns_the_commits_in_file_order(self) -> None:
        first, second = (auto.PendingCommit(Path(f"/w/{n}"), str(n) * 40, f"2026-10-30T08:1{n}:00+00:00") for n in (1, 2))
        path = self.state.parent / "p.jsonl"
        path.write_text(first.to_line() + second.to_line(), encoding="utf-8")
        self.assertEqual(auto.read_pending(path), [first, second])

    def test_a_damaged_line_is_skipped_and_told_in_hook_log_and_the_lines_around_it_are_kept(self) -> None:
        good = auto.PendingCommit(Path("/w/a"), "c" * 40, "2026-10-30T08:15:00+00:00")
        path = self.state.parent / "p.jsonl"
        path.write_text(good.to_line() + "{torn\n" + good.to_line(), encoding="utf-8")
        self.assertEqual(auto.read_pending(path), [good, good])
        [told] = hook_log(self.state)
        self.assertIn(f"{path}:2: skipped a line", told)


# ---- `house-rules auto note`, run in-process ------------------------------------------------------------------------------

class NoteCase(RepoCase):
    """A repo whose config says wrapup_auto, with one commit, a state directory of the test's own and a session id in the environment."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "state"  # not made: the note has to
        self.write_config(wrapup_auto=True)
        self.head = self.commit("base")
        patcher = mock.patch.dict(os.environ, {auto.STATE_DIR_ENV: str(self.state), auto.SESSION_ID_ENV: SESSION})
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def pending_file(self) -> Path:
        return self.state / auto.PENDING_NAME / f"{SESSION}.jsonl"

    def note(self, env: dict[str, str | None] | None = None, *, repo: Path | None = None) -> tuple[int, str, str]:
        """`house-rules auto note` in-process with `env` changed on top of the test's (None unsets a variable)."""
        with mock.patch.dict(os.environ):
            for name, value in (env or {}).items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            return self.cli("auto", "note") if repo is None else self.cli("auto", "note", "--repo", str(repo), with_repo=False)

    def assert_silent_and_nothing_written(self, outcome: tuple[int, str, str]) -> None:
        self.assertEqual(outcome, (0, "", ""))
        self.assertFalse(self.state.exists())


class NoteTest(NoteCase):
    def test_a_commit_in_a_session_is_recorded_and_nothing_is_printed(self) -> None:
        self.assertEqual(self.note(), (0, "", ""))
        [record] = read_lines(self.pending_file)
        self.assertEqual(record["worktree"], str(self.repo))
        self.assertEqual(record["head"], self.head)
        self.assertEqual(len(record["head"]), 40)
        self.assertEqual(datetime.fromisoformat(record["ts"]).utcoffset(), timedelta(0))

    def test_each_note_adds_a_line_with_the_head_at_that_moment(self) -> None:
        self.note()
        second = self.commit("second")
        self.note()
        self.assertEqual([record["head"] for record in read_lines(self.pending_file)], [self.head, second])

    def test_another_session_has_a_file_of_its_own(self) -> None:
        self.note()
        self.note({auto.SESSION_ID_ENV: "another-session"})
        self.assertEqual(sorted(path.name for path in (self.state / auto.PENDING_NAME).iterdir()),
                         sorted([f"{SESSION}.jsonl", "another-session.jsonl"]))

    def test_nothing_is_recorded_outside_a_claude_code_session(self) -> None:
        self.assert_silent_and_nothing_written(self.note({auto.SESSION_ID_ENV: None}))

    def test_nothing_is_recorded_for_an_id_that_is_not_safe_as_a_file_name(self) -> None:
        for value in ("", *UNSAFE_IDS):
            with self.subTest(value=value):
                self.assert_silent_and_nothing_written(self.note({auto.SESSION_ID_ENV: value}))

    def test_nothing_is_recorded_inside_a_review_child(self) -> None:
        self.assert_silent_and_nothing_written(self.note({REVIEW_CHILD: "1"}))

    def test_only_a_review_child_marker_of_exactly_1_silences_it(self) -> None:
        for value in ("0", "", "yes"):
            with self.subTest(value=value):
                self.note({REVIEW_CHILD: value})
                self.assertEqual(len(read_lines(self.pending_file)), 1)
                shutil.rmtree(self.state)

    def test_nothing_is_recorded_unless_the_config_says_wrapup_auto_true(self) -> None:
        for label, config in (("false", {"wrapup_auto": False}), ("missing", {"max_review_rounds": 2}), ("empty", {}),
                              ("a string", {"wrapup_auto": "true"}), ("a number", {"wrapup_auto": 1})):
            with self.subTest(label):
                self.write_config(**config)
                self.assert_silent_and_nothing_written(self.note())

    def test_nothing_is_recorded_without_a_config(self) -> None:
        (self.repo / ".house-rules.json").unlink()
        self.assert_silent_and_nothing_written(self.note())

    def test_nothing_is_recorded_when_head_does_not_exist_yet(self) -> None:
        unborn = self.tmp / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-q", "-b", "main")
        (unborn / ".house-rules.json").write_text(json.dumps({"wrapup_auto": True}), encoding="utf-8")
        self.assert_silent_and_nothing_written(self.note(repo=unborn))

    def test_a_linked_worktree_is_recorded_as_itself_and_the_main_worktrees_config_decides(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(linked))
        git(linked, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "on the side")
        self.write_config(wrapup_auto=False)  # the main worktree's file, as the linked one's own copy still says true
        (linked / ".house-rules.json").write_text(json.dumps({"wrapup_auto": True}), encoding="utf-8")
        self.assert_silent_and_nothing_written(self.note(repo=linked))
        self.write_config(wrapup_auto=True)
        (linked / ".house-rules.json").write_text(json.dumps({"wrapup_auto": False}), encoding="utf-8")
        self.assertEqual(self.note(repo=linked), (0, "", ""))
        [record] = read_lines(self.pending_file)
        self.assertEqual((record["worktree"], record["head"]), (str(linked), git(linked, "rev-parse", "HEAD")))

    def test_a_subdirectory_of_the_worktree_records_its_top(self) -> None:
        (self.repo / "src" / "deep").mkdir(parents=True)
        self.note(repo=self.repo / "src" / "deep")
        self.assertEqual([record["worktree"] for record in read_lines(self.pending_file)], [str(self.repo)])

    def test_repo_beats_the_current_directory_and_without_it_the_current_directory_is_used(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(elsewhere)
        self.assertEqual(self.note(repo=self.repo), (0, "", ""))
        self.assertEqual(len(read_lines(self.pending_file)), 1)
        os.chdir(self.repo)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["auto", "note"]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(len(read_lines(self.pending_file)), 2)

    def test_the_executable_does_the_same_as_the_in_process_call(self) -> None:
        proc = subprocess.run([sys.executable, str(EXECUTABLE_PATH), "auto", "note", "--repo", str(self.repo)],
                              capture_output=True, text=True, env={**_GIT_ENV, **session_env(self.state)})
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        [record] = read_lines(self.pending_file)
        self.assertEqual((record["worktree"], record["head"]), (str(self.repo), self.head))


class NoteFilesTest(NoteCase):
    def test_the_state_directory_and_the_pending_directory_are_private_and_the_file_is_too(self) -> None:
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        nested = self.tmp / "outer" / "inner" / "state"
        self.note({auto.STATE_DIR_ENV: str(nested)})
        for directory in (nested, nested / auto.PENDING_NAME):
            with self.subTest(directory=directory.name):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((nested / auto.PENDING_NAME / f"{SESSION}.jsonl").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.tmp / "outer").stat().st_mode), 0o755)  # the parents it had to make keep the default

    def test_an_existing_state_directory_keeps_its_mode(self) -> None:
        self.state.mkdir(mode=0o750)
        self.note()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o750)

    def test_the_line_is_added_with_one_write_on_a_file_opened_for_appending(self) -> None:
        opened: dict[int, tuple[str, int]] = {}
        writes: list[tuple[int, bytes]] = []
        real_open, real_write = os.open, os.write

        def spy_open(path, flags, mode=0o777, **kwargs):
            descriptor = real_open(path, flags, mode, **kwargs)
            opened[descriptor] = (str(path), flags)
            return descriptor

        def spy_write(descriptor, data):
            writes.append((descriptor, bytes(data)))
            return real_write(descriptor, data)

        with mock.patch("os.open", spy_open), mock.patch("os.write", spy_write):
            self.note()
        [(descriptor, flags)] = [(fd, flags) for fd, (path, flags) in opened.items() if path == str(self.pending_file)]
        self.assertTrue(flags & os.O_APPEND and flags & os.O_WRONLY)
        [(_, data)] = [write for write in writes if write[0] == descriptor]
        self.assertEqual(data, self.pending_file.read_bytes())

    def age(self, path: Path, days: float) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - days * 24 * 60 * 60
        os.utime(path, (stamp, stamp))
        return path

    def test_files_older_than_seven_days_are_deleted_from_pending_and_claimed_and_newer_ones_are_kept(self) -> None:
        gone = [self.age(self.state / "pending" / "old.jsonl", 8), self.age(self.state / "claimed" / "old.jsonl", 8)]
        kept = [self.age(self.state / "pending" / "recent.jsonl", 6), self.age(self.state / "claimed" / "recent.jsonl", 6),
                self.age(self.state / auto.HOOK_LOG_NAME, 30)]  # the log is neither pending nor claimed
        self.note()
        self.assertEqual([path.exists() for path in gone], [False, False])
        self.assertEqual([path.exists() for path in kept], [True, True, True])
        self.assertTrue(self.pending_file.exists())

    def test_the_file_a_note_adds_to_is_not_pruned_however_old_it_was(self) -> None:
        self.age(self.pending_file, 10)  # one old line, {}
        self.note()
        # Pruned before the write, the file would be gone and only the new line left: the write comes first and makes the file fresh.
        records = read_lines(self.pending_file)
        self.assertEqual(len(records), 2, records)
        self.assertEqual((records[0], records[1]["head"]), ({}, self.head))

    def test_a_missing_claimed_directory_is_nothing_to_prune(self) -> None:
        self.assertEqual(self.note(), (0, "", ""))
        self.assertFalse((self.state / auto.CLAIMED_NAME).exists())

    def test_nothing_is_pruned_by_a_note_that_records_nothing(self) -> None:
        old = self.age(self.state / "pending" / "old.jsonl", 8)
        self.note({auto.SESSION_ID_ENV: None})
        self.assertTrue(old.exists())


class NoteFailureTest(NoteCase):
    def test_a_state_directory_that_cannot_be_made_costs_no_output_and_no_exit_status(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the state directory should go\n", encoding="utf-8")
        self.assertEqual(self.note({auto.STATE_DIR_ENV: str(blocker / "state")}), (0, "", ""))
        self.assertEqual(blocker.read_text(encoding="utf-8"), "a file where the state directory should go\n")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_a_pending_directory_that_cannot_be_written_is_told_in_hook_log(self) -> None:
        pending = self.state / auto.PENDING_NAME
        pending.mkdir(parents=True, mode=0o500)
        self.addCleanup(pending.chmod, 0o700)
        self.assertEqual(self.note(), (0, "", ""))
        self.assertEqual(list(pending.iterdir()), [])
        [told] = hook_log(self.state)
        self.assertRegex(told, HOOK_LOG_LINE)
        self.assertIn("PermissionError", told)

    def test_each_failure_adds_one_line_whatever_the_message_holds(self) -> None:
        auto.log_failure("auto note: first\nsecond\tline  with   gaps")
        auto.log_failure("auto note: third")
        first, third = hook_log(self.state)
        self.assertTrue(first.endswith("auto note: first second line with gaps"))
        self.assertTrue(third.endswith("auto note: third"))

    def test_a_config_that_cannot_be_read_is_told_and_not_raised(self) -> None:
        (self.repo / ".house-rules.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.note(), (0, "", ""))
        [told] = hook_log(self.state)
        self.assertRegex(told, HOOK_LOG_LINE)
        self.assertIn("invalid JSON", told)
        self.assertFalse((self.state / auto.PENDING_NAME).exists())

    def test_a_repo_that_does_not_exist_is_told_and_not_raised(self) -> None:
        self.assertEqual(self.note(repo=self.tmp / "no-such-repo"), (0, "", ""))
        [told] = hook_log(self.state)
        self.assertIn("directory does not exist", told)

    def test_something_that_is_not_a_repo_is_told_and_not_raised(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(self.note(repo=plain), (0, "", ""))
        [told] = hook_log(self.state)
        self.assertRegex(told, HOOK_LOG_LINE)

    def test_a_bug_in_the_note_is_told_in_hook_log_like_any_other_failure(self) -> None:
        def raise_boom() -> None:
            raise KeyError("boom")  # the line the failure names: two calls below the one that catches it

        with mock.patch("house_rules.auto._note", lambda repo: raise_boom()):
            self.assertEqual(self.cli("auto", "note"), (0, "", ""))
        code = raise_boom.__code__
        [told] = hook_log(self.state)
        self.assertTrue(told.endswith(f"auto note: KeyError: 'boom' (at {code.co_filename}:{code.co_firstlineno + 1})"), told)


class StateDirEndToEndTest(NoteCase):
    """The state directory the executable really writes to, with a home directory that is a temp dir."""

    def run_tool(self, **env: str) -> subprocess.CompletedProcess:
        base = {name: value for name, value in _GIT_ENV.items() if name not in (auto.STATE_DIR_ENV, auto.XDG_STATE_ENV, "HOME")}
        proc = subprocess.run([sys.executable, str(EXECUTABLE_PATH), "auto", "note", "--repo", str(self.repo)],
                              capture_output=True, text=True, env={**base, auto.SESSION_ID_ENV: SESSION, **env})
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        return proc

    def test_house_rules_state_dir_is_used_even_when_xdg_state_home_and_home_are_set(self) -> None:
        self.run_tool(HOUSE_RULES_STATE_DIR=str(self.tmp / "mine"), XDG_STATE_HOME=str(self.tmp / "xdg"), HOME=str(self.tmp / "home"))
        self.assertTrue((self.tmp / "mine" / "pending" / f"{SESSION}.jsonl").is_file())
        self.assertFalse((self.tmp / "xdg").exists() or (self.tmp / "home").exists())

    def test_xdg_state_home_is_used_when_house_rules_state_dir_is_not_set(self) -> None:
        self.run_tool(XDG_STATE_HOME=str(self.tmp / "xdg"), HOME=str(self.tmp / "home"))
        self.assertTrue((self.tmp / "xdg" / "house-rules" / "pending" / f"{SESSION}.jsonl").is_file())
        self.assertFalse((self.tmp / "home").exists())

    def test_home_is_used_when_neither_is_set(self) -> None:
        self.run_tool(HOME=str(self.tmp / "home"))
        self.assertTrue((self.tmp / "home" / ".local" / "state" / "house-rules" / "pending" / f"{SESSION}.jsonl").is_file())

    def test_empty_variables_do_not_put_the_files_in_the_current_directory(self) -> None:
        self.run_tool(HOUSE_RULES_STATE_DIR="", XDG_STATE_HOME="", HOME=str(self.tmp / "home"))
        self.assertTrue((self.tmp / "home" / ".local" / "state" / "house-rules" / "pending" / f"{SESSION}.jsonl").is_file())


class AutoParserTest(unittest.TestCase):
    def test_auto_needs_a_subcommand(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["auto"])
        self.assertEqual(raised.exception.code, 2)

    def test_note_takes_repo_and_loads_no_config_of_its_own(self) -> None:
        args = build_parser().parse_args(["auto", "note", "--repo", "somewhere"])
        self.assertEqual((args.repo, args.standalone), ("somewhere", True))

    def test_note_is_not_a_command_that_writes_a_ledger_or_previews(self) -> None:
        for flag in ("--dry-run", "--ledger", "--config"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["auto", "note", flag, "x"])


# ---- the hook: real commits ------------------------------------------------------------------------------------------

class HookedRepoCase(RepoCase):
    """A copy of the repo `house-rules init` set up and committed, with the hooks installed the way VARIANT says."""

    VARIANT = ""

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.repo)
        shutil.copytree(TEMPLATES[self.VARIANT], self.repo, symlinks=True)
        self.state = self.tmp / "state"  # not made: the first note has to

    def session_commit(self, top: Path | None = None, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """A real `git -C top commit`, hooks and all, started from `cwd` as a shell in another directory would."""
        proc = subprocess.run(["git", "-C", str(top or self.repo), "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
                               "--no-verify", "-m", "work"], cwd=cwd or self.repo, capture_output=True, text=True,
                              env={**_GIT_ENV, **(session_env(self.state) if env is None else env)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def records(self, session: str = SESSION) -> list[dict]:
        return read_lines(self.state / auto.PENDING_NAME / f"{session}.jsonl")

    def set_config(self, **changes: object) -> None:
        path = self.repo / ".house-rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config.update(changes)
        path.write_text(json.dumps(config), encoding="utf-8")

    def drop_config_key(self, key: str) -> None:
        path = self.repo / ".house-rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        del config[key]
        path.write_text(json.dumps(config), encoding="utf-8")


class HookedCommitTests:
    """The same behaviour for every way init installs the hooks (mixed into one test case per way)."""

    def test_a_commit_made_in_a_session_is_recorded_with_its_worktree_and_head(self) -> None:
        proc = self.session_commit()
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        [record] = self.records()
        self.assertEqual(record["worktree"], str(self.repo))
        self.assertEqual(record["head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(sorted(record), ["head", "ts", "worktree"])

    def test_every_commit_adds_a_line_in_the_order_they_were_made(self) -> None:
        heads = []
        for _ in range(3):
            self.session_commit()
            heads.append(git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual([record["head"] for record in self.records()], heads)

    def test_a_commit_made_with_git_dash_c_in_a_linked_worktree_records_that_worktree(self) -> None:
        linked = self.tmp / "linked"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(linked))
        proc = self.session_commit(linked, cwd=self.tmp)  # started from a directory that is neither worktree
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        [record] = self.records()
        self.assertEqual(record["worktree"], str(linked))
        self.assertEqual(record["head"], git(linked, "rev-parse", "HEAD"))
        self.assertNotEqual(record["head"], git(self.repo, "rev-parse", "HEAD"))
        self.session_commit()
        self.assertEqual([entry["worktree"] for entry in self.records()], [str(linked), str(self.repo)])

    def test_a_commit_from_a_subdirectory_records_the_top_of_the_worktree(self) -> None:
        (self.repo / "src").mkdir()
        self.session_commit(self.repo / "src", cwd=self.repo / "src")
        self.assertEqual([record["worktree"] for record in self.records()], [str(self.repo)])

    def test_another_session_has_a_file_of_its_own(self) -> None:
        self.session_commit()
        self.session_commit(env=session_env(self.state, session="another-session"))
        self.assertEqual((len(self.records()), len(self.records("another-session"))), (1, 1))

    def test_nothing_is_recorded_outside_a_session_or_for_an_id_that_is_not_safe(self) -> None:
        for label, session in (("unset", None), ("empty", ""), *((repr(value), value) for value in UNSAFE_IDS)):
            with self.subTest(label):
                proc = self.session_commit(env=session_env(self.state, session=session))
                self.assertEqual((proc.stdout, proc.stderr), ("", ""))
                self.assertFalse(self.state.exists())

    def test_nothing_is_recorded_inside_a_review_child(self) -> None:
        proc = self.session_commit(env=session_env(self.state, **{REVIEW_CHILD: "1"}))
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        self.assertFalse(self.state.exists())

    def test_nothing_is_recorded_unless_the_config_says_wrapup_auto_true(self) -> None:
        self.set_config(wrapup_auto=False)
        self.session_commit()
        self.drop_config_key("wrapup_auto")
        self.session_commit()
        self.assertFalse(self.state.exists())
        self.set_config(wrapup_auto=True)  # the same repo, switched on: this setup does record
        self.session_commit()
        self.assertEqual(len(self.records()), 1)

    def test_a_repo_without_a_config_is_left_alone(self) -> None:
        (self.repo / ".house-rules.json").unlink()
        proc = self.session_commit()
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        self.assertFalse(self.state.exists())

    def test_a_failing_infer_does_not_stop_the_note(self) -> None:
        (self.repo / ".style-findings.jsonl").write_text("{this is not a ledger\n", encoding="utf-8")
        proc = self.session_commit()
        self.assertIn("house-rules: error", proc.stderr)  # infer did fail
        self.assertEqual(len(self.records()), 1)

    def test_a_state_directory_that_cannot_be_made_does_not_fail_or_clutter_the_commit(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("a file\n", encoding="utf-8")
        proc = self.session_commit(env=session_env(blocker / "state"))
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        self.assertEqual(blocker.read_text(encoding="utf-8"), "a file\n")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_a_pending_directory_that_cannot_be_written_does_not_fail_or_clutter_the_commit_and_is_told_in_hook_log(self) -> None:
        pending = self.state / auto.PENDING_NAME
        pending.mkdir(parents=True, mode=0o500)
        self.addCleanup(pending.chmod, 0o700)
        proc = self.session_commit()
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        [told] = hook_log(self.state)
        self.assertRegex(told, HOOK_LOG_LINE)


class PinnedHookCommitTest(HookedCommitTests, HookedRepoCase):
    VARIANT = "pinned"


class PortableHookCommitTest(HookedCommitTests, HookedRepoCase):
    VARIANT = "portable"

    def test_the_template_really_has_a_portable_hook(self) -> None:
        text = (self.repo / ".githooks" / "post-commit").read_text(encoding="utf-8")
        self.assertNotIn(sys.executable, text)
        self.assertIn("auto note", text)


# ---- the block, run with sh ---------------------------------------------------------------------------------------------

class NoteLineTest(unittest.TestCase):
    """The line the post-commit block adds, against a tool that records the arguments it is called with."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / ".house-rules.json").write_text("{}", encoding="utf-8")
        self.calls = self.tmp / "calls.log"
        self.tool = self.tmp / "tool.py"
        self.tool.write_text("import os, sys\n"
                             f"with open({str(self.calls)!r}, 'a') as log:\n"
                             "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
                             "sys.exit(int(os.environ.get('TOOL_EXIT', '0')))\n", encoding="utf-8")

    def run_block(self, hook: str, variant: str, prologue: str = "", **env: str) -> subprocess.CompletedProcess:
        pinned = (Path(sys.executable), self.tool) if variant == "pinned" else None
        script = f"#!/bin/sh\n{prologue}" + "\n".join(hook_body(hook, pinned)) + "\necho reached-the-end\n"
        full_env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "HOUSE_RULES_PY": sys.executable, "HOUSE_RULES_BIN": str(self.tool), **env}
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=self.repo, env=full_env)

    def called(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_in_a_session_the_note_follows_infer(self) -> None:
        for variant in HOOK_VARIANTS:
            with self.subTest(variant):
                self.calls.unlink(missing_ok=True)
                proc = self.run_block("post-commit", variant, **{auto.SESSION_ID_ENV: SESSION})
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                self.assertEqual(self.called(), ["infer --quiet", "auto note"])
                self.assertIn("reached-the-end", proc.stdout)

    def test_outside_a_session_python_is_not_even_started_for_the_note(self) -> None:
        for variant in HOOK_VARIANTS:
            for env in ({}, {auto.SESSION_ID_ENV: ""}):
                with self.subTest(variant=variant, env=env):
                    self.calls.unlink(missing_ok=True)
                    self.run_block("post-commit", variant, **env)
                    self.assertEqual(self.called(), ["infer --quiet"])

    def test_pre_commit_never_notes(self) -> None:
        for variant in HOOK_VARIANTS:
            with self.subTest(variant):
                self.calls.unlink(missing_ok=True)
                self.run_block("pre-commit", variant, **{auto.SESSION_ID_ENV: SESSION})
                self.assertEqual(self.called(), ["compile --check --staged"])

    def test_a_note_that_fails_does_not_end_the_hook_even_under_set_e_and_set_u(self) -> None:
        for variant in HOOK_VARIANTS:
            for flags in ("", "set -e\n", "set -eu\n"):
                with self.subTest(variant=variant, flags=flags):
                    self.calls.unlink(missing_ok=True)
                    proc = self.run_block("post-commit", variant, prologue=flags, TOOL_EXIT="3", **{auto.SESSION_ID_ENV: SESSION})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("reached-the-end", proc.stdout)
                    self.assertEqual(self.called(), ["infer --quiet", "auto note"])

    def test_without_the_tool_the_note_is_not_tried_either(self) -> None:
        self.tool.unlink()
        for variant in HOOK_VARIANTS:
            with self.subTest(variant):
                proc = self.run_block("post-commit", variant, **{auto.SESSION_ID_ENV: SESSION})
                self.assertEqual(proc.returncode, 0)
                self.assertIn("post-commit is NOT running", proc.stderr)
                self.assertEqual(self.called(), [])

    def test_the_block_names_the_variable_the_note_reads_and_the_note_comes_after_infer(self) -> None:
        for pinned in (None, (Path("/py/python3"), Path("/bin/house-rules"))):
            body = "\n".join(hook_body("post-commit", pinned))
            with self.subTest(pinned=pinned is not None):
                self.assertIn(f'[ -z "${{{auto.SESSION_ID_ENV}:-}}" ] || "$_house_rules_py" "$_house_rules_bin" auto note || true', body)
                self.assertLess(body.index("infer --quiet"), body.index("auto note"))
                self.assertEqual(body.count("auto note"), 1)
        self.assertNotIn("auto note", "\n".join(hook_body("pre-commit", None)))


# ---- init: old blocks, and the probe --------------------------------------------------------------------------------------

# The post-commit blocks init wrote before the note line existed (the pinned one with tokens for the two paths).
OLD_PINNED_POST_COMMIT = r"""# Installed by `house-rules init`; re-run it to refresh the paths, delete this block to uninstall.
if [ -n "${HOUSE_RULES_HOOK_PROBE:-}" ]; then
  echo "house-rules-hook-probe post-commit"
  exit 0
fi
_house_rules_py=@PY@
_house_rules_bin=@BIN@
# core.hooksPath can point many repos at one hooks directory; only repos with a config use house-rules.
if [ -f .house-rules.json ]; then
  if [ -x "$_house_rules_py" ] && [ -f "$_house_rules_bin" ]; then
    "$_house_rules_py" "$_house_rules_bin" infer --quiet || true
  else
    printf '%s\n' "house-rules: WARNING: cannot run $_house_rules_bin with $_house_rules_py (interpreter or script missing); post-commit is NOT running. Re-run 'house-rules init' to refresh this hook." >&2
  fi
fi
unset _house_rules_py _house_rules_bin"""

OLD_PORTABLE_POST_COMMIT = r"""# Installed by `house-rules init`; portable (no machine-specific paths), so it is safe to commit. Delete this block to uninstall.
# The tool is $HOUSE_RULES_BIN, else `bin` in .house-rules.json (relative to the repo root), else `house-rules` on PATH.
# Python is $HOUSE_RULES_PY, else python3 on PATH; it must be 3.10 or newer.
if [ -n "${HOUSE_RULES_HOOK_PROBE:-}" ]; then
  echo "house-rules-hook-probe post-commit"
  exit 0
fi
# core.hooksPath can point many repos at one hooks directory; only repos with a config use house-rules.
if [ -f .house-rules.json ]; then
  _house_rules_py="${HOUSE_RULES_PY:-python3}"
  if "$_house_rules_py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    _house_rules_bin="${HOUSE_RULES_BIN:-}"
    if [ -z "$_house_rules_bin" ]; then
      _house_rules_bin=$("$_house_rules_py" -c 'import json; print(json.load(open(".house-rules.json")).get("bin", ""))' 2>/dev/null || true)
      if [ -z "$_house_rules_bin" ]; then
        _house_rules_bin=$(command -v house-rules || true)
      fi
    fi
    if [ -f "$_house_rules_bin" ]; then
      "$_house_rules_py" "$_house_rules_bin" infer --quiet || true
    else
      printf '%s\n' "house-rules: WARNING: cannot find the house-rules executable (set HOUSE_RULES_BIN, add \"bin\" to .house-rules.json, or put house-rules on PATH); post-commit is NOT running." >&2
    fi
  else
    printf '%s\n' "house-rules: WARNING: $_house_rules_py is missing or older than Python 3.10 (set HOUSE_RULES_PY to a 3.10+ interpreter); post-commit is NOT running." >&2
  fi
fi
unset _house_rules_py _house_rules_bin"""

USER_CODE_BELOW = "echo mine, below the block\n"


class UpgradeOldBlockTest(test_init.InitCase):
    """A repo whose post-commit hook has the block of an earlier init: a rerun of init must bring the note line in and say so."""

    def old_hook(self, directory: Path, portable: bool) -> Path:
        body = OLD_PORTABLE_POST_COMMIT if portable else (OLD_PINNED_POST_COMMIT.replace("@PY@", shlex.quote(sys.executable))
                                                          .replace("@BIN@", shlex.quote(str(EXECUTABLE_PATH))))
        path = directory / "post-commit"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n\n{HASH_BEGIN}\n{body}\n{HASH_END}\n\n{USER_CODE_BELOW}", encoding="utf-8")
        path.chmod(0o755)  # executable, so that a change of status can only come from the text
        return path

    def check_upgrade(self, path: Path) -> None:
        before = path.read_text(encoding="utf-8")
        self.assertNotIn("auto note", before)
        out = self.init("--yes")
        self.assertEqual(self.statuses(out)["hook post-commit"], "updated")
        after = path.read_text(encoding="utf-8")
        self.assertIn('"$_house_rules_py" "$_house_rules_bin" auto note || true', after)
        self.assertEqual(after.count(HASH_BEGIN), 1)
        self.assertTrue(after.endswith(f"{HASH_END}\n\n{USER_CODE_BELOW}"))  # what is outside the block is untouched
        self.assertEqual(after.count("auto note"), 1)
        again = self.init("--yes")
        self.assertEqual(self.statuses(again)["hook post-commit"], "already ok")
        self.assertEqual(path.read_text(encoding="utf-8"), after)

    def test_a_pinned_block_in_dot_git_hooks_is_upgraded(self) -> None:
        self.check_upgrade(self.old_hook(self.repo / ".git" / "hooks", portable=False))

    def test_a_portable_block_in_a_hooks_path_inside_the_work_tree_is_upgraded(self) -> None:
        git(self.repo, "config", "core.hooksPath", ".githooks")
        self.check_upgrade(self.old_hook(self.repo / ".githooks", portable=True))

    def test_a_pinned_block_in_a_hooks_path_outside_the_work_tree_is_upgraded(self) -> None:
        shared = self.tmp / "shared-hooks"
        git(self.repo, "config", "core.hooksPath", str(shared))
        self.check_upgrade(self.old_hook(shared, portable=False))

    def test_pre_commit_is_not_touched_by_the_upgrade(self) -> None:
        self.old_hook(self.repo / ".git" / "hooks", portable=False)
        self.init("--yes")
        self.assertNotIn("auto note", self.text(".git/hooks/pre-commit"))
        self.assertEqual(self.statuses(self.init("--yes"))["hook pre-commit"], "already ok")

    def test_a_fresh_install_has_the_note_line_in_post_commit_only(self) -> None:
        self.init("--yes")
        self.assertIn("auto note || true", self.text(".git/hooks/post-commit"))
        self.assertNotIn("auto note", self.text(".git/hooks/pre-commit"))

    def test_the_old_blocks_above_are_what_the_tool_wrote_apart_from_the_note_line(self) -> None:
        """A guard on this file's constants: they must stay the blocks of an earlier init, so they match today's but for that line."""
        for pinned, old in (((Path("@PY@"), Path("@BIN@")), OLD_PINNED_POST_COMMIT), (None, OLD_PORTABLE_POST_COMMIT)):
            current = [line for line in hook_body("post-commit", pinned) if "auto note" not in line]
            with self.subTest(pinned=pinned is not None):
                self.assertEqual("\n".join(current), old)


class ProbeTest(test_init.InitCase):
    """init runs each installed hook once to prove it is reached; in a Claude Code session that run must not count as a commit."""

    def run_hook(self, repo: Path, path: Path, state: Path) -> None:
        proc = subprocess.run([str(path)], cwd=repo, capture_output=True, text=True, env={**_GIT_ENV, **session_env(state)})
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_the_probe_leaves_no_record_but_the_same_hook_run_for_real_does(self) -> None:
        for variant, hooks_path in HOOK_VARIANTS.items():
            with self.subTest(variant):
                repo = self.tmp / variant / "demo-repo"
                repo.mkdir(parents=True)
                git(repo, "init", "-q", "-b", "main")
                git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "base")  # a HEAD to note
                if hooks_path:
                    git(repo, "config", "core.hooksPath", hooks_path)
                state = self.tmp / variant / "state"
                with mock.patch.dict(os.environ, session_env(state)):  # the probe inherits this environment
                    code, _, err = self.cli("init", "--yes", "--repo", str(repo), with_repo=False)
                self.assertEqual(code, 0, err)
                self.assertFalse(state.exists())  # the probe ran both hooks, with the session in their environment, and left nothing
                self.run_hook(repo, repo / (hooks_path or ".git/hooks") / "post-commit", state)
                self.assertEqual(len(read_lines(state / auto.PENDING_NAME / f"{SESSION}.jsonl")), 1)


# ---- the suite itself ---------------------------------------------------------------------------------------------------

class SuiteEnvironmentTest(unittest.TestCase):
    """What tests/support.py promises every test module: a developer's real session and state are out of reach."""

    def test_no_claude_variable_and_no_review_marker_reaches_a_test(self) -> None:
        self.assertEqual([name for name in os.environ if name.startswith("CLAUDE")], [])
        for name in (*SCRUBBED_NAMES, *support.SCRUBBED_ENV):
            self.assertNotIn(name, os.environ)

    def test_support_clears_what_a_developer_session_exports_when_it_is_imported(self) -> None:
        """The checks above bite only when this run was started with those variables set; a process that imports support with them does."""
        real_state = Path.home() / ".local" / "state" / auto.STATE_NAME
        polluted = {**os.environ, **dict.fromkeys(SCRUBBED_NAMES, "1"), auto.SESSION_ID_ENV: SESSION, "CLAUDE_CODE_ENTRYPOINT": "cli",
                    auto.STATE_DIR_ENV: str(real_state), "PYTHONPATH": str(Path(support.__file__).parent)}
        proc = subprocess.run([sys.executable, "-c", "import json, os, support; print(json.dumps(dict(os.environ)))"], env=polluted,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertEqual([name for name in seen if name.startswith("CLAUDE") or name in SCRUBBED_NAMES], [])
        state = Path(seen[auto.STATE_DIR_ENV])
        self.assertNotEqual(state, real_state)
        self.assertTrue(state.is_relative_to(Path(os.path.realpath(tempfile.gettempdir()))))

    def test_the_state_directory_is_one_made_for_this_run(self) -> None:
        state = Path(os.environ[auto.STATE_DIR_ENV])
        self.assertEqual(auto.state_dir(), state)
        self.assertTrue(state.is_dir())
        self.assertTrue(state.is_relative_to(Path(os.path.realpath(tempfile.gettempdir()))))
        self.assertFalse(state.is_relative_to(Path.home() / ".local"))

    def test_the_commits_the_helpers_make_inherit_the_same_clean_environment(self) -> None:
        self.assertEqual([name for name in _GIT_ENV if name.startswith("CLAUDE")], [])
        self.assertEqual(_GIT_ENV[auto.STATE_DIR_ENV], os.environ[auto.STATE_DIR_ENV])


if __name__ == "__main__":
    unittest.main()
