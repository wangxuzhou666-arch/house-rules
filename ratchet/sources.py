"""Where compile reads its inputs and existing outputs from: the working tree, or the git index (`compile --check --staged`)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .errors import RatchetError
from .git import index_blob


class FileReader(Protocol):
    label: str  # for messages: "the working tree" / "the git index"
    missing_hint: str  # appended to a "not found" message: how to put the file where this view can see it
    copy_label: str  # prefixed to a file name in a message when this view is not the working tree: "staged copy of "

    def read(self, path: Path) -> bytes | None:
        """The file's bytes, or None when it does not exist in this view."""

    def working_tree_copy(self, path: Path) -> bytes | None:
        """The file's bytes in the working tree when that differs from this view, else None."""


class DiskReader:
    label = "the working tree"
    missing_hint = ""
    copy_label = ""

    def read(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RatchetError(f"cannot read {path}: {exc}") from exc

    def working_tree_copy(self, path: Path) -> bytes | None:
        return None


class IndexReader:
    """What the next commit will contain. A file outside the repo (a shared rules file in `../`) cannot be staged, so it is read from disk."""

    label = "the git index"
    missing_hint = " (stage it with `git add`)"
    copy_label = "staged copy of "

    def __init__(self, worktree: Path, root: Path) -> None:
        self._worktree = worktree
        self._root = root
        self._disk = DiskReader()

    def read(self, path: Path) -> bytes | None:
        rel = Path(os.path.relpath(path, self._root))
        if rel.parts[:1] == ("..",):
            return self._disk.read(path)
        return index_blob(self._worktree, rel.as_posix())

    def working_tree_copy(self, path: Path) -> bytes | None:
        return self._disk.read(path)
