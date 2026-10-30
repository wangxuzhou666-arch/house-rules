"""Where ratchet started watching the default branch: the commit a wrap-up there measures from when no review came first.

On the default branch the fork point is HEAD itself, so without a start point the commits made there never count and a wrap-up
sees only the work that is not committed yet. `ratchet init` records the branch as it stands; the automatic wrap-up records the
parent of the first commit it was told about, when no init did.

The record is `.ratchet/start-points.json` in the main worktree, shared by every worktree like the ledger and git-ignored with
the rest of `.ratchet/`: a flat `{branch: full sha}`. It is written whole and moved into place, so a reader never sees half of
it. There is no lock: two writers that race both write a start that is valid.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .errors import RatchetError
from .git import is_ancestor
from .proposal_store import encoded, read_local, replace_local

START_POINTS_NAME = ".ratchet/start-points.json"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def read_start_points(root: Path) -> dict[str, str]:
    """{branch: commit} as `root`, the main worktree, records it; empty when nothing was recorded.

    A file that is not a JSON object of branch names and full shas is an error that names it, never an empty record: it was
    written by this tool or by hand, and measuring from a start point it silently dropped would count the wrong work.
    """
    text = read_local(root, START_POINTS_NAME)
    if text is None:
        return {}
    try:
        recorded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"{root / START_POINTS_NAME} is not JSON ({exc}); fix it, or delete it and run `ratchet init`") from exc
    if not isinstance(recorded, dict) or not all(isinstance(commit, str) and _COMMIT_RE.fullmatch(commit) for commit in recorded.values()):
        raise RatchetError(f"{root / START_POINTS_NAME} must be a JSON object of branch names and 40-hex commits; "
                           "fix it, or delete it and run `ratchet init`")
    return recorded


def usable_start_point(config: Config, branch: str, tip: str) -> str | None:
    """The start point recorded for `branch` when it is usable: a commit this clone has that `tip`, the branch's commit now,
    descends from. One that is not (history was rewritten, the clone pruned) counts as none, and a new one may be recorded."""
    recorded = read_start_points(config.root).get(branch)
    if recorded is not None and is_ancestor(config.worktree, recorded, tip):
        return recorded
    return None


def record_start_point(config: Config, branch: str, commit: str) -> None:
    """Make `commit` the start point of `branch`, and keep the start points of the other branches. The caller has found that
    none is usable (`usable_start_point`): a usable one never moves."""
    assert _COMMIT_RE.fullmatch(commit), f"{commit!r} is not a full commit sha: the next read of the file would refuse it"
    recorded = read_start_points(config.root)
    replace_local(config.root, START_POINTS_NAME, encoded({**recorded, branch: commit}))
