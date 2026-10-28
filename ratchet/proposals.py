"""Feedback -> reviewable drafts -> explicit decisions -> rules and compiled consumers.

Statistics never invent a rule or a threshold. A person or calling assistant supplies draft
text grounded in finding IDs. Only the subsequent, hash-bound decision payload authorizes writes.
"""
from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from pathlib import Path
import re

from .config import Config
from .digest import plan_outputs
from .errors import RatchetError
from .ledger import effective_rule_id, fold, now_iso
from .proposal_store import (
    encoded, ensure_idle, fingerprint, mutation_lock, read_json, read_local, replace_local, transact,
)
from .rules import load_rules, parse_rules_text
from .sources import DiskReader

STATE = ".ratchet/proposals"
CHANGELOG = "CODING-STYLE.changelog.md"
VERSION = f"{STATE}/version.json"
_BATCH_ID = re.compile(r"[a-f0-9]{64}")


class ProposedReader(DiskReader):
    def __init__(self, replacements: dict[Path, str]):
        self.replacements = replacements

    def read(self, path: Path) -> bytes | None:
        if path in self.replacements:
            return self.replacements[path].encode()
        return super().read(path)


def local_name(config: Config, path: Path) -> str:
    try:
        name = path.relative_to(config.root).as_posix()
    except ValueError as exc:
        raise RatchetError("proposal mutation requires repo-local rules and consumers; use a local rules copy") from exc
    read_local(config.root, name)
    return name


def statistics(config: Config, events: list[dict]) -> dict:
    state = fold(events)
    groups: dict[str, dict] = {}
    for key, item in state.items():
        rule = effective_rule_id(item.finding, config.legacy_unmapped_alias)
        group = groups.setdefault(rule, {"rule_id": rule, "total": 0, "outcomes": Counter(), "finding_ids": []})
        group["total"] += 1
        group["outcomes"][item.outcome["outcome"] if item.outcome else "unlabeled"] += 1
        group["finding_ids"].append(key)
    return {"v": 1, "groups": list(groups.values()), "total": len(state),
            "note": "Counts are signals, not quality scores. Unlabeled is not false-positive-free; no automatic thresholds."}


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RatchetError(f"{name} must be a non-empty string")
    return value


def _snapshot(config: Config) -> dict[str, str | None]:
    paths = [*config.rules_files, config.digest_path, *(t.path for t in config.inject_targets),
             config.root / CHANGELOG, config.root / VERSION]
    names = [local_name(config, p) for p in paths]
    if len(names) != len(set(names)):
        raise RatchetError("rules, digest, consumers, changelog and version paths must be distinct")
    if any(n.startswith(STATE + "/") for n in names[:-1]):
        raise RatchetError("rule and output paths cannot overlap proposal storage")
    return {name: read_local(config.root, name) for name in names}


def _configuration(config: Config) -> dict:
    return {"rules": [local_name(config, p) for p in config.rules_files],
            "digest": local_name(config, config.digest_path),
            "inject": [[local_name(config, t.path), list(t.sections)] for t in config.inject_targets],
            "pattern": config.clause_id_pattern, "chars": config.digest_max_chars, "bytes": config.digest_max_bytes,
            "tool_rule_map": config.tool_rule_map, "scenarios": config.scenarios,
            "legacy_unmapped_alias": sorted(config.legacy_unmapped_alias)}


def clause_block(text: str, rule_id: str, config: Config) -> str:
    """A defining line and its indented continuation; do not consume adjacent global prose."""
    clauses = parse_rules_text(text, "rules", config.clause_id_pattern)
    if rule_id not in clauses:
        raise RatchetError(f"clause {rule_id} is not defined in the selected file")
    lines = text.splitlines(keepends=True)
    start = clauses[rule_id].line - 1
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end]).rstrip("\r\n")


def _draft(config: Config, raw: dict, index: int, baseline: dict, state: dict, used: set) -> dict:
    fields = {"action", "rule_id", "file", "after", "rationale", "evidence_ids"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise RatchetError(f"draft {index} needs exactly {sorted(fields)}")
    action, rule_id = raw["action"], raw["rule_id"]
    if action not in ("add", "rewrite", "retire") or not isinstance(rule_id, str) or not re.fullmatch(config.clause_id_pattern, rule_id):
        raise RatchetError(f"draft {index}: invalid action or clause ID")
    name = _text(raw["file"], "file")
    if name not in [local_name(config, p) for p in config.rules_files]:
        raise RatchetError(f"draft {index}: target must be a configured rules file")
    evidence = raw["evidence_ids"]
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) and e in state for e in evidence):
        raise RatchetError(f"draft {index}: evidence_ids must reference existing findings")
    before = "" if action == "add" else clause_block(baseline[name], rule_id, config)
    if action == "add" and rule_id in used:
        raise RatchetError(f"clause ID {rule_id} has already been used; IDs cannot be reused")
    after = raw["after"]
    if not isinstance(after, str) or (action == "retire" and after != ""):
        raise RatchetError("retire needs an empty after; other actions need rule text")
    return {**raw, "after": after.rstrip("\r\n"), "before": before,
            "rationale": _text(raw["rationale"], "rationale"), "id": f"p{index}",
            "evidence_ids": sorted(set(evidence))}


def _used_ids(config: Config, baseline: dict) -> set[str]:
    used = set(load_rules(config).clauses)
    for name in [local_name(config, p) for p in config.rules_files]:
        used.update(re.findall(r"<!-- ratchet-retired: (\S+) -->", baseline[name]))
    if baseline[VERSION]:
        version = read_json(config.root, VERSION)
        used.update(version["used_ids"])
    return used


def _replace_clause(config: Config, text: str, proposal: dict, after: str) -> str:
    rule_id, action = proposal["rule_id"], proposal["action"]
    if action == "retire":
        after = f"<!-- ratchet-retired: {rule_id} -->"
    else:
        parsed = parse_rules_text(after, "proposed text", config.clause_id_pattern)
        if set(parsed) != {rule_id} or not parsed[rule_id].text:
            raise RatchetError(f"proposal {proposal['id']} must define exactly {rule_id} with nonempty text")
        if "<!-- ratchet-retired:" in after or "```" in after or "~~~" in after:
            raise RatchetError("proposal text cannot contain retirement markers or code fences")
    if action == "add":
        return text.rstrip() + "\n\n" + after + "\n"
    before = proposal["before"]
    if text.count(before) != 1 or clause_block(text, rule_id, config) != before:
        raise RatchetError(f"stale or ambiguous clause block: {rule_id}")
    return text.replace(before, after, 1)


def _outputs(config: Config, baseline: dict, proposals: list[dict]) -> dict[str, str]:
    changed = {name: baseline[name] for name in [local_name(config, p) for p in config.rules_files]}
    for proposal in proposals:
        changed[proposal["file"]] = _replace_clause(config, changed[proposal["file"]], proposal, proposal["after"])
    before = load_rules(config)
    reader = ProposedReader({config.root / name: text for name, text in changed.items()})
    after = load_rules(config, reader)
    if before.family_severity != after.family_severity:
        raise RatchetError("a clause proposal cannot change a family severity table; use an explicit clause tag")
    touched = {p["rule_id"] for p in proposals}
    old_untouched = {key: (c.text, c.tag, c.digest_override) for key, c in before.clauses.items() if key not in touched}
    new_untouched = {key: (c.text, c.tag, c.digest_override) for key, c in after.clauses.items() if key not in touched}
    if old_untouched != new_untouched:
        raise RatchetError("proposal changed an unrelated clause")
    outputs = plan_outputs(config, after, reader)
    if len(outputs[0].new.encode()) > config.digest_max_bytes:
        raise RatchetError("proposed digest exceeds digest_max_bytes; shorten rules before approving")
    changed.update({local_name(config, o.path): o.new for o in outputs})
    return {name: text for name, text in changed.items() if baseline[name] != text}


def create_batch(config: Config, events: list[dict], drafts: list, *, dry_run: bool = False,
                 native: dict | None = None) -> dict:
    if not config.rules_files or not isinstance(drafts, list) or (not drafts and native is None):
        raise RatchetError("distill --drafts requires a nonempty draft list and configured rules_files")
    ensure_idle(config.root)
    baseline, state = _snapshot(config), fold(events)
    used = _used_ids(config, baseline)
    proposals = [_draft(config, d, i, baseline, state, used) for i, d in enumerate(drafts, 1)]
    if len({p["rule_id"] for p in proposals}) != len(proposals):
        raise RatchetError("one proposal per clause per batch")
    if proposals:
        _outputs(config, baseline, proposals)
    evidence_ids = {e for p in proposals for e in p["evidence_ids"]}
    evidence = {key: {"finding": state[key].finding, "outcome": state[key].outcome} for key in sorted(evidence_ids)}
    batch = {"v": 1, "repo": str(config.root), "config": _configuration(config), "baseline": baseline,
             "proposals": proposals, "evidence": evidence, "statistics": statistics(config, events), "used_ids": sorted(used)}
    if native is not None:
        batch["native"] = native
    batch["id"] = fingerprint(batch)
    if not dry_run:
        from .proposal_page import build_page
        with mutation_lock(config.root):
            ensure_idle(config.root)
            replace_local(config.root, f"{STATE}/{batch['id']}/batch.json", encoded(batch))
            if native is None:
                replace_local(config.root, f"{STATE}/{batch['id']}/review.html", build_page(batch))
    return batch


def _load_batch(config: Config, batch_id: str) -> dict:
    if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id):
        raise RatchetError("batch ID must be a SHA-256 hex string")
    batch = read_json(config.root, f"{STATE}/{batch_id}/batch.json")
    unsigned = {k: v for k, v in batch.items() if k != "id"}
    if batch.get("id") != batch_id or fingerprint(unsigned) != batch_id or batch.get("repo") != str(config.root):
        raise RatchetError("proposal batch changed or belongs to another repo")
    return batch


def _decisions(batch: dict, payload: dict) -> tuple[list[dict], list[dict]]:
    fields = {"v", "batch_id", "confirmed_by", "decisions"} | ({"answers"} if "native" in batch else set())
    if not isinstance(payload, dict) or set(payload) != fields:
        raise RatchetError("decisions require v, batch_id, confirmed_by and decisions")
    if type(payload["v"]) is not int or payload["v"] != 1 or payload["batch_id"] != batch["id"]:
        raise RatchetError("decisions are for another proposal batch")
    _text(payload["confirmed_by"], "confirmed_by")
    if "native" in batch:
        from .native_decisions import validate_answers
        if payload["decisions"] != validate_answers(batch, payload):
            raise RatchetError("proposal decisions must match the human's whole-group answers")
    entries = payload["decisions"]
    if not isinstance(entries, list) or len(entries) != len(batch["proposals"]):
        raise RatchetError("every proposal needs an explicit decision")
    by_id = {p["id"]: p for p in batch["proposals"]}
    accepted, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"id", "decision", "after"}:
            raise RatchetError("invalid decision fields")
        key, choice = entry.get("id"), entry.get("decision")
        if not isinstance(key, str) or key not in by_id or key in seen or choice not in ("accept", "rewrite", "reject"):
            raise RatchetError("invalid, duplicate or missing proposal decision")
        seen.add(key)
        proposal = by_id[key]
        if choice == "rewrite":
            if proposal["action"] == "retire":
                raise RatchetError("reject and redraft a retirement instead of rewriting it")
            proposal = {**proposal, "after": _text(entry.get("after"), "rewritten rule")}
        elif "after" in entry:
            raise RatchetError("only a rewrite decision may supply new text")
        if choice != "reject":
            accepted.append(proposal)
    return accepted, entries


def apply_batch(config: Config, events: list[dict], payload: dict, *, dry_run: bool = False) -> dict:
    batch_id = payload.get("batch_id", "") if isinstance(payload, dict) else ""
    batch = _load_batch(config, batch_id)
    accepted, decisions = _decisions(batch, payload)
    receipt_name = f"{STATE}/{batch_id}/receipt.json"
    with nullcontext() if dry_run else mutation_lock(config.root):
        ensure_idle(config.root)
        existing = read_local(config.root, receipt_name)
        if existing is not None:
            receipt = read_json(config.root, receipt_name)
            if receipt["decision_hash"] != fingerprint(payload) or receipt["status"] != "applied":
                raise RatchetError("batch already decided or rolled back; create a new batch")
            if any(read_local(config.root, n) != pair[1] for n, pair in receipt["changes"].items()):
                raise RatchetError("batch was applied but outputs have since changed")
            return {"status": "already_applied", "batch_id": batch_id, "changed": []}
        if _configuration(config) != batch["config"] or _snapshot(config) != batch["baseline"]:
            raise RatchetError("rules, consumers or configuration changed; create a fresh proposal batch")
        state = fold(events)
        current = {key: {"finding": state[key].finding, "outcome": state[key].outcome}
                   for key in batch["evidence"] if key in state}
        if current != batch["evidence"]:
            raise RatchetError("proposal evidence changed; distill again")
        if "native" in batch:
            from .native_decisions import verify_context
            verify_context(config, events, batch)
        after = _outputs(config, batch["baseline"], accepted) if accepted else {}
        if accepted:
            prior = read_json(config.root, VERSION) if batch["baseline"][VERSION] else {"revision": 0}
            revision = prior["revision"] + 1
            after[VERSION] = encoded({"revision": revision, "batch_id": batch_id,
                                      "used_ids": sorted(set(batch["used_ids"]) | {p["rule_id"] for p in accepted})})
            log = batch["baseline"][CHANGELOG] or "# Coding style changes\n"
            lines = [f"- {p['action']} {p['rule_id']} ({p['id']})" for p in accepted]
            after[CHANGELOG] = log.rstrip() + f"\n\n## Revision {revision}\n\nBatch `{batch_id}`.\n\n" + "\n".join(lines) + "\n"
        changes = {n: (batch["baseline"][n], text) for n, text in after.items()}
        receipt = {"v": 1, "status": "applied", "batch_id": batch_id, "ts": now_iso(),
                   "confirmed_by": payload["confirmed_by"], "decision_hash": fingerprint(payload),
                   "decisions": decisions, "changes": changes}
        if "native" in batch:
            receipt["answers"] = payload["answers"]
            receipt["context_id"] = batch["native"]["context"]["context_id"]
        if not dry_run:
            transact(config.root, {**changes, receipt_name: (None, encoded(receipt))})
        return {"status": "preview" if dry_run else "applied", "batch_id": batch_id,
                "changed": list(changes), "accepted": len(accepted), "rejected": len(decisions) - len(accepted),
                "changes": changes}


def rollback_batch(config: Config, batch_id: str, *, dry_run: bool = False) -> list[str]:
    _load_batch(config, batch_id)
    receipt_name = f"{STATE}/{batch_id}/receipt.json"
    with nullcontext() if dry_run else mutation_lock(config.root):
        ensure_idle(config.root)
        receipt = read_json(config.root, receipt_name)
        if receipt["status"] != "applied":
            raise RatchetError("batch is not currently applied")
        changes = receipt["changes"]
        for name, (_, after) in changes.items():
            if read_local(config.root, name) != after:
                raise RatchetError(f"rollback would overwrite a later edit: {name}")
        if not dry_run:
            old = read_local(config.root, receipt_name)
            receipt = {**receipt, "status": "rolled_back", "rolled_back_at": now_iso()}
            reverse = {name: (after, before) for name, (before, after) in changes.items()}
            transact(config.root, {**reverse, receipt_name: (old, encoded(receipt))})
        return list(changes)
