"""Command-line entry point: argument parsing, file IO and printing. The logic lives in the other modules."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter
from pathlib import Path

from .config import Config, load_config
from .digest import Output, display_path, plan_outputs
from .errors import RatchetError, RoundCapReached, RoundKeyUnavailable
from .fsutil import diff_lines, write_text
from .gate import gate_pass_event, plan_gate_findings
from .git import head_sha, worktree_root
from .ingest import IngestResult, code_review_events, extract_review_items_lenient, parse_json_items, report_events
from .ledger import (
    REVIEW_SOURCES, RULE_ID_SOURCES, SOURCES, VERDICTS, Event, append_events, effective_rule_id, fold, now_iso, open_findings,
    read_events, select_new_events,
)
from .outcomes import (
    BATCH_NAME, PAGE_NAME, TRAILER_KEY, build_page, commit_outcomes, false_positive_skips, gate_pass_outcomes, label_events,
    new_batch_id, read_skips, skipped_finding_ids, trailer_commits, validate_labels,
)
from .onboard import STATUS_WORDS, InitOptions, Step, closing_lines, command_name, run_init
from .review import (
    DEFAULT_LEVEL, LEVELS, check_run_id, check_target, claude_command, default_run_id, findings_table, review_path,
    review_rounds_used, review_run_event, review_run_ids, round_key, run_claude, save_review,
)
from .rules import Rules, load_rules
from .proposal_commands import add_parsers as add_proposal_parsers
from .sources import DiskReader, FileReader, IndexReader
from .vendor import MARKER_NAME, SOURCE_ROOT, check_vendored, plan_vendor, vendor_into


def _read_input(path: str | None) -> str:
    if path is None:
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RatchetError(f"cannot read input {path}: {exc}") from exc


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


def _ingest_review_text(config: Config, rules: Rules, text: str, *, source: str, run_id: str, verdict: str,
                        discovered_at: str | None, label: str) -> tuple[list[Event], str, int]:
    """(new events, summary line, number of findings the review has) for the findings in a code review's output."""
    items, repaired_quotes = extract_review_items_lenient(text)
    result = code_review_events(items, config=config, rules=rules, source=source, run_id=run_id, verdict=verdict,
                                sha=head_sha(config.root), ts=now_iso(), discovered_at=discovered_at)
    fresh = select_new_events(read_events(config.ledger_path), result.events)
    return fresh, _ingest_summary(label, fresh, len(items), result, repaired_quotes), len(result.events)


def _ingest_summary(label: str, fresh: list[Event], item_count: int, result: IngestResult, repaired_quotes: int) -> str:
    repaired = f"; escaped {repaired_quotes} stray quote(s) in the review's JSON" if repaired_quotes else ""
    return (f"{label}: +{len(fresh)} finding(s) from {item_count} item(s) "
            f"({result.merged_count} merged by file+line, {result.unmapped_count} unmapped, "
            f"{result.attributed_count} labeled from the text, "
            f"{result.severity_filled_count} without a usable severity), "
            f"{len(result.events) - len(fresh)} already recorded{repaired}")


def cmd_ingest_code_review(args: argparse.Namespace, config: Config) -> int:
    rules = load_rules(config)
    fresh, summary, _ = _ingest_review_text(config, rules, _read_input(args.input), source=args.source, run_id=args.run,
                                         verdict=args.verdict, discovered_at=args.discovered_at, label="ingest-code-review")
    for event in fresh:
        print(f"  {event['display_id']}  {event['file']}:{event['line']}  {event['rule_id']}  {event['severity']}")
    _append_or_preview(args, config, fresh, summary)
    return 0


def cmd_review(args: argparse.Namespace, config: Config) -> int:
    if args.from_file and (args.level or args.target):
        raise RatchetError("--level and --target choose what to review; they do nothing with --from-file")
    events = read_events(config.ledger_path)
    run_id = check_run_id(args.run or default_run_id(review_run_ids(events, config.root)))
    rules = load_rules(config)
    check_target(args.target)
    key = round_key(config.root, args.target, config.default_branch)
    used = review_rounds_used(events, key)
    round_number = used + 1
    over_cap = round_number > config.max_review_rounds
    refused = over_cap and not args.over_cap
    if refused and not args.dry_run:  # before claude runs: a refused round costs nothing
        raise RoundCapReached(
            f"round cap reached for {key}: {used}/{config.max_review_rounds} rounds used; {len(open_findings(events))} findings "
            "still open (ratchet count). Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded).")
    print(f"review round {round_number}/{config.max_review_rounds} for {key}" + (" (over the cap)" if over_cap else ""))
    if refused:
        print("[dry-run] a real run would be refused: the round cap is reached; --over-cap runs it anyway, recorded")
    if args.from_file:
        text = _read_input(args.from_file)
    else:
        argv = claude_command(args.level or DEFAULT_LEVEL, args.target)
        if args.dry_run:
            print(f"[dry-run] review: would run `{shlex.join(argv)}` in {config.root} (run {run_id}); nothing run or written")
            return 0
        text = run_claude(argv, config.root)
    if not args.dry_run:
        saved = save_review(config.root, run_id, text)
        print(f"saved raw review output to {display_path(saved, config.root)}")
    fresh, summary, finding_count = _ingest_review_text(config, rules, text, source="l1", run_id=run_id, verdict="PLAUSIBLE",
                                                        discovered_at=None, label="review")
    print("\n".join(findings_table(fresh)) if fresh else "no new findings")
    run_event = review_run_event(config.repo_slug, now_iso(), run_id=run_id, key=key, round_number=round_number,
                                 max_rounds=config.max_review_rounds, over_cap=over_cap, finding_count=finding_count,
                                 from_file=bool(args.from_file))
    replayed = any(event["kind"] == "review_run" and event["run"] == run_id for event in events)
    _append_or_preview(args, config, fresh if replayed else [*fresh, run_event], summary)  # re-ingesting a recorded run is not another round
    open_after = len(open_findings(events + fresh))
    print(f"open: {open_after}. A commit with a `Fixes-Finding: <id>` trailer closes a finding.")
    if open_after > config.label_page_threshold:
        print(f"open > {config.label_page_threshold}: run `ratchet label-page` to label the rest by hand")
    return 0


def cmd_count(args: argparse.Namespace, config: Config) -> int:
    events = _load_events(args, config)
    state = fold(events)
    open_list = [item.finding for item in state.values() if item.outcome is None]
    by_source = " / ".join(f"{source} {sum(1 for f in open_list if f['source'] == source)}" for source in SOURCES)
    note = "" if config.ledger_path.exists() else f" (no ledger file at {config.ledger_path})"
    print(f"findings: {len(state)} total, {len(open_list)} open ({by_source}); events: {len(events)}{note}")
    if args.label_source and not args.by_rule:
        raise RatchetError("--label-source splits the --by-rule counts; add --by-rule")
    if args.by_rule:
        counts = Counter(effective_rule_id(finding, config.legacy_unmapped_alias) for finding in open_list)
        for rule_id, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
            print(f"  {rule_id}: {count}" + (_label_sources(rule_id, open_list, config) if args.label_source else ""))
    try:
        key = round_key(config.root, None, config.default_branch)
    except RoundKeyUnavailable:
        return 0  # a detached HEAD (or no known default branch) has no change to count rounds on
    print(f"review rounds on {key}: {review_rounds_used(events, key)}/{config.max_review_rounds}")
    return 0


def _label_sources(rule_id: str, open_list: list[Event], config: Config) -> str:
    """` (model 2, attributed 1)`: how the open findings of one rule were labeled. `none` is an event from before the field existed."""
    by_source = Counter(finding.get("rule_id_source", "none") for finding in open_list
                        if effective_rule_id(finding, config.legacy_unmapped_alias) == rule_id)
    order = [*RULE_ID_SOURCES, "none"]
    return " (" + ", ".join(f"{source} {by_source[source]}" for source in order if by_source[source]) + ")"


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

    if args.quiet and not outcomes:
        return 0
    summary = (f"infer: +{len(trailers.outcomes)} fixed (auto:commit), +{len(by_gate)} fixed (auto:gate_pass); "
               f"open {open_after}")
    _append_or_preview(args, config, outcomes, summary)
    if open_after > config.label_page_threshold and not args.quiet:
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
    write_text(page_path, build_page(open_list, preselect, batch_id, config.legacy_unmapped_alias))
    batch = {"batch_id": batch_id, "created": created, "finding_ids": [finding["id"] for finding in open_list]}
    write_text(batch_path, json.dumps(batch, ensure_ascii=False))
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
    for line in diff_lines(display_path(output.path, root), output.old, output.new):
        print(line)


def _compile_check(stale: list[Output], total: int, root: Path, staged: bool) -> int:
    if not stale:
        print(f"compile --check{' --staged' if staged else ''}: {total} output(s) up to date")
        return 0
    for output in stale:
        if staged:
            state = "not staged" if output.old is None else "the STAGED version is out of date"
        else:
            state = "missing" if output.old is None else "out of date"
        print(f"stale: {display_path(output.path, root)} ({output.kind}, {state})", file=sys.stderr)
    if staged:
        names = " ".join(shlex.quote(display_path(output.path, root)) for output in stale)
        print("this check reads what is staged, not the working tree: run `ratchet compile` if the working tree is stale too, "
              f"then `git add {names}`", file=sys.stderr)
    else:
        print("run `ratchet compile` to regenerate", file=sys.stderr)
    return 1


def _compile_reader(args: argparse.Namespace, config: Config) -> FileReader:
    """`--staged` compares what the next commit will contain (the index) instead of the working tree."""
    if not args.staged:
        return DiskReader()
    if not args.check:
        raise RatchetError("--staged only applies to --check")
    return IndexReader(worktree_root(Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()), config.root)


def cmd_compile(args: argparse.Namespace, config: Config) -> int:
    if not config.rules_files:
        raise RatchetError("compile needs rules_files in the config")
    reader = _compile_reader(args, config)
    rules = load_rules(config, reader)
    if not any(rules.severity_of(clause_id) for clause_id in rules.clauses):
        print("warning: no clause has a severity (no `| family | severity |` table found); "
              "every clause is listed under Reviewer rules", file=sys.stderr)
    outputs = plan_outputs(config, rules, reader)
    stale = [output for output in outputs if output.is_stale]

    digest_name = display_path(config.digest_path, config.root)
    digest_bytes = len(outputs[0].new.encode("utf-8"))
    over_budget = digest_bytes > config.digest_max_bytes
    if over_budget:
        print(f"warning: digest {digest_name} is {digest_bytes} bytes, over digest_max_bytes {config.digest_max_bytes}", file=sys.stderr)

    if args.check:
        status = _compile_check(stale, len(outputs), config.root, args.staged)
    elif args.dry_run:
        for output in stale:
            print(f"[dry-run] compile: would write {display_path(output.path, config.root)} ({output.kind})")
            _print_diff(output, config.root)
        print(f"[dry-run] compile: {len(stale)} of {len(outputs)} output(s) would change; nothing written")
        status = 0
    else:
        for output in stale:
            write_text(output.path, output.new)
            print(f"wrote {display_path(output.path, config.root)} ({output.kind})")
        print(f"compile: {len(stale)} written, {len(outputs) - len(stale)} unchanged; "
              f"digest {digest_name}: {digest_bytes} bytes (limit {config.digest_max_bytes})")
        status = 0
    return 1 if status or (args.strict and over_budget) else 0


def _read_gate_input(value: str | None) -> str:
    """`--input`: `-` or nothing is stdin, text starting with `[` or `{` is the JSON itself, anything else is a file."""
    if value is None or value == "-":
        return sys.stdin.read()
    if value.lstrip().startswith(("[", "{")):
        return value
    return _read_input(value)


def cmd_gate_record(args: argparse.Namespace, config: Config) -> int:
    items = parse_json_items(_read_gate_input(args.input))
    plan = plan_gate_findings(read_events(config.ledger_path), items, config=config, rules=load_rules(config),
                              sha=head_sha(config.root), ts=now_iso())
    new_ids = {event["id"] for event in plan.fresh}
    for event in plan.built:
        print(f"  {event['id']}  {event['file']}:{event['line']}  {event['rule_id']}  "
              f"{'new' if event['id'] in new_ids else 'already open'}")
    _append_or_preview(args, config, plan.fresh,
                       f"gate record: +{len(plan.fresh)} finding(s), {len(plan.built) - len(plan.fresh)} already open")
    return 0


def cmd_gate_pass(args: argparse.Namespace, config: Config) -> int:
    event = gate_pass_event(config, args.files, sha=head_sha(config.root), ts=now_iso())
    _append_or_preview(args, config, [event], f"gate pass: {len(event['files'])} file(s) at {event['sha']}")
    return 0


def cmd_vendor(args: argparse.Namespace) -> int:
    target = Path(args.into).expanduser().resolve()
    if args.against_source and not args.check:
        raise RatchetError("--against-source compares a copy with the source; add --check")
    if args.check:
        problems = check_vendored(target, SOURCE_ROOT if args.against_source else None)
        for problem in problems:
            print(f"vendor --check: {problem}", file=sys.stderr)
        if not problems:
            print(f"vendor --check: {target} matches {MARKER_NAME}" + (" and the current source" if args.against_source else ""))
        return 1 if problems else 0
    plan = plan_vendor(SOURCE_ROOT)
    prefix = "[dry-run] " if args.dry_run else ""
    if not args.dry_run:
        vendor_into(SOURCE_ROOT, target, plan)
    print(f"{prefix}vendor: {len(plan.files)} file(s) from {SOURCE_ROOT} into {target} "
          f"(source_commit {plan.marker.source_commit[:12]}, content_sha256 {plan.marker.content_sha256[:12]})"
          + ("; nothing written" if args.dry_run else ""))
    return 0


def _print_step(step: Step, dry_run: bool, root: Path) -> None:
    first, *more = step.detail.split("\n")
    print(f"  {step.name:<16} {STATUS_WORDS[step.status][1 if dry_run else 0]:<12} {first}")
    for line in [*more, *step.notes]:
        print(f"      {line}")
    if dry_run:  # the diff is the exact text a real run would write
        for change in step.changes:
            label = display_path(change.path, root) if change.path.is_relative_to(root) else str(change.path)
            for line in diff_lines(label, change.old, change.new):
                print(f"      {line}")


def cmd_init(args: argparse.Namespace, config: Config) -> int:
    ask_for_answers = not (args.yes or args.dry_run)
    if ask_for_answers and not sys.stdin.isatty():
        raise RatchetError("init asks questions but stdin is not a terminal; pass --yes to accept the defaults, "
                           "or --dry-run to see the plan")
    options = InitOptions(dry_run=args.dry_run, rules_file=args.rules_file, use_template=args.template,
                          install_hooks=not args.no_hooks, edit_claude_md=not args.no_claude_md)

    print(f"{'[dry-run] ' if args.dry_run else ''}ratchet init: {config.root}")
    result = run_init(config, options, _prompt if ask_for_answers else None, lambda step: _print_step(step, args.dry_run, config.root))
    failed = [step.name for step in result.steps if step.status == "failed"]
    if failed:
        raise RatchetError(f"init finished with FAILED step(s): {', '.join(failed)}; the reason is printed above. Fix it and rerun")
    print("[dry-run] nothing written" if args.dry_run else "\n".join(closing_lines(result, options, command_name())))
    return 0


def _prompt(question: str) -> str:
    try:
        return input(question)
    except EOFError as exc:
        raise RatchetError("no answer: end of input") from exc


# ---- parser ---------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="repo to operate on (default: the git repo of the current directory)")
    common.add_argument("--ledger", help="ledger file (default: <repo>/<config 'ledger'>, i.e. .style-findings.jsonl)")
    common.add_argument("--config", help="config file (default: <repo>/.ratchet.json)")
    repo_only = argparse.ArgumentParser(add_help=False)
    repo_only.add_argument("--repo", help="repo to operate on (default: the git repo of the current directory)")
    writes = argparse.ArgumentParser(add_help=False)
    writes.add_argument("--dry-run", action="store_true", help="write nothing; print what would be written")

    parser = argparse.ArgumentParser(prog="ratchet",
                                     description="A ledger of code-review findings and what became of them.")
    parser.set_defaults(standalone=False)  # standalone commands need no repo, so no config is loaded for them
    sub = parser.add_subparsers(dest="command", required=True)

    add_proposal_parsers(sub, common, writes)

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
    count.add_argument("--label-source", action="store_true",
                       help="with --by-rule: split each rule by how it was labeled (model, human, tool, attributed, none)")
    count.set_defaults(func=cmd_count)

    infer = sub.add_parser("infer", parents=[common, writes], help="close findings from commit trailers and gate passes")
    infer.add_argument("--all", action="store_true", help="read trailers from every ref (git log --all) instead of HEAD")
    infer.add_argument("--quiet", action="store_true", help="print nothing unless a finding was closed (for the post-commit hook)")
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
    compile_cmd.add_argument("--staged", action="store_true",
                             help="with --check: compare the versions in the git index (what the next commit contains), for pre-commit hooks")
    compile_cmd.set_defaults(func=cmd_compile)

    review = sub.add_parser("review", parents=[common, writes],
                            help="run Claude Code's /code-review, save the raw output and record its findings")
    review.add_argument("--level", choices=LEVELS, help=f"review effort (default: {DEFAULT_LEVEL})")
    review.add_argument("--target", help="PR number, branch or path to review (default: the current diff)")
    review.add_argument("--from-file", help="ingest a saved review instead of running claude")
    review.add_argument("--run", help="run id, the prefix of each display_id (default: r<k>, k = 1 + the review runs so far)")
    review.add_argument("--over-cap", action="store_true",
                        help="review again although max_review_rounds are used up on this change (recorded as over_cap)")
    review.set_defaults(func=cmd_review)

    init = sub.add_parser("init", parents=[repo_only, writes],
                          help="set up this repo: config, rules file, digest, CLAUDE.md import, git hooks, .gitignore")
    init.add_argument("--yes", action="store_true", help="accept the defaults instead of asking")
    source = init.add_mutually_exclusive_group()
    source.add_argument("--rules-file", help="use this existing rules file instead of the bundled template")
    source.add_argument("--template", action="store_true", help="copy the bundled template to CODING-STYLE.md (never overwrites)")
    init.add_argument("--no-hooks", action="store_true", help="do not touch .git/hooks")
    init.add_argument("--no-claude-md", action="store_true", help="do not touch CLAUDE.md")
    init.set_defaults(func=cmd_init, ledger=None, config=None)

    gate = sub.add_parser("gate", help="feed a lint or style gate's results into the ledger")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    record = gate_sub.add_parser("record", parents=[common, writes],
                                 help="record gate findings: a JSON list of {file, line, code, message}")
    record.add_argument("--input", help="a JSON file, the JSON itself, or - for stdin (default: stdin)")
    record.set_defaults(func=cmd_gate_record)
    passed = gate_sub.add_parser("pass", parents=[common, writes], help="record that the gate passed over these files")
    passed.add_argument("--files", nargs="+", required=True, metavar="PATH", help="the files the gate checked")
    passed.set_defaults(func=cmd_gate_pass)

    vendor = sub.add_parser("vendor", parents=[writes], help="copy the tool into a repo, or check a vendored copy")
    vendor.add_argument("--into", required=True, help="directory to hold the copy")
    vendor.add_argument("--check", action="store_true", help="write nothing; exit 1 if the copy was edited")
    vendor.add_argument("--against-source", action="store_true", help="with --check: also exit 1 if the source has moved on")
    vendor.set_defaults(func=cmd_vendor, standalone=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.standalone:
            return args.func(args)
        config = load_config(
            Path(args.repo).expanduser().resolve() if args.repo else None,
            Path(args.ledger).expanduser().resolve() if args.ledger else None,
            Path(args.config).expanduser().resolve() if args.config else None,
            Path.cwd(),
        )
        return args.func(args, config)
    except RatchetError as exc:
        print(f"ratchet: error: {exc}", file=sys.stderr)
        return exc.exit_code
