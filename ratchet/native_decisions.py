"""Platform-native questions over an immutable assessment and proposal batch.

The host agent asks the human. This module persists the material and records the
answer; no recommendation or default choice is treated as consent.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from .config import Config
from .decision_context import assess, build_context
from .errors import RatchetError
from .ledger import read_events
from .proposal_store import encoded, fingerprint, read_json, read_local
from .proposals import STATE, _BATCH_ID, _load_batch, apply_batch, create_batch
from .source_evidence import read_source


def read_payload(name: str) -> dict:
    path = Path(name).absolute()
    source = read_source(Path(path.anchor), str(path.relative_to(path.anchor)), None)
    if source["status"] != "available":
        raise RatchetError(f"cannot read decision input: {source['reason']}")
    try:
        return json.loads(source["text"])
    except json.JSONDecodeError as exc:
        raise RatchetError(f"invalid decision input: {exc}") from exc


def locations_from(values: list[str] | None) -> dict[str, tuple[int, int]]:
    locations = {}
    for value in values or []:
        try:
            key, span = value.rsplit("=", 1)
            start, end = (int(number) for number in span.split(":"))
        except ValueError as exc:
            raise RatchetError("location must be FINDING_ID=START:END") from exc
        if key in locations:
            raise RatchetError("duplicate finding location")
        locations[key] = (start, end)
    return locations


def prepare(config: Config, events: list[dict], payload: dict, ids=None, locations=None,
            *, dry_run: bool = False, parent: str | None = None) -> dict:
    if parent is not None:
        _load_batch(config, parent)  # A revision points to a real prior batch, not a chat nickname.
    context = build_context(config, events, ids, locations)
    result = assess(config, events, context, payload)
    groups, offset = [], 0
    for number, card in enumerate(result["cards"], 1):
        proposals = [f"p{i}" for i in range(offset + 1, offset + len(card["changes"]) + 1)]
        offset += len(proposals)
        groups.append({"id": f"a{number}", "proposal_ids": proposals, "assessment": card})
    native = {"context": context, "groups": groups, "parent_batch": parent,
              "locations": {key: list(span) for key, span in (locations or {}).items()},
              "nonce": uuid4().hex}  # Reconsidering a deferred/rejected proposal is a new decision.
    return create_batch(config, events, result["drafts"], dry_run=dry_run, native=native)


def validate_answers(batch: dict, payload: dict) -> list[dict]:
    """One answer per analysis group; a merge cannot be approved only in part."""
    answers = payload.get("answers")
    groups = batch["native"]["groups"]
    by_id = {group["id"]: group for group in groups}
    if not isinstance(answers, list) or len(answers) != len(groups):
        raise RatchetError("every assessment needs an explicit human answer (accept/revise/reject/defer)")
    seen, decisions = set(), []
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) != {"id", "decision", "response"}:
            raise RatchetError("answer needs id, decision and the human's response")
        key, choice, response = answer["id"], answer["decision"], answer["response"]
        if not isinstance(key, str) or key not in by_id or key in seen or choice not in ("accept", "revise", "reject", "defer"):
            raise RatchetError("invalid or duplicate native decision")
        if not isinstance(response, str) or not response.strip():
            raise RatchetError("record the human's actual response; an empty/default selection is not consent")
        seen.add(key)
        decisions.extend({"id": key, "decision": "accept" if choice == "accept" else "reject"}
                         for key in by_id[key]["proposal_ids"])
    return decisions


def verify_context(config: Config, events: list[dict], batch: dict) -> None:
    saved = batch["native"]["context"]
    ids = [item["finding"]["id"] for item in saved["findings"]]
    locations = batch["native"].get("locations")
    if locations is None:  # Earlier batches only retained readable selected ranges.
        locations = {item["finding"]["id"]: (item["source"]["line_start"], item["source"]["line_end"])
                     for item in saved["findings"] if item["source"]["location_basis"] == "selected_current_range"
                     and item["source"]["status"] == "available"}
    if build_context(config, events, ids, locations) != saved:
        raise RatchetError("decision evidence or source code changed; prepare and confirm a fresh batch")


def apply_answers(config: Config, events: list[dict], payload: dict, *, dry_run: bool = False) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"v", "batch_id", "confirmed_by", "answers"}:
        raise RatchetError("native decision input needs v, batch_id, confirmed_by and answers")
    batch = _load_batch(config, payload["batch_id"])
    if "native" not in batch:
        raise RatchetError("use apply-proposals for a legacy batch")
    decisions = validate_answers(batch, payload)
    result = apply_batch(config, events, {**payload, "decisions": decisions}, dry_run=dry_run)
    result["answer_counts"] = dict(Counter(answer["decision"] for answer in payload["answers"]))
    return result


def summary(config: Config) -> dict:
    counts = {phase: {kind: Counter() for kind in ("change", "no_change")} for phase in ("initial", "revision")}
    total, rolled_back = 0, 0
    folder = config.root / STATE
    for path in sorted(folder.iterdir()) if folder.exists() else []:
        if not _BATCH_ID.fullmatch(path.name):
            continue
        batch = _load_batch(config, path.name)
        if "native" not in batch:
            continue
        total += 1
        phase = "revision" if batch["native"]["parent_batch"] else "initial"
        receipt_name = f"{STATE}/{batch['id']}/receipt.json"
        answers = {}
        if read_local(config.root, receipt_name) is not None:
            receipt = read_json(config.root, receipt_name)
            payload = {k: receipt[k] for k in ("v", "batch_id", "confirmed_by", "answers", "decisions")}
            if receipt["decision_hash"] != fingerprint(payload) or validate_answers(batch, payload) != payload["decisions"]:
                raise RatchetError("decision receipt changed; cannot summarize it")
            if receipt["status"] not in ("applied", "rolled_back"):
                raise RatchetError("unknown decision receipt status")
            rolled_back += receipt["status"] == "rolled_back"
            answers = {answer["id"]: answer["decision"] for answer in receipt["answers"]}
        for group in batch["native"]["groups"]:
            kind = "change" if group["proposal_ids"] else "no_change"
            counts[phase][kind][answers.get(group["id"], "pending")] += 1
    return {"v": 1, "batches": total, "rolled_back_batches": rolled_back, "groups": counts,
            "note": "Author-specific decision counts, not accuracy. Initial and revised proposals are separate; findings are not independent votes."}


def run_prepare(args, config: Config) -> int:
    batch = prepare(config, read_events(config.ledger_path), read_payload(args.assessment), args.finding,
                    locations_from(args.location), dry_run=args.dry_run, parent=args.parent_batch)
    print(encoded({"batch_id": batch["id"], "status": "preview" if args.dry_run else "prepared",
                   "path": None if args.dry_run else str(config.root / STATE / batch["id"] / "batch.json"),
                   "groups": batch["native"]["groups"], "proposals": batch["proposals"]}), end="")
    return 0


def run_apply(args, config: Config) -> int:
    result = apply_answers(config, read_events(config.ledger_path), read_payload(args.input), dry_run=args.dry_run)
    print(encoded({key: value for key, value in result.items() if key != "changes"}), end="")
    return 0


def run_summary(args, config: Config) -> int:
    print(encoded(summary(config)), end="")
    return 0


def add_parsers(sub, common, writes):
    prepare_cmd = sub.add_parser("prepare-decisions", parents=[common, writes], help="save evidence and assessments for native questions")
    prepare_cmd.add_argument("--assessment", required=True)
    prepare_cmd.add_argument("--finding", action="append")
    prepare_cmd.add_argument("--location", action="append")
    prepare_cmd.add_argument("--parent-batch", help="prior batch being revised or reconsidered")
    prepare_cmd.set_defaults(func=run_prepare)
    apply_cmd = sub.add_parser("apply-decisions", parents=[common, writes], help="record native human answers and apply accepted groups")
    apply_cmd.add_argument("--input", required=True)
    apply_cmd.set_defaults(func=run_apply)
    summary_cmd = sub.add_parser("decision-summary", parents=[common], help="count actual author decisions without scoring accuracy")
    summary_cmd.set_defaults(func=run_summary)
