"""Shared test helpers. Every test module imports this first so the repo root is importable.

Everything here is synthetic: sample rules, sample findings and throwaway git repos in temp dirs.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ratchet.cli import main  # noqa: E402
from ratchet.config import DEFAULT_CLAUSE_ID_PATTERN, Config  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Isolate test repos from the developer's own git config (signing, hooks, identity).
_GIT_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
_GIT_ENV.update(
    GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
    GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
    GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
)

SAMPLE_RULES = """\
# Sample rules

## Exceptions

1. **[P0E.1]** Keep the guarded block to the one line expected to fail. (blocker)
2. **[P0E.6]** Never swallow an error silently. (blocker)

## Complexity

1. **[P1.1]** Keep nesting depth under five.

## Comments

- [P3.1] Explain why, not what. (nice)

## Reviewer

1. **[R.1]** Standards come from this file, not from the reviewer.
2. **[R.7]** A new rule needs a red and a green test.

| ID | Severity | Rule |
|---|---|---|
| P2.3 | should-fix | No config layer for a one-off need |

See [P0E.1] above for the narrow-try rule; a cross reference is not a definition.

```text
1. **[X9.9]** an example inside a code fence is not a definition
```
"""
SAMPLE_CLAUSES = {"P0E.1", "P0E.6", "P1.1", "P3.1", "R.1", "R.7", "P2.3"}

SEVERITY_TABLE = """\
| family | severity |
|---|---|
| P0E, P0F | blocker |
| P1, P2 | should-fix |
| P3 | nice |
"""

# Clause severities come only from the family table at the bottom; no clause line carries its own tag.
STYLE_RULES = """\
# Synthetic style rules

## Exceptions

1. **[P0E.1]** Keep the guarded `try` block to the **one line** expected to fail. Move the rest to `else:`.
2. **[P0E.2]** Catch a specific type; one handler per action.
3. **[P0F.1]** Fail fast at the boundary.

## Complexity and abstraction

1. **[P1.1]** Nesting depth of five or more is a violation.
2. **[P2.1]** One implementation does not need a base class.

## Comments

1. **[P3.1]** Explain why, not what.

## Reviewer

1. **[R.1]** Standards come from this file, not from the reviewer.
2. **[R.2]** Never rubber-stamp a clean diff.

""" + SEVERITY_TABLE


def make_config(rules_files: tuple[Path, ...] = (), **overrides: object) -> Config:
    """A Config that never touches git or the disk; override any field by name."""
    config = Config(
        root=Path("."), repo_slug="demo", ledger_path=Path("ledger.jsonl"), rules_files=rules_files, tool_rule_map={},
        scenarios={}, skips_path=None, label_page_threshold=20, clause_id_pattern=DEFAULT_CLAUSE_ID_PATTERN,
        legacy_unmapped_alias=frozenset(), digest_path=Path("digest.md"), digest_max_chars=90, digest_max_bytes=4096,
        inject_targets=())
    return dataclasses.replace(config, **overrides)


def git(cwd: Path, *args: str, **extra_env: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**_GIT_ENV, **extra_env})
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def report_item(**overrides: object) -> dict:
    item = {"id": "R1-001", "file": "src/a.py", "line": 10, "summary": "Synthetic summary",
            "failure_scenario": "Synthetic failure scenario", "verdict": "PLAUSIBLE", "rule_id": "P0E.1"}
    item.update(overrides)
    return item


def review_item(**overrides: object) -> dict:
    """One object of the JSON array a code review prints."""
    item = {"file": "src/a.py", "line": 10, "summary": "Synthetic summary", "failure_scenario": "Synthetic failure scenario"}
    item.update(overrides)
    return item


class RepoCase(unittest.TestCase):
    """A fresh git repo named `demo-repo` in a temp dir, plus helpers to run the CLI against it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.repo = self.tmp / "demo-repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.ledger = self.repo / ".style-findings.jsonl"

    def write_rules(self, text: str = SAMPLE_RULES, name: str = "rules.md") -> Path:
        path = self.repo / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_config(self, **config: object) -> None:
        (self.repo / ".ratchet.json").write_text(json.dumps(config), encoding="utf-8")

    def with_sample_rules(self, **config: object) -> None:
        self.write_rules()
        self.write_config(rules_files=["rules.md"], **config)

    def commit(self, subject: str, body: str | None = None, date: str | None = None) -> str:
        args = ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", subject]
        if body is not None:
            args += ["-m", body]
        extra_env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {}
        git(self.repo, *args, **extra_env)
        return git(self.repo, "rev-parse", "HEAD")

    def cli(self, *argv: str, stdin: str = "", with_repo: bool = True) -> tuple[int, str, str]:
        """Run the CLI in-process; returns (exit code, stdout, stderr)."""
        full = list(argv) + (["--repo", str(self.repo)] if with_repo else [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = main(full)
        return code, out.getvalue(), err.getvalue()

    def cli_ok(self, *argv: str, stdin: str = "") -> str:
        code, out, err = self.cli(*argv, stdin=stdin)
        self.assertEqual(code, 0, f"ratchet {argv} failed: {err}")
        return out

    def add_findings(self, items: list[dict], source: str = "l1", run: str = "R1") -> str:
        return self.cli_ok("add", "--source", source, "--run", run, stdin=json.dumps(items))

    def events(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def ledger_bytes(self) -> bytes | None:
        return self.ledger.read_bytes() if self.ledger.exists() else None

    def append_raw(self, *events: dict) -> None:
        with self.ledger.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def gate_finding(file: str, hexid: str = "a1b2c3d4e5f6", **overrides: object) -> dict:
    event = {
        "v": 1, "kind": "finding", "id": f"demo-repo:gate:{hexid}", "display_id": hexid, "run_id": "gate",
        "repo": "demo-repo", "source": "gate", "ts": "2026-01-05T09:00:00", "sha": "0a1b2c3d", "file": file, "line": 3,
        "rule_code": "F811", "rule_id": "ruff:F811", "category": "hygiene", "severity": "should-fix",
        "summary": "synthetic gate finding", "failure_scenario": "synthetic gate scenario", "verdict": "CONFIRMED",
    }
    event.update(overrides)
    return event


def review_finding(display_id: str = "R1-001", source: str = "l1", run: str = "R1", **overrides: object) -> dict:
    event = {
        "v": 1, "kind": "finding", "id": f"demo-repo:{source}:{run}:{display_id}", "display_id": display_id,
        "run_id": run, "repo": "demo-repo", "source": source, "ts": "2026-01-07T11:00:00", "sha": "1f2e3d4c",
        "file": "src/a.py", "line": 10, "rule_code": "R.1", "rule_id": "R.1", "category": "reviewer",
        "severity": "should-fix", "summary": "synthetic review finding", "failure_scenario": "synthetic scenario",
        "verdict": "PLAUSIBLE",
    }
    event.update(overrides)
    return event


def gate_pass(files: list[str], ts: str = "2026-01-06T10:00:00", sha: str = "1f2e3d4c") -> dict:
    return {"v": 1, "kind": "gate_pass", "repo": "demo-repo", "ts": ts, "sha": sha, "files": files}


def review_round(key: str = "main@0a1b2c3d4e5f", **overrides: object) -> dict:
    """A `review_run` ledger event: one review round of the change named `key`."""
    event = {"v": 1, "kind": "review_run", "repo": "demo-repo", "ts": "2026-01-07T11:00:00", "run": "r1", "round_key": key,
             "round": 1, "max_rounds": 3, "over_cap": False, "finding_count": 0, "source": "claude"}
    event.update(overrides)
    return event
