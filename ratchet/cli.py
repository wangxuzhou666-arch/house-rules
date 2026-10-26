"""Command-line entry point: argument parsing, file IO and printing. The logic lives in the other modules."""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections import Counter
from pathlib import Path

from .config import Config, load_config
from .digest import Output, display_path, plan_outputs
from .errors import RatchetError
from .git import head_sha
from .ingest import code_review_events, extract_review_items, parse_json_items, report_events
from .ledger import (
    REVIEW_SOURCES, SOURCES, VERDICTS, Event, append_events, effective_rule_id, fold, now_iso, open_findings,
    read_events, select_new_events,
)
from .outcomes import (
    BATCH_NAME, PAGE_NAME, TRAILER_KEY, build_page, commit_outcomes, false_positive_skips, gate_pass_outcomes, label_events,
    new_batch_id, read_skips, skipped_finding_ids, trailer_commits, validate_labels,
)
from .rules import load_rules


def _read_input(path: str | None) -> str:
    if path is None:
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RatchetError(f"cannot read input {path}: {exc}") from exc


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise RatchetError(f"cannot write {path}: {exc}") from exc


def _dump(event: Event) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def _append_or_preview(args: argparse.Namespace, config: Config, events: list[Event], summary: str) -> None:
    if args.dry_run:
        print(f"[dry-run] {summary}; nothing written to {config.ledger_path}")
        for event in events:
            print(_dump(event))
        return
    append_events(config.ledger_path, events)
    print(summary)


def _load_events(args: argparse.Namespace, config: Config) -> list[Event]:
    """Read the ledger. A missing default ledger is an empty one; a missing --ledger path is a typo."""
    if args.ledger is not None and not config.ledger_path.exists():
        raise RatchetError(f"ledger not found: {config.ledger_path}")
    return read_events(config.ledger_path)


# ---- commands -------------------------------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace, config: Config) -> int:
    rules = load_rules(config)
    items = parse_json_items(_read_input(args.input))
    events = report_events(items, config=config, rules=rules, source=args.source, run_id=args.run,
                           sha=head_sha(config.root), ts=now_iso())
    fresh = select_new_events(read_events(config.ledger_path), events)
    added = sum(1 for event in fresh if event["kind"] == "finding")
    sent = sum(1 for event in events if event["kind"] == "finding")
    _append_or_preview(args, config, fresh, f"add: +{added} finding(s), {sent - added} duplicate(s) ignored")
    return 0


def cmd_ingest_code_review(args: argparse.Namespace, config: Config) -> int:
    rules = load_rules(config)
    items = extract_review_items(_read_input(args.input))
    result = code_review_events(items, config=config, rules=rules, source=args.source, run_id=args.run,
                                verdict=args.verdict, sha=head_sha(config.root), ts=now_iso(),
                                discovered_at=args.discovered_at)
    fresh = select_new_events(read_events(config.ledger_path), result.events)
    for event in fresh:
        print(f"  {event['display_id']}  {event['file']}:{event['line']}  {event['rule_id']}  {event['severity']}")
    summary = (f"ingest-code-review: +{len(fresh)} finding(s) from {len(items)} item(s) "
               f"({result.merged_count} merged by file+line, {result.unmapped_count} unmapped, "
               f"{result.severity_filled_count} without a usable severity), "
               f"{len(result.events) - len(fresh)} already recorded")
    _append_or_preview(args, config, fresh, summary)
    return 0


def cmd_count(args: argparse.Namespace, config: Config) -> int:
    events = _load_events(args, config)
    state = fold(events)
    open_list = [item.finding for item in state.values() if item.outcome is None]
    by_source = " / ".join(f"{source} {sum(1 for f in open_list if f['source'] == source)}" for source in SOURCES)
    note = "" if config.ledger_path.exists() else f" (no ledger file at {config.ledger_path})"
    print(f"findings: {len(state)} total, {len(open_list)} open ({by_source}); events: {len(events)}{note}")
    if args.by_rule:
        counts = Counter(effective_rule_id(finding, config.legacy_unmapped_alias) for finding in open_list)
        for rule_id, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
            print(f"  {rule_id}: {count}")
    return 0


def cmd_infer(args: argparse.Namespace, config: Config) -> int:
    events = _load_events(args, config)
    ts = now_iso()
    trailers = commit_outcomes(events, trailer_commits(config.root, args.all), ts)
    skipped = skipped_finding_ids(read_skips(config.skips_path))
    # Fold the trailer outcomes in first so one finding never gets two outcomes in the same run.
    by_gate = gate_pass_outcomes(events + trailers.outcomes, ts, skipped)
    outcomes = trailers.outcomes + by_gate
    open_after = len(open_findings(events + outcomes))

    for sha, token in trailers.unmatched:
        print(f"warning: commit {sha[:8]} has a {TRAILER_KEY} trailer naming {token!r}, which matches no finding", file=sys.stderr)
    for sha, token in trailers.ambiguous:
        print(f"warning: commit {sha[:8]}: {token!r} is the display_id of several findings; use the full id", file=sys.stderr)

    summary = (f"infer: +{len(trailers.outcomes)} fixed (auto:commit), +{len(by_gate)} fixed (auto:gate_pass); "
               f"open {open_after}")
    _append_or_preview(args, config, outcomes, summary)
    if open_after > config.label_page_threshold:
        print(f"open > {config.label_page_threshold}: run `ratchet label-page` to label the rest by hand")
    return 0


def cmd_label_page(args: argparse.Namespace, config: Config) -> int:
    open_list = open_findings(_load_events(args, config))
    if args.auto and len(open_list) <= config.label_page_threshold:
        print(f"open {len(open_list)} <= {config.label_page_threshold}: no label page generated")
        return 0
    if not open_list:
        print("no open findings: no label page generated")
        return 0
    created = now_iso()
    batch_id = new_batch_id(created)
    open_ids = {finding["id"] for finding in open_list}
    preselect = false_positive_skips(read_skips(config.skips_path)) & open_ids
    page_path = config.ledger_path.parent / PAGE_NAME
    batch_path = config.ledger_path.parent / BATCH_NAME
    if args.dry_run:
        print(f"[dry-run] label-page: would write {page_path} and {batch_path} "
              f"({len(open_list)} findings, batch {batch_id})")
        return 0
    _write_text(page_path, build_page(open_list, preselect, batch_id, config.legacy_unmapped_alias))
    batch = {"batch_id": batch_id, "created": created, "finding_ids": [finding["id"] for finding in open_list]}
    _write_text(batch_path, json.dumps(batch, ensure_ascii=False))
    print(f"label page: {page_path} ({len(open_list)} findings, batch {batch_id}); "
          "open it, label, Copy JSON, then run `ratchet apply-labels`")
    return 0


def cmd_apply_labels(args: argparse.Namespace, config: Config) -> int:
    batch_path = config.ledger_path.parent / BATCH_NAME
    if not batch_path.exists():
        raise RatchetError(f"no label batch found at {batch_path}; run `ratchet label-page` first")
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RatchetError(f"cannot read label batch {batch_path}: {exc}") from exc
    try:
        payload = json.loads(_read_input(args.input))
    except json.JSONDecodeError as exc:
        raise RatchetError(f"input is not valid JSON: {exc}") from exc
    errors = validate_labels(payload, batch, fold(_load_events(args, config)))
    if errors:
        raise RatchetError("batch rejected, ledger untouched:\n  " + "\n  ".join(errors))
    events = label_events(payload, batch["batch_id"], now_iso())
    _append_or_preview(args, config, events, f"apply-labels: {len(events)} human outcome(s) (batch {batch['batch_id']})")
    return 0


def _print_diff(output: Output, root: Path) -> None:
    name = display_path(output.path, root)
    old_lines = [] if output.old is None else output.old.splitlines()
    diff = difflib.unified_diff(old_lines, output.new.splitlines(), fromfile="/dev/null" if output.old is None else f"a/{name}",
                                tofile=f"b/{name}", lineterm="")
    for line in diff:
        print(line)


def _compile_check(stale: list[Output], total: int, root: Path) -> int:
    if not stale:
        print(f"compile --check: {total} output(s) up to date")
        return 0
    for output in stale:
        state = "missing" if output.old is None else "out of date"
        print(f"stale: {display_path(output.path, root)} ({output.kind}, {state})", file=sys.stderr)
    print("run `ratchet compile` to regenerate", file=sys.stderr)
    return 1


def cmd_compile(args: argparse.Namespace, config: Config) -> int:
    if not config.rules_files:
        raise RatchetError("compile needs rules_files in the config")
    rules = load_rules(config)
    if not any(rules.severity_of(clause_id) for clause_id in rules.clauses):
        print("warning: no clause has a severity (no `| family | severity |` table found); "
              "every clause is listed under Reviewer rules", file=sys.stderr)
    outputs = plan_outputs(config, rules)
    stale = [output for output in outputs if output.is_stale]

    digest_bytes = len(outputs[0].new.encode("utf-8"))
    over_budget = digest_bytes > config.digest_max_bytes
    if over_budget:
        print(f"warning: digest is {digest_bytes} bytes, over digest_max_bytes {config.digest_max_bytes}", file=sys.stderr)

    if args.check:
        status = _compile_check(stale, len(outputs), config.root)
    elif args.dry_run:
        for output in stale:
            print(f"[dry-run] compile: would write {display_path(output.path, config.root)} ({output.kind})")
            _print_diff(output, config.root)
        print(f"[dry-run] compile: {len(stale)} of {len(outputs)} output(s) would change; nothing written")
        status = 0
    else:
        for output in stale:
            _write_text(output.path, output.new)
            print(f"wrote {display_path(output.path, config.root)} ({output.kind})")
        print(f"compile: {len(stale)} written, {len(outputs) - len(stale)} unchanged; "
              f"digest {digest_bytes} bytes (limit {config.digest_max_bytes})")
        status = 0
    return 1 if status or (args.strict and over_budget) else 0


# ---- parser ---------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="repo to operate on (default: the git repo of the current directory)")
    common.add_argument("--ledger", help="ledger file (default: <repo>/<config 'ledger'>, i.e. .style-findings.jsonl)")
    common.add_argument("--config", help="config file (default: <repo>/.ratchet.json)")
    writes = argparse.ArgumentParser(add_help=False)
    writes.add_argument("--dry-run", action="store_true", help="write nothing; print what would be written")

    parser = argparse.ArgumentParser(prog="ratchet",
                                     description="A ledger of code-review findings and what became of them.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", parents=[common, writes], help="record findings from a JSON report")
    add.add_argument("--source", choices=REVIEW_SOURCES, required=True)
    add.add_argument("--run", required=True, help="run id, e.g. R12 or 2026-01-31-l2")
    add.add_argument("--input", help="JSON file (default: stdin)")
    add.set_defaults(func=cmd_add)

    ingest = sub.add_parser("ingest-code-review", parents=[common, writes],
                            help="record the findings printed by a code review (JSON array, or text with a ```json block)")
    ingest.add_argument("--source", choices=REVIEW_SOURCES, required=True)
    ingest.add_argument("--run", required=True, help="run id; becomes the prefix of each display_id")
    ingest.add_argument("--input", help="review output file (default: stdin)")
    ingest.add_argument("--verdict", choices=VERDICTS, default="PLAUSIBLE")
    ingest.add_argument("--discovered-at", help="when the review produced these findings, YYYY-MM-DDTHH:MM:SS local time")
    ingest.set_defaults(func=cmd_ingest_code_review)

    count = sub.add_parser("count", parents=[common], help="print finding and open counts")
    count.add_argument("--by-rule", action="store_true", help="also break open findings down by rule id")
    count.set_defaults(func=cmd_count)

    infer = sub.add_parser("infer", parents=[common, writes], help="close findings from commit trailers and gate passes")
    infer.add_argument("--all", action="store_true", help="read trailers from every ref (git log --all) instead of HEAD")
    infer.set_defaults(func=cmd_infer)

    page = sub.add_parser("label-page", parents=[common, writes], help="generate a local HTML page for labeling open findings")
    page.add_argument("--auto", action="store_true", help="only when open findings exceed label_page_threshold")
    page.set_defaults(func=cmd_label_page)

    apply = sub.add_parser("apply-labels", parents=[common, writes], help="record the labels copied from the label page")
    apply.add_argument("--input", help="JSON file (default: stdin)")
    apply.set_defaults(func=cmd_apply_labels)

    compile_cmd = sub.add_parser("compile", parents=[common, writes],
                                 help="write the style digest from rules_files, and inject it into inject_targets")
    compile_cmd.add_argument("--check", action="store_true", help="write nothing; exit 1 listing every stale output")
    compile_cmd.add_argument("--strict", action="store_true", help="exit 1 when the digest is over digest_max_bytes")
    compile_cmd.set_defaults(func=cmd_compile)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(
            Path(args.repo).expanduser().resolve() if args.repo else None,
            Path(args.ledger).expanduser().resolve() if args.ledger else None,
            Path(args.config).expanduser().resolve() if args.config else None,
            Path.cwd(),
        )
        return args.func(args, config)
    except RatchetError as exc:
        print(f"ratchet: error: {exc}", file=sys.stderr)
        return 1
