"""`ratchet compile`: turn the parsed rules into a compact, deterministic digest (no model involved).

The digest is one markdown file meant to be pulled into an agent's context with an `@import` line, and the same
sections can be injected between markers in other files. This module only computes texts; the CLI does the writing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import INJECT_SECTIONS, Config
from .errors import RatchetError
from .rules import Clause, Rules
from .sources import DiskReader, FileReader

BEGIN_MARKER = "<!-- style-digest:begin -->"
END_MARKER = "<!-- style-digest:end -->"
ELLIPSIS = "\u2026"

# A clause's short rule ends at the first full stop, semicolon or CJK equivalent. ASCII ones count only before
# whitespace or the end of the text, so `os.path` and `1.5` survive.
_SENTENCE_END_RE = re.compile(r"\u3002|\uff1b|\.(?=\s|$)|;(?=\s|$)")
# A sentence end inside an open bracket is part of a parenthetical, so cutting there would leave it unclosed.
_BRACKET_OPENERS = "([\uff08\u3010\u300c"
_BRACKET_CLOSERS = ")]\uff09\u3011\u300d"
# What is left after the cut often ends in the colon that introduced the sub-bullets the digest does not carry.
_TRAILING_JUNK_RE = re.compile(r"[\s:\uff1a]+$")
_BACKTICK_RUN_RE = re.compile(r"`+")

LABELING = """\
## Labeling

When you report findings as a JSON array, every finding object MUST carry two keys besides `file`, `line`, `summary` and `failure_scenario`:
- `"category"`: the ID of the violated clause in kebab-case (`P0E.6` -> `p0e-6`), or `"unmapped"` if no clause truly matches. Prefer `unmapped` over stretching a clause to fit.
- `"severity"`: `blocker`, `should-fix` or `nice`, the one listed for that clause under Violations.

Example: `{"file": "src/a.py", "line": 12, "summary": "...", "failure_scenario": "...", "category": "p0e-2", "severity": "blocker"}`

File one finding per defect. Mention other matching IDs in the summary instead of filing duplicates.
"""


def _first_sentence_end(text: str) -> int:
    """Index of the first sentence end that is outside every bracket pair, else len(text)."""
    depth = 0
    for index, char in enumerate(text):
        if char in _BRACKET_OPENERS:
            depth += 1
        elif char in _BRACKET_CLOSERS:
            depth = max(depth - 1, 0)
        elif depth == 0 and _SENTENCE_END_RE.match(text, index):
            return index
    return len(text)


def _open_code_span_start(text: str) -> int | None:
    """Index of the backtick run that opens a code span `text` ends inside, else None.

    A span closes at a run of the same length (CommonMark), so a longer or shorter run inside it is just text.
    """
    opened: re.Match[str] | None = None
    for run in _BACKTICK_RUN_RE.finditer(text):
        if opened is None:
            opened = run
        elif len(run.group()) == len(opened.group()):
            opened = None
    return None if opened is None else opened.start()


def _cap(text: str, max_chars: int) -> str:
    """`text` cut to max_chars including the ellipsis. A cut inside a code span backs off to just before the span,
    unless that would leave less than half the cap, where a cut span is the lesser evil."""
    cut = text[:max_chars - 1]
    span_start = _open_code_span_start(cut)
    if span_start is not None:
        backed_off = cut[:span_start].rstrip()
        if len(backed_off) * 2 >= max_chars:
            return backed_off + ELLIPSIS
    return cut.rstrip() + ELLIPSIS


def short_rule(clause: Clause, max_chars: int) -> str:
    """One-line form of a clause: its override comment verbatim, else the first sentence capped at max_chars."""
    if clause.digest_override is not None:
        return clause.digest_override
    text = clause.text.replace("**", "")
    text = _TRAILING_JUNK_RE.sub("", text[:_first_sentence_end(text)]).lstrip()
    return text if len(text) <= max_chars else _cap(text, max_chars)


def render_sections(rules: Rules, max_chars: int) -> dict[str, str]:
    """The digest's three sections by name (keys of config.INJECT_SECTIONS), each ending in a newline, in rules-file order."""
    violations: list[str] = []
    reviewer: list[str] = []
    for clause in rules.clauses.values():
        rule = short_rule(clause, max_chars)
        if not rule:
            raise RatchetError(f"clause {clause.id} has no rule text on its defining line; add `<!-- digest: ... -->` to it")
        severity = rules.severity_of(clause.id)
        if severity is None:
            reviewer.append(f"{clause.id} | {rule}")
        else:
            violations.append(f"{clause.id} | {severity} | {rule}")
    return {
        "violations": "\n".join(["## Violations", *(violations or ["(none)"])]) + "\n",
        "reviewer": "\n".join(["## Reviewer rules", *(reviewer or ["(none)"])]) + "\n",
        "labeling": LABELING,
    }


def join_sections(sections: dict[str, str], names: tuple[str, ...]) -> str:
    """The named sections, a blank line between them."""
    return "\n".join(sections[name] for name in names)


def display_path(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def source_stamp(rules: Rules, root: Path) -> str:
    return ", ".join(f"{display_path(source.path, root)}@{source.sha12}" for source in rules.sources)


def render_digest(stamp: str, body: str) -> str:
    return f"<!-- generated by ratchet compile from {stamp}; do not edit by hand -->\n# Style digest\n\n{body}"


def render_block(stamp: str, body: str) -> str:
    return f"<!-- generated by ratchet compile from {stamp}; do not edit between the style-digest markers -->\n{body}"


def _marker_index(text: str, marker: str, name: str) -> int:
    count = text.count(marker)
    if count == 0:
        raise RatchetError(f"{name}: marker {marker} not found")
    if count > 1:
        raise RatchetError(f"{name}: marker {marker} appears {count} times; it must appear exactly once")
    return text.index(marker)


def inject_block(text: str, block: str, name: str) -> str:
    """`text` with everything between the begin and end markers replaced by `block`."""
    begin = _marker_index(text, BEGIN_MARKER, name)
    end = _marker_index(text, END_MARKER, name)
    if end < begin + len(BEGIN_MARKER):
        raise RatchetError(f"{name}: the end marker comes before the begin marker")
    return f"{text[:begin + len(BEGIN_MARKER)]}\n{block}{text[end:]}"


@dataclass(frozen=True)
class Output:
    path: Path
    kind: str  # "digest" or "injected block"
    old: str | None  # None: the file does not exist yet
    new: str

    @property
    def is_stale(self) -> bool:
        return self.old != self.new


def _read_existing(path: Path, reader: FileReader) -> str | None:
    raw = reader.read(path)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RatchetError(f"{path} is not valid UTF-8 in {reader.label}: {exc}") from exc


def _inject_problem(reader: FileReader, target: Path, name: str, staged_text: str, problem: str) -> str:
    """The message for a target whose markers are unusable, worded for the view it was read from."""
    if not reader.copy_label:
        return problem
    has_no_markers = BEGIN_MARKER not in staged_text and END_MARKER not in staged_text
    working_copy = reader.working_tree_copy(target)
    if has_no_markers and working_copy is not None and BEGIN_MARKER.encode() in working_copy:
        return f"{reader.copy_label}{name} has no style-digest markers (the working tree copy does); stage it with `git add {name}`"
    return f"{reader.copy_label}{problem}"


def plan_outputs(config: Config, rules: Rules, reader: FileReader = DiskReader()) -> list[Output]:
    """Everything compile would write, digest first, each compared with its existing content as `reader` sees it.

    Raises before any file is touched if a target is unusable.
    """
    sections = render_sections(rules, config.digest_max_chars)
    stamp = source_stamp(rules, config.root)
    digest_old = _read_existing(config.digest_path, reader)
    outputs = [Output(config.digest_path, "digest", digest_old, render_digest(stamp, join_sections(sections, INJECT_SECTIONS)))]
    for target in config.inject_targets:
        old = _read_existing(target.path, reader)
        name = display_path(target.path, config.root)
        if old is None:
            raise RatchetError(f"inject target not found in {reader.label}: {target.path}{reader.missing_hint}")
        block = render_block(stamp, join_sections(sections, target.sections))
        try:
            injected = inject_block(old, block, name)
        except RatchetError as exc:
            raise RatchetError(_inject_problem(reader, target.path, name, old, str(exc))) from exc
        outputs.append(Output(target.path, "injected block", old, injected))
    return outputs
