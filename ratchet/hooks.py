"""The two git hooks `ratchet init` installs: the shell block, where it goes in a script, and a live check that it runs.

A hook is a block between `# ratchet:begin` and `# ratchet:end` in the user's shell script. It always goes at the top, just under
the shebang. Anywhere lower, an `exit`, an `exec`, a function that exits, a failing command under `set -e` or an early return in
the user's own code could end the script before the block, with init reporting success. At the top none of the user's code runs
first, so the block cannot be skipped, and every installed block is run once with RATCHET_HOOK_PROBE set (`probe_hook`) to prove it:
the block answers that before doing any work.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .blocks import HASH_BEGIN, HASH_END, find_block, upsert_block
from .fsutil import is_executable, read_text
from .git import unstaged_env

HOOK_NAMES = ("pre-commit", "post-commit")
HOOK_ARGS = {"pre-commit": "compile --check --staged", "post-commit": "infer --quiet"}
# pre-commit blocks the commit on a stale digest; post-commit runs after the commit exists and must never fail it.
HOOK_ON_FAILURE = {"pre-commit": "exit 1", "post-commit": "true"}
# A passing check prints a line on every commit; problems go to stderr, which stays visible. infer reports what it closed.
HOOK_STDOUT = {"pre-commit": " >/dev/null", "post-commit": ""}

PROBE_ENV = "RATCHET_HOOK_PROBE"
PROBE_TOKEN = "ratchet-hook-probe"
PROBE_TIMEOUT_SECONDS = 60  # the block answers at once, so a hook that takes longer is stuck before it

# Pinned (absolute-path) hooks: the interpreter that ran init and that checkout's bin/ratchet.
Pinned = tuple[Path, Path]

_SHELL_SHEBANG_RE = re.compile(r"^#!\s*(?:/usr/bin/env\s+)?(?:\S*/)?(?:sh|bash|dash|zsh|ksh)\b")
_ONLY_WITH_CONFIG = "# core.hooksPath can point many repos at one hooks directory; only repos with a config use ratchet."
_CONFIG_GUARD = "if [ -f .ratchet.json ]; then"
_PYTHON_OK_CHECK = "import sys; sys.exit(sys.version_info < (3, 10))"
_READ_BIN_FROM_CONFIG = 'import json; print(json.load(open(".ratchet.json")).get("bin", ""))'
# The block runs at the top of the user's hook, in its shell: a variable it set would reach the user's own code below. So it
# keeps its state in these two private names (never RATCHET_PY / RATCHET_BIN, which that code may use) and unsets them at the end.
# No subshell: the probe's `exit 0` and pre-commit's `|| exit 1` must end the whole hook.
_UNSET_PRIVATE_VARS = "unset _ratchet_py _ratchet_bin"


# ---- the block ----------------------------------------------------------------------------------------------------

def _run_line(hook: str) -> str:
    return f'"$_ratchet_py" "$_ratchet_bin" {HOOK_ARGS[hook]}{HOOK_STDOUT[hook]} || {HOOK_ON_FAILURE[hook]}'


def _warn(hook: str, message: str, advice: str = "") -> str:
    """A loud, non-blocking line on stderr; `message` and `advice` may use shell variables."""
    return f"printf '%s\\n' \"ratchet: WARNING: {message}; {hook} is NOT running.{advice}\" >&2"


def _probe_lines(hook: str) -> list[str]:
    return [f'if [ -n "${{{PROBE_ENV}:-}}" ]; then', f'  echo "{PROBE_TOKEN} {hook}"', "  exit 0", "fi"]


def _pinned_body(hook: str, interpreter: Path, executable: Path) -> list[str]:
    """The interpreter is the absolute path `init` ran under, not `env python3`: a hook runs with whatever PATH git was
    started from, which may not hold the Python (3.10+) the tool needs."""
    return [
        "# Installed by `ratchet init`; re-run it to refresh the paths, delete this block to uninstall.",
        *_probe_lines(hook),
        f"_ratchet_py={shlex.quote(str(interpreter))}",
        f"_ratchet_bin={shlex.quote(str(executable))}",
        _ONLY_WITH_CONFIG,
        _CONFIG_GUARD,
        '  if [ -x "$_ratchet_py" ] && [ -f "$_ratchet_bin" ]; then',
        f"    {_run_line(hook)}",
        "  else",
        "    " + _warn(hook, "cannot run $_ratchet_bin with $_ratchet_py (interpreter or script missing)",
                    " Re-run 'ratchet init' to refresh this hook."),
        "  fi",
        "fi",
        _UNSET_PRIVATE_VARS,
    ]


def _portable_body(hook: str) -> list[str]:
    """No machine-specific path, so the hook file can be committed and works on every machine. The tool and the Python are
    looked up when the hook runs. `|| true` keeps a failed lookup from ending a `set -e` hook silently."""
    return [
        "# Installed by `ratchet init`; portable (no machine-specific paths), so it is safe to commit. Delete this block to uninstall.",
        "# The tool is $RATCHET_BIN, else `bin` in .ratchet.json (relative to the repo root), else `ratchet` on PATH.",
        "# Python is $RATCHET_PY, else python3 on PATH; it must be 3.10 or newer.",
        *_probe_lines(hook),
        _ONLY_WITH_CONFIG,
        _CONFIG_GUARD,
        '  _ratchet_py="${RATCHET_PY:-python3}"',
        f"  if \"$_ratchet_py\" -c '{_PYTHON_OK_CHECK}' 2>/dev/null; then",
        '    _ratchet_bin="${RATCHET_BIN:-}"',
        '    if [ -z "$_ratchet_bin" ]; then',
        f"      _ratchet_bin=$(\"$_ratchet_py\" -c '{_READ_BIN_FROM_CONFIG}' 2>/dev/null || true)",
        '      if [ -z "$_ratchet_bin" ]; then',
        "        _ratchet_bin=$(command -v ratchet || true)",
        "      fi",
        "    fi",
        '    if [ -f "$_ratchet_bin" ]; then',
        f"      {_run_line(hook)}",
        "    else",
        "      " + _warn(hook, 'cannot find the ratchet executable (set RATCHET_BIN, add \\"bin\\" to .ratchet.json, or put ratchet on PATH)'),
        "    fi",
        "  else",
        "    " + _warn(hook, "$_ratchet_py is missing or older than Python 3.10 (set RATCHET_PY to a 3.10+ interpreter)"),
        "  fi",
        "fi",
        _UNSET_PRIVATE_VARS,
    ]


def hook_body(hook: str, pinned: Pinned | None) -> list[str]:
    """The block's lines between the markers: pinned to absolute paths, or portable when `pinned` is None."""
    return _portable_body(hook) if pinned is None else _pinned_body(hook, *pinned)


# ---- where the block goes -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    text: str
    status: str  # "add" | "update" | "ok"


def place_block(base: str, body: list[str], name: str) -> Placement:
    """`base` with the ratchet block at the top, just under the shebang.

    A block that already is there (nothing but the shebang and blank lines above it) is refreshed in place. One further down,
    which an earlier version appended or the user moved, is taken out and written again at the top: an "update". Everything
    outside the block is kept as it is.
    """
    assert base.startswith("#!"), "plan_hook hands over only a script with a shebang line (an empty one is given #!/bin/sh)"
    lines = base.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    span = find_block(plain, HASH_BEGIN, HASH_END, name)
    top = 1  # index of the first line after the shebang
    if span is not None and all(not line.strip() for line in plain[top:span[0]]):
        text, status = upsert_block(base, HASH_BEGIN, HASH_END, body, name)
        return Placement(text, status)
    kept = lines if span is None else lines[:span[0]] + lines[span[1] + 1:]
    if span is not None and kept[span[0] - 1].strip() == "" and (span[0] == len(kept) or kept[span[0]].strip() == ""):
        del kept[span[0] - 1]  # the blank line that separated the old block from the text above it
    above = "".join(kept[:top])
    if not above.endswith("\n"):  # a file that is only a shebang line
        above += "\n"
    rest = kept[top:]
    block = "".join(f"{line}\n" for line in [HASH_BEGIN, *body, HASH_END])
    blank_below = "\n" if rest and rest[0].strip() else ""
    return Placement(f"{above}\n{block}{blank_below}{''.join(rest)}", "add" if span is None else "update")


# ---- planning one hook --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HookPlan:
    text: str | None  # the script to write; None: init leaves the file alone (status "manual")
    status: str  # "add" | "update" | "ok" | "manual"
    note: str  # for "manual": what to do by hand


def plan_hook(path: Path, hook: str, pinned: Pinned | None) -> HookPlan:
    existing = read_text(path)
    if existing and not _SHELL_SHEBANG_RE.match(existing.splitlines()[0]):
        command = HOOK_ARGS[hook] if pinned is None else f"{pinned[0]} {pinned[1]} {HOOK_ARGS[hook]}"
        return HookPlan(None, "manual", f"{path.name} is not a shell script; add this yourself: {command}")
    base = existing if existing and existing.strip() else "#!/bin/sh\n"
    placement = place_block(base, hook_body(hook, pinned), path.name)
    status = placement.status
    if status == "ok" and not is_executable(path):
        status = "update"  # git silently ignores a hook without the execute bit
    return HookPlan(placement.text, status, "")


# ---- the live check -----------------------------------------------------------------------------------------------

def _output_tail(proc: subprocess.CompletedProcess[str]) -> str:
    lines = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
    return " | ".join(line.strip() for line in lines) or "(no output)"


def probe_hook(path: Path, hook: str, top: Path) -> str | None:
    """Run the installed hook once with RATCHET_HOOK_PROBE set. None when the ratchet block answered, else why not.

    The block is the first thing in the hook (`place_block`) and answers the probe in its first lines, then exits. So none of the
    user's own code runs: it all comes after the block, and a probe cannot deploy, push or rewrite files. The environment has
    nothing staged anyway (`unstaged_env`), so the run cannot touch the real index either.
    """
    with tempfile.TemporaryDirectory() as scratch:
        env = {**unstaged_env(top, Path(scratch)), PROBE_ENV: "1"}
        try:
            proc = subprocess.run([str(path)], cwd=top, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  timeout=PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return f"{path} did not finish within {PROBE_TIMEOUT_SECONDS}s, so the ratchet block was never reached"
        except OSError as exc:
            return f"cannot run {path}: {exc}"
    if proc.returncode == 0 and f"{PROBE_TOKEN} {hook}" in proc.stdout.splitlines():
        return None
    return (f"the ratchet block in {path} is never reached: the hook ended (status {proc.returncode}) without answering the live "
            f"check, although init put the block right under the shebang. Look at the shebang line and the interpreter it names, "
            f"then rerun `ratchet init`. Hook output: {_output_tail(proc)}")
