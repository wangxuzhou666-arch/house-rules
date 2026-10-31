"""Gate findings: what a lint or style gate in a pre-commit hook writes to the ledger.

`house-rules gate record` turns {file, line, code, message} reports into `gate` findings, `house-rules gate pass` records that a
gate passed over some files. The finding id is a content hash that an existing gate may already be writing by hand, so
the hash recipe below is fixed: changing it would orphan every open gate finding in a ledger.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import HouseRulesError
from .fsutil import read_text
from .ledger import SEVERITIES, Event, select_new_events
from .rules import TOOL_ID_RE, Rules, category_of

HASH_LENGTH = 12
SNIPPET_BEFORE, SNIPPET_AFTER = 2, 2  # lines of context around the flagged line


@dataclass(frozen=True)
class GatePlan:
    built: list[Event]  # one finding per input item, in input order
    fresh: list[Event]  # the ones to append: not already open in the ledger


def finding_hash(file: str, rule_code: str, line_text: str) -> str:
    """First 12 hex of sha1(`file|rule_code|line text with all whitespace removed`).

    Whitespace is dropped so re-indenting the line, or moving it, keeps the id: a re-detection folds into one finding.
    """
    return hashlib.sha1(f"{file}|{rule_code}|{''.join(line_text.split())}".encode()).hexdigest()[:HASH_LENGTH]


def repo_relative(root: Path, file: str) -> str:
    """The path as the ledger spells it: relative to the worktree's top, `/`-separated, no `./` or `..` steps."""
    path = Path(os.path.normpath(file))
    if path.is_absolute():
        path = path.resolve()
        root = root.resolve()
        if not path.is_relative_to(root):
            raise HouseRulesError(f"gate file {file!r} is outside the repo {root}")
        path = path.relative_to(root)
    if path.parts[:1] == ("..",):
        raise HouseRulesError(f"gate file {file!r} is outside the repo {root}")
    return path.as_posix()


def rule_code_of(code: str) -> str:
    """What goes into the hash and the `rule_code` field: a tool code without its tool prefix (`ruff:F811` -> `F811`,
    as the tool prints it), a clause id as it is."""
    return code.split(":", 1)[1] if TOOL_ID_RE.match(code) else code


def _item_text(item: dict[str, Any], key: str, number: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HouseRulesError(f"gate item {number}: field {key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _item_line(item: dict[str, Any], number: int) -> int:
    value = item.get("line")
    if type(value) is not int or value < 0:
        raise HouseRulesError(f"gate item {number}: field 'line' must be an integer >= 0, got {value!r}")
    return value


def _file_lines(root: Path, rel: str, number: int) -> list[str]:
    text = read_text(root / rel)
    if text is None:
        raise HouseRulesError(f"gate item {number}: cannot read {rel} in {root} (the hash and the snippet come from its text)")
    return text.splitlines()


def gate_events(items: list[dict[str, Any]], *, config: Config, rules: Rules, sha: str, ts: str) -> list[Event]:
    """One `gate` finding per item {file, line, code, message[, severity]}.

    `code` is `<tool>:<code>` or a clause id. It is looked up in `tool_rule_map` first, so `ruff:F811` can be recorded as a
    clause; an id that is neither a known clause nor shaped like a tool id is refused. `line` 0 means "no position" (a
    syntax error without one) and is recorded as line 1.
    """
    events: list[Event] = []
    line_cache: dict[str, list[str]] = {}
    for number, item in enumerate(items, start=1):
        raw_code = _item_text(item, "code", number)
        rule_id, rule_id_source = rules.resolve_reported(raw_code)
        rule_code = rule_code_of(raw_code)
        rel = repo_relative(config.worktree, _item_text(item, "file", number))
        line = _item_line(item, number)
        if rel not in line_cache:
            line_cache[rel] = _file_lines(config.worktree, rel, number)
        lines = line_cache[rel]
        digest = finding_hash(rel, rule_code, lines[line - 1] if 0 < line <= len(lines) else "")
        summary = _item_text(item, "message", number)
        severity = item.get("severity", rules.severity_of(rule_id) or "should-fix")
        if severity not in SEVERITIES:
            raise HouseRulesError(f"gate item {number}: severity {severity!r} not in {list(SEVERITIES)}")
        finding: Event = {
            "v": 1, "kind": "finding", "id": f"{config.repo_slug}:gate:{digest}", "display_id": digest, "run_id": "gate",
            "repo": config.repo_slug, "source": "gate", "ts": ts, "sha": sha, "file": rel, "line": max(line, 1),
            "rule_code": rule_code, "rule_id": rule_id, "category": category_of(rule_id), "severity": severity,
            "summary": summary,
            "failure_scenario": rules.scenarios.get(rule_id) or f"The gate flagged {rule_id}: {summary}",
            "verdict": "CONFIRMED",
            "snippet": "\n".join(lines[max(line - 1 - SNIPPET_BEFORE, 0):line + SNIPPET_AFTER]),
        }
        if rule_id_source is not None:
            finding["rule_id_source"] = rule_id_source
        events.append(finding)
    return events


def plan_gate_findings(existing: list[Event], items: list[dict[str, Any]], *, config: Config, rules: Rules, sha: str,
                       ts: str) -> GatePlan:
    """The findings for `items`, and which of them the ledger does not already hold open."""
    built = gate_events(items, config=config, rules=rules, sha=sha, ts=ts)
    return GatePlan(built, select_new_events(existing, built))


def gate_pass_event(config: Config, files: list[str], *, sha: str, ts: str) -> Event:
    """A gate passed over `files`: every open gate finding in one of them is closed by it."""
    paths = sorted({repo_relative(config.worktree, file) for file in files})
    return {"v": 1, "kind": "gate_pass", "repo": config.repo_slug, "ts": ts, "sha": sha, "files": paths}
