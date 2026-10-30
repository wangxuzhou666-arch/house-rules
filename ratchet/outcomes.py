"""Attribution: give open findings an outcome, from commit trailers, gate passes, or a human label page.

Rules (see docs/LEDGER.md):
  - A commit closes a finding only through an explicit `Fixes-Finding: <display_id or full id>` trailer. There is
    no time check: the trailer is the author's stated intent, and ledger `ts` is when the finding was *written*,
    which can be later than the fix.
  - A review (l1/l2) finding is never closed by a gate pass. A gate finding is closed by the first later gate_pass
    listing its file - unless it was skipped past the gate, because the gate only inspects changed lines and the
    violation is still in the file.
  - Skipped findings the skips file marks `false_positive` are only pre-selected on the label page, never auto-labeled.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import RatchetError
from .git import head_sha, run_git
from .ledger import OUTCOMES, Event, FindingState, effective_rule_id, fold

TRAILER_KEY = "Fixes-Finding"
PAGE_NAME = ".ratchet-label-page.html"
BATCH_NAME = ".ratchet-label-batch.json"

_FIELD_SEP = "\x1f"
_VALUE_SEP = "\x1e"
_RECORD_END = "\x1d"
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
_COMMIT_EVIDENCE_RE = re.compile(r"^commit ([0-9a-f]{8})\b")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    tokens: tuple[str, ...]  # every id named by a Fixes-Finding trailer, in order


def trailer_commits(root: Path, all_refs: bool) -> list[Commit]:
    """Commits (oldest first) carrying at least one Fixes-Finding trailer, read with git's own trailer parser.

    git only recognizes trailers in the last paragraph of the message, so a stray "Fixes-Finding:" mid-body is ignored.
    """
    if not all_refs and head_sha(root) == "none":
        return []
    pretty = (f"%H{_FIELD_SEP}%s{_FIELD_SEP}"
              f"%(trailers:key={TRAILER_KEY},valueonly=true,unfold=true,separator=%x1e){_RECORD_END}")
    revs = ["--all"] if all_refs else ["HEAD"]
    out = run_git(["log", "--reverse", f"--format={pretty}", *revs], root).stdout
    commits = []
    for record in out.split(_RECORD_END):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, trailer_values = record.split(_FIELD_SEP, 2)
        tokens = tuple(token for value in trailer_values.split(_VALUE_SEP)
                       for token in _TOKEN_SPLIT_RE.split(value.strip()) if token)
        if tokens:
            commits.append(Commit(sha, subject, tokens))
    return commits


def _fixed(finding_id: str, by: str, evidence: str, ts: str) -> Event:
    return {"v": 1, "kind": "outcome", "id": finding_id, "outcome": "fixed", "outcome_by": by,
            "evidence": evidence[:300], "ts": ts}


@dataclass(frozen=True)
class TrailerResult:
    outcomes: list[Event]
    unmatched: list[tuple[str, str]]  # (commit sha, token) naming no finding in the ledger
    ambiguous: list[tuple[str, str]]  # (commit sha, display_id) shared by several findings; use the full id


def _consumed_commits(events: list[Event]) -> set[tuple[str, str]]:
    """(finding id, short sha) pairs an earlier outcome already used as evidence.

    A gate finding can re-open when the violation comes back; the old fix commit must not close the new occurrence.
    """
    pairs = set()
    for event in events:
        if event["kind"] == "outcome" and event["outcome_by"] == "auto:commit":
            match = _COMMIT_EVIDENCE_RE.match(event["evidence"])
            if match is not None:
                pairs.add((event["id"], match.group(1)))
    return pairs


def finding_lookup(state: dict[str, FindingState]) -> Callable[[str], list[str]]:
    """token -> the ids of the findings it names: the finding with that full id, else every finding with that display_id.

    Empty: it names nothing. Two or more: the display_id is shared (it is only unique within a run), and only a full id
    picks one. A `Fixes-Finding` trailer and `ratchet label` read their ids through this one lookup.
    """
    by_display_id: dict[str, list[str]] = {}
    for finding_id, item in state.items():
        by_display_id.setdefault(item.finding["display_id"], []).append(finding_id)
    return lambda token: [token] if token in state else by_display_id.get(token, [])


def commit_outcomes(events: list[Event], commits: list[Commit], ts: str) -> TrailerResult:
    state = fold(events)
    lookup = finding_lookup(state)
    consumed = _consumed_commits(events)

    outcomes: list[Event] = []
    unmatched: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []
    for commit in commits:
        for token in commit.tokens:
            targets = lookup(token)
            if not targets:
                unmatched.append((commit.sha, token))
            elif len(targets) > 1:
                ambiguous.append((commit.sha, token))
            elif state[targets[0]].outcome is None and (targets[0], commit.sha[:8]) not in consumed:
                outcome = _fixed(targets[0], "auto:commit", f"commit {commit.sha[:8]} {commit.subject}", ts)
                state[targets[0]].outcome = outcome
                outcomes.append(outcome)
    return TrailerResult(outcomes, unmatched, ambiguous)


def gate_pass_outcomes(events: list[Event], ts: str, skipped_ids: set[str]) -> list[Event]:
    """Close open gate findings covered by a later gate_pass (later = further down the file, not a later timestamp)."""
    state = fold(events)
    last_seen = {event["id"]: index for index, event in enumerate(events) if event["kind"] == "finding"}
    passes = [(index, event) for index, event in enumerate(events) if event["kind"] == "gate_pass"]
    outcomes = []
    for finding_id, item in state.items():
        finding = item.finding
        if item.outcome is not None or finding["source"] != "gate" or finding_id in skipped_ids:
            continue
        hit = next((event for index, event in passes
                    if index > last_seen[finding_id] and finding["file"] in event["files"]), None)
        if hit is not None:
            evidence = f"gate_pass {hit['ts']} sha {hit['sha']} covers {finding['file']}"
            outcomes.append(_fixed(finding_id, "auto:gate_pass", evidence, ts))
    return outcomes


# ---- skips file -----------------------------------------------------------------------------------------------

def read_skips(path: Path | None) -> list[dict[str, Any]]:
    """Rows of the skips file: JSON lines, each optionally with `finding_ids` and `reason_kind`.

    No configured path, or a file that does not exist yet (gates create it on the first skip), means no skips.
    """
    if path is None or not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RatchetError(f"{path}:{number}: not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise RatchetError(f"{path}:{number}: expected a JSON object")
        ids = row.get("finding_ids", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise RatchetError(f"{path}:{number}: finding_ids must be a list of strings")
        rows.append(row)
    return rows


def skipped_finding_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {finding_id for row in rows for finding_id in row.get("finding_ids", [])}


def false_positive_skips(rows: list[dict[str, Any]]) -> set[str]:
    return {finding_id for row in rows if row.get("reason_kind") == "false_positive"
            for finding_id in row.get("finding_ids", [])}


# ---- label page -----------------------------------------------------------------------------------------------

_TEMPLATE_PATH = Path(__file__).with_name("label_page.html")


def build_page(findings: list[Event], preselect_false_positive: set[str], batch_id: str,
               legacy_unmapped_alias: frozenset[str]) -> str:
    cards = []
    for finding in findings:
        shown_rule = effective_rule_id(finding, legacy_unmapped_alias)
        if shown_rule != finding["rule_id"]:
            shown_rule = f"{shown_rule} (legacy {finding['rule_id']})"
        cards.append({
            "id": finding["id"], "display_id": finding["display_id"], "source": finding["source"],
            "severity": finding["severity"], "rule": shown_rule, "file": finding["file"], "line": finding["line"],
            "sha": finding["sha"], "summary": finding["summary"], "failure_scenario": finding["failure_scenario"],
            "snippet": finding.get("snippet", ""),
            "also": [match["rule_id"] for match in finding.get("also_matches", [])],
        })
    payload = {"batch_id": batch_id, "findings": cards, "preselect_false_positive": sorted(preselect_false_positive)}
    # Escaping every "<" keeps a "</script>" or "<!--" inside a summary from breaking out of the data block.
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return _TEMPLATE_PATH.read_text(encoding="utf-8").replace("__DATA__", data)


def new_batch_id(now: str) -> str:
    return now.replace("-", "").replace(":", "").replace("T", "-") + "-" + secrets.token_hex(3)


def validate_labels(payload: Any, batch: dict[str, Any], state: dict[str, FindingState]) -> list[str]:
    """All problems with a label submission (empty = valid). One bad label rejects the whole batch."""
    if not isinstance(payload, dict) or not isinstance(payload.get("labels"), list):
        return ["top level must be {batch_id, labels: [...]}"]
    errors = []
    if payload.get("batch_id") != batch["batch_id"]:
        errors.append(f"batch_id mismatch: got {payload.get('batch_id')!r}, current batch is {batch['batch_id']!r} "
                      "(was the label page regenerated?)")
    if not payload["labels"]:
        errors.append("labels is empty")
    seen: set[str] = set()
    for number, label in enumerate(payload["labels"]):
        if not isinstance(label, dict) or not isinstance(label.get("id"), str):
            errors.append(f"labels[{number}] must be an object with a string id")
            continue
        finding_id = label["id"]
        if not isinstance(label.get("note", ""), str):
            errors.append(f"labels[{number}] note must be a string")
        if finding_id in seen:
            errors.append(f"labels[{number}] duplicate id: {finding_id}")
        seen.add(finding_id)
        if finding_id not in state:
            errors.append(f"labels[{number}] id is not in the ledger: {finding_id!r}")
        elif state[finding_id].outcome is not None:
            errors.append(f"labels[{number}] already has an outcome (not open): {finding_id}")
        elif finding_id not in batch["finding_ids"]:
            errors.append(f"labels[{number}] is not in the current batch: {finding_id}")
        if label.get("outcome") not in OUTCOMES:
            errors.append(f"labels[{number}] outcome={label.get('outcome')!r} not in {OUTCOMES}")
    return errors


def label_events(payload: dict[str, Any], default_evidence: str, ts: str) -> list[Event]:
    """One `human` outcome per label, built here for both `apply-labels` and `ratchet label` so the two cannot drift apart.
    A label's note is its evidence; a label without one gets `default_evidence`, which says where it came from."""
    return [{"v": 1, "kind": "outcome", "id": label["id"], "outcome": label["outcome"], "outcome_by": "human",
             "ts": ts, "evidence": (label.get("note") or "").strip() or default_evidence}
            for label in payload["labels"]]

