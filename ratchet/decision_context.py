"""Read-only evidence packets and compact model assessments for a human decision.

Source coordinates come from tools, not model text. A saved HEAD and a current
workspace excerpt are references, never proof of the code a historical review saw.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Config
from .errors import RatchetError
from .ledger import fold, read_events
from .proposal_store import encoded, fingerprint
from .proposals import clause_block, create_batch
from .rules import load_rules, parse_rules_text

from .source_evidence import read_source as _read_source
from .review_evidence import source_for

ACTIONS = ("covered", "keep", "add", "rewrite", "merge", "retire")


def build_context(config: Config, events: list[dict], ids: list[str] | None = None,
                  locations: dict[str, tuple[int, int]] | None = None) -> dict:
    state = fold(events)
    selected = ids if ids else list(state)
    if len(set(selected)) != len(selected) or any(key not in state for key in selected):
        raise RatchetError("use unique full finding IDs from the ledger")
    locations = locations or {}
    for key, (start, end) in locations.items():
        if key not in selected or type(start) is not int or type(end) is not int or not 1 <= start <= end < start + 80:
            raise RatchetError("a location must name a selected finding and 1–80 positive source lines")
    rules = []
    for path in config.rules_files:
        source = _read_source(config.root, str(path), None)
        if source["status"] != "available":
            raise RatchetError(f"cannot prepare rules context: {source['reason']}")
        rules.append(source)
    findings = []
    for key in selected:
        item = state[key]
        finding = item.finding
        start, end = locations.get(key, (finding["line"], None))
        source = _read_source(config.root, finding["file"], start, end)
        # The ledger stores import-time HEAD, not a snapshot of an uncommitted diff.
        reviewed = source_for(config, finding)
        source.update(recorded_sha=finding["sha"], review_snapshot=reviewed["status"], reported_line=finding["line"],
                      location_basis="selected_current_range" if key in locations else "reported_line_unverified")
        raw = _read_source(config.root, f".ratchet/reviews/{finding['run_id']}.txt", None)
        raw.pop("text", None)  # The original finding is already included; do not repeat a whole run per card.
        findings.append({"finding": finding, "outcome": item.outcome,
                         "source": source, "review_source": reviewed, "raw_review": raw})
    context = {"v": 1, "repo": str(config.root), "rules": rules, "findings": findings}
    return {**context, "context_id": fingerprint(context)}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RatchetError(f"{label} must be nonempty text")
    return value


def assess(config: Config, events: list[dict], context: dict, payload: dict) -> dict:
    """Validate model decisions and attach tool-owned sources; never apply a rule change."""
    if not isinstance(payload, dict) or set(payload) != {"v", "context_id", "assessments"}:
        raise RatchetError("assessment needs v, context_id and assessments")
    if type(payload["v"]) is not int or payload["v"] != 1 or payload["context_id"] != context["context_id"]:
        raise RatchetError("assessment context changed; collect and assess again")
    entries = payload["assessments"]
    if not isinstance(entries, list) or not entries:
        raise RatchetError("assessments must be a nonempty list")
    by_id = {item["finding"]["id"]: item for item in context["findings"]}
    rules = load_rules(config)
    seen, cards, drafts = set(), [], []
    for entry in entries:
        fields = {"finding_ids", "verification", "action", "rule_ids", "reason", "changes"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise RatchetError(f"each assessment needs exactly {sorted(fields)}")
        ids = entry["finding_ids"]
        if (not isinstance(ids, list) or not ids or not all(isinstance(k, str) and k in by_id for k in ids)
                or len(set(ids)) != len(ids) or seen.intersection(ids)):
            raise RatchetError("each finding must appear exactly once; group related findings together")
        seen.update(ids)
        action = entry["action"]
        if action not in ACTIONS:
            raise RatchetError(f"action must be one of {ACTIONS}")
        verification = entry["verification"]
        if (not isinstance(verification, dict) or set(verification) != {"status", "basis"}
                or verification["status"] not in ("supported", "uncertain", "refuted")):
            raise RatchetError("verification needs status (supported/uncertain/refuted) and basis")
        _text(verification["basis"], "verification basis")
        _text(entry["reason"], "reason")
        matched = entry["rule_ids"]
        if (not isinstance(matched, list) or not all(isinstance(k, str) and k in rules.clauses for k in matched)
                or len(set(matched)) != len(matched)):
            raise RatchetError("rule_ids must name distinct existing clauses")
        if action in ("covered", "rewrite", "merge", "retire") and not matched:
            raise RatchetError(f"{action} needs the existing rule IDs")
        changes = entry["changes"]
        if not isinstance(changes, list):
            raise RatchetError("changes must be a list")
        if (action in ("covered", "keep")) != (not changes):
            raise RatchetError("covered/keep must have no changes; other actions require changes")
        if changes and verification["status"] != "supported":
            raise RatchetError("uncertain/refuted evidence cannot produce a rule-change draft")
        group = []
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"action", "rule_id", "file", "after"}:
                raise RatchetError("change needs action, rule_id, file and after")
            if not all(isinstance(value, str) for value in change.values()):
                raise RatchetError("change fields must be strings")
            if change["action"] != "add" and change["rule_id"] not in matched:
                raise RatchetError("rewritten/retired rules must appear in rule_ids")
            group.append({**change, "rationale": entry["reason"], "evidence_ids": ids})
        if action in ("add", "rewrite", "retire") and (len(group) != 1 or group[0]["action"] != action):
            raise RatchetError("add/rewrite/retire assessments require one matching change")
        if action == "merge":
            rewritten = [d for d in group if d["action"] == "rewrite"]
            retired = [d for d in group if d["action"] == "retire"]
            if (len(matched) < 2 or len(rewritten) != 1 or len(retired) != len(matched) - 1
                    or {d["rule_id"] for d in group} != set(matched) or len(group) != len(matched)):
                raise RatchetError("merge must rewrite one surviving rule and retire the other matched rules")
        drafts.extend(group)
        matches = []
        for rule_id in matched:
            for source in context["rules"]:
                # Definitions are unique across configured files; do not guess a file from a prose mention.
                parsed = parse_rules_text(source["text"], source["file"], config.clause_id_pattern)
                if rule_id in parsed:
                    matches.append({"rule_id": rule_id, "absolute_path": source["absolute_path"],
                                    "line": parsed[rule_id].line,
                                    "text": clause_block(source["text"], rule_id, config)})
        cards.append({**entry, "verification": {**verification, "by": "model"},
                      "evidence": [by_id[key] for key in ids], "matched_rules": matches})
    if seen != set(by_id):
        raise RatchetError("assessment must account for every selected finding, including keep decisions")
    if drafts:
        create_batch(config, events, drafts, dry_run=True)
    return {"v": 1, "context_id": context["context_id"], "cards": cards, "drafts": drafts,
            "note": "Model recommendations only. No decision or rule change has been applied."}


def run(args: argparse.Namespace, config: Config) -> int:
    events = read_events(config.ledger_path)
    locations = {}
    for value in args.location or []:
        try:
            key, span = value.rsplit("=", 1)
            start, end = (int(number) for number in span.split(":"))
        except ValueError as exc:
            raise RatchetError("location must be FINDING_ID=START:END") from exc
        if key in locations:
            raise RatchetError("duplicate finding location")
        locations[key] = (start, end)
    context = build_context(config, events, args.finding, locations)
    result = context
    if args.assessment:
        path = Path(args.assessment).absolute()
        source = _read_source(Path(path.anchor), str(path.relative_to(path.anchor)), None)
        if source["status"] != "available":
            raise RatchetError(f"cannot read assessment: {source['reason']}")
        try:
            payload = json.loads(source["text"])
        except json.JSONDecodeError as exc:
            raise RatchetError(f"cannot read assessment: {exc}") from exc
        result = assess(config, events, context, payload)
    print(encoded(result), end="")
    return 0


def add_parser(sub, common) -> None:
    parser = sub.add_parser("decision-context", parents=[common],
                            help="read-only structured findings and code sources; optionally validate model assessments")
    parser.add_argument("--finding", action="append", help="full finding ID; repeat to select a batch (default: all)")
    parser.add_argument("--location", action="append", help="FINDING_ID=START:END; inspected current range, at most 80 lines")
    parser.add_argument("--assessment", help="model assessment JSON to validate against the current context")
    parser.set_defaults(func=run)
