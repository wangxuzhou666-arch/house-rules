"""CLI adapters for proposal preparation, approval, application and recovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import Config
from .errors import RatchetError
from .fsutil import diff_lines
from .ledger import read_events
from .proposal_store import encoded, recover
from .proposals import STATE, apply_batch, create_batch, rollback_batch, statistics


def input_json(path: str) -> object:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RatchetError(f"cannot read proposal input: {exc}") from exc


def run_distill(args: argparse.Namespace, config: Config) -> int:
    events = read_events(config.ledger_path)
    if not args.drafts:
        print(encoded(statistics(config, events)), end="")
        return 0
    batch = create_batch(config, events, input_json(args.drafts), dry_run=args.dry_run)
    if args.dry_run:
        print(encoded(batch), end="")
    else:
        print(f"proposal batch: {batch['id']}")
        print(f"review page: {config.root / STATE / batch['id'] / 'review.html'}")
        print("No rules changed. Ask the user to choose every proposal and return the exported decisions JSON.")
    return 0


def run_apply(args: argparse.Namespace, config: Config) -> int:
    result = apply_batch(config, read_events(config.ledger_path), input_json(args.input), dry_run=args.dry_run)
    for name, (before, after) in result.get("changes", {}).items():
        for line in diff_lines(name, before, after):
            print(line)
    print(encoded({k: v for k, v in result.items() if k != "changes"}), end="")
    print("Next: compile --check, then review a violating example and an independent clean control. Application is not effectiveness.")
    return 0


def run_rollback(args: argparse.Namespace, config: Config) -> int:
    names = rollback_batch(config, args.batch, dry_run=args.dry_run)
    print(encoded({"status": "preview" if args.dry_run else "rolled_back", "files": names}), end="")
    return 0


def run_recover(args: argparse.Namespace, config: Config) -> int:
    print(f"restored {recover(config.root)} file(s) from the interrupted proposal transaction")
    return 0


def add_parsers(sub, common, writes) -> None:
    distill = sub.add_parser("distill", parents=[common, writes], help="summarize feedback; with --drafts, prepare a human decision page")
    distill.add_argument("--drafts", help="JSON draft list; omit for statistics only, - for stdin")
    distill.set_defaults(func=run_distill)
    apply = sub.add_parser("apply-proposals", parents=[common, writes], help="apply explicit decisions and compile all consumers")
    apply.add_argument("--input", required=True, help="human decisions JSON file, or - for stdin")
    apply.set_defaults(func=run_apply)
    rollback = sub.add_parser("rollback-proposals", parents=[common, writes], help="restore an applied batch if no later edits would be lost")
    rollback.add_argument("--batch", required=True)
    rollback.set_defaults(func=run_rollback)
    recovery = sub.add_parser("recover-proposals", parents=[common], help="restore the pre-write state of an interrupted transaction")
    recovery.set_defaults(func=run_recover)
