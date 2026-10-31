"""The stable Python API, for gates that would rather import house_rules than run `house-rules gate ...`.

Three functions, nothing else in the package is promised to stay put:

    from house_rules.api import ledger_path, record_gate_findings, record_gate_pass

    record_gate_findings([{"file": "src/a.py", "line": 12, "code": "ruff:F811", "message": "redefinition of unused x"}])
    record_gate_pass(["src/a.py", "src/b.py"])

`repo` is any path inside the repo (default: the current directory); config, rules and ledger location come from its
`.house-rules.json`, exactly as for the command line. In a linked worktree the files are that worktree's and the ledger is the
main worktree's. Problems raise `house_rules.errors.HouseRulesError`, including a ledger that
cannot be written, so a gate that must not fail on the ledger catches that one exception type.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Config, load_config
from .errors import HouseRulesError
from .gate import gate_pass_event, plan_gate_findings
from .git import head_sha
from .ledger import Event, append_events, now_iso, read_events
from .rules import load_rules

__all__ = ["GateRecording", "HouseRulesError", "ledger_path", "record_gate_findings", "record_gate_pass"]


@dataclass(frozen=True)
class GateRecording:
    ids: list[str]  # the id of every reported finding, in order, including ones that were already open
    recorded: list[Event]  # the events appended just now (none for a finding that was already open)


def _config(repo: Path | str | None) -> Config:
    return load_config(Path(repo).expanduser().resolve() if repo is not None else None, None, None, Path.cwd())


def ledger_path(repo: Path | str | None = None) -> Path:
    """Where the repo's ledger file is (it may not exist yet)."""
    return _config(repo).ledger_path


def record_gate_findings(findings: Iterable[dict[str, Any]], repo: Path | str | None = None) -> GateRecording:
    """Record gate findings `{file, line, code, message}` (optional `severity`); see docs/GATES.md.

    A finding already open in the ledger is not written again. All items are checked before anything is written.
    """
    config = _config(repo)
    plan = plan_gate_findings(read_events(config.ledger_path), list(findings), config=config, rules=load_rules(config),
                              sha=head_sha(config.worktree), ts=now_iso())
    append_events(config.ledger_path, plan.fresh)
    return GateRecording([event["id"] for event in plan.built], plan.fresh)


def record_gate_pass(files: Iterable[str], repo: Path | str | None = None) -> Event:
    """Record that the gate passed over `files`; the open gate findings in those files are closed by `house-rules infer`."""
    config = _config(repo)
    event = gate_pass_event(config, list(files), sha=head_sha(config.worktree), ts=now_iso())
    append_events(config.ledger_path, [event])
    return event
