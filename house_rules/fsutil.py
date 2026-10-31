"""Small file helpers that turn OS errors into HouseRulesError, and the text diff both compile and init print."""
from __future__ import annotations

import difflib
import os
from pathlib import Path

from .errors import HouseRulesError


def diff_lines(name: str, old: str | None, new: str) -> list[str]:
    """Unified diff of a file's text before and after. `old` None: the file does not exist yet, so it diffs against /dev/null."""
    return list(difflib.unified_diff([] if old is None else old.splitlines(), new.splitlines(),
                                     fromfile="/dev/null" if old is None else f"a/{name}", tofile=f"b/{name}", lineterm=""))


def read_text(path: Path) -> str | None:
    """The file's text, or None when it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise HouseRulesError(f"cannot read {path}: {exc}") from exc


def write_text(path: Path, text: str, *, make_executable: bool = False) -> None:
    """Write `text` (creating parent directories); with `make_executable`, also set the execute bits that match the read bits."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if make_executable:
            mode = path.stat().st_mode
            path.chmod(mode | ((mode & 0o444) >> 2))
    except OSError as exc:
        raise HouseRulesError(f"cannot write {path}: {exc}") from exc


def is_executable(path: Path) -> bool:
    return os.access(path, os.X_OK)
