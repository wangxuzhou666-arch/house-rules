"""Per-repo configuration: `.ratchet.json` at the repo root, plus CLI overrides. Every key is optional."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import RatchetError
from .git import main_root, origin_url

CONFIG_NAME = ".ratchet.json"
DEFAULT_LEDGER = ".style-findings.jsonl"
# Matches ids like P0E.2, P3.1, R.7: an uppercase family, a dot, a number.
DEFAULT_CLAUSE_ID_PATTERN = r"[A-Z][A-Z0-9]*\.[0-9]+"
DEFAULT_LABEL_PAGE_THRESHOLD = 20
DEFAULT_DIGEST = "CODING-STYLE.digest.md"
DEFAULT_DIGEST_MAX_CHARS = 90
DEFAULT_DIGEST_MAX_BYTES = 4096
DEFAULT_MAX_REVIEW_ROUNDS = 3
# `ratchet wrapup` reviews only when the work since the last review reaches either number.
DEFAULT_WRAPUP_MIN_CHANGED_LINES = 200
DEFAULT_WRAPUP_MIN_FILES = 5
# The sections of the digest an inject target can ask for, in the order they are written.
INJECT_SECTIONS = ("violations", "reviewer", "labeling")

SLUG_RE = re.compile(r"^[a-z0-9-]+$")  # checked with fullmatch: `$` alone also holds before a final newline
_KNOWN_KEYS = frozenset({
    "repo_slug", "ledger", "rules_files", "tool_rule_map", "scenarios", "skips_file",
    "label_page_threshold", "clause_id_pattern", "legacy_unmapped_alias",
    "digest", "digest_max_chars", "digest_max_bytes", "inject_targets", "bin", "max_review_rounds", "default_branch",
    "wrapup_min_changed_lines", "wrapup_min_files",
})


@dataclass(frozen=True)
class InjectTarget:
    path: Path
    sections: tuple[str, ...]  # a subset of INJECT_SECTIONS, in that order


@dataclass(frozen=True)
class Config:
    root: Path
    repo_slug: str
    ledger_path: Path
    rules_files: tuple[Path, ...]
    tool_rule_map: dict[str, str]
    scenarios: dict[str, str]
    skips_path: Path | None
    label_page_threshold: int
    clause_id_pattern: str
    legacy_unmapped_alias: frozenset[str]
    digest_path: Path
    digest_max_chars: int
    digest_max_bytes: int
    inject_targets: tuple[InjectTarget, ...]
    max_review_rounds: int = DEFAULT_MAX_REVIEW_ROUNDS
    default_branch: str | None = None  # None: origin/HEAD's target, else main, else master
    wrapup_min_changed_lines: int = DEFAULT_WRAPUP_MIN_CHANGED_LINES
    wrapup_min_files: int = DEFAULT_WRAPUP_MIN_FILES


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_slug(root: Path) -> str:
    """Basename of the origin remote (without `.git`), else the repo directory name."""
    url = origin_url(root)
    name = url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1].removesuffix(".git") if url else root.name
    slug = slugify(name)
    if not slug:
        raise RatchetError(f"cannot derive a repo slug from {name!r}; set repo_slug in {CONFIG_NAME}")
    return slug


def read_config_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RatchetError(f"cannot read config {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RatchetError(f"{path}: top level must be a JSON object")
    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        raise RatchetError(f"{path}: unknown config key(s) {unknown}; allowed: {sorted(_KNOWN_KEYS)}")
    return raw


def _string_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RatchetError(f"config {key!r} must be a list of non-empty strings")
    return value


def _string_map(raw: dict, key: str) -> dict[str, str]:
    value = raw.get(key, {})
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in value.items()):
        raise RatchetError(f"config {key!r} must be an object mapping non-empty strings to non-empty strings")
    return value


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int or value < 1:
        raise RatchetError(f"config {key!r} must be a positive integer")
    return value


def _inject_sections(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise RatchetError(f"{where}: 'sections' must be a non-empty list of section names, from {list(INJECT_SECTIONS)}")
    unknown = [item for item in value if item not in INJECT_SECTIONS]
    if unknown:
        raise RatchetError(f"{where}: unknown section(s) {unknown}; allowed: {list(INJECT_SECTIONS)}")
    if len(set(value)) != len(value):
        raise RatchetError(f"{where}: 'sections' lists a section twice")
    return tuple(name for name in INJECT_SECTIONS if name in value)


def _inject_target(entry: object, index: int, root: Path) -> InjectTarget:
    where = f"config inject_targets[{index}]"
    fields = {"path": entry} if isinstance(entry, str) else entry
    if not isinstance(fields, dict) or set(fields) - {"path", "sections"}:
        raise RatchetError(f"{where} must be a path or an object with 'path' and optionally 'sections'")
    path = fields.get("path")
    if not isinstance(path, str) or not path:
        raise RatchetError(f"{where}: 'path' must be a non-empty string")
    sections = _inject_sections(fields["sections"], where) if "sections" in fields else INJECT_SECTIONS
    return InjectTarget(root / path, sections)


def _inject_targets(raw: dict, root: Path) -> tuple[InjectTarget, ...]:
    entries = raw.get("inject_targets", [])
    if not isinstance(entries, list):
        raise RatchetError("config 'inject_targets' must be a list of paths or {\"path\", \"sections\"} objects")
    targets = [_inject_target(entry, index, root) for index, entry in enumerate(entries)]
    paths = [target.path for target in targets]
    repeated = sorted({path.name for path in paths if paths.count(path) > 1})
    if repeated:
        raise RatchetError(f"config 'inject_targets' lists {repeated} more than once; each file gets one block")
    return tuple(targets)


def load_config(repo: Path | None, ledger: Path | None, config_file: Path | None, cwd: Path) -> Config:
    """Resolve the repo root (main worktree of --repo, else of cwd) and read its config.

    Relative paths inside the config are relative to the repo root; `ledger` (the CLI override) is used as given.
    """
    root = main_root(repo if repo is not None else cwd)
    if config_file is not None:
        raw = read_config_file(config_file)
    elif (root / CONFIG_NAME).exists():
        raw = read_config_file(root / CONFIG_NAME)
    else:
        raw = {}

    slug = raw["repo_slug"] if "repo_slug" in raw else default_slug(root)
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise RatchetError(f"config repo_slug {slug!r} must match {SLUG_RE.pattern}")

    ledger_name = raw.get("ledger", DEFAULT_LEDGER)
    if not isinstance(ledger_name, str) or not ledger_name:
        raise RatchetError("config 'ledger' must be a non-empty string")

    skips_name = raw.get("skips_file")
    if skips_name is not None and (not isinstance(skips_name, str) or not skips_name):
        raise RatchetError("config 'skips_file' must be a non-empty string")

    threshold = raw.get("label_page_threshold", DEFAULT_LABEL_PAGE_THRESHOLD)
    if type(threshold) is not int or threshold < 0:
        raise RatchetError("config 'label_page_threshold' must be a non-negative integer")

    pattern = raw.get("clause_id_pattern", DEFAULT_CLAUSE_ID_PATTERN)
    if not isinstance(pattern, str) or not pattern:
        raise RatchetError("config 'clause_id_pattern' must be a non-empty string")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RatchetError(f"config clause_id_pattern is not a valid regex: {exc}") from exc

    digest_name = raw.get("digest", DEFAULT_DIGEST)
    if not isinstance(digest_name, str) or not digest_name:
        raise RatchetError("config 'digest' must be a non-empty string")

    default_branch = raw.get("default_branch")
    if default_branch is not None and (not isinstance(default_branch, str) or not default_branch):
        raise RatchetError("config 'default_branch' must be a non-empty string")

    # Only the portable git hooks read `bin` (a shell reads it at commit time); the tool itself just refuses a malformed value.
    if "bin" in raw and (not isinstance(raw["bin"], str) or not raw["bin"]):
        raise RatchetError("config 'bin' must be a non-empty string")

    return Config(
        root=root,
        repo_slug=slug,
        ledger_path=ledger if ledger is not None else root / ledger_name,
        rules_files=tuple(root / name for name in _string_list(raw, "rules_files")),
        tool_rule_map=_string_map(raw, "tool_rule_map"),
        scenarios=_string_map(raw, "scenarios"),
        skips_path=root / skips_name if skips_name else None,
        label_page_threshold=threshold,
        clause_id_pattern=pattern,
        legacy_unmapped_alias=frozenset(_string_list(raw, "legacy_unmapped_alias")),
        digest_path=root / digest_name,
        digest_max_chars=_positive_int(raw, "digest_max_chars", DEFAULT_DIGEST_MAX_CHARS),
        digest_max_bytes=_positive_int(raw, "digest_max_bytes", DEFAULT_DIGEST_MAX_BYTES),
        inject_targets=_inject_targets(raw, root),
        max_review_rounds=_positive_int(raw, "max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS),
        default_branch=default_branch,
        wrapup_min_changed_lines=_positive_int(raw, "wrapup_min_changed_lines", DEFAULT_WRAPUP_MIN_CHANGED_LINES),
        wrapup_min_files=_positive_int(raw, "wrapup_min_files", DEFAULT_WRAPUP_MIN_FILES),
    )
