"""Bounded local source reads; source locations are tool-owned evidence."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .errors import HouseRulesError
from .git import committed_blob
from .proposal_store import local_path

MAX_SOURCE_BYTES = 256 * 1024


def _checked_path(root: Path, name: str) -> tuple[str, Path]:
    """`name` relative to `root` (an absolute path inside `root` is accepted) and where that is on disk.

    ValueError: an absolute path outside `root`. HouseRulesError: a path `local_path` refuses (`..`, `.git`, `.env*` in any case,
    a symlink).
    """
    path = Path(name)
    relative = path.relative_to(root).as_posix() if path.is_absolute() else name
    return relative, local_path(root, relative)


def _unavailable(name: str, line: int | None, version: str) -> dict:
    return {"file": name, "absolute_path": None, "line": line, "status": "unavailable", "version": version}


def _source_from_bytes(result: dict, data: bytes, line: int | None, end_line: int | None) -> dict:
    """`result` with the file's text, or with the reason the bytes are not bounded UTF-8 text or the line is not in them."""
    if len(data) > MAX_SOURCE_BYTES or b"\0" in data:
        result["reason"] = "Source exceeds the text limit or is binary."
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result["reason"] = "Source is not UTF-8 text."
        return result
    result["sha256"] = hashlib.sha256(data).hexdigest()
    lines = text.splitlines()
    if line is not None and (not 1 <= line <= len(lines) or end_line is not None and end_line > len(lines)):
        result["reason"] = "Reported line is outside the current file; location may have moved."
        return result
    start, end = (max(1, line - 3), min(len(lines), line + 3)) if line else (1, len(lines))
    if end_line is not None:
        start, end = line, end_line
    result.update(status="available", line_start=start, line_end=end,
                  text="\n".join(lines[start - 1:end]))
    return result


def read_source(root: Path, name: str, line: int | None, end_line: int | None = None) -> dict:
    """Bounded, repo-local text only. Missing/unsafe evidence stays explicitly unavailable."""
    result = _unavailable(name, line, "current_workspace")
    try:
        relative, path = _checked_path(root, name)
        result.update(file=relative, absolute_path=str(path))
        with path.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except (OSError, ValueError, HouseRulesError) as exc:
        result["reason"] = str(exc)
        return result
    return _source_from_bytes(result, data, line, end_line)


def read_outer_source(path: Path, name: str) -> dict:
    """The whole text of a file outside the repo, read where the config points: a rules layer shared with other repos, `name`
    being what the config calls it. Nothing writes it. `read_source` refuses `..` and symlinks because a model may supply its
    names; this path is the owner's own config, so it is read as `load_rules` reads it, with the same text checks on the bytes.
    """
    path = Path(os.path.normpath(path))  # `repo/../shared/x.md` as the config reaches it; the evidence names the place itself
    result = {**_unavailable(name, None, "current_workspace"), "absolute_path": str(path)}
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        result["reason"] = str(exc)
        return result
    return _source_from_bytes(result, data, None, None)


def read_committed_source(root: Path, commit: str, name: str) -> dict:
    """The whole file `name` as `commit` holds it, refused for the same reasons `read_source` refuses it.

    The refusals are those very functions: `local_path` for the name (it looks at the working tree, which is right for a
    file that has not changed since `commit`) and the same text checks on the bytes. On top of that the commit itself must
    hold a regular file of at most MAX_SOURCE_BYTES there: not a symlink, a directory or a submodule, and not nothing.
    """
    result = _unavailable(name, None, f"commit:{commit}")
    try:
        relative, _ = _checked_path(root, name)
        data = committed_blob(root, commit, relative, MAX_SOURCE_BYTES)
    except (ValueError, HouseRulesError) as exc:
        result["reason"] = str(exc)
        return result
    if data is None:
        result["reason"] = "The commit holds no regular file of that size at that path."
        return result
    result["file"] = relative
    return _source_from_bytes(result, data, None, None)
