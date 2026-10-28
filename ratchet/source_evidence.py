"""Bounded local source reads; source locations are tool-owned evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import RatchetError
from .proposal_store import local_path

MAX_SOURCE_BYTES = 256 * 1024


def read_source(root: Path, name: str, line: int | None, end_line: int | None = None) -> dict:
    """Bounded, repo-local text only. Missing/unsafe evidence stays explicitly unavailable."""
    result = {"file": name, "absolute_path": None, "line": line,
              "status": "unavailable", "version": "current_workspace"}
    try:
        path = Path(name)
        relative = path.relative_to(root).as_posix() if path.is_absolute() else name
        path = local_path(root, relative)
        result.update(file=relative, absolute_path=str(path))
        with path.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except (OSError, ValueError, RatchetError) as exc:
        result["reason"] = str(exc)
        return result
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
