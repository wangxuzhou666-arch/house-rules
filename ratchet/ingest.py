"""Turn external finding lists into ledger events: `add` (explicit report shape) and `ingest-code-review`."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .errors import RatchetError
from .ledger import SEVERITIES, STATUS_TO_OUTCOME, UNMAPPED, Event
from .rules import Rules, category_of

_FENCE_RE = re.compile(r"```json[^\S\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# What `code_review_events` asks of a caller that wants snippets: for a file and a line, the code around it (None: there
# is none to show) and where it was read from (one of ledger.SNIPPET_SOURCES). Only `ratchet review` supplies one.
Locator = Callable[[str, int], tuple[str | None, str]]


def parse_json_items(text: str) -> list[dict[str, Any]]:
    """A JSON array of objects, or {"findings": [...]}."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"input is not valid JSON: {exc}") from exc
    return _items_from(data)


def _items_from(data: Any) -> list[dict[str, Any]]:
    items = data["findings"] if isinstance(data, dict) and "findings" in data else data
    if not isinstance(items, list):
        raise RatchetError("expected a JSON array of findings (or an object with a 'findings' array)")
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RatchetError(f"item {number}: expected a JSON object, got {type(item).__name__}")
    return items


def extract_review_items(text: str) -> list[dict[str, Any]]:
    """The raw review text, or just its JSON: the whole input if it parses, else the first ```json fenced array."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        whole_problem = str(exc)
    else:
        return _items_from(data)
    parse_errors = []
    for match in _FENCE_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue
        if isinstance(data, list):
            return _items_from(data)
    detail = f"; fenced blocks that failed to parse: {parse_errors}" if parse_errors else ""
    raise RatchetError(f"input is neither a JSON array nor text containing a ```json fenced array "
                       f"(whole-input parse: {whole_problem}){detail}")


# One string field on its own line, as a pretty-printed review prints them. The value runs to the last quote on the line.
_STRING_FIELD_RE = re.compile(r'^(?P<head>\s*"(?:file|summary|failure_scenario|category|severity)"\s*:\s*")(?P<value>.*?)(?P<tail>"\s*,?\s*)$')


def _escape_stray_quotes(value: str) -> tuple[str, int]:
    """`value` with every double quote that is not already escaped escaped, and how many that was."""
    out: list[str] = []
    fixed = 0
    escaped = False
    for char in value:
        if char == '"' and not escaped:
            out.append("\\")
            fixed += 1
        out.append(char)
        escaped = char == "\\" and not escaped
    return "".join(out), fixed


def escape_stray_quotes(text: str) -> tuple[str, int]:
    """Models quote code in their findings (`config["key"]`) and sometimes forget to escape the quotes, which makes the
    whole array invalid JSON. Escape them inside the one-line string fields of the known schema; touch nothing else."""
    lines: list[str] = []
    total = 0
    for line in text.splitlines(keepends=True):
        match = _STRING_FIELD_RE.match(line.rstrip("\r\n"))
        if match is None:
            lines.append(line)
            continue
        value, fixed = _escape_stray_quotes(match.group("value"))
        total += fixed
        lines.append(match.group("head") + value + match.group("tail") + line[len(line.rstrip("\r\n")):])
    return "".join(lines), total


def extract_review_items_lenient(text: str) -> tuple[list[dict[str, Any]], int]:
    """(items, number of stray quotes escaped). The strict reading is tried first and decides every input that already
    parses; only a failure is retried on the repaired text, and if that still fails the original error is the one raised."""
    try:
        return extract_review_items(text), 0
    except RatchetError as strict_error:
        repaired, fixed = escape_stray_quotes(text)
        if fixed == 0:
            raise
        try:
            return extract_review_items(repaired), fixed
        except RatchetError:
            raise strict_error from None


def _text_field(item: dict[str, Any], key: str, number: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RatchetError(f"item {number}: field {key!r} must be a non-empty string, got {value!r}")
    return value


def _line_field(item: dict[str, Any], number: int) -> int:
    value = item.get("line")
    if type(value) is not int or value < 1:
        raise RatchetError(f"item {number}: field 'line' must be an integer >= 1, got {value!r}")
    return value


def _scenario(item: dict[str, Any], rule_id: str, rules: Rules, number: int) -> str:
    """The item's own failure scenario, else the configured default text for its rule."""
    value = item.get("failure_scenario")
    if isinstance(value, str) and value.strip():
        return value
    default = rules.scenarios.get(rule_id)
    if default is None:
        raise RatchetError(f"item {number}: missing 'failure_scenario' and no default in config scenarios for {rule_id!r}")
    return default


# ---- add: the explicit report shape ---------------------------------------------------------------------------

def report_events(items: list[dict[str, Any]], *, config: Config, rules: Rules, source: str, run_id: str,
                  sha: str, ts: str) -> list[Event]:
    """Report items -> finding events (plus a human outcome for items carrying a `status`).

    Required per item: id, file, line, summary, verdict, rule_id (failure_scenario unless the config has a default
    for the rule). Optional: severity, snippet, status, discovered_at.
    """
    events: list[Event] = []
    for number, item in enumerate(items, start=1):
        raw_rule_id = _text_field(item, "rule_id", number)
        rule_id, rule_id_source = rules.resolve_reported(raw_rule_id)
        severity = item.get("severity", rules.severity_of(rule_id) or "should-fix")
        if severity not in SEVERITIES:
            raise RatchetError(f"item {number}: severity {severity!r} not in {list(SEVERITIES)}")
        finding_id = f"{config.repo_slug}:{source}:{run_id}:{_text_field(item, 'id', number)}"
        finding: Event = {
            "v": 1, "kind": "finding", "id": finding_id, "display_id": item["id"], "run_id": run_id,
            "repo": config.repo_slug, "source": source, "ts": ts, "sha": sha,
            "file": _text_field(item, "file", number), "line": _line_field(item, number),
            "rule_code": raw_rule_id, "rule_id": rule_id, "category": category_of(rule_id), "severity": severity,
            "summary": _text_field(item, "summary", number),
            "failure_scenario": _scenario(item, rule_id, rules, number),
            "verdict": _text_field(item, "verdict", number),
        }
        if rule_id_source is not None:
            finding["rule_id_source"] = rule_id_source
        if item.get("snippet"):
            finding["snippet"] = item["snippet"]
        if item.get("discovered_at"):
            finding["discovered_at"] = item["discovered_at"]
        events.append(finding)
        status = item.get("status")
        if status is not None:
            if status not in STATUS_TO_OUTCOME:
                raise RatchetError(f"item {number}: status {status!r} not in {sorted(STATUS_TO_OUTCOME)}")
            events.append({"v": 1, "kind": "outcome", "id": finding_id, "outcome": STATUS_TO_OUTCOME[status],
                           "outcome_by": "human", "evidence": f"report status={status}", "ts": ts})
    return events


# ---- ingest-code-review ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Candidate:
    order: int
    file: str
    line: int
    summary: str
    failure_scenario: str
    rule_code: str
    rule_id: str
    rule_id_source: str  # "model" or "attributed"
    severity: str
    severity_defaulted: bool


@dataclass(frozen=True)
class IngestResult:
    events: list[Event]
    merged_count: int  # reviewer items folded into another finding's also_matches
    unmapped_count: int
    attributed_count: int  # no usable category; the clause was read out of the summary or failure scenario
    severity_filled_count: int  # reviewer gave no usable severity; taken from the rules file or defaulted


def _label(item: dict[str, Any], summary: str, rules: Rules) -> tuple[str, str, str]:
    """(rule_id, rule_code, rule_id_source) for one review item.

    A `category` the model gave is its answer, right or wrong: a known clause id in kebab-case, else `unmapped`, and
    `rule_code` keeps what it wrote. The exception is a Reviewer rule: no finding violates one, so naming it is no
    answer. Without an answer (no category, or such a clause) the first clause that is not a Reviewer rule mentioned in
    the summary, then in the failure scenario, is taken instead (`attributed`); no mention at all is `unmapped`.
    """
    category = item.get("category")
    category_text = category.strip() if isinstance(category, str) else ""
    claimed = rules.from_kebab(category_text) if category_text else None
    names_reviewer_rule = claimed is not None and rules.is_reviewer_rule(claimed)
    if category_text and not names_reviewer_rule:
        return claimed or UNMAPPED, category_text, "model"
    scenario = item.get("failure_scenario")
    mention = rules.first_clause_mention(summary) or rules.first_clause_mention(scenario if isinstance(scenario, str) else "")
    if mention is None:
        return UNMAPPED, category_text or UNMAPPED, "model"
    return mention[0], mention[1], "attributed"


def _candidate(number: int, item: dict[str, Any], rules: Rules) -> _Candidate:
    """Normalize one review item. `category` and `severity` are optional extras the model may get wrong."""
    summary = _text_field(item, "summary", number)
    rule_id, rule_code, rule_id_source = _label(item, summary, rules)
    claimed_severity = item.get("severity")
    severity_defaulted = claimed_severity not in SEVERITIES
    severity = (rules.severity_of(rule_id) or "should-fix") if severity_defaulted else claimed_severity
    return _Candidate(
        order=number,
        file=_text_field(item, "file", number).strip(),
        line=_line_field(item, number),
        summary=summary,
        failure_scenario=_scenario(item, rule_id, rules, number),
        rule_code=rule_code,
        rule_id=rule_id,
        rule_id_source=rule_id_source,
        severity=severity,
        severity_defaulted=severity_defaulted,
    )


def code_review_events(items: list[dict[str, Any]], *, config: Config, rules: Rules, source: str, run_id: str,
                       verdict: str, sha: str, ts: str, discovered_at: str | None,
                       locate: Locator | None = None) -> IngestResult:
    """Findings on the same file+line collapse into one: the most severe is primary, the rest go to also_matches.

    display_ids are `<run_id>-1`, `-2`, ... in input order after collapsing, so re-ingesting the same review is a no-op.
    With `locate`, each finding gets the code around its line as `snippet` and the place it came from as `snippet_source`;
    when there is no code to show, `snippet_source` is still recorded (as "unavailable") and `snippet` is left out.
    """
    candidates = [_candidate(number, item, rules) for number, item in enumerate(items, start=1)]
    groups: dict[tuple[str, int], list[_Candidate]] = {}
    for candidate in candidates:
        groups.setdefault((candidate.file, candidate.line), []).append(candidate)

    events: list[Event] = []
    for position, group in enumerate(groups.values(), start=1):
        primary = min(group, key=lambda c: (SEVERITIES.index(c.severity), c.order))
        display_id = f"{run_id}-{position}"
        finding: Event = {
            "v": 1, "kind": "finding", "id": f"{config.repo_slug}:{source}:{run_id}:{display_id}",
            "display_id": display_id, "run_id": run_id, "repo": config.repo_slug, "source": source, "ts": ts,
            "sha": sha, "file": primary.file, "line": primary.line, "rule_code": primary.rule_code,
            "rule_id": primary.rule_id, "category": category_of(primary.rule_id), "severity": primary.severity,
            "summary": primary.summary, "failure_scenario": primary.failure_scenario, "verdict": verdict,
            "rule_id_source": primary.rule_id_source,
        }
        if discovered_at is not None:
            finding["discovered_at"] = discovered_at
        if locate is not None:
            snippet, snippet_source = locate(primary.file, primary.line)
            if snippet is not None:
                finding["snippet"] = snippet
            finding["snippet_source"] = snippet_source
        others = [c for c in group if c is not primary]
        if others:
            finding["also_matches"] = [
                {"rule_id": c.rule_id, "severity": c.severity, "summary": c.summary, "failure_scenario": c.failure_scenario}
                for c in others]
        events.append(finding)

    return IngestResult(
        events=events,
        merged_count=len(candidates) - len(groups),
        unmapped_count=sum(1 for c in candidates if c.rule_id == UNMAPPED),
        attributed_count=sum(1 for c in candidates if c.rule_id_source == "attributed"),
        severity_filled_count=sum(1 for c in candidates if c.severity_defaulted),
    )
