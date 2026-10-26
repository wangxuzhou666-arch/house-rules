"""Thin git helpers. Every call goes through run_git so failures surface as RatchetError with git's own message."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import RatchetError

# A process spawned by a git hook inherits these and would make git act on the hook's repo instead of cwd.
_HOOK_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")


def run_git(args: list[str], cwd: Path, allowed_returncodes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    if not cwd.is_dir():
        raise RatchetError(f"directory does not exist: {cwd}")
    env = {key: value for key, value in os.environ.items() if key not in _HOOK_ENV}
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    except FileNotFoundError as exc:  # cwd exists (checked above), so the missing file is the git executable
        raise RatchetError("git executable not found on PATH") from exc
    if proc.returncode not in allowed_returncodes:
        raise RatchetError(f"`git {' '.join(args)}` failed in {cwd} (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc


def main_root(start: Path) -> Path:
    """Root of the main worktree. --git-common-dir is `.git` in the main repo and the main repo's `.git` in a linked worktree."""
    out = run_git(["rev-parse", "--git-common-dir"], start).stdout.strip()
    return Path(os.path.realpath(start / out)).parent


def head_sha(root: Path) -> str:
    """Short HEAD sha, or "none" on an unborn branch (rc=1 from --verify -q is that state, not a failure)."""
    proc = run_git(["rev-parse", "--short=8", "--verify", "-q", "HEAD"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else "none"


def origin_url(root: Path) -> str | None:
    """`git config --get` exits 1 when the key is unset."""
    proc = run_git(["config", "--get", "remote.origin.url"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None
