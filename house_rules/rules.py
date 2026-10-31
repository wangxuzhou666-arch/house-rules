"""Clause registry parsed from the rules files named in the config.

A clause is *defined* by a line that starts with its id in one of two notations:
  - bracketed:   `1. **[P0E.1]** text`, `- [P3.2] text`, `### [R.7] title`
  - table row:   `| P0E.1 | blocker | text |`
An id mentioned mid-line (a cross reference) does not define anything. An optional severity tag on the defining
line - `(blocker)`, `[should-fix]`, `` `nice` ``, a table cell, or `severity: blocker` - becomes that clause's own
severity. Lines inside code fences are ignored.

A family severity table maps whole clause families to a severity; it is the default for clauses without their own tag:
  | family | severity |
  |---|---|
  | P0E, P0F | blocker |
A family is the part of a clause id before the dot (`P0E.1` -> `P0E`) and matches exactly.

Rules files are layered in config order (global first, project last). A later file may add clauses and table rows
but never redefine either: that is an error.

A layer outside the repo is shared with other repos: it is read here and never written. One that is not on this machine
refuses (SharedLayerMissing), because a rule set without it is incomplete; `skip_missing` is for a check that judges nothing.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Iterator

from .config import Config, is_outer, rules_name
from .errors import HouseRulesError, SharedLayerMissing
from .ledger import SEVERITIES, UNMAPPED
from .sources import DiskReader, FileReader

# Checked with fullmatch: `$` alone also holds before a final newline.
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_.@/-]+$")

_SEVERITY_WORDS = "blocker|should-fix|nice"
_SEVERITY_TAG_RE = re.compile(
    rf"[(\[`]\s*({_SEVERITY_WORDS})\s*[)\]`]"
    rf"|\|\s*({_SEVERITY_WORDS})\s*(?=\|)"
    rf"|\bseverity\s*[:=]\s*({_SEVERITY_WORDS})\b",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_COMMENT_RE = re.compile(r"<!--.*?-->")
_DIGEST_OVERRIDE_RE = re.compile(r"<!--\s*digest:\s*(\S.*?)\s*-->")
_BOLD_CLOSE_RE = re.compile(r"^(?:\*\*|__)")
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
_SEVERITY_TABLE_HEADER = ["family", "severity"]


@dataclass(frozen=True)
class Clause:
    id: str
    tag: str | None  # severity written on the defining line, if any
    line: int
    text: str  # what follows the id on the defining line, minus comments and severity tags
    digest_override: str | None  # from an inline `<!-- digest: ... -->` comment


@dataclass(frozen=True)
class RulesSource:
    path: Path
    sha12: str  # first 12 hex of the sha256 of the file bytes
    text: str


def family_of(clause_id: str) -> str:
    return clause_id.split(".", 1)[0]


def _definition_regexes(id_pattern: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    bracketed = re.compile(rf"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{{1,6}}\s+)?(?:\*\*|__)?\[(?P<id>(?:{id_pattern}))\]")
    table_row = re.compile(rf"^\s*\|\s*(?:\*\*|`)?(?P<id>(?:{id_pattern}))(?:\*\*|`)?\s*\|")
    return bracketed, table_row


def _severity_tag(line: str, where: str) -> str | None:
    tags = {next(group for group in match.groups() if group).lower() for match in _SEVERITY_TAG_RE.finditer(line)}
    if len(tags) > 1:
        raise HouseRulesError(f"{where}: conflicting severity tags {sorted(tags)} on one clause line")
    return tags.pop() if tags else None


def _content_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, line) for every line outside a code fence."""
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            yield number, line


def _row_cells_text(rest: str) -> str:
    """The cells after the id cell of a table-row clause, minus any severity cell."""
    cells = [cell.strip() for cell in rest.split("|")]
    return " - ".join(cell for cell in cells if cell and cell.lower() not in SEVERITIES)


def _clause_text(rest: str, is_table_row: bool) -> tuple[str, str | None]:
    """(text, digest override) from what follows the id on a defining line."""
    override_match = _DIGEST_OVERRIDE_RE.search(rest)
    text = _COMMENT_RE.sub("", rest)
    text = _row_cells_text(text) if is_table_row else _BOLD_CLOSE_RE.sub("", text.lstrip())
    text = _SEVERITY_TAG_RE.sub("", text)
    return " ".join(text.split()), override_match.group(1) if override_match else None


def parse_rules_text(text: str, name: str, id_pattern: str) -> dict[str, Clause]:
    """{clause id: Clause} for one rules file, in file order."""
    bracketed, table_row = _definition_regexes(id_pattern)
    found: dict[str, Clause] = {}
    for number, line in _content_lines(text):
        bracketed_match = bracketed.match(line)
        match = bracketed_match or table_row.match(line)
        if match is None:
            continue
        is_table_row = bracketed_match is None
        clause_id = match.group("id")
        where = f"{name}:{number}"
        if clause_id in found:
            raise HouseRulesError(f"{where}: clause {clause_id} is already defined at {name}:{found[clause_id].line}")
        clause_text, override = _clause_text(line[match.end():], is_table_row)
        found[clause_id] = Clause(clause_id, _severity_tag(line, where), number, clause_text, override)
    return found


# ---- family severity table ------------------------------------------------------------------------------------

def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    return [cell.strip().strip("`*").strip() for cell in stripped.strip("|").split("|")]


def _table_blocks(text: str) -> list[list[tuple[int, list[str]]]]:
    """Runs of adjacent table lines outside code fences, as (line number, cells)."""
    blocks: list[list[tuple[int, list[str]]]] = []
    current: list[tuple[int, list[str]]] = []
    for number, line in _content_lines(text):
        cells = _cells(line)
        if cells is not None and (not current or current[-1][0] == number - 1):
            current.append((number, cells))
            continue
        if current:
            blocks.append(current)
        current = [] if cells is None else [(number, cells)]
    if current:
        blocks.append(current)
    return blocks


def _set_family(mapping: dict[str, tuple[str, str]], family: str, severity: str, where: str) -> None:
    """Record family -> (severity, origin); a second, different severity for the same family is an error."""
    if family in mapping and mapping[family][0] != severity:
        raise HouseRulesError(
            f"{where}: family {family} is {severity} here but {mapping[family][0]} at {mapping[family][1]}")
    mapping.setdefault(family, (severity, where))


def parse_severity_table(text: str, name: str) -> dict[str, tuple[str, str]]:
    """{family: (severity, origin)} from every `| family | severity |` table in one rules file."""
    mapping: dict[str, tuple[str, str]] = {}
    for block in _table_blocks(text):
        if [cell.lower() for cell in block[0][1]] != _SEVERITY_TABLE_HEADER:
            continue
        header_line = block[0][0]
        if len(block) < 2 or not all(_SEPARATOR_CELL_RE.match(cell) for cell in block[1][1]):
            raise HouseRulesError(f"{name}:{header_line}: the severity table header must be followed by a separator row such as |---|---|")
        for number, cells in block[2:]:
            where = f"{name}:{number}"
            if len(cells) != 2:
                raise HouseRulesError(f"{where}: a severity table row has two cells (families, severity), got {len(cells)}")
            severity = cells[1].lower()
            if severity not in SEVERITIES:
                raise HouseRulesError(f"{where}: severity {cells[1]!r} is not one of {list(SEVERITIES)}")
            families = [family.strip().strip("`*").strip() for family in cells[0].split(",")]
            if not all(families):
                raise HouseRulesError(f"{where}: empty family name in {cells[0]!r}")
            for family in families:
                _set_family(mapping, family, severity, where)
    return mapping


# ---- registry -------------------------------------------------------------------------------------------------

def kebab_key(text: str) -> str:
    return re.sub(r"[.\s_]+", "-", text.strip().lower())


def _mention_pattern(kebab_keys: list[str]) -> re.Pattern[str]:
    """One regex for "a known clause id as a whole token, in any of the notations `P0E.2`, `p0e-2`, `P0E_2`".

    Neighbouring letters or digits disqualify a hit, so `P1.1` is not found inside `P1.10`; a sentence-ending dot does not.
    """
    parts_joined = ["[.\\-_]".join(re.escape(part) for part in key.split("-")) for key in kebab_keys]
    return re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(parts_joined)})(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Rules:
    clauses: dict[str, Clause]  # definition order: files in config order, lines in file order
    family_severity: dict[str, str]
    tool_rule_map: dict[str, str]
    scenarios: dict[str, str]
    id_by_kebab: dict[str, str]
    sources: tuple[RulesSource, ...]
    missing: tuple[str, ...] = ()  # the shared layers this machine lacks, named as the config names them (`skip_missing` only)

    def is_known_clause(self, rule_id: str) -> bool:
        return rule_id in self.clauses

    def severity_of(self, rule_id: str) -> str | None:
        """The clause's own tag, else its family's severity from the table, else None."""
        clause = self.clauses.get(rule_id)
        if clause is None:
            return None
        return clause.tag or self.family_severity.get(family_of(rule_id))

    def from_kebab(self, text: str) -> str | None:
        """`p0e-6` -> `P0E.6`; None when it names no known clause."""
        return self.id_by_kebab.get(kebab_key(text))

    @cached_property
    def _has_severity_data(self) -> bool:
        return any(self.severity_of(clause_id) is not None for clause_id in self.clauses)

    def is_reviewer_rule(self, rule_id: str) -> bool:
        """A known clause that `compile` lists under Reviewer rules: guidance for the reviewer, no finding violates it.

        That is a clause without a severity. The exception is rules with no severity anywhere (no tag, no family table):
        `compile` then lists every clause there and warns, so the file draws no line between violations and guidance,
        and each clause stays something a finding can violate (its severity defaults to should-fix).
        """
        return rule_id in self.clauses and self.severity_of(rule_id) is None and self._has_severity_data

    @cached_property
    def _mention_re(self) -> re.Pattern[str] | None:
        """Over the clauses a finding can violate, not the Reviewer rules."""
        keys = [key for key, clause_id in self.id_by_kebab.items() if not self.is_reviewer_rule(clause_id)]
        return _mention_pattern(keys) if keys else None

    def first_clause_mention(self, text: str) -> tuple[str, str] | None:
        """(clause id, the token as written) for the first clause id found in free text that is not a Reviewer rule, else None."""
        if self._mention_re is None:
            return None
        match = self._mention_re.search(text)
        if match is None:
            return None
        return self.id_by_kebab[kebab_key(match.group())], match.group()

    def resolve_reported(self, raw: str) -> tuple[str, str | None]:
        """(rule_id, rule_id_source) for an id supplied in a report. Unknown clause ids are rejected, never coerced."""
        if raw in self.tool_rule_map:
            return self.tool_rule_map[raw], "tool"
        if raw == UNMAPPED or self.is_known_clause(raw):
            return raw, None
        if TOOL_ID_RE.fullmatch(raw):
            return raw, "tool"
        hint = "no rules_files configured" if not self.clauses else f"{len(self.clauses)} known clauses"
        raise HouseRulesError(
            f"unknown rule_id {raw!r}: expected a clause defined in rules_files ({hint}), "
            f"{UNMAPPED!r}, or '<tool>:<code>' such as 'ruff:F811'")


def category_of(rule_id: str) -> str:
    """Grouping label: clause family (`P0E.3` -> `P0E`), tool name (`ruff:F811` -> `ruff`) or `unmapped`."""
    if rule_id == UNMAPPED:
        return UNMAPPED
    if ":" in rule_id:
        return rule_id.split(":", 1)[0]
    return family_of(rule_id)


def _read_rules_file(config: Config, path: Path, reader: FileReader) -> RulesSource:
    """The rules file at `path`, as a layer. One that is not there is an error: 'not found' in the view `reader` gives for a
    file of the repo, SharedLayerMissing for a layer outside it."""
    raw_bytes = reader.read(path)
    if raw_bytes is None:
        if is_outer(config, path):
            raise SharedLayerMissing(rules_name(config, path), os.path.normpath(path))  # `repo/../shared/x.md` is said as the place it is
        label, hint = reader.missing_in(path)
        raise HouseRulesError(f"cannot read rules file {path}: not found in {label}{hint}")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HouseRulesError(f"rules file {path} is not valid UTF-8: {exc}") from exc
    return RulesSource(path, hashlib.sha256(raw_bytes).hexdigest()[:12], text)


def shared_layers(config: Config, reader: FileReader = DiskReader()) -> list[RulesSource]:
    """The layers outside the repo as they are on disk now, in config order; one this machine lacks raises SharedLayerMissing."""
    return [_read_rules_file(config, path, reader) for path in config.rules_files if is_outer(config, path)]


def load_rules(config: Config, reader: FileReader = DiskReader(), *, skip_missing: bool = False) -> Rules:
    """The rule set of every layer in `rules_files`. A shared layer this machine lacks refuses (SharedLayerMissing), so nothing
    is judged or written against an incomplete rule set; with `skip_missing` it is said on stderr and left out, and
    `Rules.missing` names it. Left out, the references in the config to its clauses cannot be checked either."""
    clauses: dict[str, Clause] = {}
    origin: dict[str, str] = {}
    families: dict[str, tuple[str, str]] = {}
    sources: list[RulesSource] = []
    missing: list[str] = []
    for path in config.rules_files:
        try:
            source = _read_rules_file(config, path, reader)
        except SharedLayerMissing as exc:
            if not skip_missing:
                raise
            print(f"warning: {exc.what}; skipped", file=sys.stderr)
            missing.append(exc.name)
            continue
        parsed = parse_rules_text(source.text, str(path), config.clause_id_pattern)
        if not parsed:
            raise HouseRulesError(f"{path}: no clause definitions found with clause_id_pattern {config.clause_id_pattern!r}")
        for clause_id, clause in parsed.items():
            if clause_id in origin:
                raise HouseRulesError(f"{path}:{clause.line}: clause {clause_id} is already defined in {origin[clause_id]}")
            origin[clause_id] = f"{path}:{clause.line}"
            clauses[clause_id] = clause
        for family, (severity, where) in parse_severity_table(source.text, str(path)).items():
            _set_family(families, family, severity, where)
        sources.append(source)

    id_by_kebab: dict[str, str] = {}
    for clause_id in clauses:
        key = kebab_key(clause_id)
        if key in id_by_kebab:
            raise HouseRulesError(f"clauses {id_by_kebab[key]} and {clause_id} collide when normalized to {key!r}")
        id_by_kebab[key] = clause_id

    family_severity = {family: severity for family, (severity, _) in families.items()}
    rules = Rules(clauses, family_severity, dict(config.tool_rule_map), dict(config.scenarios), id_by_kebab, tuple(sources),
                  tuple(missing))
    if not missing:
        _check_config_references(rules)
    return rules


def _check_config_references(rules: Rules) -> None:
    for tool_id, clause_id in rules.tool_rule_map.items():
        if not TOOL_ID_RE.fullmatch(tool_id):
            raise HouseRulesError(f"config tool_rule_map key {tool_id!r} must look like '<tool>:<code>'")
        if not rules.is_known_clause(clause_id):
            raise HouseRulesError(f"config tool_rule_map maps {tool_id!r} to {clause_id!r}, which no rules file defines")
    for rule_id in rules.scenarios:
        if not (rule_id == UNMAPPED or rules.is_known_clause(rule_id) or TOOL_ID_RE.fullmatch(rule_id)):
            raise HouseRulesError(f"config scenarios key {rule_id!r} is not a known clause, 'unmapped' or '<tool>:<code>'")
