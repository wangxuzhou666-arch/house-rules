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
from .rules import Clause, family_of, load_rules, parse_rules_text
from .sources import DiskReader

STATE = ".ratchet/proposals"
CHANGELOG = "CODING-STYLE.changelog.md"
CHANGELOG_TITLE = "# Coding style changes\n"
VERSION = f"{STATE}/version.json"
SNIPPET_MAX_LINES = 8
_BATCH_ID = re.compile(r"[a-f0-9]{64}")
_REVISION_HEADING = re.compile(r"^## Revision (\d+)\b", re.MULTILINE)
# The shapes of a clause's defining line that decide where a new clause goes and how it is joined to the one before it.
_ITEM_MARKER = re.compile(r"[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_ORDERED_ITEM = re.compile(r"[ \t]*(\d+)([.)])([ \t]+)")
_TABLE_ROW = re.compile(r"[ \t]*\|")
_HEADING = re.compile(r" {0,3}#{1,6}[ \t]")
_FENCE = re.compile(r"[ \t]*(```|~~~)")
# The legacy page offers accept / rewrite / reject. The native `revise` is never applied, so an applied "revise" is a legacy rewrite:
# its After text is the human's own.
_LEGACY_CHOICE = {"accept": "accept", "rewrite": "revise", "reject": "reject"}


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


# ---- where a new clause goes -------------------------------------------------------------------------------------------

def _block_end(lines: list[str], start: int, clause_lines: set[int]) -> int:
    """The index after the last line of the clause block that starts at `lines[start]`, the blank lines behind it left out.

    A list item or a table row owns the indented lines under it, blank lines between them included (what `clause_block`
    returns). A heading owns everything under it up to the next heading, since its body is plain paragraphs. Neither runs
    into the next clause, and a fence inside a heading's body hides the `#` comment lines in it from the heading test.
    """
    owns_body = _HEADING.match(lines[start]) is not None
    end, in_fence = start + 1, False
    while end < len(lines) and end not in clause_lines:
        in_fence ^= _FENCE.match(lines[end]) is not None  # each fence line opens or closes one
        if owns_body and not in_fence and _HEADING.match(lines[end]):
            break
        if not owns_body and lines[end].strip() and not lines[end].startswith((" ", "\t")):
            break
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _numbered_like(first_line: str, anchor_line: str) -> str:
    """The defining line of a new clause, numbered to continue the ordered list `anchor_line` is an item of. As written when
    the anchor is not an ordered item, or the line is a table row or a heading, which no number goes in front of.
    """
    ordered = _ORDERED_ITEM.match(anchor_line)
    if ordered is None or _TABLE_ROW.match(first_line) or _HEADING.match(first_line):
        return first_line
    number, delimiter, gap = ordered.groups()
    own_marker = _ITEM_MARKER.match(first_line)  # the draft may carry any number, a bullet, or none
    body = first_line[own_marker.end():] if own_marker else first_line.lstrip()
    return f"{int(number) + 1}{delimiter}{gap}{body}"


def _joins_the_list(lines: list[str], mates: list[Clause], first_line: str, clause_lines: set[int]) -> bool:
    """Whether the new clause follows the last of its family with no blank line between, as the family is written.

    Table rows always do: a blank line would cut the table. Two list items follow the spacing of the family's last two
    clauses (touching or not); a family of one clause is spaced by the shape of that clause: a one-line item is followed
    directly, an item with continuation lines is set apart. Any other pair is set apart.
    """
    anchor = lines[mates[-1].line - 1]
    if _TABLE_ROW.match(anchor) and _TABLE_ROW.match(first_line):
        return True
    if not (_ITEM_MARKER.match(anchor) and _ITEM_MARKER.match(first_line)):
        return False
    anchor_index = mates[-1].line - 1
    if len(mates) > 1:
        return _block_end(lines, mates[-2].line - 1, clause_lines) == anchor_index
    return _block_end(lines, anchor_index, clause_lines) == anchor_index + 1


def _add_clause(config: Config, text: str, rule_id: str, after: str, defined_on_line: int) -> str:
    """`text` with the new clause `after` placed behind the last clause block of its family (`P2.6` joins `P2`), so it sits
    among its own kind and the digest, which follows the file's order, lists it there. A family with no clause in this file
    gets the end of the file.

    `defined_on_line` is the line of `after` that defines the clause. When it is the first and the family is an ordered
    list, the clause takes the next number of that list, whatever number the draft carries (see `_numbered_like`). The
    clause is joined the way the family's clauses are (see `_joins_the_list`), in the line ending the family's last clause
    has. Nothing else in the file moves, so a rollback that restores the old bytes restores exactly this.
    """
    clauses = parse_rules_text(text, "rules", config.clause_id_pattern)
    mates = [clause for clause in clauses.values() if family_of(clause.id) == family_of(rule_id)]
    if not mates:
        return text.rstrip() + "\n\n" + after + "\n"
    lines = text.splitlines(keepends=True)
    clause_lines = {clause.line - 1 for clause in clauses.values()}
    anchor = lines[mates[-1].line - 1]
    written = after.splitlines()
    first_line = _numbered_like(written[0], anchor) if defined_on_line == 1 else written[0]
    new_lines = [first_line, *written[1:]]
    newline = "\r\n" if anchor.endswith("\r\n") else "\n"
    end = _block_end(lines, mates[-1].line - 1, clause_lines)
    head = "".join(lines[:end])
    if not head.endswith(("\n", "\r")):  # the family's last clause ends a file that has no final newline
        head += newline
    separator = "" if _joins_the_list(lines, mates, new_lines[0], clause_lines) else newline
    return head + separator + "".join(line + newline for line in new_lines) + "".join(lines[end:])


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
        return _add_clause(config, text, rule_id, after, parsed[rule_id].line)
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


# ---- the readable rule-change record ---------------------------------------------------------------------------------

def _one_line(text: str) -> str:
    """Model and human text goes onto one bullet line, so a newline in it cannot start a heading or a list item."""
    return " ".join(text.split())


def _fenced(text: str, language: str = "") -> list[str]:
    """`text` as a fenced block under its bullet. The fence is longer than any backtick run inside, so no line can close it early."""
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return [f"  {fence}{language}", *(f"  {line}" if line else "" for line in text.splitlines()), f"  {fence}"]


def _clause_lines(label: str, clause: str, absent: str) -> list[str]:
    return [f"- {label}: {absent}"] if not clause else [f"- {label}:", *_fenced(clause, "markdown")]


def _snippet_lines(snippet: str) -> list[str]:
    if not snippet.strip():
        return []
    shown = snippet.strip("\r\n").splitlines()
    return _fenced("\n".join([*shown[:SNIPPET_MAX_LINES], *(["..."] if len(shown) > SNIPPET_MAX_LINES else [])]))


def _evidence_lines(finding: dict) -> list[str]:
    head = f"- Evidence: `{finding['id']}` — `{finding['file']}:{finding['line']}` — {_one_line(finding['summary'])}"
    # `snippet` is optional: only a finding recorded with code context has one.
    snippet = _snippet_lines(finding.get("snippet", ""))
    # A `worktree` snippet was read when the review was imported, not when it ran; the reviewer may have seen other code.
    note = (["  Snippet from the working tree when the review was imported; it may differ from the code the reviewer saw:"]
            if snippet and finding.get("snippet_source") == "worktree" else [])
    return [head, *note, *snippet]


def _verdict(choice: str, words: str | None, confirmed_by: str) -> str:
    text = f"{choice} by {_one_line(confirmed_by)}"
    return f'{text} — "{_one_line(words)}"' if words else text


def _human_choices(batch: dict, payload: dict) -> dict[str, tuple[str, str | None]]:
    """proposal id -> (the human's choice, the human's own words; None where the legacy flow records no words)."""
    if "native" not in batch:
        return {entry["id"]: (_LEGACY_CHOICE[entry["decision"]], None) for entry in payload["decisions"]}
    groups = {group["id"]: group["proposal_ids"] for group in batch["native"]["groups"]}
    return {proposal_id: (answer["decision"], answer["response"])
            for answer in payload["answers"] for proposal_id in groups[answer["id"]]}


def _applied_lines(proposal: dict, evidence: dict, verdict: str) -> list[str]:
    lines = [f"### {proposal['action']} {proposal['rule_id']}", ""]
    lines += _clause_lines("Before", proposal["before"], "(new clause)")
    lines += _clause_lines("After", proposal["after"], "(retired)")
    lines.append(f"- Why: {_one_line(proposal['rationale'])}")
    for finding_id in proposal["evidence_ids"]:
        lines += _evidence_lines(evidence[finding_id]["finding"])
    return [*lines, f"- Decision: {verdict}", ""]


def _changelog_entry(batch: dict, payload: dict, accepted: list[dict], revision: int, day: str) -> str:
    """One applied batch as readable Markdown: what changed, why, on what evidence, and what the human decided.

    Everything comes from the saved batch and the decision payload; nothing is looked up or invented here.
    """
    choices = _human_choices(batch, payload)
    applied = {proposal["id"]: proposal for proposal in accepted}
    lines = [f"## Revision {revision} — {day} (batch {batch['id']})", ""]
    declined = []
    for proposal in batch["proposals"]:
        choice, words = choices[proposal["id"]]
        verdict = _verdict(choice, words, payload["confirmed_by"])
        if proposal["id"] in applied:
            lines += _applied_lines(applied[proposal["id"]], batch["evidence"], verdict)
        else:
            declined.append(f"- {proposal['action']} {proposal['rule_id']} — {verdict}")
    not_applied = ["### Not applied", "", *declined, ""] if declined else []
    return "\n".join([*lines, *not_applied])


def _next_revision(local_revision: int, changelog: str) -> int:
    """version.json is local state and the changelog is committed, so a fresh clone must not reuse a number the changelog shows."""
    shown = [int(number) for number in _REVISION_HEADING.findall(changelog)]
    return max([local_revision, *shown]) + 1


def _revision_files(config: Config, batch: dict, payload: dict, accepted: list[dict], applied_at: str) -> dict[str, str]:
    """version.json and the changelog with this batch's entry appended; written in the same transaction as the rules."""
    prior = read_json(config.root, VERSION) if batch["baseline"][VERSION] else {"revision": 0}
    log = batch["baseline"][CHANGELOG] or CHANGELOG_TITLE
    revision = _next_revision(prior["revision"], log)
    entry = _changelog_entry(batch, payload, accepted, revision, applied_at.partition("T")[0])
    used_ids = sorted(set(batch["used_ids"]) | {p["rule_id"] for p in accepted})
    return {VERSION: encoded({"revision": revision, "batch_id": batch["id"], "used_ids": used_ids}),
            CHANGELOG: log.rstrip() + "\n\n" + entry}


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
        applied_at = now_iso()
        after = _outputs(config, batch["baseline"], accepted) if accepted else {}
        if accepted:
            after.update(_revision_files(config, batch, payload, accepted, applied_at))
        changes = {n: (batch["baseline"][n], text) for n, text in after.items()}
        receipt = {"v": 1, "status": "applied", "batch_id": batch_id, "ts": applied_at,
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
