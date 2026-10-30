"""Command-line entry point: argument parsing, file IO and printing. The logic lives in the other modules."""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import json
import shlex
import sys
from collections import Counter
from pathlib import Path

from .auto import note_commit
from .auto_stop import stop_hook
from .config import Config, load_config
from .decision_context import add_parser as add_decision_parser
from .native_decisions import add_parsers as add_native_parsers
from .review_evidence import (
    capture as capture_review_evidence, save as save_review_evidence, snapshot_snippets, worktree_snippets,
)
from .digest import Output, display_path, plan_outputs
from .direct_label import DEFAULT_EVIDENCE, direct_label_events
from .errors import RatchetError, RoundCapReached, RoundKeyUnavailable
from .fsutil import diff_lines, write_text
from .gate import gate_pass_event, plan_gate_findings
from .git import commit_sha, commit_tree, head_sha
from .ingest import (
    IngestResult, Locator, code_review_events, extract_review_items_lenient, parse_json_items, report_events,
)
from .ledger import (
    OUTCOMES, REVIEW_SOURCES, RULE_ID_SOURCES, SOURCES, VERDICTS, Event, append_events, effective_rule_id, fold, now_iso, open_findings,
    read_events, select_new_events,
)
from .outcomes import (
    BATCH_NAME, PAGE_NAME, TRAILER_KEY, build_page, commit_outcomes, false_positive_skips, gate_pass_outcomes, label_events,
    new_batch_id, read_skips, skipped_finding_ids, trailer_commits, validate_labels,
)
from .onboard import STATUS_WORDS, InitOptions, Step, closing_lines, command_name, run_init
from .review import (
    DEFAULT_LEVEL, LEVELS, NO_COMMIT, REFUSE_CLAUDE, REFUSE_ROUND_CAP, Refusal, check_run_id, check_target, claude_command,
    critic_version, critic_version_counts, default_run_id, findings_table, replaced_critic_version, review_path,
    review_refusal, review_rounds_used, review_run_event, review_run_ids, round_key, run_claude, save_review,
)
from .rules import Rules, load_rules
from .proposal_commands import add_parsers as add_proposal_parsers
from .sources import DiskReader, FileReader, IndexReader
from .state_tree import state_tree
from .vendor import MARKER_NAME, SOURCE_ROOT, check_vendored, plan_vendor, vendor_into
from .wrapup import (
    CAPPED, DRY_RUN, REPORTED, REVIEWED, SKIPPED, Base, Thresholds, blind_spot_lines, choose_base, config_thresholds, measure_lines,
    open_unmapped_line, plan_wrapup, report, review_range, review_run_events, run_blind_spots, unmapped_findings, wrapup_cap_message,
)


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
                           sha=head_sha(config.worktree), ts=now_iso())
    fresh = select_new_events(read_events(config.ledger_path), events)
    added = sum(1 for event in fresh if event["kind"] == "finding")
    sent = sum(1 for event in events if event["kind"] == "finding")
    _append_or_preview(args, config, fresh, f"add: +{added} finding(s), {sent - added} duplicate(s) ignored")
    return 0


def _ingest_review_text(config: Config, rules: Rules, text: str, *, source: str, run_id: str, verdict: str, sha: str,
                        discovered_at: str | None, label: str, locate: Locator | None = None) -> tuple[list[Event], str, int]:
    """(new events, summary line, number of findings the review has) for the findings in a code review's output.

    `sha` is the short HEAD the findings are stamped with. `locate` gives them a code snippet; only `review` passes one,
    so `ingest-code-review` records none.
    """
    items, repaired_quotes = extract_review_items_lenient(text)
    ingest_result = code_review_events(items, config=config, rules=rules, source=source, run_id=run_id, verdict=verdict,
                                       sha=sha, ts=now_iso(), discovered_at=discovered_at, locate=locate)
    fresh = select_new_events(read_events(config.ledger_path), ingest_result.events)
    return fresh, _ingest_summary(label, fresh, len(items), ingest_result, repaired_quotes), len(ingest_result.events)


def _ingest_summary(label: str, fresh: list[Event], item_count: int, ingest_result: IngestResult, repaired_quotes: int) -> str:
    repaired = f"; escaped {repaired_quotes} stray quote(s) in the review's JSON" if repaired_quotes else ""
    return (f"{label}: +{len(fresh)} finding(s) from {item_count} item(s) "
            f"({ingest_result.merged_count} merged by file+line, {ingest_result.unmapped_count} unmapped, "
            f"{ingest_result.attributed_count} labeled from the text, "
            f"{ingest_result.severity_filled_count} without a usable severity), "
            f"{len(ingest_result.events) - len(fresh)} already recorded{repaired}")


def cmd_ingest_code_review(args: argparse.Namespace, config: Config) -> int:
    rules = load_rules(config)
    fresh, summary, _ = _ingest_review_text(config, rules, _read_input(args.input), source=args.source, run_id=args.run,
                                         verdict=args.verdict, sha=head_sha(config.worktree), discovered_at=args.discovered_at,
                                         label="ingest-code-review")
    for event in fresh:
        print(f"  {event['display_id']}  {event['file']}:{event['line']}  {event['rule_id']}  {event['severity']}")
    _append_or_preview(args, config, fresh, summary)
    return 0


def _read_critic_version(tree: Path) -> str | None:
    """`claude --version`, asked once per review. A version that cannot be read is said out loud and recorded as null:
    the review is worth more than its label, so it goes ahead."""
    try:
        return critic_version(tree)
    except RatchetError as exc:
        reason = str(exc).splitlines()[0]
        print(f"warning: critic version not recorded ({reason}); this review's reviewer_version is null", file=sys.stderr)
        return None


def _record_state_tree(tree: Path) -> str | None:
    """The tree id of the worktree as the review starts. One that cannot be taken is said out loud and left out of the
    review_run (a wrap-up then measures from the review's HEAD): the review is worth more than its label, so it goes ahead."""
    try:
        return state_tree(tree)
    except RatchetError as exc:
        reason = str(exc).splitlines()[0]
        print(f"warning: working tree not recorded ({reason}); this review_run has no tree, so a wrap-up measures from its HEAD", file=sys.stderr)
        return None


def _review_range(config: Config, events: list[Event], base: Base | None) -> str | None:
    """The commit range a review of the current work is told to read (`review_range`), measured from `base`. A review that a
    wrap-up runs gets the wrap-up's own base; one that runs on its own picks the base a wrap-up would."""
    return review_range(config.worktree, choose_base(config, events, None) if base is None else base, config.default_branch)


def _review_tree(tree: Path, head: str, target: str | None, committed_range: str | None) -> str | None:
    """The tree a review_run records, which a later wrap-up measures new work from.

    None for a --target review: its round key names no branch and a wrap-up measures only from a branch's review, so a tree would
    be objects nothing reads. A review of a commit range read committed work only: its tree is that of the commit the range
    ends at, `head`, which holds none of the work still uncommitted in the worktree, so the next wrap-up counts that work again.
    Any other review read the worktree as it was when it started.
    """
    if target is not None:
        return None
    if committed_range is not None:
        return commit_tree(tree, head)
    return _record_state_tree(tree)


# The refusals a dry run reports and carries on past. The others leave nothing to preview: no rules to ingest with, no round to name.
_DRY_RUN_PREVIEWED = (REFUSE_ROUND_CAP, REFUSE_CLAUDE)


def _dry_run_refusal_line(refusal: Refusal) -> str:
    if refusal.kind == REFUSE_ROUND_CAP:
        return "[dry-run] a real run would be refused: the round cap is reached; --over-cap runs it anyway, recorded"
    return f"[dry-run] a real run would be refused: {refusal.message}"


def cmd_review(args: argparse.Namespace, config: Config, *, base: Base | None = None) -> int:
    """`base` is what the wrap-up that asked for this review measured the work from; None for a review run on its own."""
    if args.from_file and (args.level or args.target):
        raise RatchetError("--level and --target choose what to review; they do nothing with --from-file")
    events = read_events(config.ledger_path)
    run_id = check_run_id(args.run or default_run_id(review_run_ids(events, config.root)))
    replayed = any(event["kind"] == "review_run" and event["run"] == run_id for event in events)  # re-ingesting a recorded run is not a round
    if replayed and not args.from_file:  # only a re-ingest is free; a fresh `claude` run under a recorded id is billed and then fails in save_review
        raise RatchetError(f"run {run_id} is already recorded; omit --run for a new round, "
                           "or pass --from-file to re-ingest a saved review under that run id")
    check_target(args.target)
    refusal = review_refusal(config, events, target=args.target, from_file=bool(args.from_file),
                             over_cap_allowed=args.over_cap, replayed=replayed)
    if refusal is not None and not (args.dry_run and refusal.kind in _DRY_RUN_PREVIEWED):  # before claude runs: a refused round costs nothing
        raise refusal.error()
    rules = load_rules(config)  # the check above only looked at them; the ingest below needs them loaded
    tree = config.worktree  # the code under review; the ledger, saved reviews and config stay under config.root
    key = round_key(tree, args.target, config.default_branch)
    used = review_rounds_used(events, key)
    round_number = used + 1
    over_cap = round_number > config.max_review_rounds
    if replayed:
        print(f"review: replay of run {run_id} (already recorded; not another round)")
    else:
        print(f"review round {round_number}/{config.max_review_rounds} for {key}" + (" (over the cap)" if over_cap else ""))
    if refusal is not None:  # only a dry run is still here
        print(_dry_run_refusal_line(refusal))
    evidence_snapshot = None
    level, reviewer_version = None, None  # a --from-file review does not know either
    if args.from_file:
        text = _read_input(args.from_file)
        head = commit_sha(tree, "HEAD") or NO_COMMIT
        state_tree_id = None if args.dry_run or replayed else _record_state_tree(tree)  # a tree is objects in the repo: a dry run and a replay (no review_run) write none
        locate = worktree_snippets(tree)
    else:
        level = args.level or DEFAULT_LEVEL
        committed_range = None if args.target is not None else _review_range(config, events, base)  # --target names the scope already
        argv = claude_command(level, args.target or committed_range)
        if args.dry_run:
            print(f"[dry-run] review: would run `{shlex.join(argv)}` in {tree} (run {run_id}); nothing run or written")
            return 0
        evidence_snapshot = capture_review_evidence(config, tree, args.target, committed_range)
        head = evidence_snapshot["head"] or NO_COMMIT  # HEAD as the reviewer starts, not as it is when the review ends
        state_tree_id = _review_tree(tree, head, args.target, committed_range)  # taken as the reviewer starts, for the same reason
        reviewer_version = _read_critic_version(tree)
        text = run_claude(argv, tree)
        locate = snapshot_snippets(config, tree, evidence_snapshot)
    if not args.dry_run:
        saved = save_review(config.root, run_id, text)
        print(f"saved raw review output to {display_path(saved, config.root)}")
        if evidence_snapshot is not None:
            save_review_evidence(config, tree, run_id, evidence_snapshot, text)
    fresh, summary, finding_count = _ingest_review_text(config, rules, text, source="l1", run_id=run_id, verdict="PLAUSIBLE",
                                                        sha=head_sha(tree), discovered_at=None, label="review", locate=locate)
    replaced_version = replaced_critic_version(events, reviewer_version)
    if replaced_version is not None:
        print(f"critic version changed: {replaced_version} -> {reviewer_version} (first review on this version)")
    print("\n".join(findings_table(fresh)) if fresh else "no new findings")
    run_event = review_run_event(config.repo_slug, now_iso(), run_id=run_id, key=key, round_number=round_number,
                                 max_rounds=config.max_review_rounds, over_cap=over_cap, finding_count=finding_count,
                                 level=level, head=head, reviewer_version=reviewer_version,
                                 version_changed=replaced_version is not None, from_file=bool(args.from_file),
                                 tree=state_tree_id)
    _append_or_preview(args, config, fresh if replayed else [*fresh, run_event], summary)
    open_after = len(open_findings(events + fresh))
    print(f"open: {open_after}. A commit with a `Fixes-Finding: <id>` trailer closes a finding.")
    if open_after > config.label_page_threshold:
        print(f"open > {config.label_page_threshold}: run `ratchet label-page` to label the rest by hand")
    return 0


def _review_rounds_line(events: list[Event], config: Config, tree: Path) -> str:
    """The last line of `count`. Counting only reads, so a rounds problem is said here and never fails the command.

    The change counted is the one in `tree`, the key a review run there would get.
    """
    try:
        key = round_key(tree, None, config.default_branch)
    except RoundKeyUnavailable as exc:
        return f"review rounds: n/a ({exc.reason})"  # a detached HEAD, say, has no change to count rounds on
    except RatchetError as exc:  # a default_branch in the config that is not a branch, a failing git call
        return f"review rounds: unavailable ({exc})"
    return f"review rounds on {key}: {review_rounds_used(events, key)}/{config.max_review_rounds}"


def cmd_count(args: argparse.Namespace, config: Config) -> int:
    if args.label_source and not args.by_rule:
        raise RatchetError("--label-source splits the --by-rule counts; add --by-rule")
    events = _load_events(args, config)
    state = fold(events)
    open_list = [item.finding for item in state.values() if item.outcome is None]
    by_source = " / ".join(f"{source} {sum(1 for f in open_list if f['source'] == source)}" for source in SOURCES)
    note = "" if config.ledger_path.exists() else f" (no ledger file at {config.ledger_path})"
    print(f"findings: {len(state)} total, {len(open_list)} open ({by_source}); events: {len(events)}{note}")
    if args.by_rule:
        counts = Counter(effective_rule_id(finding, config.legacy_unmapped_alias) for finding in open_list)
        for rule_id, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
            print(f"  {rule_id}: {count}" + (_label_sources(rule_id, open_list, config) if args.label_source else ""))
    version_rows = critic_version_counts(events, state)  # empty unless the findings come from two or more critic versions
    if version_rows:
        print("by critic version:")
        for version, finding_total, open_total in version_rows:
            print(f"  {version or 'no version recorded'}: {finding_total} total, {open_total} open")
    print(_review_rounds_line(events, config, config.worktree))
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
    trailers = commit_outcomes(events, trailer_commits(config.worktree, args.all), ts)  # HEAD of the worktree the commit was made in
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


def cmd_label(args: argparse.Namespace, config: Config) -> int:
    state = fold(_load_events(args, config))
    events = direct_label_events(state, args.ids, args.outcome, args.note, now_iso())
    shown = ", ".join(state[event["id"]].finding["display_id"] for event in events)
    _append_or_preview(args, config, events, f"label: {len(events)} human outcome(s), {args.outcome} ({shown})")
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
    events = label_events(payload, f"label page batch {batch['batch_id']}", now_iso())
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
    return IndexReader(config.worktree)


def cmd_compile(args: argparse.Namespace, config: Config) -> int:
    if not config.rules_files:
        raise RatchetError("compile needs rules_files in the config")
    reader = _compile_reader(args, config)
    rules = load_rules(config, reader, skip_missing=args.check)  # a plain compile would write a digest that lacks a layer's clauses
    if rules.missing:  # a pre-commit hook must not block a commit on a machine that lacks the shared file
        print(f"compile --check{' --staged' if args.staged else ''}: digest not compared, it is generated from every rules layer "
              f"and this machine lacks {', '.join(rules.missing)}", file=sys.stderr)
        return 0
    if not any(rules.severity_of(clause_id) for clause_id in rules.clauses):
        print("warning: no clause has a severity (no `| family | severity |` table found); "
              "every clause is listed under Reviewer rules", file=sys.stderr)
    outputs = plan_outputs(config, rules, reader)
    stale = [output for output in outputs if output.is_stale]

    digest_name = display_path(config.digest_path, config.worktree)
    digest_bytes = len(outputs[0].new.encode("utf-8"))
    over_budget = digest_bytes > config.digest_max_bytes
    if over_budget:
        print(f"warning: digest {digest_name} is {digest_bytes} bytes, over digest_max_bytes {config.digest_max_bytes}", file=sys.stderr)

    if args.check:
        status = _compile_check(stale, len(outputs), config.worktree, args.staged)
    elif args.dry_run:
        for output in stale:
            print(f"[dry-run] compile: would write {display_path(output.path, config.worktree)} ({output.kind})")
            _print_diff(output, config.worktree)
        print(f"[dry-run] compile: {len(stale)} of {len(outputs)} output(s) would change; nothing written")
        status = 0
    else:
        for output in stale:
            write_text(output.path, output.new)
            print(f"wrote {display_path(output.path, config.worktree)} ({output.kind})")
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
                              sha=head_sha(config.worktree), ts=now_iso())
    new_ids = {event["id"] for event in plan.fresh}
    for event in plan.built:
        print(f"  {event['id']}  {event['file']}:{event['line']}  {event['rule_id']}  "
              f"{'new' if event['id'] in new_ids else 'already open'}")
    _append_or_preview(args, config, plan.fresh,
                       f"gate record: +{len(plan.fresh)} finding(s), {len(plan.built) - len(plan.fresh)} already open")
    return 0


def cmd_gate_pass(args: argparse.Namespace, config: Config) -> int:
    event = gate_pass_event(config, args.files, sha=head_sha(config.worktree), ts=now_iso())
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


def cmd_auto_note(args: argparse.Namespace) -> int:
    note_commit(Path(args.repo).expanduser().resolve() if args.repo else None)
    return 0


def cmd_auto_stop(args: argparse.Namespace) -> int:
    return stop_hook(sys.stdin)


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
                          install_hooks=not args.no_hooks, edit_claude_md=not args.no_claude_md,
                          offer_wrapup_auto=not args.no_wrapup_auto)

    print(f"{'[dry-run] ' if args.dry_run else ''}ratchet init: {config.worktree}")
    if config.worktree != config.root:
        print(f"  a linked worktree: its tracked files are set up here; the config and the ledger stay shared with {config.root}")
    init_result = run_init(config, options, _prompt if ask_for_answers else None, lambda step: _print_step(step, args.dry_run, config.worktree))
    failed = [step.name for step in init_result.steps if step.status == "failed"]
    if failed:
        raise RatchetError(f"init finished with FAILED step(s): {', '.join(failed)}; the reason is printed above. Fix it and rerun")
    print("[dry-run] nothing written" if args.dry_run else "\n".join(closing_lines(init_result, options, command_name())))
    return 0


def _prompt(question: str) -> str:
    try:
        return input(question)
    except EOFError as exc:
        raise RatchetError("no answer: end of input") from exc


def _review_args(args: argparse.Namespace) -> argparse.Namespace:
    """The arguments `ratchet review` takes for this wrap-up: the same repo, and its level or saved review.

    Parsed by the real parser, so a review option added later has its default here without wrapup knowing about it. The
    ledger and config are not passed: `cmd_review` reads them from the `config` it is handed, which is already resolved.
    """
    argv = ["review"]
    for flag, value in (("--repo", args.repo), ("--level", args.level), ("--from-file", args.from_file)):
        if value:
            argv += [flag, value]
    return build_parser().parse_args(argv)


def _run_wrapup_review(args: argparse.Namespace, config: Config, events: list[Event], base: Base) -> str:
    """Run `ratchet review` as it would run on its own, over the work measured from `base`, and return the run id it recorded."""
    try:
        cmd_review(_review_args(args), config, base=base)
    except RoundCapReached as exc:
        raise RoundCapReached(wrapup_cap_message(str(exc))) from exc
    runs = review_run_events(read_events(config.ledger_path))
    assert len(runs) == len(review_run_events(events)) + 1, "a review that ends without error records exactly one review_run"
    return runs[-1]["run"]


def _review_plan(args: argparse.Namespace, config: Config, base: Base) -> str:
    if args.from_file:
        return f"would ingest {args.from_file} as a review"
    committed_range = review_range(config.worktree, base, config.default_branch)
    return f"would run `{shlex.join(claude_command(args.level or DEFAULT_LEVEL, committed_range))}`"


def _wrapup_report_only(events: list[Event], config: Config, thresholds: Thresholds) -> dict:
    runs = review_run_events(events)
    if not runs:
        raise RatchetError("no review run is recorded yet, so there is nothing to report; run `ratchet wrapup` or `ratchet review` first")
    run = runs[-1]["run"]
    blind_spots = run_blind_spots(events, config.legacy_unmapped_alias, run)
    open_unmapped = len(unmapped_findings(events, config.legacy_unmapped_alias))
    print("\n".join(blind_spot_lines(run, blind_spots, open_unmapped)))
    return report(REPORTED, base=None, changes=None, thresholds=thresholds, run=run, blind_spots=blind_spots,
                  open_unmapped=open_unmapped, reason=f"the open unmapped findings of the latest review run, {run}")


def _wrapup(args: argparse.Namespace, config: Config, events: list[Event]) -> tuple[int, dict]:
    """Measure the work, review once if it is enough, and print what was found. Returns (exit code, the `--json` object)."""
    if args.report_only:
        return 0, _wrapup_report_only(events, config, config_thresholds(config))
    plan = plan_wrapup(config, events, explicit_base=args.base, from_file=bool(args.from_file), foresee_refusal=args.dry_run)
    print("\n".join(measure_lines(plan.base, plan.changes)))
    summary = functools.partial(report, base=plan.base, changes=plan.changes, thresholds=plan.thresholds)
    if not plan.due:
        print(f"wrapup: skipped, {plan.reason}; no review run\n{open_unmapped_line(plan.open_unmapped)}")
        return 0, summary(SKIPPED, open_unmapped=plan.open_unmapped, reason=plan.reason)
    if args.dry_run:
        refusal = plan.would_refuse
        step = _review_plan(args, config, plan.base) if refusal is None else f"a real run would be refused: {refusal.message}"
        print(f"[dry-run] wrapup: {plan.reason}; {step}; nothing run or written")
        return 0, summary(DRY_RUN, open_unmapped=plan.open_unmapped, reason=plan.reason,
                          would_refuse=None if refusal is None else dataclasses.asdict(refusal))
    print(f"wrapup: {plan.reason}; running a review")
    try:
        run = _run_wrapup_review(args, config, events, plan.base)
    except RoundCapReached as exc:
        if not args.json:
            raise
        print(f"wrapup: {exc}")
        return exc.exit_code, summary(CAPPED, open_unmapped=plan.open_unmapped, reason=str(exc))
    after = read_events(config.ledger_path)
    blind_spots = run_blind_spots(after, config.legacy_unmapped_alias, run)
    open_after = len(unmapped_findings(after, config.legacy_unmapped_alias))
    print("\n".join(blind_spot_lines(run, blind_spots, open_after)))
    return 0, summary(REVIEWED, run=run, blind_spots=blind_spots, open_unmapped=open_after, reason=plan.reason)


def cmd_wrapup(args: argparse.Namespace, config: Config) -> int:
    if args.from_file and args.level:
        raise RatchetError("--level chooses what to review; it does nothing with --from-file")
    if args.report_only and (args.base or args.level or args.from_file or args.dry_run):
        raise RatchetError("--report-only measures and reviews nothing; it takes none of --base, --level, --from-file or --dry-run")
    events = read_events(config.ledger_path)  # as `review` reads it: a --ledger that does not exist yet is a ledger to start
    human_output = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()  # stdout is the one JSON object
    with human_output:
        code, summary = _wrapup(args, config, events)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


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
    add_decision_parser(sub, common)
    add_native_parsers(sub, common, writes)

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

    label = sub.add_parser("label", parents=[common, writes],
                           help="record an outcome for the findings you name by id, without the label page")
    label.add_argument("ids", nargs="+", metavar="ID", help="a display_id as the review table shows it (r3-2), or a full finding id")
    label.add_argument("--outcome", choices=OUTCOMES, required=True, help="what became of them")
    label.add_argument("--note", help=f"why, recorded as the evidence (default: {DEFAULT_EVIDENCE!r})")
    label.set_defaults(func=cmd_label)

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

    wrapup = sub.add_parser("wrapup", parents=[common, writes],
                            help="count what changed since the last review; review once if it reached wrapup_min_changed_lines or wrapup_min_files")
    wrapup.add_argument("--level", choices=LEVELS, help=f"review effort (default: {DEFAULT_LEVEL})")
    wrapup.add_argument("--base", metavar="REF", help="measure from this commit instead of the last review's working tree or the fork point")
    wrapup.add_argument("--from-file", help="ingest a saved review instead of running claude (the thresholds still apply)")
    wrapup.add_argument("--json", action="store_true", help="print one JSON object on stdout; the human report goes to stderr")
    wrapup.add_argument("--report-only", action="store_true",
                        help="measure and review nothing; report the open unmapped findings of the latest review run")
    wrapup.set_defaults(func=cmd_wrapup)

    init = sub.add_parser("init", parents=[repo_only, writes],
                          help="set up this repo: config, rules file, digest, CLAUDE.md import, git hooks, .gitignore")
    init.add_argument("--yes", action="store_true", help="accept the defaults instead of asking")
    source = init.add_mutually_exclusive_group()
    source.add_argument("--rules-file", help="use this existing rules file instead of the bundled template")
    source.add_argument("--template", action="store_true", help="copy the bundled template to CODING-STYLE.md (never overwrites)")
    init.add_argument("--no-hooks", action="store_true", help="do not touch .git/hooks")
    init.add_argument("--no-claude-md", action="store_true", help="do not touch CLAUDE.md")
    init.add_argument("--no-wrapup-auto", action="store_true",
                      help='leave the automatic wrap-up review off without asking (writes "wrapup_auto": false)')
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

    auto = sub.add_parser("auto", help="the hooks of the automatic wrap-up, run by git and by Claude Code, not by hand")
    auto_sub = auto.add_subparsers(dest="auto_command", required=True)
    auto_note = auto_sub.add_parser("note", parents=[repo_only],
                                    help="record the commit just made in a Claude Code session, for the wrap-up at the end of the turn")
    auto_note.set_defaults(func=cmd_auto_note, standalone=True)  # it reads the config itself: a broken one must not print or fail
    auto_stop = auto_sub.add_parser("stop", help="the plugin's Stop hook: judge the commits noted in this session (read from stdin), "
                                                 "and when the work is due ask Claude for the wrap-up")
    auto_stop.set_defaults(func=cmd_auto_stop, standalone=True)  # each noted worktree has a config of its own, which it loads itself

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
