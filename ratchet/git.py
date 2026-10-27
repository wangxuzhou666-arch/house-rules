"""Thin git helpers. Every call goes through run_git so failures surface as RatchetError with git's own message."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import RatchetError

# A process spawned by a git hook inherits these and would make git act on the hook's repo instead of cwd.
_HOOK_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")


def _git_env(keep_index_file: bool) -> dict[str, str]:
    dropped = tuple(key for key in _HOOK_ENV if not (keep_index_file and key == "GIT_INDEX_FILE"))
    return {key: value for key, value in os.environ.items() if key not in dropped}


def _run(args: list[str], cwd: Path, keep_index_file: bool, text: bool,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if not cwd.is_dir():
        raise RatchetError(f"directory does not exist: {cwd}")
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text,
                              env={**_git_env(keep_index_file), **(env_extra or {})})
    except FileNotFoundError as exc:  # cwd exists (checked above), so the missing file is the git executable
        raise RatchetError("git executable not found on PATH") from exc


def run_git(args: list[str], cwd: Path, allowed_returncodes: tuple[int, ...] = (0,), *,
            keep_index_file: bool = False, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """`keep_index_file`: a pre-commit hook's GIT_INDEX_FILE names the index the commit will be built from, which
    `git commit -a` / `git commit <path>` point at a temporary file. Only the staged-content reads want it kept.
    `env_extra` is added to the environment after the hook variables are dropped."""
    proc = _run(args, cwd, keep_index_file, text=True, env_extra=env_extra)
    if proc.returncode not in allowed_returncodes:
        raise RatchetError(f"`git {' '.join(args)}` failed in {cwd} (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc


def main_root(start: Path) -> Path:
    """Root of the main worktree. --git-common-dir is `.git` in the main repo and the main repo's `.git` in a linked worktree."""
    out = run_git(["rev-parse", "--git-common-dir"], start).stdout.strip()
    return Path(os.path.realpath(start / out)).parent


def worktree_root(start: Path) -> Path:
    """Top of the worktree `start` is in (the main one, or a linked one)."""
    return Path(os.path.realpath(run_git(["rev-parse", "--show-toplevel"], start).stdout.strip()))


def hooks_dir(start: Path) -> Path:
    """Where git looks for hooks: `.git/hooks`, or `core.hooksPath` when set (git expands `~` and resolves relative paths against the worktree top)."""
    top = worktree_root(start)
    out = run_git(["rev-parse", "--git-path", "hooks"], top).stdout.strip()
    return Path(os.path.realpath(top / out))


def default_hooks_dir(start: Path) -> Path:
    """Where git looks for hooks when core.hooksPath is unset: `hooks` in the git dir (the shared one for a linked worktree)."""
    top = worktree_root(start)
    out = run_git(["rev-parse", "--git-common-dir"], top).stdout.strip()
    return Path(os.path.realpath(top / out)) / "hooks"


def hooks_path_origin(start: Path) -> Path | None:
    """The config file that sets core.hooksPath, or None when it is unset or comes from somewhere that is not a file (`-c`)."""
    top = worktree_root(start)
    proc = run_git(["config", "--show-origin", "--get", "core.hooksPath"], top, allowed_returncodes=(0, 1))
    origin = proc.stdout.split("\t", 1)[0] if proc.returncode == 0 else ""
    # A repo-local file is printed relative to the directory git ran in.
    return Path(os.path.realpath(top / origin.removeprefix("file:"))) if origin.startswith("file:") else None


def head_sha(root: Path) -> str:
    """Short HEAD sha, or "none" on an unborn branch (rc=1 from --verify -q is that state, not a failure)."""
    proc = run_git(["rev-parse", "--short=8", "--verify", "-q", "HEAD"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else "none"


def current_branch(root: Path) -> str | None:
    """The checked-out branch name (also on an unborn branch), or None on a detached HEAD (rc=1 from -q)."""
    proc = run_git(["symbolic-ref", "-q", "--short", "HEAD"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None


def commit_sha(root: Path, ref: str) -> str | None:
    """Full sha of the commit `ref` names, or None when it names none (an unborn HEAD, a missing branch)."""
    proc = run_git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None


def origin_head_branch(root: Path) -> str | None:
    """The branch `refs/remotes/origin/HEAD` points at (`main`), or None when origin has no HEAD ref."""
    proc = run_git(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip().removeprefix("origin/") if proc.returncode == 0 else None


def merge_base_with_head(root: Path, commit: str) -> str | None:
    """Full sha where HEAD forked from `commit`, or None when their histories share nothing (rc=1)."""
    proc = run_git(["merge-base", "HEAD", commit], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None


def origin_url(root: Path) -> str | None:
    """`git config --get` exits 1 when the key is unset."""
    proc = run_git(["config", "--get", "remote.origin.url"], root, allowed_returncodes=(0, 1))
    return proc.stdout.strip() if proc.returncode == 0 else None


def index_blob(top: Path, rel_path: str) -> bytes | None:
    """The bytes of `rel_path` as staged in the index (what the next commit will contain), or None when it is not staged.

    `top` is the worktree top, `rel_path` is relative to it. Literal pathspecs: a file name with `[` or `*` is a name.
    """
    listed = run_git(["--literal-pathspecs", "ls-files", "--stage", "--", rel_path], top, keep_index_file=True)
    if not listed.stdout.strip():
        return None
    proc = _run(["cat-file", "blob", f":{rel_path}"], top, keep_index_file=True, text=False)
    if proc.returncode != 0:
        raise RatchetError(f"cannot read the staged version of {rel_path}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def uncommitted_paths(top: Path, rel_paths: list[str]) -> list[str]:
    """The `rel_paths` (relative to the worktree top `top`) git sees as untracked, modified, or staged but not committed.

    Ignored files are not listed: `git add` would refuse them.
    """
    if not rel_paths:
        return []
    out = run_git(["--no-optional-locks", "--literal-pathspecs", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all",
                   "--", *rel_paths], top).stdout
    listed = {entry[3:] for entry in out.split("\0") if entry}  # "XY path"
    return [path for path in rel_paths if path in listed]


def unstaged_env(top: Path, scratch_dir: Path) -> dict[str, str]:
    """An environment for running a hook once as if nothing were staged.

    GIT_INDEX_FILE names a throwaway index equal to HEAD (empty on an unborn branch), so `git diff --cached` inside the hook
    shows nothing and a `git add` it runs lands in the throwaway file, never in the real index.
    """
    index = scratch_dir / "index"
    has_head = run_git(["rev-parse", "--verify", "-q", "HEAD"], top, allowed_returncodes=(0, 1)).returncode == 0
    run_git(["read-tree", "HEAD" if has_head else "--empty"], top, env_extra={"GIT_INDEX_FILE": str(index)})
    return {**_git_env(keep_index_file=False), "GIT_INDEX_FILE": str(index)}
