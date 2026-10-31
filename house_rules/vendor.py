"""`house-rules vendor`: copy the tool into another repo, and later prove the copy was not edited or left behind.

Some repos run their tests in throwaway worktrees, or copy scripts into temp repos, where an installed checkout is not on
the path. They keep the tool inside the repo instead. `VENDORED_FROM.json` records which source the copy came from and a
hash of its files, so a hand edit (which the next vendoring would silently lose) and a stale copy both show up in CI.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import HouseRulesError
from .fsutil import read_text, write_text
from .git import run_git

SOURCE_ROOT = Path(__file__).resolve().parent.parent
MARKER_NAME = "VENDORED_FROM.json"
# What a vendored copy holds, relative to the source root. Docs stay behind. The rules template is small and `init --template`
# reads it, so a copy without it would fail there.
VENDORED_ENTRIES = ("house_rules", "bin/house-rules", "schemas", "templates", "LICENSE")
UNCOMMITTED = "uncommitted"
# Running the copy makes bytecode caches (*.pyc); a Finder window makes .DS_Store. Neither is part of the tool.
_IGNORED_NAMES = frozenset({".DS_Store"})
_BYTECODE_SUFFIX = ".pyc"


@dataclass(frozen=True)
class Marker:
    source_commit: str
    content_sha256: str


def tree_files(base: Path) -> list[str]:
    """The vendored files under `base` as sorted `/`-separated relative paths; an entry that is absent contributes nothing."""
    found: set[str] = set()
    for entry in VENDORED_ENTRIES:
        path = base / entry
        if path.is_file():
            found.add(entry)
        elif path.is_dir():
            for root, _, names in os.walk(path):
                found.update((Path(root) / name).relative_to(base).as_posix()
                             for name in names if name not in _IGNORED_NAMES and not name.endswith(_BYTECODE_SUFFIX))
    return sorted(found)


def content_sha256(base: Path, files: list[str]) -> str:
    """sha256 over one `path NUL sha256-of-bytes LF` line per file, in path order: it moves when a byte, a name or the file set does."""
    digest = hashlib.sha256()
    for rel in files:
        digest.update(f"{rel}\0{hashlib.sha256((base / rel).read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def source_commit(source: Path) -> str:
    """The full HEAD sha when `source` is the top of a git repo whose vendored files all match HEAD, else `uncommitted`.

    Anything less would name a commit the copy does not correspond to: an unborn branch, a dirty tree, or a source that
    only sits inside some other repository (a copy that was itself vendored).
    """
    top = run_git(["rev-parse", "--show-toplevel"], source, allowed_returncodes=(0, 128))
    if top.returncode != 0 or Path(os.path.realpath(top.stdout.strip())) != Path(os.path.realpath(source)):
        return UNCOMMITTED
    head = run_git(["rev-parse", "--verify", "-q", "HEAD"], source, allowed_returncodes=(0, 1))
    if head.returncode != 0:
        return UNCOMMITTED
    changed = run_git(["status", "--porcelain", "--untracked-files=all", "--", *VENDORED_ENTRIES], source).stdout.strip()
    return UNCOMMITTED if changed else head.stdout.strip()


def read_marker(target: Path) -> Marker:
    text = read_text(target / MARKER_NAME)
    if text is None:
        raise HouseRulesError(f"{target / MARKER_NAME} not found: {target} was not made by `house-rules vendor`")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HouseRulesError(f"{target / MARKER_NAME}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) and raw[key] for key in ("source_commit", "content_sha256")):
        raise HouseRulesError(f"{target / MARKER_NAME}: expected an object with string fields source_commit and content_sha256")
    return Marker(raw["source_commit"], raw["content_sha256"])


@dataclass(frozen=True)
class VendorPlan:
    files: list[str]
    marker: Marker


def plan_vendor(source: Path) -> VendorPlan:
    files = tree_files(source)
    missing = [entry for entry in VENDORED_ENTRIES if not any(rel == entry or rel.startswith(f"{entry}/") for rel in files)]
    if missing:
        raise HouseRulesError(f"{source} has nothing to vendor at {missing}; is it a house-rules checkout?")
    return VendorPlan(files, Marker(source_commit(source), content_sha256(source, files)))


def vendor_into(source: Path, target: Path, plan: VendorPlan) -> None:
    """Copy the planned files into `target` and write the marker. A directory that already holds a vendored copy is
    refreshed; one that holds the tool's paths without a marker is somebody else's files and is refused."""
    if target.is_relative_to(source):
        raise HouseRulesError(f"--into {target} is inside the source checkout {source}")
    has_marker = (target / MARKER_NAME).is_file()
    in_the_way = [entry for entry in VENDORED_ENTRIES if os.path.lexists(target / entry)]
    if in_the_way and not has_marker:
        raise HouseRulesError(f"{target} already has {in_the_way} but no {MARKER_NAME}; refusing to overwrite files "
                           "that `house-rules vendor` did not create")
    workspace = None
    keep_backup = False
    installed, backed_up = [], []
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix=".house-rules-vendor-", dir=target.parent))
        staged, backup = workspace / "new", workspace / "old"
        for rel in plan.files:
            (staged / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, staged / rel)  # Keep the launcher's executable mode.
        if content_sha256(staged, tree_files(staged)) != plan.marker.content_sha256:
            raise HouseRulesError("prepared vendor copy does not match the plan; source or copied content changed")
        marker = {"source_commit": plan.marker.source_commit, "content_sha256": plan.marker.content_sha256}
        write_text(staged / MARKER_NAME, json.dumps(marker, indent=2) + "\n")
        # Only owned entries move; unrelated host files, including other bin tools, stay in place.
        for entry in (*VENDORED_ENTRIES, MARKER_NAME):
            (target / entry).parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target / entry):
                (backup / entry).parent.mkdir(parents=True, exist_ok=True)
                os.replace(target / entry, backup / entry)
                backed_up.append(entry)
            os.replace(staged / entry, target / entry)
            installed.append(entry)
    except (OSError, HouseRulesError) as exc:
        try:
            for entry in reversed(installed):
                os.replace(target / entry, staged / entry)
            for entry in reversed(backed_up):
                os.replace(backup / entry, target / entry)
        except OSError as restore_error:
            keep_backup = True
            raise HouseRulesError(f"vendor replacement failed ({exc}); restoration failed ({restore_error}); "
                               f"preserved recovery files at {workspace}; do not retry before restoring them") from restore_error
        raise HouseRulesError(f"cannot replace vendor copy at {target}: {exc}; previous files retained or restored") from exc
    finally:
        if workspace is not None and not keep_backup:
            shutil.rmtree(workspace)


def check_vendored(target: Path, source: Path | None) -> list[str]:
    """Problems with a vendored copy, empty when it is intact (and, given `source`, still current)."""
    marker = read_marker(target)
    problems = []
    actual = content_sha256(target, tree_files(target))
    if actual != marker.content_sha256:
        problems.append(f"{target} was edited since it was vendored (files hash to {actual[:12]}, "
                        f"{MARKER_NAME} says {marker.content_sha256[:12]}); re-run `house-rules vendor` to restore it")
    if source is not None:
        current = content_sha256(source, tree_files(source))
        if current != marker.content_sha256:
            problems.append(f"{target} is stale: the source at {source} hashes to {current[:12]}, "
                            f"the copy was vendored from {marker.content_sha256[:12]}; re-run `house-rules vendor`")
    return problems
