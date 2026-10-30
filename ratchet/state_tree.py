"""The git tree of a worktree as it is right now, so a later look can tell what changed since.

`ratchet review` records the tree when a review starts (`state_tree`), and `ratchet wrapup` takes it again and diffs the two
(`numstat_since`). A commit holds only the committed part of the work; a tree holds every file as it is on disk, tracked or
not, so work a review has already read is not counted a second time, whether or not it was committed since.

It is built in a temporary copy of the index: the real index and the working tree are never touched. A tree is made of
objects (blobs of new or edited files, and the trees above them), and where they go depends on what the tree is for:

- `state_tree` is the one to keep, so its objects are written to the repo. Nothing refers to them, so `git gc` removes
  them in time, and `tree_exists` is how a caller finds out.
- `numstat_since` only measures, so its objects go to a temporary object directory that is deleted afterwards: a command
  that is only counting, a `--dry-run` included, leaves nothing in the repo's object store.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .git import run_git

MAX_UNTRACKED_BYTES = 1 << 20  # an untracked file over this is not read: hashing a dataset or a build output costs more than it is worth
# What stands in the tree for such a file. The NUL byte makes git call it binary, so a diff shows the file as changed with no
# lines, the way it shows any binary file. All of them share this one blob: an edit to an oversize file is not seen.
OVERSIZE_STAND_IN = b"\0ratchet: untracked file over the size limit, its content is not recorded\n"
INDEX_ENTRIES_PER_CALL = 100  # `--cacheinfo` pairs handed to one git call, which keeps the command line short enough for the OS


def state_tree(worktree: Path) -> str:
    """The id of a tree holding `worktree` as it is now: every tracked file as it is on disk (a deleted one is gone) and
    every untracked file git does not ignore. An untracked file over MAX_UNTRACKED_BYTES is not read; OVERSIZE_STAND_IN is
    in the tree under its name. The tree's objects are written to the repo, for a later `numstat_since` to compare with.

    `worktree` is the top of a worktree, the main one or a linked one: each has its own index, and that is the one copied.
    """
    with tempfile.TemporaryDirectory(prefix="ratchet-index-") as scratch:
        return _write_tree(worktree, Path(scratch), {})


def numstat_since(worktree: Path, tree: str) -> str:
    """The raw `git diff --numstat -z` from `tree` to `worktree` as it is now, the tree `state_tree` would make of it.

    The tree of now is built in a temporary object directory and deleted with it. Git writes new objects to one object
    directory (GIT_OBJECT_DIRECTORY) and only reads the others, so the repo's own is named as an alternate: `tree` and every
    file that did not change are read from there, and nothing is written to it. The diff runs in the same environment,
    because one side of it is in the temporary directory.
    """
    with tempfile.TemporaryDirectory(prefix="ratchet-measure-") as scratch_name:
        scratch = Path(scratch_name)
        objects = scratch / "objects"
        objects.mkdir()
        objects_env = {"GIT_OBJECT_DIRECTORY": str(objects), "GIT_ALTERNATE_OBJECT_DIRECTORIES": _alternate_entry(_repo_objects(worktree))}
        now = _write_tree(worktree, scratch, objects_env)
        return run_git(["diff", "--numstat", "-z", tree, now], worktree, env_extra=objects_env).stdout


def tree_exists(worktree: Path, tree: str) -> bool:
    """Whether this repo still has the tree object `tree`. A snapshot's tree is referenced by nothing, so `git gc` can remove it."""
    return run_git(["rev-parse", "--verify", "-q", f"{tree}^{{tree}}"], worktree, allowed_returncodes=(0, 1)).returncode == 0


def _write_tree(worktree: Path, scratch: Path, objects_env: dict[str, str]) -> str:
    """The tree of `worktree`, built in a copy of its index in `scratch`. New objects go where `objects_env` points git:
    the repo's own object store when it is empty."""
    oversize = _oversize_untracked(worktree)
    env = {**objects_env, "GIT_INDEX_FILE": str(scratch / "index")}
    _copy_index(worktree, scratch / "index")
    _stage_all(worktree, env, scratch, oversize)
    if oversize:
        _stage_stand_ins(worktree, env, scratch, oversize)
    return run_git(["write-tree"], worktree, env_extra=env).stdout.strip()


def _repo_objects(worktree: Path) -> Path:
    """The repo's object directory: `.git/objects` of the main worktree, which every linked worktree shares."""
    return Path(os.path.realpath(worktree / run_git(["rev-parse", "--git-path", "objects"], worktree).stdout.strip()))


def _alternate_entry(objects: Path) -> str:
    """`objects` as the one entry of GIT_ALTERNATE_OBJECT_DIRECTORIES, a list that git splits at `:`. A path with the separator
    in it is C-quoted, which is how git reads an entry that starts with a double quote."""
    text = str(objects)
    if os.pathsep not in text:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _copy_index(worktree: Path, destination: Path) -> None:
    """This worktree's real index, copied so that files that did not change are not hashed again. No index yet means an empty copy.

    `copy2` keeps the file's time: git compares the files' times with the index's own to catch an edit made in the very
    instant the index was written.
    """
    source = Path(os.path.realpath(worktree / run_git(["rev-parse", "--git-path", "index"], worktree).stdout.strip()))
    if source.exists():
        shutil.copy2(source, destination)


def _oversize_untracked(worktree: Path) -> list[str]:
    """The untracked files git does not ignore that are regular files over MAX_UNTRACKED_BYTES. A symlink is its target's name, not its size."""
    names = run_git(["ls-files", "--others", "--exclude-standard", "-z"], worktree).stdout.split("\0")
    return [name for name in names if name and _is_oversize(worktree / name)]


def _is_oversize(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:  # removed between git listing it and now
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > MAX_UNTRACKED_BYTES


def _stage_all(worktree: Path, env: dict[str, str], scratch: Path, oversize: Sequence[str]) -> None:
    """`git add -A` into the copy of the index, without the oversize files."""
    if not oversize:
        run_git(["add", "-A"], worktree, env_extra=env)
        return
    # NUL-separated, so a name needs no quoting, and `literal`, so `*` and `[` in a name are only characters. An exclusion
    # needs a pathspec to subtract from: `.`, the whole worktree. A pathspec file has no limit on how many names it holds.
    pathspecs = scratch / "pathspecs"
    pathspecs.write_bytes(b".\0" + b"".join(f":(exclude,literal){name}\0".encode() for name in oversize))
    run_git(["add", "-A", f"--pathspec-from-file={pathspecs}", "--pathspec-file-nul"], worktree, env_extra=env)


def _stage_stand_ins(worktree: Path, env: dict[str, str], scratch: Path, oversize: Sequence[str]) -> None:
    """Put OVERSIZE_STAND_IN in the copy of the index under each oversize file's name."""
    stand_in = scratch / "oversize-stand-in"
    stand_in.write_bytes(OVERSIZE_STAND_IN)
    blob = run_git(["hash-object", "-w", str(stand_in)], worktree, env_extra=env).stdout.strip()  # `env`: the blob goes where the tree does
    for start in range(0, len(oversize), INDEX_ENTRIES_PER_CALL):
        entries = [argument for name in oversize[start:start + INDEX_ENTRIES_PER_CALL] for argument in ("--cacheinfo", f"100644,{blob},{name}")]
        run_git(["update-index", "--add", *entries], worktree, env_extra=env)
