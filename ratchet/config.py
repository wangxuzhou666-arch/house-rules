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

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_KNOWN_KEYS = frozenset({
    "repo_slug", "ledger", "rules_files", "tool_rule_map", "scenarios", "skips_file",
    "label_page_threshold", "clause_id_pattern", "legacy_unmapped_alias",
    "digest", "digest_max_chars", "digest_max_bytes", "inject_targets",
})


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
    inject_targets: tuple[Path, ...]


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


def _read_config_file(path: Path) -> dict:
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


def load_config(repo: Path | None, ledger: Path | None, config_file: Path | None, cwd: Path) -> Config:
    """Resolve the repo root (main worktree of --repo, else of cwd) and read its config.

    Relative paths inside the config are relative to the repo root; `ledger` (the CLI override) is used as given.
    """
    root = main_root(repo if repo is not None else cwd)
    if config_file is not None:
        raw = _read_config_file(config_file)
    elif (root / CONFIG_NAME).exists():
        raw = _read_config_file(root / CONFIG_NAME)
    else:
        raw = {}

    slug = raw["repo_slug"] if "repo_slug" in raw else default_slug(root)
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
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
        inject_targets=tuple(root / name for name in _string_list(raw, "inject_targets")),
    )
