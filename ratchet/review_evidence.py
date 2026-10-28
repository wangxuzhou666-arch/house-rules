"""Bounded local evidence captured before review; imports never invent past snapshots."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import Config
from .errors import RatchetError
from .git import commit_sha, run_git
from .ledger import now_iso
from .proposal_store import encoded, fingerprint, read_json, read_local, replace_local
from .source_evidence import read_source

MAX_FILES = 100
MAX_TEXT_BYTES = 2 * 1024 * 1024


def capture(config: Config, target: str | None) -> dict:
    head = commit_sha(config.root, "HEAD")
    tracked = ["diff", "--name-only", "-z", "HEAD"] if head else ["ls-files", "-z"]
    names = set(run_git(tracked, config.root).stdout.split("\0"))
    names.update(run_git(["ls-files", "--others", "--exclude-standard", "-z"], config.root).stdout.split("\0"))
    names.discard("")
    names = sorted(n for n in names if not n.startswith(".ratchet/") and n != config.ledger_path.name)
    files, used = {}, 0
    for name in names[:MAX_FILES]:
        source = read_source(config.root, name, None)
        size = len(source.get("text", "").encode())
        if used + size > MAX_TEXT_BYTES:
            source.pop("text", None)
            source.update(status="unavailable", reason="Review evidence text budget exceeded.")
        else:
            used += size
        files[name] = source
    rules = [read_source(config.root, str(path), None) for path in config.rules_files]
    return {"v": 1, "captured_at": now_iso(), "head": head, "target": target,
            "basis": "local_pre_review" if target is None else "local_pre_review_target_unverified",
            "files": files, "rules": rules, "omitted_files": names[MAX_FILES:]}


def save(config: Config, run_id: str, snapshot: dict, raw: str) -> None:
    name = f".ratchet/reviews/{run_id}.evidence.json"
    if read_local(config.root, name) is not None:
        raise RatchetError("review evidence already exists for this run; choose a new run ID")
    stable = {}
    for file, source in snapshot["files"].items():
        after = read_source(config.root, file, None)
        stable[file] = (source["status"] == after["status"] == "available"
                        and source["sha256"] == after["sha256"])
    record = {**snapshot, "run_id": run_id, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
              "source_stable_during_review": stable}
    record["id"] = fingerprint(record)
    replace_local(config.root, name, encoded(record))


def source_for(config: Config, finding: dict) -> dict:
    name = f".ratchet/reviews/{finding['run_id']}.evidence.json"
    if read_local(config.root, name) is None:
        return {"status": "unavailable", "reason": "No original review evidence was saved."}
    record = read_json(config.root, name)
    if record.get("id") != fingerprint({k: v for k, v in record.items() if k != "id"}):
        raise RatchetError("saved review evidence changed")
    raw = read_local(config.root, f".ratchet/reviews/{finding['run_id']}.txt")
    if raw is None or hashlib.sha256(raw.encode()).hexdigest() != record["raw_sha256"]:
        raise RatchetError("raw review no longer matches its saved evidence")
    file = Path(finding["file"])
    if file.is_absolute():
        try:
            file = file.relative_to(config.root)
        except ValueError:
            return {"status": "unavailable", "reason": "Reported source is outside the local snapshot."}
    name_in_snapshot = file.as_posix()
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
    start, end = max(1, line - 3), min(len(lines), line + 3)
    result.pop("reason")
    result.update(status="available", file=name_in_snapshot, line_start=start, line_end=end,
                  text="\n".join(lines[start - 1:end]), sha256=source["sha256"],
                  stable_during_review=record["source_stable_during_review"][name_in_snapshot])
    return result
