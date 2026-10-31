"""Bounded local evidence captured before review; imports never invent past snapshots."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import Config, is_outer, rules_name
from .errors import HouseRulesError
from .git import commit_sha, run_git
from .ingest import Locator
from .ledger import now_iso
from .proposal_store import encoded, fingerprint, read_json, read_local, replace_local
from .source_evidence import read_committed_source, read_outer_source, read_source

MAX_FILES = 100
MAX_TEXT_BYTES = 2 * 1024 * 1024
LOCAL_BASIS = "local_pre_review"  # the evidence is the local files as they were when a review of them began (for a commit range, as HEAD held them)
SNIPPET_BEFORE, SNIPPET_AFTER = 3, 3  # lines of context either side of the flagged line
MAX_SNIPPET_CHARS = 600
TRUNCATED_MARK = "[snippet truncated]"  # the last line of a snippet that is less than the whole window
NO_SNIPPET = (None, "unavailable")


def is_local_state(config: Config, name: str) -> bool:
    """the tool's own files, the saved reviews under `.house-rules/` and the ledger: they change as house-rules writes them, so no evidence holds them."""
    return name.startswith(".house-rules/") or name == config.ledger_path.name


def _read_for_evidence(tree: Path, name: str, from_commit: str | None) -> dict:
    """`name` as the commit `from_commit` holds it, or as the working tree of `tree` holds it when there is no such commit."""
    if from_commit is None:
        return read_source(tree, name, None)
    return read_committed_source(tree, from_commit, name)


def capture(config: Config, tree: Path, target: str | None, committed_range: str | None = None) -> dict:
    """The evidence for a review about to start in `tree`, the worktree being reviewed: not always `config.root`, the main one.

    A review of `committed_range` (`<start>..<HEAD>`) was told to read those commits, not the working tree. Its evidence is the
    files the range changed, as HEAD holds them, whatever `tree` has uncommitted: the lines the reviewer names are the commit's,
    and a file edited since would put the same code on other lines. Any other review reads the local changes, the files that
    differ from HEAD and the untracked ones, as they are on disk.

    The rules files are tracked files like the code, so they are the ones of `tree` too (`config.rules_files` are its own), read
    from disk even for a range: the reviewer loads them from there. A layer outside the repo, shared with other repos, is the
    one file the config points to, whichever tree is reviewed.
    """
    head = commit_sha(tree, "HEAD")
    if committed_range is not None:
        from_commit = head
        names = set(run_git(["diff", "--name-only", "-z", committed_range], tree).stdout.split("\0"))
    else:
        from_commit = None
        tracked = ["diff", "--name-only", "-z", "HEAD"] if head else ["ls-files", "-z"]
        names = set(run_git(tracked, tree).stdout.split("\0"))
        names.update(run_git(["ls-files", "--others", "--exclude-standard", "-z"], tree).stdout.split("\0"))
    names.discard("")
    names = sorted(n for n in names if not is_local_state(config, n))
    files, used = {}, 0
    for name in names[:MAX_FILES]:
        source = _read_for_evidence(tree, name, from_commit)
        size = len(source.get("text", "").encode())
        if used + size > MAX_TEXT_BYTES:
            source.pop("text", None)
            source.update(status="unavailable", reason="Review evidence text budget exceeded.")
        else:
            used += size
        files[name] = source
    rules = [read_outer_source(path, rules_name(config, path)) if is_outer(config, path) else read_source(tree, str(path), None)
             for path in config.rules_files]
    return {"v": 1, "captured_at": now_iso(), "head": head, "target": target, "committed_range": committed_range,
            "basis": LOCAL_BASIS if target is None else f"{LOCAL_BASIS}_target_unverified",
            "files": files, "rules": rules, "omitted_files": names[MAX_FILES:]}


def save(config: Config, tree: Path, run_id: str, snapshot: dict, raw: str) -> None:
    """Write the evidence under the main worktree's `.house-rules/`; `tree` is where the snapshot's files are re-read to see if they held still.

    A range review's files are re-read from the commit they were captured at: the working tree is not what they were.
    """
    name = f".house-rules/reviews/{run_id}.evidence.json"
    if read_local(config.root, name) is not None:
        raise HouseRulesError("review evidence already exists for this run; choose a new run ID")
    from_commit = snapshot["head"] if snapshot["committed_range"] is not None else None
    stable = {}
    for file, source in snapshot["files"].items():
        after = _read_for_evidence(tree, file, from_commit)
        stable[file] = (source["status"] == after["status"] == "available"
                        and source["sha256"] == after["sha256"])
    record = {**snapshot, "run_id": run_id, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
              "source_stable_during_review": stable}
    record["id"] = fingerprint(record)
    replace_local(config.root, name, encoded(record))


def snapshot_name(root: Path, file: str) -> str | None:
    """A finding's `file` as a snapshot spells it (relative to the worktree top `root`, `/`-separated); None for a file outside it."""
    path = Path(file)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    return path.as_posix()


def snippet_around(lines: list[str], line: int) -> str | None:
    """`line` (1-based) with up to three lines either side, or None when the file has no such line.

    The block is held to MAX_SNIPPET_CHARS by dropping the line farthest from the flagged one, so the flagged line is the
    last to go; if it is longer than the budget on its own it is cut. Anything dropped or cut is said so in a last line:
    a snippet must never look like the whole window when it is not. A block clipped by the start or end of the file is
    the whole window, and says nothing.
    """
    if not 1 <= line <= len(lines):
        return None
    first, last = max(line - 1 - SNIPPET_BEFORE, 0), min(line + SNIPPET_AFTER, len(lines))  # lines[first:last]
    window = (first, last)
    while len("\n".join(lines[first:last])) > MAX_SNIPPET_CHARS and last - first > 1:
        if line - 1 - first >= last - line:  # as many lines above as below, or more: the farthest one is above
            first += 1
        else:
            last -= 1
    text = "\n".join(lines[first:last])
    cut = (first, last) != window
    if len(text) > MAX_SNIPPET_CHARS:  # only the flagged line is left, and it is too long on its own
        text, cut = text[:MAX_SNIPPET_CHARS], True
    return f"{text}\n{TRUNCATED_MARK}" if cut else text


def snapshot_snippets(config: Config, tree: Path, snapshot: dict) -> Locator:
    """Snippets of the code the reviewer saw when it started, not of the working tree as it is after the review.

    A file `capture` saved is read from the snapshot. A file it neither saved nor left out had no change since HEAD when
    the review began, so the commit `capture` recorded as `head` holds exactly what the reviewer read: that is the
    snippet. The commit is no stand-in for a file `capture` did see change but could not keep (over the text budget,
    binary, deleted), for a changed file beyond MAX_FILES, for the tool's own files, or for a review that began before
    the first commit: those have no snippet.

    A review of a commit range read commits only, so for it the commit is the one source: `capture` saved the committed
    text of the files the range changed, and any other file is the commit's as well, whatever the working tree has
    uncommitted in it. A file that exists only in the working tree has no snippet.

    Only a review of the local changes qualifies. For a --target review `capture` marks the snapshot target-unverified:
    the review looked at something else (a PR, another branch), so the local files, and HEAD, may be other code than the
    finding is about. Those findings record "unavailable" rather than a snippet that might mislead.
    """
    trusted = snapshot["basis"] == LOCAL_BASIS
    head = snapshot["head"]
    left_out = set(snapshot["omitted_files"])

    def reviewed_text(name: str) -> str | None:
        entry = snapshot["files"].get(name)
        if entry is not None:
            return entry["text"] if entry["status"] == "available" else None
        elif head is None or name in left_out or is_local_state(config, name):
            return None
        else:
            source = read_committed_source(tree, head, name)
            return source["text"] if source["status"] == "available" else None

    def locate(file: str, line: int) -> tuple[str | None, str]:
        name = snapshot_name(tree, file)
        if not trusted or name is None:
            return NO_SNIPPET
        text = reviewed_text(name)
        snippet = None if text is None else snippet_around(text.splitlines(), line)
        return NO_SNIPPET if snippet is None else (snippet, "review")

    return locate


def worktree_snippets(tree: Path) -> Locator:
    """Snippets read from the working tree of `tree` as it is now, for a review that left no evidence (--from-file).

    The review may have seen other code than this: the source says so, and nothing here pretends otherwise.
    """
    def locate(file: str, line: int) -> tuple[str | None, str]:
        source = read_source(tree, file, None)  # the same bounded, repo-local reader as every other source excerpt
        if source["status"] != "available":
            return NO_SNIPPET
        text = snippet_around(source["text"].splitlines(), line)
        return NO_SNIPPET if text is None else (text, "worktree")

    return locate


def source_for(config: Config, finding: dict) -> dict:
    name = f".house-rules/reviews/{finding['run_id']}.evidence.json"
    if read_local(config.root, name) is None:
        return {"status": "unavailable", "reason": "No original review evidence was saved."}
    record = read_json(config.root, name)
    if record.get("id") != fingerprint({k: v for k, v in record.items() if k != "id"}):
        raise HouseRulesError("saved review evidence changed")
    raw = read_local(config.root, f".house-rules/reviews/{finding['run_id']}.txt")
    if raw is None or hashlib.sha256(raw.encode()).hexdigest() != record["raw_sha256"]:
        raise HouseRulesError("raw review no longer matches its saved evidence")
    name_in_snapshot = snapshot_name(config.worktree, finding["file"])
    if name_in_snapshot is None:
        return {"status": "unavailable", "reason": "Reported source is outside the local snapshot."}
    source = record["files"].get(name_in_snapshot)
    result = {"status": "unavailable", "snapshot_id": record["id"],
              "absolute_path": str(config.root / name), "basis": record["basis"],
              "reason": "Reported source or line was not captured."}
    if source is None or source["status"] != "available":
        return result
    lines = source["text"].splitlines()
    line = finding["line"]
    if not 1 <= line <= len(lines):
        return result
    start, end = max(1, line - SNIPPET_BEFORE), min(len(lines), line + SNIPPET_AFTER)
    result.pop("reason")
    result.update(status="available", file=name_in_snapshot, line_start=start, line_end=end,
                  text="\n".join(lines[start - 1:end]), sha256=source["sha256"],
                  stable_during_review=record["source_stable_during_review"][name_in_snapshot])
    return result
