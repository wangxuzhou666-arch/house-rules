"""Repo-local proposal state and recoverable file updates. No shell commands are accepted here.

Two kinds of file are named here, by their path from the top of a worktree. Ratchet's own state (everything under STATE) is
shared by every worktree of the repo and lives in the main one, `root`. The tracked files a rule change edits (the rules,
the digest, the inject targets, the changelog) belong to the worktree being worked in, `tree`: `base_of` tells them apart.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import RatchetError


STATE = ".ratchet/proposals"
JOURNAL = f"{STATE}/transaction.json"


def base_of(root: Path, tree: Path, name: str) -> Path:
    """The worktree `name` is in: the main one for ratchet's own state, the one being worked in for a tracked file."""
    return root if name.startswith(f"{STATE}/") else tree


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _is_protected(part: str) -> bool:
    """One path segment that is `..` or names a secret: `.git` (its config can hold a remote URL with a token) or `.env*`.

    Compared with the case folded: macOS (APFS) and Windows open `.ENV` as the file `.env` and `.GIT/config` as `.git/config`.
    NFC/NFD normalization adds nothing: both names are ASCII, and no other code point folds into them (checked over every
    code point). On APFS only the case variants open them; fullwidth, zero-width and trailing-dot spellings do not
    (probed 2026-10-29).
    """
    folded = part.casefold()
    return folded in ("..", ".git") or folded.startswith(".env")


def local_path(root: Path, name: str) -> Path:
    """State and write targets stay inside the repo, with no symlink or secret-file traversal."""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(_is_protected(part) for part in relative.parts):
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
    path = local_path(root, f"{STATE}/write.lock")
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


def journal_tree(root: Path, journal: dict) -> str:
    """The worktree an interrupted transaction was writing tracked files in. A journal from before worktrees were told
    apart has no `tree`: it was written in the main worktree."""
    return journal.get("tree", str(root))


def ensure_idle(root: Path, tree: Path) -> None:
    if read_local(root, JOURNAL) is None:
        return
    owner = journal_tree(root, read_json(root, JOURNAL))
    if owner == str(tree):
        raise RatchetError("interrupted proposal transaction; run ratchet recover-proposals first")
    raise RatchetError(f"interrupted proposal transaction in {owner}; run ratchet recover-proposals there first")


def transact(root: Path, tree: Path, changes: dict[str, tuple[str | None, str | None]]) -> None:
    """Validate all baselines, journal before writing, recover explicitly after any partial failure.

    Multi-file visibility is not atomic. The journal makes interruption detectable and allows
    restoring the previous version without overwriting edits made after the interruption. It names `tree`, because
    the tracked files it restores are that worktree's, and only a recovery run there can put them back.
    """
    ensure_idle(root, tree)
    for name, (before, _) in changes.items():
        if read_local(base_of(root, tree, name), name) != before:
            raise RatchetError(f"file changed during proposal planning: {name}")
    replace_local(root, JOURNAL, encoded({"v": 1, "tree": str(tree), "changes": changes}))
    try:
        for name, (before, after) in changes.items():
            base = base_of(root, tree, name)
            if read_local(base, name) != before:
                raise RatchetError(f"concurrent edit detected: {name}")
            replace_local(base, name, after)
    except (OSError, RatchetError) as exc:
        raise RatchetError("proposal write interrupted; run recover-proposals before continuing") from exc
    replace_local(root, JOURNAL, None)


def recover(root: Path, tree: Path) -> int:
    with mutation_lock(root):
        if read_local(root, JOURNAL) is None:
            return 0
        journal = read_json(root, JOURNAL)
        changes = journal.get("changes")
        if journal.get("v") != 1 or not isinstance(changes, dict):
            raise RatchetError("invalid proposal recovery journal")
        owner = journal_tree(root, journal)
        if owner != str(tree):
            raise RatchetError(f"the interrupted proposal transaction was writing files in {owner}; "
                               "run ratchet recover-proposals there")
        for name, pair in changes.items():
            if not isinstance(pair, list) or len(pair) != 2 or not all(v is None or isinstance(v, str) for v in pair):
                raise RatchetError("invalid proposal recovery entry")
            if name == JOURNAL or read_local(base_of(root, tree, name), name) not in pair:
                raise RatchetError(f"recovery would overwrite a later edit: {name}")
        for name, (before, _) in changes.items():
            replace_local(base_of(root, tree, name), name, before)
        replace_local(root, JOURNAL, None)
        return len(changes)
