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

    def missing_in(self, path: Path) -> tuple[str, str]:
        """(label, missing_hint) for a "not found" message about `path`: where this view looked for it, and how to put it there."""


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

    def missing_in(self, path: Path) -> tuple[str, str]:
        return self.label, self.missing_hint


class IndexReader:
    """What the next commit will contain, in the index of `worktree`: the main worktree's or a linked one's, each has its own.

    The paths it is asked for are that worktree's own (`config.tracked_path`). A file outside the repo (a shared rules file in
    `../`) cannot be staged, so it is read from disk.
    """

    label = "the git index"
    missing_hint = " (stage it with `git add`)"
    outside_hint = " (it is outside the repository, so it is read from disk and cannot be staged)"
    copy_label = "staged copy of "

    def __init__(self, worktree: Path) -> None:
        self._worktree = worktree
        self._disk = DiskReader()

    def read(self, path: Path) -> bytes | None:
        rel = Path(os.path.relpath(path, self._worktree))
        if self._is_outside(rel):
            return self._disk.read(path)
        return index_blob(self._worktree, rel.as_posix())

    def working_tree_copy(self, path: Path) -> bytes | None:
        return self._disk.read(path)

    def missing_in(self, path: Path) -> tuple[str, str]:
        if self._is_outside(Path(os.path.relpath(path, self._worktree))):
            return self._disk.label, self.outside_hint
        return self.label, self.missing_hint

    @staticmethod
    def _is_outside(rel: Path) -> bool:
        return rel.parts[:1] == ("..",)
