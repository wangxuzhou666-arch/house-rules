"""`ratchet label`: record one human outcome for each finding named on the command line, with no label page and no batch."""
from __future__ import annotations

from .errors import RatchetError
from .ledger import Event, FindingState
from .outcomes import finding_lookup, label_events

DEFAULT_EVIDENCE = "ratchet label"  # what an outcome without a --note says about where it came from


def direct_label_events(state: dict[str, FindingState], tokens: list[str], outcome: str, note: str | None, ts: str) -> list[Event]:
    """The outcome events for `tokens`, in the order given, or a RatchetError that lists every token that cannot be labeled.

    A token is a full finding id or the display_id the review table shows. All or nothing: the caller appends what this
    returns in one write, so a single bad token has to stop the whole batch here.
    """
    lookup = finding_lookup(state)
    named: dict[str, str] = {}  # finding id -> the token that named it first
    problems: list[str] = []
    for token in tokens:
        found = lookup(token)
        if not found:
            problems.append(f"{token!r} matches no finding in the ledger")
            continue
        if len(found) > 1:
            problems.append(f"{token!r} is the display_id of several findings; use the full id: {', '.join(found)}")
            continue
        finding_id = found[0]
        if finding_id in named:
            problems.append(f"duplicate id: {token!r} names {finding_id} again")
            continue
        named[finding_id] = token
        existing = state[finding_id].outcome
        if existing is not None:
            problems.append(f"{token!r} already has an outcome ({existing['outcome']} by {existing['outcome_by']}); "
                            "a label never overwrites one")
    if problems:
        raise RatchetError("label rejected, ledger untouched:\n  " + "\n  ".join(problems))
    labels = [{"id": finding_id, "outcome": outcome, "note": note} for finding_id in named]
    return label_events({"labels": labels}, DEFAULT_EVIDENCE, ts)
