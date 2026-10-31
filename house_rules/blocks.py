"""Marked blocks: the lines `house-rules init` owns inside a file the user owns (CLAUDE.md, .gitignore, a git hook).

A block sits between a begin and an end marker line. A rerun finds it and refreshes it in place; text outside the markers
is never touched.
"""
from __future__ import annotations

from .errors import HouseRulesError

HTML_BEGIN, HTML_END = "<!-- house-rules:begin -->", "<!-- house-rules:end -->"
HASH_BEGIN, HASH_END = "# house-rules:begin", "# house-rules:end"


def find_block(lines: list[str], begin: str, end: str, name: str) -> tuple[int, int] | None:
    """(index of the begin line, index of the end line), or None when neither marker is present."""
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise HouseRulesError(f"{name}: expected exactly one `{begin}` followed by one `{end}`; fix or remove them and rerun")
    return begins[0], ends[0]


def upsert_block(text: str, begin: str, end: str, body: list[str], name: str) -> tuple[str, str]:
    """(new text, "add" | "update" | "ok"): `body` between the markers, replacing a previous block or appended after the text."""
    block = "\n".join([begin, *body, end]) + "\n"
    lines = text.splitlines(keepends=True)
    span = find_block([line.rstrip("\r\n") for line in lines], begin, end, name)
    if span is None:
        head = text if text == "" or text.endswith("\n") else text + "\n"
        separator = "\n" if head and not head.endswith("\n\n") else ""
        return head + separator + block, "add"
    first, last = span
    new_text = "".join(lines[:first]) + block + "".join(lines[last + 1:])
    return new_text, "ok" if new_text == text else "update"


def split_block(text: str, begin: str, end: str, name: str) -> tuple[list[str], bool]:
    """(the lines outside the marked block, whether a block exists)."""
    lines = text.splitlines()
    span = find_block(lines, begin, end, name)
    return (lines, False) if span is None else (lines[:span[0]] + lines[span[1] + 1:], True)
