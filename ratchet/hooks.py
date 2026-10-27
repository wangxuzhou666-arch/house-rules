"""The two git hooks `ratchet init` installs: the shell block, where it goes in an existing script, and a live check that it runs.

A hook is a block between `# ratchet:begin` and `# ratchet:end` appended to the user's shell script. Appending is wrong when the
script ends in a top-level `exit` (or `exec`): the block would sit after it and never run, while init reported success. So the
block goes before the first such line when one is found by a line-based reading of the script (`scan_for_top_level_exit`), and
every installed block is run once with RATCHET_HOOK_PROBE set (`probe_hook`): the block answers that before doing any work, so
a hook that never reaches it is caught whatever the reason.
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
PROBE_TIMEOUT_SECONDS = 60  # the user's own code before the block runs too

# Pinned (absolute-path) hooks: the interpreter that ran init and that checkout's bin/ratchet.
Pinned = tuple[Path, Path]

_SHELL_SHEBANG_RE = re.compile(r"^#!\s*(?:/usr/bin/env\s+)?(?:\S*/)?(?:sh|bash|dash|zsh|ksh)\b")
_ONLY_WITH_CONFIG = "# core.hooksPath can point many repos at one hooks directory; only repos with a config use ratchet."
_CONFIG_GUARD = "if [ -f .ratchet.json ]; then"
_PYTHON_OK_CHECK = "import sys; sys.exit(sys.version_info < (3, 10))"
_READ_BIN_FROM_CONFIG = 'import json; print(json.load(open(".ratchet.json")).get("bin", ""))'


# ---- the block ----------------------------------------------------------------------------------------------------

def _run_line(hook: str) -> str:
    return f'"$RATCHET_PY" "$RATCHET_BIN" {HOOK_ARGS[hook]}{HOOK_STDOUT[hook]} || {HOOK_ON_FAILURE[hook]}'


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
        f"RATCHET_PY={shlex.quote(str(interpreter))}",
        f"RATCHET_BIN={shlex.quote(str(executable))}",
        _ONLY_WITH_CONFIG,
        _CONFIG_GUARD,
        '  if [ -x "$RATCHET_PY" ] && [ -f "$RATCHET_BIN" ]; then',
        f"    {_run_line(hook)}",
        "  else",
        "    " + _warn(hook, "cannot run $RATCHET_BIN with $RATCHET_PY (interpreter or script missing)",
                    " Re-run 'ratchet init' to refresh this hook."),
        "  fi",
        "fi",
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
        '  RATCHET_PY="${RATCHET_PY:-python3}"',
        f"  if \"$RATCHET_PY\" -c '{_PYTHON_OK_CHECK}' 2>/dev/null; then",
        '    if [ -z "${RATCHET_BIN:-}" ]; then',
        f"      RATCHET_BIN=$(\"$RATCHET_PY\" -c '{_READ_BIN_FROM_CONFIG}' 2>/dev/null || true)",
        '      if [ -z "$RATCHET_BIN" ]; then',
        "        RATCHET_BIN=$(command -v ratchet || true)",
        "      fi",
        "    fi",
        '    if [ -f "$RATCHET_BIN" ]; then',
        f"      {_run_line(hook)}",
        "    else",
        "      " + _warn(hook, 'cannot find the ratchet executable (set RATCHET_BIN, add \\"bin\\" to .ratchet.json, or put ratchet on PATH)'),
        "    fi",
        "  else",
        "    " + _warn(hook, "$RATCHET_PY is missing or older than Python 3.10 (set RATCHET_PY to a 3.10+ interpreter)"),
        "  fi",
        "fi",
    ]


def hook_body(hook: str, pinned: Pinned | None) -> list[str]:
    """The block's lines between the markers: pinned to absolute paths, or portable when `pinned` is None."""
    return _portable_body(hook) if pinned is None else _pinned_body(hook, *pinned)


# ---- where the block goes -----------------------------------------------------------------------------------------

# `exit` ends the script; `exec cmd` replaces it. `exec >log` and `exec 3<file` only redirect and do not end anything.
_ENDS_SCRIPT_RE = re.compile(r"^(?:exit(?=[ \t;|&]|$)|exec[ \t]+(?![0-9]*[<>])\S)")
_SEPARATOR_RE = re.compile(r"&&|\|\||[;|&]")
_ARITHMETIC_RE = re.compile(r"\$\(\([^()]*\)\)")
_SUBSTITUTION_RE = re.compile(r"\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`")
_HEREDOC_RE = re.compile(r"<<(?!<)")
_OPENERS = frozenset({"if", "case", "for", "while", "until", "select"})
_CLOSERS = frozenset({"fi", "esac", "done"})
_COMMAND_FOLLOWS = frozenset({"if", "elif", "while", "until", "then", "else", "do", "!"})


@dataclass(frozen=True)
class ExitScan:
    index: int | None  # the first line that ends the script at top level, or may (see `doubt`)
    doubt: str | None  # why the reading is a guess at `index`; None when it is certain


class UnclearExit(Exception):
    """The script has a line that may end it at top level, but its structure cannot be read safely."""

    def __init__(self, index: int, line: str, doubt: str) -> None:
        super().__init__(doubt)
        self.index, self.line, self.doubt = index, line, doubt


def _blank_strings_and_comments(line: str) -> tuple[str, str | None]:
    """(the line with quoted text, escapes and the comment replaced by `_` or dropped, why the line cannot be read or None)."""
    kept: list[str] = []
    quote = ""
    position = 0
    while position < len(line):
        char = line[position]
        if quote:
            if char == "\\" and quote == '"':
                position += 1
            elif char == quote:
                quote = ""
                kept.append("_")
        elif char == "\\":
            position += 1
            kept.append("_")
        elif char in "'\"":
            quote = char
        elif char == "#" and (not kept or kept[-1].isspace()):
            break
        else:
            kept.append(char)
        position += 1
    return "".join(kept), "a quoted string continues on the next line" if quote else None


def _unreadable_reason(line: str, text: str) -> str | None:
    if "`" in text or "$(" in text or "${" in text:
        return "a command substitution continues on the next line"
    if _HEREDOC_RE.search(text):
        return "a here-document follows"
    if line.endswith("\\"):
        return "a line continuation follows"
    return None


def _simplify(line: str) -> tuple[str, str | None]:
    """(the line reduced to shell words and separators, why it cannot be trusted or None)."""
    text, quote_doubt = _blank_strings_and_comments(line)
    text = _ARITHMETIC_RE.sub("_", text)
    previous = None
    while previous != text:  # substitutions nest
        previous = text
        text = _SUBSTITUTION_RE.sub("_", text)
    return text, quote_doubt or _unreadable_reason(line, text)


def _structure_doubt(text: str, depth: int) -> str | None:
    if depth < 0:
        return "a closing keyword has no opener"
    if depth == 0 and text.count("(") != text.count(")"):
        return "a subshell spans lines"
    return None


def _depth_after(text: str, depth: int) -> int:
    """How many if/case/loop/brace groups are open after `text`. A keyword only counts where a command can start."""
    for segment in _SEPARATOR_RE.split(text):
        command_position = True
        for word in segment.split():
            if word == "{" or (command_position and word in _OPENERS):
                depth += 1
            elif word == "}" or (command_position and word in _CLOSERS):
                depth -= 1
            command_position = word == "{" or (command_position and word in _COMMAND_FOLLOWS)
    return depth


def scan_for_top_level_exit(lines: list[str]) -> ExitScan:
    """The first line that starts with `exit` or `exec cmd` outside every function, if, loop, case and brace group.

    Line-based and conservative: it reads the keywords that open and close a group, not the shell grammar. Only a line
    at column 0 can be the one, so an indented `exit` (inside a function, or a guard such as `[ -z "$x" ] && exit 0`) is
    never it. Once the text turns unreadable (a here-document, a string over several lines, a subshell spanning lines) the
    state is a guess, and a candidate line after that point is reported with its `doubt`.
    """
    depth = 0
    doubt: str | None = None
    for index, line in enumerate(lines):
        if _ENDS_SCRIPT_RE.match(line) and (depth == 0 or doubt is not None):
            return ExitScan(index, doubt)
        text, line_doubt = _simplify(line)
        depth = _depth_after(text, depth)
        doubt = doubt or line_doubt or _structure_doubt(text, depth)
    return ExitScan(None, None)


@dataclass(frozen=True)
class Placement:
    text: str
    status: str  # "add" | "update" | "ok"


def place_block(base: str, body: list[str], name: str) -> Placement:
    """`base` with the ratchet block in the right place: refreshed where it is when that is before the script's top-level
    `exit`, else (re)inserted just before that line, else appended. Raises UnclearExit when it cannot tell."""
    lines = base.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    span = find_block(plain, HASH_BEGIN, HASH_END, name)
    others = plain if span is None else plain[:span[0]] + plain[span[1] + 1:]
    scan = scan_for_top_level_exit(others)
    block_is_before_exit = scan.index is None or (span is not None and span[0] <= scan.index)
    if block_is_before_exit:
        text, status = upsert_block(base, HASH_BEGIN, HASH_END, body, name)
        return Placement(text, status)
    if scan.doubt is not None:
        raise UnclearExit(scan.index, others[scan.index], scan.doubt)
    kept = lines if span is None else lines[:span[0]] + lines[span[1] + 1:]
    if span is not None and span[0] > 0 and kept[span[0] - 1].strip() == "" and (span[0] == len(kept) or kept[span[0]].strip() == ""):
        del kept[span[0] - 1]  # the blank line that separated the old block from the text above it
    before, after = kept[:scan.index], kept[scan.index:]
    separator = [] if not before or before[-1].strip() == "" else ["\n"]
    block = [f"{line}\n" for line in [HASH_BEGIN, *body, HASH_END]]
    return Placement("".join([*before, *separator, *block, "\n", *after]), "add" if span is None else "update")


# ---- planning one hook --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HookPlan:
    text: str | None  # the script to write; None: init leaves the file alone (status "manual")
    status: str  # "add" | "update" | "ok" | "manual"
    note: str  # for "manual": what to do by hand


def _unclear_advice(name: str, unclear: UnclearExit, body: list[str]) -> str:
    block = "\n".join(f"    {line}" for line in [HASH_BEGIN, *body, HASH_END])
    return (f"{name}: line {unclear.index + 1} (`{unclear.line}`) may end the script before an appended block could run, "
            f"and the script ({unclear.doubt}) cannot be read safely here. Paste this block above that line "
            f"(and delete any older `{HASH_BEGIN}` block below it), then rerun `ratchet init` to check it:\n{block}")


def plan_hook(path: Path, hook: str, pinned: Pinned | None) -> HookPlan:
    existing = read_text(path)
    if existing and not _SHELL_SHEBANG_RE.match(existing.splitlines()[0]):
        command = HOOK_ARGS[hook] if pinned is None else f"{pinned[0]} {pinned[1]} {HOOK_ARGS[hook]}"
        return HookPlan(None, "manual", f"{path.name} is not a shell script; add this yourself: {command}")
    base = existing if existing and existing.strip() else "#!/bin/sh\n"
    body = hook_body(hook, pinned)
    try:
        placement = place_block(base, body, path.name)
    except UnclearExit as unclear:
        return HookPlan(None, "manual", _unclear_advice(path.name, unclear, body))
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

    Whatever the user's script does before the block runs for real, once, in `top` with an environment in which nothing is
    staged (`unstaged_env`): a lint step sees an empty change list and a `git add` it runs touches a throwaway index.
    Nothing else is sandboxed, so a script that does work unconditionally before the block does that work.
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
    return (f"the ratchet block in {path} is never reached: the hook ended (status {proc.returncode}) before it. "
            f"Anything that ends a hook early disables the check: an `exit` or `exec`, a function that exits, a failing command under "
            f"`set -e`, an early return when nothing is staged. Move the `{HASH_BEGIN}` ... `{HASH_END}` block above that point "
            f"(just under the shebang always works) and rerun `ratchet init`. Hook output: {_output_tail(proc)}")
