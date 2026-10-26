"""The append-only ledger: event validation, file IO, and the fold that turns events into current state.

Three event kinds share one JSON-lines file: `finding` (something was flagged), `outcome` (what became of it) and
`gate_pass` (an automated gate passed over these files). Current state = fold of the events in file order.
Validation mirrors schemas/finding.v1.schema.json; tests/test_schema.py keeps the two in step.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import RatchetError

Event = dict[str, Any]

SOURCES = ("gate", "l1", "l2")
REVIEW_SOURCES = ("l1", "l2")
VERDICTS = ("CONFIRMED", "PLAUSIBLE")
OUTCOMES = ("fixed", "dismissed", "false_positive")
OUTCOME_BY = ("auto:commit", "auto:gate_pass", "human")
SEVERITIES = ("blocker", "should-fix", "nice")  # most severe first, so the index is the rank
RULE_ID_SOURCES = ("model", "human", "tool", "attributed")
STATUS_TO_OUTCOME = {"fixed": "fixed", "skipped": "dismissed", "no_change_needed": "false_positive"}
UNMAPPED = "unmapped"

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_SHA_RE = re.compile(r"^([0-9a-f]{7,40}|none)$")
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$")
# Structural only: whether a clause id exists is decided against the configured rules files when an event is
# created, never when old events are read back.
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_GATE_ID_RE = re.compile(r"^[a-z0-9-]+:gate:[0-9a-f]{12}$")
_REVIEW_ID_RE = re.compile(r"^[a-z0-9-]+:(l1|l2):[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$")


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---- validation -----------------------------------------------------------------------------------------------

Check = Callable[[Any], bool]


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _matches(pattern: re.Pattern[str]) -> Check:
    return lambda value: isinstance(value, str) and pattern.match(value) is not None


def _one_of(options: tuple[str, ...]) -> Check:
    return lambda value: isinstance(value, str) and value in options


def _is_int_at_least(minimum: int) -> Check:
    return lambda value: type(value) is int and value >= minimum  # `type is int` keeps True/False out


def _is_also_matches(value: Any) -> bool:
    return isinstance(value, list) and all(_is_also_match(item) for item in value)


def _is_also_match(item: Any) -> bool:
    return isinstance(item, dict) and set(item) == set(_ALSO_MATCH_FIELDS) and all(
        check(item[key]) for key, check in _ALSO_MATCH_FIELDS.items())


_ALSO_MATCH_FIELDS: dict[str, Check] = {
    "rule_id": _matches(_RULE_ID_RE), "severity": _one_of(SEVERITIES),
    "summary": _nonempty_str, "failure_scenario": _nonempty_str,
}
_FINDING_FIELDS: dict[str, Check] = {
    "v": lambda value: type(value) is int and value == 1,
    "kind": lambda value: value == "finding",
    "id": _nonempty_str,
    "display_id": _matches(_TOKEN_RE),
    "run_id": _matches(_TOKEN_RE),
    "repo": _matches(_SLUG_RE),
    "source": _one_of(SOURCES),
    "ts": _matches(_TS_RE),
    "sha": _matches(_SHA_RE),
    "file": _nonempty_str,
    "line": _is_int_at_least(1),
    "rule_code": _nonempty_str,
    "rule_id": _matches(_RULE_ID_RE),
    "category": _nonempty_str,
    "severity": _one_of(SEVERITIES),
    "summary": _nonempty_str,
    "failure_scenario": _nonempty_str,
    "verdict": _one_of(VERDICTS),
    "snippet": lambda value: isinstance(value, str),
    "discovered_at": _matches(_TS_RE),
    "also_matches": _is_also_matches,
    "rule_id_source": _one_of(RULE_ID_SOURCES),
}
_FINDING_OPTIONAL = frozenset({"snippet", "discovered_at", "also_matches", "rule_id_source"})
_OUTCOME_FIELDS: dict[str, Check] = {
    "v": _FINDING_FIELDS["v"],
    "kind": lambda value: value == "outcome",
    "id": _nonempty_str,
    "outcome": _one_of(OUTCOMES),
    "outcome_by": _one_of(OUTCOME_BY),
    "evidence": _nonempty_str,
    "ts": _matches(_TS_RE),
}
_GATE_PASS_FIELDS: dict[str, Check] = {
    "v": _FINDING_FIELDS["v"],
    "kind": lambda value: value == "gate_pass",
    "repo": _matches(_SLUG_RE),
    "ts": _matches(_TS_RE),
    "sha": _matches(_SHA_RE),
    "files": lambda value: isinstance(value, list) and len(value) >= 1 and all(_nonempty_str(item) for item in value),
}
_FIELDS_BY_KIND: dict[str, tuple[dict[str, Check], frozenset[str]]] = {
    "finding": (_FINDING_FIELDS, _FINDING_OPTIONAL),
    "outcome": (_OUTCOME_FIELDS, frozenset()),
    "gate_pass": (_GATE_PASS_FIELDS, frozenset()),
}


def _source_problems(event: Event) -> list[str]:
    """Id shapes differ by source: gate ids are content hashes, review ids spell out source and run."""
    source = event.get("source")
    expected: dict[str, re.Pattern[str]]
    if source == "gate":
        expected = {"id": _GATE_ID_RE, "display_id": _HEX12_RE}
    elif source in REVIEW_SOURCES:
        expected = {"id": _REVIEW_ID_RE}
    else:
        return []
    problems = [f"{key} {event[key]!r} does not fit a {source} finding"
                for key, pattern in expected.items() if key in event and not _matches(pattern)(event[key])]
    if source == "gate" and "run_id" in event and event["run_id"] != "gate":
        problems.append(f"run_id must be 'gate' for a gate finding, got {event['run_id']!r}")
    return problems


def validation_problems(event: Any) -> list[str]:
    if not isinstance(event, dict):
        return ["event is not a JSON object"]
    kind = event.get("kind")
    if not isinstance(kind, str) or kind not in _FIELDS_BY_KIND:
        return [f"unknown event kind: {kind!r}"]
    checks, optional = _FIELDS_BY_KIND[kind]
    problems: list[str] = []
    for key, check in checks.items():
        if key not in event:
            if key not in optional:
                problems.append(f"missing field {key!r}")
        elif not check(event[key]):
            problems.append(f"bad value for {key!r}: {event[key]!r}")
    problems.extend(f"unexpected field {key!r}" for key in event if key not in checks)
    if kind == "finding":
        problems.extend(_source_problems(event))
    return problems


def validate(event: Any) -> None:
    problems = validation_problems(event)
    if problems:
        kind = event.get("kind") if isinstance(event, dict) else None
        raise RatchetError(f"invalid {kind} event: " + "; ".join(problems))


# ---- file IO --------------------------------------------------------------------------------------------------

def append_events(path: Path, events: list[Event]) -> None:
    """Validate everything first, then write once with O_APPEND so concurrent appenders cannot interleave half lines."""
    for event in events:
        validate(event)
    if not events:
        return
    data = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except OSError as exc:
        raise RatchetError(f"cannot open ledger {path} for append: {exc}") from exc
    try:
        written = os.write(fd, data)
    finally:
        os.close(fd)
    if written != len(data):
        raise RatchetError(f"short write to ledger {path}: {written} of {len(data)} bytes")


def read_events(path: Path) -> list[Event]:
    """Every line must be valid JSON and a valid event; a ledger with a bad line is refused, not partly read."""
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RatchetError(f"{path}:{number}: not valid JSON: {exc}") from exc
            problems = validation_problems(event)
            if problems:
                raise RatchetError(f"{path}:{number}: " + "; ".join(problems))
            events.append(event)
    return events


# ---- state ----------------------------------------------------------------------------------------------------

@dataclass
class FindingState:
    finding: Event
    outcome: Event | None


def fold(events: list[Event]) -> dict[str, FindingState]:
    """{finding id: latest finding event + its outcome (or None)}, in order of first appearance.

    A finding event that follows an outcome for the same id re-opens it.
    """
    state: dict[str, FindingState] = {}
    for event in events:
        if event["kind"] == "finding":
            state[event["id"]] = FindingState(event, None)
        elif event["kind"] == "outcome":
            if event["id"] not in state:
                raise RatchetError(f"outcome refers to a finding that is not in the ledger: {event['id']}")
            state[event["id"]].outcome = event
    return state


def open_findings(events: list[Event]) -> list[Event]:
    return [item.finding for item in fold(events).values() if item.outcome is None]


def effective_rule_id(finding: Event, legacy_unmapped_alias: frozenset[str]) -> str:
    """Reviewer findings recorded under a catch-all clause read as `unmapped` when the config says that clause was one.

    Only the aggregated view changes; the stored event is never rewritten.
    """
    if finding["source"] in REVIEW_SOURCES and finding["rule_id"] in legacy_unmapped_alias:
        return UNMAPPED
    return finding["rule_id"]


def _is_duplicate(prior: FindingState, event: Event) -> bool:
    """Same id still open is a repeat. After an outcome, a gate re-detection is a new occurrence (re-open); a review replay is not."""
    return prior.outcome is None or event["source"] != "gate"


def _reject_conflicting_replay(prior: Event, event: Event) -> None:
    """A review id reused for different content would be silently dropped as a duplicate; refuse instead."""
    if event["source"] == "gate":
        return  # gate ids are content hashes; the same finding legitimately moves to another line
    if any(prior[key] != event[key] for key in ("file", "line", "summary")):
        raise RatchetError(
            f"finding id {event['id']} is already recorded with different file/line/summary; "
            "reuse of a run id for a different review? use a new --run value")


def select_new_events(existing: list[Event], events: list[Event]) -> list[Event]:
    """The subset of `events` that should be appended. A dropped duplicate finding takes its same-batch outcome with it."""
    state = fold(existing)
    fresh: list[Event] = []
    dropped_ids: set[str] = set()
    for event in events:
        validate(event)
        if event["kind"] == "finding":
            prior = state.get(event["id"])
            if prior is not None and _is_duplicate(prior, event):
                _reject_conflicting_replay(prior.finding, event)
                dropped_ids.add(event["id"])
                continue
            dropped_ids.discard(event["id"])
            state[event["id"]] = FindingState(event, None)
        elif event["kind"] == "outcome":
            if event["id"] in dropped_ids:
                continue
            if event["id"] not in state:
                raise RatchetError(f"outcome refers to a finding that is not in the ledger: {event['id']}")
            state[event["id"]].outcome = event
        fresh.append(event)
    return fresh
