"""`ratchet auto stop`: the plugin's Stop hook judges the commits the git hook noted and asks Claude for the wrap-up when it is due.

The commits are real: a copy of a repo `ratchet init` set up takes `git commit`s with the session id in their environment, hooks and
all, in the main worktree and in a linked one (`git -C`). The hook is run as Claude Code runs it: a process with the Stop input on
stdin. No test reaches the real claude: the wrap-ups that run use an executable fake.
"""
import contextlib
import io
import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts the repo root on sys.path, and starts every test from a clean environment)
import test_wrapup
from support import _GIT_ENV, REPO_ROOT, git, review_round
from test_wrapup import numbered, review_text

from ratchet import auto, auto_stop
from ratchet.auto import PendingCommit
from ratchet.cli import main
from ratchet.hooks import _PYTHON_OK_CHECK
from ratchet.review import Refusal, round_key

SESSION = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
OTHER_SESSION = "f0e1d2c3-b4a5-6789-0123-456789abcdef"
BIN = REPO_ROOT / "bin" / "ratchet"
RUN_SH = REPO_ROOT / "hooks" / "run.sh"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
TEMPLATE: list[Path] = []
HOOK_LOG_LINE = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 auto stop: "
FAST_EXIT_CPU_SECONDS = 0.3  # a bin/ratchet start costs 0.07 s of CPU here; the budget of the hook is about 0.1 s of wall time


def setUpModule() -> None:
    """One repo that `ratchet init` set up and committed; each test works on a copy, as init takes most of a second."""
    holder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(holder.cleanup)
    repo = Path(os.path.realpath(holder.name)) / "demo-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["init", "--yes", "--repo", str(repo)])
    if code != 0:
        raise AssertionError(f"init failed: {err.getvalue()}")
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "set up ratchet")  # no session: nothing noted
    TEMPLATE.append(repo)


def stop_input(**fields: object) -> str:
    """What Claude Code writes to a Stop hook's stdin."""
    payload = {"session_id": SESSION, "cwd": "/somewhere/else", "transcript_path": "/somewhere/else/t.jsonl", "permission_mode": "default",
               "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "Done.", "session_crons": [],
               "background_tasks": []}
    return json.dumps({**payload, **fields})


def task(kind: str, **fields: object) -> dict:
    return {"id": "bg1", "type": kind, "status": "running", "description": "a task", **fields}


def due_line(worktree: Path, lines: int = 250, files: int = 1) -> str:
    """The line the hook gives Claude for a worktree that is due, at the default thresholds."""
    return (f"review-ratchet: {worktree} has {lines} changed line(s) in {files} file(s) since its last review, past the wrap-up "
            f"threshold (200 lines or 5 files). Run the automatic wrap-up now: /review-ratchet:wrapup auto {worktree} "
            f"(Skill tool: review-ratchet:wrapup, args: auto {worktree}; equivalent: ratchet wrapup --repo {worktree}).")


def refusal_line(worktree: Path, message: str) -> str:
    return f"review-ratchet: the wrap-up review of {worktree} is due but would be refused: {message}"


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def stop_command() -> str:
    """The command line of the plugin's Stop hook, as hooks.json gives it to Claude Code."""
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]["command"]


class StopCase(test_wrapup.WrapupCase):
    """A copy of the repo init set up, a state directory of the test's own, and a claude that exists but is never run by a Stop."""

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.repo)
        shutil.copytree(TEMPLATE[0], self.repo, symlinks=True)
        self.state = self.tmp / "state"  # not made: the first note has to
        patcher = mock.patch.dict(os.environ, {auto.STATE_DIR_ENV: str(self.state)})  # for what a test runs in-process
        patcher.start()
        self.addCleanup(patcher.stop)
        self.claude = self.fake_claude(review_text())
        self.work_count = 0

    # ---- the repo -------------------------------------------------------------------------------------------------

    def begin(self, **config: object) -> None:
        """Start the branch `feature` that the work goes on. `config` goes into the config first, committed on main where the branch
        forks from, so that it is not part of the work."""
        if config:
            self.set_config(**config)
            git(self.repo, "add", ".ratchet.json")
            git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "configure")
        git(self.repo, "checkout", "-q", "-b", "feature")

    def linked(self, branch: str = "side") -> Path:
        """A linked worktree on a branch of its own, forked from main."""
        path = self.tmp / f"linked-{branch}"
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        return path

    def another_repo(self) -> Path:
        """A second repo that init set up, on its branch `feature`, for a session that commits in two."""
        path = self.tmp / "other"
        shutil.copytree(TEMPLATE[0], path, symlinks=True)
        git(path, "checkout", "-q", "-b", "feature")
        return path

    def session_env(self, session: str = SESSION) -> dict[str, str]:
        return {auto.STATE_DIR_ENV: str(self.state), auto.SESSION_ID_ENV: session}

    def commit_work(self, top: Path | None = None, *, lines: int = 250, session: str = SESSION) -> str:
        """A new file of `lines` lines, committed as Claude Code's Bash tool would: `git -C top commit` with the session id in the
        environment, so the post-commit hook init installed notes it. The pre-commit check is left out."""
        top = top or self.repo
        self.work_count += 1
        name = f"work-{self.work_count}.py"
        (top / name).write_text(numbered(lines), encoding="utf-8")
        git(top, "add", name)
        proc = subprocess.run(["git", "-C", str(top), "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", f"add {name}"],
                              cwd=self.tmp, capture_output=True, text=True, env={**_GIT_ENV, **self.session_env(session)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return git(top, "rev-parse", "HEAD")

    def set_config(self, root: Path | None = None, **changes: object) -> None:
        """Change keys of the config on disk, uncommitted."""
        path = (root or self.repo) / ".ratchet.json"
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **changes}), encoding="utf-8")

    def cap_the_branch(self, top: Path | None = None) -> str:
        """One round used on the branch of `top`, with `max_review_rounds` of 1 (set it in `begin`): its next wrap-up is refused."""
        key = round_key(top or self.repo, None, None)
        self.append_raw(review_round(key, run=f"past{len(self.events()) + 1}"))  # not an `r<k>` id, which the next review would pick
        return key

    # ---- the hook -------------------------------------------------------------------------------------------------

    def hook_env(self, overrides: dict[str, str | None] | None = None) -> dict[str, str]:
        env = {**os.environ, auto.STATE_DIR_ENV: str(self.state), "RATCHET_CLAUDE_CMD": str(self.claude)}
        for name, value in (overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def stop(self, text: str | None = None, *, env: dict[str, str | None] | None = None, **fields: object) -> tuple[int, str, str]:
        """`ratchet auto stop` as a process, started from a directory that is no repo, with the Stop input `fields` make (or `text`)."""
        proc = subprocess.run([sys.executable, str(BIN), "auto", "stop"], input=stop_input(**fields) if text is None else text,
                              cwd=self.tmp, capture_output=True, text=True, env=self.hook_env(env), timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def answer(self, text: str | None = None, *, env: dict[str, str | None] | None = None, **fields: object) -> dict | None:
        """The object the hook printed, or None when it printed nothing. It must exit 0 and print nothing on stderr either way."""
        code, out, err = self.stop(text, env=env, **fields)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out) if out else None

    def assert_silent(self, text: str | None = None, *, env: dict[str, str | None] | None = None, **fields: object) -> None:
        self.assertEqual(self.stop(text, env=env, **fields), (0, "", ""))

    def context_lines(self, answer: dict | None) -> list[str]:
        """What the hook told Claude, a line for each worktree; the answer must be that and nothing else."""
        self.assertIsNotNone(answer)
        self.assertEqual(list(answer), ["hookSpecificOutput"])
        self.assertEqual(answer["hookSpecificOutput"]["hookEventName"], "Stop")
        return answer["hookSpecificOutput"]["additionalContext"].split("\n")

    def assert_asks_for(self, answer: dict | None, worktree: Path | None = None) -> None:
        """The hook asked for the wrap-up of exactly one worktree, whatever the counts."""
        [line] = self.context_lines(answer)
        top = worktree or self.repo
        self.assertTrue(line.startswith(f"review-ratchet: {top} has "), line)
        self.assertIn(f"/review-ratchet:wrapup auto {top} ", line)

    # ---- what it leaves -------------------------------------------------------------------------------------------

    def pending_file(self, session: str = SESSION) -> Path:
        return self.state / auto.PENDING_NAME / f"{session}.jsonl"

    def claimed_files(self) -> list[Path]:
        directory = self.state / auto.CLAIMED_NAME
        return sorted(directory.glob("*")) if directory.exists() else []

    def hook_log(self) -> list[str]:
        path = self.state / auto.HOOK_LOG_NAME
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def announced(self) -> dict[str, str]:
        path = self.state / auto_stop.ANNOUNCED_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def plant(self, worktree: Path | None = None, session: str = SESSION) -> None:
        """A note made by hand: the commit it names need not exist, which suits the checks that never get as far as git."""
        path = self.pending_file(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(PendingCommit(worktree or self.repo, "0" * 40, auto.utc_timestamp()).to_line())

    def assert_held_back_and_then_due(self, **later: object) -> None:
        """The pending file is still there; a Stop without the condition that held it back then asks for the wrap-up."""
        self.assertTrue(self.pending_file().exists())
        self.assertEqual(self.context_lines(self.answer(**later)), [due_line(self.repo)])
        self.assertFalse(self.pending_file().exists())


# ---- the cheap exits ----------------------------------------------------------------------------------------------

class FastExitTest(StopCase):
    def setUp(self) -> None:
        super().setUp()
        self.begin()
        self.commit_work()

    def test_a_review_child_is_left_alone_and_its_pending_file_stays(self) -> None:
        self.assert_silent(env={"RATCHET_REVIEW_CHILD": "1"})
        self.assert_held_back_and_then_due()

    def test_only_a_review_child_marker_of_exactly_1_silences_it(self) -> None:
        for value in ("0", "", "true"):
            with self.subTest(value=value):
                self.assert_asks_for(self.answer(env={"RATCHET_REVIEW_CHILD": value}))
                self.commit_work()

    def test_a_stop_that_continues_after_a_stop_hook_is_left_alone_and_its_pending_file_stays(self) -> None:
        self.assert_silent(stop_hook_active=True)
        self.assert_held_back_and_then_due(stop_hook_active=False)

    def test_a_headless_session_is_left_alone_unless_the_pipeline_says_otherwise(self) -> None:
        self.assert_silent(env={"CLAUDE_CODE_SESSION_ATTENDED": "0"})
        self.assertTrue(self.pending_file().exists())
        for value in ("0", "", "true"):  # only a 1 is the pipeline's say-so, as for the review child marker
            with self.subTest(headless_ok=value):
                self.assert_silent(env={"CLAUDE_CODE_SESSION_ATTENDED": "0", "RATCHET_AUTO_HEADLESS": value})
        self.assert_asks_for(self.answer(env={"CLAUDE_CODE_SESSION_ATTENDED": "0", "RATCHET_AUTO_HEADLESS": "1"}))

    def test_an_attended_session_is_judged_whether_the_variable_says_so_or_is_missing_or_odd(self) -> None:
        for value in ("1", None, "yes", ""):
            with self.subTest(value=value):
                self.assert_asks_for(self.answer(env={"CLAUDE_CODE_SESSION_ATTENDED": value}))
                self.commit_work()

    def test_input_that_is_empty_or_not_an_object_is_left_alone(self) -> None:
        for text in ("", "  \n", "not json", "[1, 2]", "42"):
            with self.subTest(text=text):
                self.assert_silent(text, env={auto.SESSION_ID_ENV: SESSION})  # a real hook has the session in its environment too
                self.assertTrue(self.pending_file().exists())

    def test_input_that_is_not_an_object_is_told_in_hook_log_but_empty_input_is_not(self) -> None:
        self.assert_silent("")
        self.assert_silent("  \n")
        self.assertEqual(self.hook_log(), [])
        self.assert_silent("not json")
        self.assert_silent("[1, 2]")
        first, second = self.hook_log()
        self.assertRegex(first, HOOK_LOG_LINE + "the input is not JSON")
        self.assertRegex(second, HOOK_LOG_LINE + "the input is a JSON list, not an object")

    def test_a_session_with_nothing_noted_is_left_alone(self) -> None:
        self.assert_silent(session_id=OTHER_SESSION)
        self.assertTrue(self.pending_file().exists())

    def test_an_input_session_id_that_is_no_id_is_replaced_by_the_one_in_the_environment(self) -> None:
        self.assert_silent(session_id="../escape", env={auto.SESSION_ID_ENV: OTHER_SESSION})  # the environment's id has no file
        self.assertTrue(self.pending_file().exists())
        for bad in ("../escape", ["x"], None, ""):
            with self.subTest(bad=bad):
                self.assert_asks_for(self.answer(session_id=bad, env={auto.SESSION_ID_ENV: SESSION}))
                self.commit_work()

    def test_without_a_session_id_in_the_input_the_environment_decides(self) -> None:
        payload = json.loads(stop_input())
        del payload["session_id"]
        self.assert_silent(json.dumps(payload))  # and the environment of this test has none
        self.assert_asks_for(self.answer(json.dumps(payload), env={auto.SESSION_ID_ENV: SESSION}))

    def test_one_session_never_takes_the_pending_file_of_another(self) -> None:
        self.commit_work(session=OTHER_SESSION)
        self.assertEqual(self.context_lines(self.answer(session_id=OTHER_SESSION)), [due_line(self.repo, lines=500, files=2)])
        self.assertTrue(self.pending_file().exists())


class NothingNotedCostsNothingTest(StopCase):
    """The hook ends every turn of every session, so a session with nothing noted must not reach git, the ledger or the state files."""

    def test_nothing_is_created_and_nothing_is_told(self) -> None:
        self.assert_silent()
        self.assertFalse(self.state.exists())

    def test_no_process_is_started_before_the_pending_file_has_been_found(self) -> None:
        self.plant()
        cases = [("a review child", {"RATCHET_REVIEW_CHILD": "1"}, stop_input()), ("headless", {"CLAUDE_CODE_SESSION_ATTENDED": "0"}, stop_input()),
                 ("active", {}, stop_input(stop_hook_active=True)), ("no input", {}, ""), ("another session", {}, stop_input(session_id=OTHER_SESSION))]
        for label, env, text in cases:
            with self.subTest(label), mock.patch.dict(os.environ, env), mock.patch("subprocess.run", side_effect=AssertionError("git was started")), \
                    mock.patch("subprocess.Popen", side_effect=AssertionError("a process was started")), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(auto_stop.stop_hook(io.StringIO(text)), 0)
                self.assertEqual(out.getvalue(), "")
        self.assertTrue(self.pending_file().exists())

    def test_what_a_session_with_nothing_noted_imports_stays_small(self) -> None:
        script = ("import io, sys; sys.path.insert(0, sys.argv[1])\n"
                  "from ratchet import auto_stop\n"
                  "auto_stop.stop_hook(io.StringIO('{\"session_id\": \"s1\"}'))\n"
                  "heavy = {'ratchet.cli', 'ratchet.wrapup', 'ratchet.onboard', 'ratchet.proposals', 'ratchet.state_tree'}\n"
                  "print(sorted(heavy & set(sys.modules)))\n")
        proc = subprocess.run([sys.executable, "-c", script, str(REPO_ROOT)], capture_output=True, text=True, env=self.hook_env())
        self.assertEqual((proc.returncode, proc.stdout.strip(), proc.stderr), (0, "[]", ""))

    def test_the_whole_exit_through_the_executable_costs_well_under_the_budget(self) -> None:
        runs = 5
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        for _ in range(runs):
            self.assert_silent()
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        self.assertLess(cpu / runs, FAST_EXIT_CPU_SECONDS)


# ---- waiting for background tasks ---------------------------------------------------------------------------------

class DeferralTest(StopCase):
    def setUp(self) -> None:
        super().setUp()
        self.begin()
        self.commit_work()

    def test_a_running_subagent_workflow_or_teammate_holds_the_judgement_back(self) -> None:
        for kind in ("subagent", "workflow", "teammate"):
            with self.subTest(kind=kind):
                self.assert_silent(background_tasks=[task(kind)])
                self.assertEqual(self.claimed_files(), [])
        self.assert_held_back_and_then_due(background_tasks=[])

    def test_a_shell_running_a_ratchet_wrapup_or_review_holds_it_back(self) -> None:
        commands = ("ratchet wrapup --repo /x --json", "python3.12 /p/bin/ratchet review --level high", "/plugin/bin/ratchet wrapup",
                    "cd /x && ratchet wrapup --json 2>&1")
        for command in commands:
            with self.subTest(command=command):
                self.assert_silent(background_tasks=[task("shell", command=command)])
        self.assert_held_back_and_then_due(background_tasks=[task("shell", command="ratchet count --by-rule")])

    def test_other_shells_monitors_and_kinds_never_hold_it_back(self) -> None:
        for background in (task("shell", command="npm run dev"), task("shell", command="ratchet label r1-1 --outcome dismissed"),
                           task("shell", command="myratchet wrapup --repo /x"), task("shell", command="ratchet reviewer"),
                           task("shell"), task("monitor"), task("cloud session"), task("MCP task")):
            with self.subTest(background=background):
                self.assert_asks_for(self.answer(background_tasks=[background]))
                self.commit_work()

    def test_a_task_that_has_ended_holds_nothing_back(self) -> None:
        for status in ("completed", "Completed", "failed", "killed", "stopped", "cancelled", "exited"):
            with self.subTest(status=status):
                self.assert_asks_for(self.answer(background_tasks=[task("subagent", status=status)]))
                self.commit_work()

    def test_a_task_whose_status_is_missing_or_unknown_is_taken_for_running(self) -> None:
        missing = task("subagent")
        del missing["status"]
        for background in (missing, task("subagent", status="running"), task("subagent", status="pending"), task("subagent", status="")):
            with self.subTest(background=background):
                self.assert_silent(background_tasks=[background])
                self.assertTrue(self.pending_file().exists())

    def test_one_task_among_others_is_enough(self) -> None:
        self.assert_silent(background_tasks=[task("shell", command="npm run dev"), task("monitor"), task("subagent")])
        self.assertTrue(self.pending_file().exists())

    def test_every_status_that_means_over_holds_nothing_back_in_any_case(self) -> None:
        for status in ("completed", "complete", "done", "finished", "succeeded", "failed", "error", "killed", "stopped", "cancelled",
                       "canceled", "aborted", "exited", "terminated"):
            for spelled in (status, status.title(), status.upper()):
                with self.subTest(status=spelled):
                    self.assertFalse(auto_stop._keeps_waiting(task("subagent", status=spelled)))

    def test_an_input_without_background_tasks_is_judged(self) -> None:  # what a Claude Code before 2.1.145 sends
        payload = json.loads(stop_input())
        del payload["background_tasks"]
        self.assert_asks_for(self.answer(json.dumps(payload)))


# ---- judging ------------------------------------------------------------------------------------------------------

class DueTest(StopCase):
    def test_work_past_the_threshold_asks_claude_for_the_wrapup_naming_the_worktree_and_the_command(self) -> None:
        self.begin()
        self.commit_work()
        code, out, err = self.stop()
        self.assertEqual((code, err), (0, ""))
        answer = json.loads(out)
        self.assertEqual(list(answer), ["hookSpecificOutput"])
        self.assertEqual(answer["hookSpecificOutput"], {"hookEventName": "Stop", "additionalContext": due_line(self.repo)})
        self.assertEqual(out.count("\n"), 1)  # one line of JSON

    def test_the_text_holds_what_claude_needs_to_act_on(self) -> None:
        self.begin()
        self.commit_work()
        [line] = self.context_lines(self.answer())
        for part in ("250 changed line(s) in 1 file(s)", "since its last review", "200 lines or 5 files", str(self.repo),
                     f"/review-ratchet:wrapup auto {self.repo}", f"args: auto {self.repo}", f"ratchet wrapup --repo {self.repo}"):
            self.assertIn(part, line)

    def test_the_thresholds_are_the_ones_of_the_repos_config(self) -> None:
        self.begin(wrapup_min_changed_lines=100, wrapup_min_files=2)
        self.commit_work(lines=120)
        [line] = self.context_lines(self.answer())
        self.assertIn("120 changed line(s) in 1 file(s)", line)
        self.assertIn("(100 lines or 2 files)", line)

    def test_a_path_with_a_space_is_quoted_for_the_command(self) -> None:
        self.begin()
        spaced = self.tmp / "my repo"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(spaced), "main")
        self.commit_work(spaced)
        quoted = shlex.quote(str(spaced))
        [line] = self.context_lines(self.answer())
        self.assertIn(f"review-ratchet: {quoted} has 250 changed line(s)", line)
        self.assertIn(f"/review-ratchet:wrapup auto {quoted} ", line)
        self.assertIn(f"args: auto {quoted};", line)
        self.assertTrue(line.endswith(f"ratchet wrapup --repo {quoted})."))

    def test_the_pending_file_is_consumed_and_the_claimed_one_is_deleted(self) -> None:
        self.begin()
        self.commit_work()
        self.answer()
        self.assertFalse(self.pending_file().exists())
        self.assertEqual(self.claimed_files(), [])
        self.assertTrue((self.state / auto.CLAIMED_NAME).is_dir())
        self.assert_silent()  # the same turn judged again says nothing: the commits are not counted twice

    def test_a_commit_in_a_linked_worktree_made_with_git_dash_c_is_judged_in_that_worktree(self) -> None:
        self.begin()
        linked = self.linked()
        self.commit_work(linked)
        self.assertEqual(self.context_lines(self.answer()), [due_line(linked)])

    def test_the_main_worktree_and_a_linked_one_are_each_asked_for(self) -> None:
        self.begin()
        linked = self.linked()
        self.commit_work()
        self.commit_work(linked, lines=300)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo), due_line(linked, lines=300)])

    def test_many_commits_in_one_worktree_are_one_line(self) -> None:
        self.begin()
        for _ in range(3):
            self.commit_work(lines=100)
        self.assertEqual(len(read_lines(self.pending_file())), 3)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=300, files=3)])

    def test_work_too_small_to_review_says_nothing_and_its_record_is_consumed(self) -> None:
        self.begin()
        self.commit_work(lines=50)
        self.assert_silent()
        self.assertFalse(self.pending_file().exists())
        self.assertEqual(self.claimed_files(), [])

    def test_small_commits_add_up_over_the_turns_until_the_threshold(self) -> None:
        self.begin()
        self.commit_work(lines=120)
        self.assert_silent()
        self.commit_work(lines=120)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=240, files=2)])

    def test_the_files_threshold_counts_too(self) -> None:
        self.begin()
        for _ in range(5):
            self.commit_work(lines=10)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=50, files=5)])

    def test_a_worktree_whose_path_is_gone_is_dropped_and_the_others_are_still_judged(self) -> None:
        self.begin()
        gone = self.linked("gone")
        self.commit_work(gone)
        self.commit_work()
        shutil.rmtree(gone)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo)])
        self.assertEqual(self.hook_log(), [])  # a path that is gone is no failure

    def test_a_repo_that_does_not_ask_for_the_wrapup_any_more_is_dropped(self) -> None:
        self.begin()
        self.commit_work()
        self.set_config(wrapup_auto=False)
        self.assert_silent()
        self.assertFalse(self.pending_file().exists())

    def test_a_repo_whose_config_lost_the_key_is_dropped_too(self) -> None:
        self.begin()
        self.commit_work()
        path = self.repo / ".ratchet.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        del config["wrapup_auto"]
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assert_silent()
        self.assertFalse(self.pending_file().exists())

    def test_each_repo_of_a_session_that_commits_in_two_is_judged_by_its_own_config(self) -> None:
        self.begin()
        other = self.another_repo()
        self.commit_work()
        self.commit_work(other)
        self.set_config(other, wrapup_auto=False)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo)])


# ---- after a wrap-up ----------------------------------------------------------------------------------------------

class BaselineMovesTest(StopCase):
    def wrap_up(self, *argv: str, with_repo: bool = True) -> dict:
        """`ratchet wrapup --json` with the fake claude: the object it printed."""
        with mock.patch.dict(os.environ, {"RATCHET_CLAUDE_CMD": str(self.claude)}):
            code, out, err = self.cli("wrapup", "--json", *argv, with_repo=with_repo)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_after_the_wrapup_has_run_another_commit_and_stop_say_nothing(self) -> None:
        self.begin()
        linked = self.linked()
        self.commit_work(linked)
        self.assertEqual(self.wrap_up("--repo", str(linked), with_repo=False)["status"], "reviewed")
        self.assertEqual(len(self.claude_calls()), 1)
        self.commit_work(linked, lines=20)
        self.assert_silent()
        self.assertFalse(self.pending_file().exists())
        self.assertEqual(len(self.claude_calls()), 1)  # a Stop never reviews

    def test_work_after_the_wrapup_is_counted_from_it_and_asks_again_when_it_is_enough(self) -> None:
        self.begin()
        self.commit_work()
        self.assertEqual(self.wrap_up()["status"], "reviewed")
        self.commit_work(lines=260)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=260)])


# ---- refusals -----------------------------------------------------------------------------------------------------

class RefusalTest(StopCase):
    def test_a_full_round_cap_is_told_to_the_person_once_and_claude_is_not_asked(self) -> None:
        self.begin(max_review_rounds=1)
        key = self.cap_the_branch()
        self.commit_work()
        answer = self.answer()
        self.assertEqual(list(answer), ["systemMessage"])
        [line] = answer["systemMessage"].split("\n")
        self.assertTrue(line.startswith(refusal_line(self.repo, f"round cap reached for {key}: 1/1 rounds used;")), line)
        self.assertTrue(line.endswith("ratchet wrapup has no --over-cap: `ratchet review --over-cap` runs one more round, recorded"), line)

    def test_the_same_refusal_after_another_commit_on_the_capped_branch_is_silent(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.commit_work()
        self.assertIsNotNone(self.answer())
        for _ in range(2):
            self.commit_work(lines=10)
            self.assert_silent()
        self.assertFalse(self.pending_file().exists())

    def test_what_was_told_is_kept_in_announced_json_by_repo_worktree_kind_and_round_key(self) -> None:
        self.begin(max_review_rounds=1)
        key = self.cap_the_branch()
        self.commit_work()
        self.answer()
        [(stored, when)] = self.announced().items()
        self.assertEqual(json.loads(stored), [str(self.repo), str(self.repo), "round_cap", key])
        self.assertLess(abs(datetime.now(timezone.utc) - datetime.fromisoformat(when)), timedelta(minutes=5))

    def test_a_different_kind_of_refusal_for_the_same_worktree_is_told_again(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.commit_work()
        self.assertIn("round cap reached", self.answer()["systemMessage"])
        self.set_config(max_review_rounds=3)
        self.commit_work(lines=10)
        missing = self.tmp / "no-such-claude"
        self.assertIn(f"`{missing}` not found on PATH", self.answer(env={"RATCHET_CLAUDE_CMD": str(missing)})["systemMessage"])
        self.assertEqual(len(self.announced()), 2)
        self.commit_work(lines=10)
        self.assert_silent(env={"RATCHET_CLAUDE_CMD": str(missing)})

    def test_a_refusal_without_a_round_key_is_told_by_its_message(self) -> None:
        self.begin()
        (self.repo / "CODING-STYLE.md").unlink()
        self.commit_work()
        [line] = self.answer()["systemMessage"].split("\n")
        self.assertTrue(line.startswith(refusal_line(self.repo, "")), line)
        [stored] = self.announced()
        self.assertEqual(json.loads(stored)[2:], ["rules", line.removeprefix(refusal_line(self.repo, ""))])
        self.commit_work(lines=10)
        self.assert_silent()

    def test_another_worktree_of_the_repo_has_a_refusal_of_its_own(self) -> None:
        self.begin(max_review_rounds=1)
        linked = self.linked()
        self.cap_the_branch()
        self.cap_the_branch(linked)
        self.commit_work()
        self.assertIn(str(self.repo), self.answer()["systemMessage"])
        self.commit_work(linked)
        message = self.answer()["systemMessage"]
        self.assertIn(str(linked), message)
        self.assertNotIn(str(self.repo), message)
        self.assertEqual(len(self.announced()), 2)
        for stored in self.announced():
            self.assertEqual(json.loads(stored)[0], str(self.repo))  # the repo is its main worktree, whichever worktree the commit was made in

    def test_a_refusal_and_a_worktree_that_is_due_share_one_answer(self) -> None:
        self.begin(max_review_rounds=1)
        linked = self.linked()
        self.cap_the_branch()  # only the main worktree's branch is full
        self.commit_work()
        self.commit_work(linked, lines=300)
        answer = self.answer()
        self.assertEqual(sorted(answer), ["hookSpecificOutput", "systemMessage"])
        self.assertEqual(answer["hookSpecificOutput"]["additionalContext"], due_line(linked, lines=300))
        self.assertTrue(answer["systemMessage"].startswith(refusal_line(self.repo, "round cap reached")))
        self.commit_work()
        self.commit_work(linked, lines=10)
        self.assertEqual(self.context_lines(self.answer()), [due_line(linked, lines=310, files=2)])  # the refusal is not told twice

    def test_a_refusal_is_forgotten_after_30_days_and_not_before(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.commit_work()
        self.answer()
        for days, told_again in ((29, False), (31, True)):
            with self.subTest(days=days):
                then = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
                (self.state / auto_stop.ANNOUNCED_NAME).write_text(json.dumps({key: then for key in self.announced()}), encoding="utf-8")
                self.commit_work(lines=10)
                self.assertEqual(self.answer() is not None, told_again)

    def test_entries_older_than_30_days_are_dropped_when_the_file_is_written_again(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.state.mkdir()
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
        (self.state / auto_stop.ANNOUNCED_NAME).write_text(json.dumps({"stale": old}), encoding="utf-8")
        self.commit_work()
        self.assertIsNotNone(self.answer())
        self.assertEqual(len(self.announced()), 1)
        self.assertNotIn("stale", self.announced())

    def test_a_damaged_announced_file_counts_as_empty_is_told_and_is_written_again(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.state.mkdir()
        (self.state / auto_stop.ANNOUNCED_NAME).write_text("{not json", encoding="utf-8")
        self.commit_work()
        self.assertIn("round cap reached", self.answer()["systemMessage"])
        [line] = self.hook_log()
        self.assertRegex(line, HOOK_LOG_LINE + r".*announced\.json is damaged")
        self.assertEqual(len(self.announced()), 1)

    def test_work_whose_review_would_start_never_touches_the_file(self) -> None:
        self.begin()
        self.commit_work()
        self.answer()
        self.assertFalse((self.state / auto_stop.ANNOUNCED_NAME).exists())

    def test_work_whose_review_would_start_does_not_even_read_the_file(self) -> None:
        self.begin()
        self.state.mkdir()
        path = self.state / auto_stop.ANNOUNCED_NAME
        path.write_text("{not json", encoding="utf-8")
        self.commit_work()
        self.assertIsNotNone(self.answer())
        self.assertEqual((path.read_text(encoding="utf-8"), self.hook_log()), ("{not json", []))  # not read, so not told damaged

    def test_the_refusals_of_two_worktrees_told_at_once_are_a_line_each(self) -> None:
        self.begin(max_review_rounds=1)
        linked = self.linked()
        self.cap_the_branch()
        self.cap_the_branch(linked)
        self.commit_work()
        self.commit_work(linked)
        answer = self.answer()
        self.assertEqual(list(answer), ["systemMessage"])
        told = answer["systemMessage"].split("\n")
        self.assertEqual(len(told), 2, told)
        first, second = told
        self.assertTrue(first.startswith(refusal_line(self.repo, "round cap reached")), first)
        self.assertTrue(second.startswith(refusal_line(linked, "round cap reached")), second)

    def test_a_refusal_message_is_told_on_one_line_whatever_it_holds(self) -> None:
        due = auto_stop.Due(self.repo, self.repo, None)  # the line reads only the worktree
        refusal = Refusal("rules", None, "the first line\n\tthe second,   and a third")
        self.assertEqual(auto_stop._refusal_line(due, refusal), refusal_line(self.repo, "the first line the second, and a third"))

    def test_the_file_is_replaced_whole_so_a_write_that_fails_leaves_it_as_it_was(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.state.mkdir()
        path = self.state / auto_stop.ANNOUNCED_NAME
        before = json.dumps({"a refusal told before": auto.utc_timestamp()})
        path.write_text(before, encoding="utf-8")
        self.commit_work()
        with mock.patch.object(Path, "replace", side_effect=OSError("disk full")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(auto_stop.stop_hook(io.StringIO(stop_input())), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_refusal_is_remembered_before_it_is_told(self) -> None:
        self.begin(max_review_rounds=1)
        self.cap_the_branch()
        self.commit_work()
        with mock.patch("builtins.print", side_effect=BrokenPipeError), contextlib.suppress(BrokenPipeError):  # the hook dies while it tells
            auto_stop.stop_hook(io.StringIO(stop_input()))
        self.assertEqual(len(self.announced()), 1)  # one message lost, where the other order would tell it again at the end of every turn
        self.commit_work(lines=10)
        self.assert_silent()


# ---- two stops at once, and a hook that fails ---------------------------------------------------------------------

class ClaimTest(StopCase):
    def test_a_claim_is_made_once(self) -> None:
        self.plant()
        pending = self.pending_file()
        claimed = auto_stop._claim(pending)
        self.assertEqual(claimed.parent, self.state / auto.CLAIMED_NAME)
        self.assertTrue(claimed.exists() and not pending.exists())
        self.assertIsNone(auto_stop._claim(pending))

    def test_a_second_claim_of_the_session_does_not_take_the_name_of_one_still_being_judged(self) -> None:
        self.plant()
        first = auto_stop._claim(self.pending_file())
        self.plant()  # a commit made while that Stop is still judging
        with mock.patch.object(os, "getpid", return_value=os.getpid() + 1):  # the next Stop is another process
            second = auto_stop._claim(self.pending_file())
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_two_stops_at_once_judge_the_file_once(self) -> None:
        self.begin()
        self.commit_work()
        processes = [subprocess.Popen([sys.executable, str(BIN), "auto", "stop"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, cwd=self.tmp, env=self.hook_env()) for _ in range(2)]
        for process in processes:  # both have started before either has its input, so both read it and go to the file at about one time
            process.stdin.write(stop_input())
            process.stdin.flush()
        results = [(process.communicate(timeout=120), process.returncode) for process in processes]
        self.assertEqual([code for _, code in results], [0, 0])
        self.assertEqual([err for (_, err), _ in results], ["", ""])
        outputs = sorted(out for (out, _), _ in results)
        self.assertEqual(outputs[0], "")
        self.assertEqual(self.context_lines(json.loads(outputs[1])), [due_line(self.repo)])
        self.assertEqual(self.claimed_files(), [])

    def test_a_file_a_killed_hook_left_in_claimed_is_not_read_again(self) -> None:
        self.begin()
        self.commit_work()
        leftover = self.state / auto.CLAIMED_NAME / f"{SESSION}.99999.jsonl"
        leftover.parent.mkdir()
        self.pending_file().rename(leftover)
        self.assert_silent()
        self.assertTrue(leftover.exists())


class FailureTest(StopCase):
    def test_an_input_the_hook_cannot_handle_exits_0_prints_nothing_and_is_told_in_hook_log(self) -> None:
        self.begin()
        self.commit_work()
        self.assert_silent(background_tasks=[1])  # a task that is no object: not what Claude Code sends
        [line] = self.hook_log()
        self.assertRegex(line, HOOK_LOG_LINE + r"AttributeError: .* \(at .*auto_stop\.py:\d+\)$")
        self.assertTrue(self.pending_file().exists())  # it failed before the claim, so the next Stop still has the commits

    def test_a_failure_after_the_claim_leaves_nothing_claimed_and_the_next_commit_catches_up(self) -> None:
        self.begin()
        self.commit_work()

        def boom(claimed: Path) -> list:
            raise RuntimeError("boom")

        with mock.patch.object(auto_stop, "_judge", boom), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(auto_stop.stop_hook(io.StringIO(stop_input())), 0)
        self.assertEqual(out.getvalue(), "")
        [line] = self.hook_log()
        self.assertRegex(line, HOOK_LOG_LINE + r"RuntimeError: boom \(at .*test_auto_stop\.py:\d+\)$")
        self.assertFalse(self.pending_file().exists())
        self.assertEqual(self.claimed_files(), [])
        self.commit_work(lines=10)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=260, files=2)])

    def test_a_worktree_that_cannot_be_judged_is_told_and_the_others_are_judged(self) -> None:
        self.begin()
        other = self.another_repo()
        self.commit_work(other)
        self.commit_work()
        (other / ".ratchet.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo)])
        [line] = self.hook_log()
        self.assertRegex(line, HOOK_LOG_LINE + re.escape(str(other)) + r": RatchetError: ")

    def test_a_damaged_line_in_the_pending_file_does_not_hide_the_commits_around_it(self) -> None:
        self.begin()
        self.commit_work()
        with self.pending_file().open("a", encoding="utf-8") as handle:
            handle.write("not a record\n")
        self.commit_work(lines=10)
        self.assertEqual(self.context_lines(self.answer()), [due_line(self.repo, lines=260, files=2)])
        [line] = self.hook_log()
        self.assertIn("skipped a line", line)


# ---- the plugin's wrapper -----------------------------------------------------------------------------------------

STUB = """\
#!{python}
import json, os, sys
sys.stdout.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


class RunShTest(unittest.TestCase):
    """hooks/run.sh, started the way a hook starts it: with the plugin root and the session in the environment, and a note for the
    session (without one run.sh ends at once). The tool it runs is a stub, and PATH holds only what the test puts there."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.root = self.tmp / "plugin"
        (self.root / "bin").mkdir(parents=True)
        stub = self.root / "bin" / "ratchet"
        stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        stub.chmod(0o755)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.state = self.tmp / "state"
        self.note = self.touch_note(self.state / auto.PENDING_NAME / f"{SESSION}.jsonl")  # as `auto note` leaves it: run.sh asks if it exists

    def touch_note(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def python_on_path(self, name: str) -> None:
        (self.bin / name).symlink_to(sys.executable)

    def program(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def python_that_tells(self) -> Path:
        """An unversioned python3 on PATH that adds a line to the log it returns each time it is started, and then runs as the real one
        does. run.sh has to start it to test its version, so a turn that ends before Python is looked for leaves no line."""
        log = self.tmp / "started.log"
        self.program("python3", f'echo started >> {shlex.quote(str(log))}; exec {shlex.quote(sys.executable)} "$@"')
        return log

    def pending_of_the_tool(self, variables: dict[str, str]) -> Path:
        """Where `auto.state_dir()` puts the note of SESSION when, of the three variables that decide it, only `variables` are set."""
        with mock.patch.dict(os.environ):
            for name in (auto.STATE_DIR_ENV, auto.XDG_STATE_ENV, "HOME"):
                os.environ.pop(name, None)
            os.environ.update(variables)
            return auto.pending_path(SESSION)

    def run_sh(self, *args: str, stdin: str = "", env: dict[str, str | None] | None = None, root: Path | None = None) -> subprocess.CompletedProcess:
        """run.sh in a hook's environment: the session has a note, so the tool is run, unless `env` changes that (None drops a variable)."""
        full = {"PATH": str(self.bin), "CLAUDE_PLUGIN_ROOT": str(root or self.root), auto.SESSION_ID_ENV: SESSION, auto.STATE_DIR_ENV: str(self.state)}
        for name, value in (env or {}).items():
            if value is None:
                full.pop(name, None)
            else:
                full[name] = value
        return subprocess.run(["/bin/sh", str(RUN_SH), *args], input=stdin, capture_output=True, text=True, env=full, timeout=60)

    def assert_exits_quietly(self, proc: subprocess.CompletedProcess) -> None:
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_it_runs_the_tool_with_the_arguments_and_the_stdin_it_was_given(self) -> None:
        self.python_on_path("python3.12")
        proc = self.run_sh("auto", "stop", stdin='{"session_id": "s"}')
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(json.loads(proc.stdout), {"argv": ["auto", "stop"], "stdin": '{"session_id": "s"}'})

    def test_it_returns_the_exit_status_of_the_tool_except_2(self) -> None:
        self.python_on_path("python3.12")
        for status in (1, 3, 7, 255):
            with self.subTest(status=status):
                self.assertEqual(self.run_sh("x", env={"STUB_EXIT": str(status)}).returncode, status)

    def test_exit_status_2_of_the_tool_becomes_1_because_2_would_make_claude_go_on_with_the_turn(self) -> None:
        self.python_on_path("python3.12")
        proc = self.run_sh("x", stdin="the input", env={"STUB_EXIT": "2"})
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["stdin"]), (1, "the input"))  # only the status changes

    def test_a_usage_error_of_the_tool_is_exit_status_1_with_its_message_kept(self) -> None:
        self.python_on_path("python3.12")
        proc = self.run_sh("no-such-subcommand", root=REPO_ROOT)  # as a bin/ratchet from before `auto stop` answers `auto stop`: argparse exits 2
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("usage: ratchet", proc.stderr)

    def test_a_machine_without_python_3_10_gets_exit_0_and_no_output_and_the_tool_is_not_run(self) -> None:
        self.program("python3", "exit 1")  # what the python3 that macOS ships (3.9) answers the version test
        self.assert_exits_quietly(self.run_sh("auto", "stop", stdin="{}", env={"STUB_EXIT": "5"}))

    def test_a_machine_with_no_python_at_all_is_the_same(self) -> None:
        self.assert_exits_quietly(self.run_sh("auto", "stop", stdin="{}", env={"STUB_EXIT": "5"}))

    def test_a_python_3_9_is_no_candidate_though_it_names_its_version(self) -> None:
        started = self.tmp / "started.log"
        self.program("python3.9", f"echo started >> {shlex.quote(str(started))}; exit 9")
        self.assert_exits_quietly(self.run_sh("auto", "stop", stdin="{}", env={"STUB_EXIT": "5"}))
        self.assertFalse(started.exists())

    def test_an_unversioned_python3_is_used_when_it_passes_the_version_test(self) -> None:
        self.python_on_path("python3")
        self.assertEqual(json.loads(self.run_sh("go").stdout)["argv"], ["go"])

    def test_a_python_that_names_its_version_is_used_without_a_version_test_and_the_newest_comes_first(self) -> None:
        log = self.tmp / "started.log"
        self.program("python3.14", f'echo "$1" >> {shlex.quote(str(log))}; exec {shlex.quote(sys.executable)} "$@"')
        for older in ("python3.12", "python3.10"):
            self.program(older, f'echo old >> {shlex.quote(str(log))}; exit 9')
        proc = self.run_sh("go")
        self.assertEqual(json.loads(proc.stdout)["argv"], ["go"])
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), [str(self.root / "bin" / "ratchet")])  # no `-c` test, no older one

    def test_each_python_that_names_its_version_from_3_10_to_3_14_is_a_candidate(self) -> None:
        for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
            with self.subTest(version=version):
                self.python_on_path(f"python{version}")
                self.assertEqual(json.loads(self.run_sh("go").stdout)["argv"], ["go"])
                (self.bin / f"python{version}").unlink()

    def test_ratchet_py_comes_first_as_in_the_git_hooks_and_one_that_fails_the_test_is_passed_over(self) -> None:
        proc = self.run_sh("go", env={"RATCHET_PY": sys.executable})  # no python on PATH at all
        self.assertEqual(json.loads(proc.stdout)["argv"], ["go"])
        self.python_on_path("python3.12")
        bad = self.program("bad", "exit 1")
        proc = self.run_sh("go", env={"RATCHET_PY": str(bad)})
        self.assertEqual(json.loads(proc.stdout)["argv"], ["go"])

    def test_it_puts_the_git_hooks_version_test_to_a_python_it_cannot_tell_the_version_of(self) -> None:
        self.assertIn(_PYTHON_OK_CHECK, RUN_SH.read_text(encoding="utf-8"))

    def test_a_plugin_without_bin_ratchet_gets_exit_0_and_no_output(self) -> None:
        self.python_on_path("python3.12")
        empty = self.tmp / "empty-plugin"
        empty.mkdir()
        self.assert_exits_quietly(self.run_sh("auto", "stop", root=empty))

    def test_without_the_plugin_root_it_finds_the_tool_next_to_itself(self) -> None:
        self.python_on_path("python3.12")
        proc = self.run_sh("--help", env={"CLAUDE_PLUGIN_ROOT": None, "PATH": f"{self.bin}:/usr/bin:/bin"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: ratchet", proc.stdout)

    def test_the_command_of_hooks_json_runs_as_a_hook_runs_it(self) -> None:
        self.python_on_path("python3.12")
        state = self.tmp / "no-note-here"
        proc = subprocess.run(stop_command(), shell=True, input=stop_input(), capture_output=True, text=True, timeout=60,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT), auto.SESSION_ID_ENV: SESSION,
                                   auto.STATE_DIR_ENV: str(state)})
        self.assert_exits_quietly(proc)  # nothing is noted for this session
        self.assertFalse(state.exists())

    # ---- the pre-check: a turn with nothing to judge ends before Python is started ----------------------------------

    def test_python_is_started_for_a_session_with_a_note_and_not_for_one_without(self) -> None:
        started = self.python_that_tells()
        self.assertEqual(json.loads(self.run_sh("auto", "stop", stdin=stop_input()).stdout)["argv"], ["auto", "stop"])
        starts = started.read_text(encoding="utf-8").splitlines()
        self.assertTrue(starts)
        self.note.unlink()
        self.touch_note(self.state / auto.PENDING_NAME / f"{OTHER_SESSION}.jsonl")  # another session's note is not this session's
        self.assert_exits_quietly(self.run_sh("auto", "stop", stdin=stop_input(), env={"STUB_EXIT": "5"}))
        self.assert_exits_quietly(self.run_sh("auto", "stop", stdin=stop_input(), env={"STUB_EXIT": "5", auto.STATE_DIR_ENV: str(self.tmp / "none")}))
        self.assertEqual(started.read_text(encoding="utf-8").splitlines(), starts)  # no start since

    def test_a_review_child_ends_it_before_python_is_started_but_only_a_value_of_1_does(self) -> None:
        started = self.python_that_tells()
        self.assert_exits_quietly(self.run_sh("auto", "stop", env={"RATCHET_REVIEW_CHILD": "1", "STUB_EXIT": "5"}))
        self.assertFalse(started.exists())
        for value in ("0", ""):
            with self.subTest(value=value):
                self.assertEqual(json.loads(self.run_sh("auto", "stop", env={"RATCHET_REVIEW_CHILD": value}).stdout)["argv"], ["auto", "stop"])

    def test_no_session_id_ends_it_before_python_is_started(self) -> None:
        started = self.python_that_tells()
        self.touch_note(self.state / auto.PENDING_NAME / ".jsonl")  # the file an empty id would name: the id is checked, not left to the path
        for label, value in (("unset", None), ("empty", "")):
            with self.subTest(session_id=label):
                self.assert_exits_quietly(self.run_sh("auto", "stop", stdin=stop_input(), env={auto.SESSION_ID_ENV: value, "STUB_EXIT": "5"}))
        self.assertFalse(started.exists())

    def test_it_looks_for_the_note_where_state_dir_of_the_tool_puts_it(self) -> None:
        self.python_on_path("python3.12")
        mine, xdg, home = (str(self.tmp / name) for name in ("mine", "xdg", "home"))
        unset = {auto.STATE_DIR_ENV: None, auto.XDG_STATE_ENV: None, "HOME": None}  # run_sh sets RATCHET_STATE_DIR; a case has only its own
        for label, variables in (
            ("RATCHET_STATE_DIR beats the others", {auto.STATE_DIR_ENV: mine, auto.XDG_STATE_ENV: xdg, "HOME": home}),
            ("XDG_STATE_HOME", {auto.XDG_STATE_ENV: xdg, "HOME": home}),
            ("HOME", {"HOME": home}),
            ("an empty RATCHET_STATE_DIR is unset", {auto.STATE_DIR_ENV: "", auto.XDG_STATE_ENV: xdg, "HOME": home}),
            ("an empty XDG_STATE_HOME is unset", {auto.STATE_DIR_ENV: "", auto.XDG_STATE_ENV: "", "HOME": home}),
        ):
            with self.subTest(label):
                note = self.touch_note(self.pending_of_the_tool(variables))
                self.assertEqual(json.loads(self.run_sh("go", env={**unset, **variables}).stdout)["argv"], ["go"])
                note.unlink()
                self.assert_exits_quietly(self.run_sh("go", env={**unset, **variables}))


class HookCommandEndToEndTest(StopCase):
    def test_a_due_worktree_reaches_claude_through_the_command_in_hooks_json(self) -> None:
        self.begin()
        self.commit_work()
        bin_dir = self.tmp / "py"
        bin_dir.mkdir()
        (bin_dir / "python3.12").symlink_to(sys.executable)
        env = {**self.hook_env({auto.SESSION_ID_ENV: SESSION}), "PATH": f"{bin_dir}:{os.environ['PATH']}", "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}
        proc = subprocess.run(stop_command(), shell=True, input=stop_input(), capture_output=True, text=True, timeout=120, cwd=self.tmp, env=env)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.context_lines(json.loads(proc.stdout)), [due_line(self.repo)])


if __name__ == "__main__":
    unittest.main()
