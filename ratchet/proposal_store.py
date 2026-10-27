"""Repo-local proposal state and recoverable file updates. No shell commands are accepted here."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import RatchetError


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def local_path(root: Path, name: str) -> Path:
    """State and write targets stay inside the repo, with no symlink or secret-file traversal."""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(p in ("..", ".git") or p.startswith(".env") for p in relative.parts):
        raise RatchetError(f"unsafe proposal path: {name}")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise RatchetError(f"proposal paths cannot use symlinks: {name}")
    if path.exists() and not path.is_file():
        raise RatchetError(f"expected a regular file: {name}")
    return path


def read_local(root: Path, name: str) -> str | None:
    path = local_path(root, name)
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise RatchetError(f"cannot read proposal file {name}: {exc}") from exc


def read_json(root: Path, name: str) -> dict:
    text = read_local(root, name)
    if text is None:
        raise RatchetError(f"missing proposal file: {name}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"invalid proposal JSON: {name}") from exc
    if not isinstance(value, dict):
        raise RatchetError(f"expected a JSON object: {name}")
    return value


def replace_local(root: Path, name: str, text: str | None) -> None:
    path = local_path(root, name)
    if text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ratchet-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        local_path(root, name)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def mutation_lock(root: Path):
    # flock releases on process exit, unlike a sentinel file left behind after a crash.
    try:
        import fcntl
    except ImportError as exc:
        raise RatchetError("proposal writes currently require Unix file locks") from exc
    path = local_path(root, ".ratchet/proposals/write.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RatchetError("another proposal write is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


JOURNAL = ".ratchet/proposals/transaction.json"


def ensure_idle(root: Path) -> None:
    if read_local(root, JOURNAL) is not None:
        raise RatchetError("interrupted proposal transaction; run ratchet recover-proposals first")


def transact(root: Path, changes: dict[str, tuple[str | None, str | None]]) -> None:
    """Validate all baselines, journal before writing, recover explicitly after any partial failure.

    Multi-file visibility is not atomic. The journal makes interruption detectable and allows
    restoring the previous version without overwriting edits made after the interruption.
    """
    ensure_idle(root)
    for name, (before, _) in changes.items():
        if read_local(root, name) != before:
            raise RatchetError(f"file changed during proposal planning: {name}")
    replace_local(root, JOURNAL, encoded({"v": 1, "changes": changes}))
    try:
        for name, (before, after) in changes.items():
            if read_local(root, name) != before:
                raise RatchetError(f"concurrent edit detected: {name}")
            replace_local(root, name, after)
    except (OSError, RatchetError) as exc:
        raise RatchetError("proposal write interrupted; run recover-proposals before continuing") from exc
    replace_local(root, JOURNAL, None)


def recover(root: Path) -> int:
    with mutation_lock(root):
        if read_local(root, JOURNAL) is None:
            return 0
        journal = read_json(root, JOURNAL)
        changes = journal.get("changes")
        if journal.get("v") != 1 or not isinstance(changes, dict):
            raise RatchetError("invalid proposal recovery journal")
        for name, pair in changes.items():
            if not isinstance(pair, list) or len(pair) != 2 or not all(v is None or isinstance(v, str) for v in pair):
                raise RatchetError("invalid proposal recovery entry")
            if name == JOURNAL or read_local(root, name) not in pair:
                raise RatchetError(f"recovery would overwrite a later edit: {name}")
        for name, (before, _) in changes.items():
            replace_local(root, name, before)
        replace_local(root, JOURNAL, None)
        return len(changes)
