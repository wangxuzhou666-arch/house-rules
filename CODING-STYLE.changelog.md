# Coding style changes

## Backfill — 2026-10-28 (proposals A–E)

These five changes were accepted on 2026-10-28 and recorded by hand in `CODING-STYLE.md` before this file existed. Entries written by `ratchet` follow.

### add PRJ.1

- Before: (new clause)
- After:
  ```markdown
  1. **[PRJ.1]** Importing a saved review result without executing a reviewer is a replay, not a new review round; every new execution must pass run-identity and round-limit checks before the reviewer is called. (should-fix) <!-- digest: review-ratchet only: import without execution is replay; reused IDs never exempt new execution from pre-call identity and round checks -->
     Reusing a run ID alone does not grant a replay exemption. Only an import path that does not execute a new review can use that exemption.
     **Why added (proposal A, accepted 2026-10-28):** Existing fallback rules do not define execution identity. The two findings establish complementary boundaries: do not count or block a genuine replay as a new round, and do not let a new reviewer execution bypass its checks by reusing an ID.
     **Finding sources:** `review-ratchet:l1:r1:r1-1`; `review-ratchet:l1:r2:r2-1`.
     **Examples:** Violates: calling the reviewer again with an old run ID to bypass the cap. Complies: importing the same saved output without calling the reviewer or consuming another round.
  ```
- Why: Existing fallback rules do not define execution identity. The two findings establish complementary boundaries: do not count or block a genuine replay as a new round, and do not let a new reviewer execution bypass its checks by reusing an ID.
- Evidence: `review-ratchet:l1:r1:r1-1` — `ratchet/cli.py:111` — The round-cap check runs before the replay check, so re-ingesting an already-recorded run is refused as if it were another round.
- Evidence: `review-ratchet:l1:r2:r2-1` — `ratchet/cli.py:123` — The replay exemption from the round cap is applied to real `claude` runs as well as `--from-file` ingests, so `--run <recorded id>` runs a paid review past the cap.
- Decision: accept (proposal A, 2026-10-28). Backfilled on 2026-10-29 from the clause text; no decision receipt exists.

### add PRJ.2

- Before: (new clause)
- After:
  ```markdown
  2. **[PRJ.2]** Attribute code findings only to clauses that define code violations, using the rules file's documented conventions to distinguish them from reviewer guidance. (should-fix) <!-- digest: review-ratchet only: map code clauses by file conventions for category and body refs; retain valid clauses where severity omission is allowed -->
     Where the file permits code-violation clauses without a declared severity, missing severity alone must not exclude a legitimate clause. Explicit category attribution and attribution inferred from body references must use the same criteria. Follow the file contract; this does not require a new clause-type field or forbid a documented severity-based convention in a mixed file.
     **Why added (proposal B, accepted 2026-10-28):** Excluding reviewer guidance is valid, but a blanket missing-severity filter also excludes valid clauses in files that omit severity declarations. The later counterexamples refine the same rule; two attribution paths are not two independent reasons to add rules.
     **Finding sources:** `review-ratchet:l1:r1:r1-2`; `review-ratchet:l1:r2:r2-2`; `review-ratchet:l1:r2:r2-3`.
     **Examples:** Violates: marking every finding unmapped solely because a custom rules file omits severity. Complies: excluding actual reviewer guidance under the documented file contract while retaining legitimate code clauses in both attribution paths.
  ```
- Why: Excluding reviewer guidance is valid, but a blanket missing-severity filter also excludes valid clauses in files that omit severity declarations. The later counterexamples refine the same rule; two attribution paths are not two independent reasons to add rules.
- Evidence: `review-ratchet:l1:r1:r1-2` — `ratchet/rules.py:223` — `first_clause_mention` builds its pattern from every clause id, including severity-less Reviewer rules (R.1..R.7), so a finding that cites one is attributed to a non-violation clause.
- Evidence: `review-ratchet:l1:r2:r2-2` — `ratchet/ingest.py:210` — A clause 'without a severity' is treated as a Reviewer rule, but `severity_of` also returns None for any ordinary clause in a rules file that has no severity tags and no family table. Those clauses are now rejected as categories and cannot be attributed from prose.
- Evidence: `review-ratchet:l1:r2:r2-3` — `ratchet/rules.py:225` — `_mention_re` uses the same 'severity is not None' filter. With no severity data in the rules file it becomes None, which silently turns off prose attribution for every clause.
- Decision: accept (proposal B, 2026-10-28). Backfilled on 2026-10-29 from the clause text; no decision receipt exists.

### add REL.1

- Before: (new clause)
- After:
  ```markdown
  1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Keep usable state until replacement is ready and validated; retain or restore it on failure; disposable rebuildable temporary artifacts are exempt -->
     This applies to installed tools, configuration and deployment artifacts with existing usable state. Disposable temporary artifacts that can be discarded and rebuilt are exempt. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
     **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
     **Finding source:** `review-ratchet:l1:r1:r1-4`.
     **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.
  ```
- Why: Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
- Evidence: `review-ratchet:l1:r1:r1-4` — `ratchet/vendor.py:99` — `vendor_into` deletes the existing vendored entries before copying the new ones, so a copy failure leaves the target with no working tool.
- Decision: accept (proposal C, 2026-10-28). Backfilled on 2026-10-29 from the clause text; no decision receipt exists.

### add REL.2

- Before: (new clause)
- After:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Persistent ID changes need compatible reads or migration and old-identity checks; display-only changes preserving stored identity are exempt -->
     A display-format change that does not change persistent identity is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- Why: An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
- Evidence: `review-ratchet:l1:r1:r1-5` — `ratchet/ingest.py:252` — The display_id format changed from zero-padded (`r1-001`) to unpadded (`r1-1`) with no migration, which breaks replay idempotence for ledgers written by the previous version.
- Decision: accept (proposal D, 2026-10-28). Backfilled on 2026-10-29 from the clause text; no decision receipt exists.

### rewrite R.7

- Before:
  ```markdown
  7. **[R.7]** A new gate or rule needs a failing example that proves it fires. <!-- ADJUST: drop this clause if you do not write your own lint gates. -->
     A gate that runs green proves nothing, because a gate that always returns green also runs green. The proof is to build what it should catch and watch it turn red. Give every rule at least one positive example (must fail) and one negative (must pass), starting with the exceptions the rule states explicitly, since those are what a later tightening breaks first. Reverting to the old behavior must turn it green again, so the red is caused by the new part. Pin each positive to the specific rule that fired. With several rules, disable them one at a time and confirm only that rule's example goes green. Commit the tests; a one-off manual run proves nothing later.
  ```
- After:
  ```markdown
  7. **[R.7]** A new gate or rule needs a failing example that proves it fires. <!-- digest: Prove gates with fail/pass, reversal and per-rule mutation checks; isolate unrelated probe effects; explicitly authorize real integration effects --> <!-- ADJUST: drop this clause if you do not write your own lint gates. -->
     A gate that runs green proves nothing, because a gate that always returns green also runs green. The proof is to build what it should catch and watch it turn red. Give every rule at least one positive example (must fail) and one negative (must pass), starting with the exceptions the rule states explicitly, since those are what a later tightening breaks first. Reverting to the old behavior must turn it green again, so the red is caused by the new part. Pin each positive to the specific rule that fired. With several rules, disable them one at a time and confirm only that rule's example goes green. Commit the tests; a one-off manual run proves nothing later.
     When performing this validation or installation probes, isolate writes and external actions unrelated to the validation purpose. Isolating the Git index does not isolate the working tree, network or deployment behavior. Real integration validation must have an explicitly authorized side-effect scope; a probe must not accidentally deploy, push or execute user scripts.
     **Why amended (proposal E, accepted 2026-10-28):** Effective validation needs both evidence that the check works and a boundary around its side effects. This extends the existing testing guidance without adding a code-violation severity or a separate rule.
     **Finding source:** `review-ratchet:l1:r1:r1-8`.
     **Examples:** Violates: an installation probe unexpectedly executes a user's deployment script. Complies: isolated synthetic fixtures, or a real integration check whose side effects are explicitly authorized and bounded.
  ```
- Why: Effective validation needs both evidence that the check works and a boundary around its side effects. This extends the existing testing guidance without adding a code-violation severity or a separate rule.
- Evidence: `review-ratchet:l1:r1:r1-8` — `ratchet/hooks.py:295` — `probe_hook` runs the user's real hook script during `ratchet init`, and the sandbox only covers the git index.
- Decision: accept (proposal E, 2026-10-28). Backfilled on 2026-10-29 from the clause text; no decision receipt exists.

## Revision 1 — 2026-10-30 (batch 16036bf116d3700c8b95050ff3fa7224e90da84e0bc11d5faaa3c04df2ab31ad)

### add REL.3

- Before: (new clause)
- After:
  ```markdown
  3. **[REL.3]** Don't guard only the syntax of a state file you read back: a wrong shape must take the same fallback, or the same named error, as a parse failure. (should-fix) <!-- digest: Don't guard only a state file's syntax: a wrong shape takes the same fallback or named error as a parse failure, else one bad file fails every run -->
     Scope: a file a program wrote on an earlier run and reads on a later one (state, cache, saved records), which a person, an older version or a crash may have left in another shape. Input from the host or the caller is outside this rule.
     Violates: `except ValueError` around `json.loads`, then `.items()` on a list. Complies: an `isinstance` check that takes the parse-failure path, so the next save repairs the file.
  ```
- Why: No clause says that a syntax-only guard on a file read back is incomplete: P0F.1 and P0F.2 name the boundary and the explicit fallback, and the reviewer left this finding unmapped. Two sibling readers already check shape (PendingCommit.from_line, read_start_points); this one missed it, and unlike a bad input a bad state file fails every later run.
- Evidence: `review-ratchet:l1:r3:r3-2` — `ratchet/auto_stop.py:198` — A damaged `announced.json` that is valid JSON but the wrong shape makes `_read_announced` raise on every refusal, and nothing ever rewrites the file to fix it.
  ```
          log_failure(f"auto stop: {path} is damaged, so its refusals count as new: {exc}")
          return {}
      cutoff = datetime.now(timezone.utc) - ANNOUNCED_KEEP
      return {key: when for key, when in told.items() if datetime.fromisoformat(when) >= cutoff}


  def _announced_key(due: Due, refusal: Refusal) -> str:
  ```
- Decision: accept by Colar — "接受 (Recommended)"

### retire R.5

- Before:
  ```markdown
  5. **[R.5]** Tells of generated code, reported under the matching P clause: one try around everything, null checks on values that cannot be null, one-implementation base classes, docstrings that repeat the name, "just in case" defaults, boilerplate foreign to this repo. <!-- digest: Generated-code tells, report under the P clause: one big try, needless null checks, single-implementation base class, echo docstring, "just in case" default -->
  ```
- After: (retired)
- Why: Pays for REL.3 (a1), which cannot fit alone: the digest has 9 bytes left and REL.3 needs 168. Each of the five tells in R.5's digest line maps onto a digest line of its own (P0E.1, P0F.3, P0F.4, P2.1, P3.1), so retiring it frees 163 bytes in one decision, where five drafted shortening rewrites (PRJ.1, PRJ.2, REL.1, REL.2, R.7) free only 106 bytes in total. Lost: the generated-code framing, the 'boilerplate foreign to this repo' tell (full text only, never in the digest) and the 'report under the P clause' routing, which the Labeling section already gives. r3-7 and r3-8 get no clause now; they stay open and are cited only because every change must cite a finding.
- Evidence: `review-ratchet:l1:r3:r3-7` — `ratchet/auto.py:128` — `hook.log` is append-only with no size cap or rotation, while `prune()` cleans only `pending/` and `claimed/`.
  ```
      state.mkdir(mode=0o700, exist_ok=True)
      if subdir is None:
          return state
      directory = state / subdir
      directory.mkdir(mode=0o700, exist_ok=True)
      return directory
  ```
- Evidence: `review-ratchet:l1:r3:r3-8` — `ratchet/auto_stop.py:218` — The read-modify-write of `announced.json` has no lock. The scratch name is per-process, but two concurrent Stops can both read the old file and overwrite each other.
  ```
      if fresh:
          now = utc_timestamp()
          told.update({_announced_key(due, refusal): now for due, refusal in fresh})
          scratch = path.with_name(f"{path.name}.{os.getpid()}")
          scratch.write_text(json.dumps(told, indent=2) + "\n", encoding="utf-8")
          scratch.replace(path)  # a reader never sees half a file
      return fresh
  ```
- Decision: accept by Colar — "接受退役 (Recommended)"

## Revision 2 — 2026-10-30 (batch 17d090f41337b1e3b123b207eaf4bc7e0af512baeccc1b18c0656c10df1336c9)

### add REL.4

- Before: (new clause)
- After:
  ```markdown
  4. **[REL.4]** Don't count a cap's use only at the end, on success or by opt-in: record each attempt as it starts, in code every caller passes through. (should-fix) <!-- digest: Don't count a cap's use at the end, on success only or by opt-in: count each attempt as it starts -->
     Scope: a limit on something billed, rate-limited or repeated (a daily review cap, a round limit, a quota). A cap checked when the work starts but counted when it ends lets concurrent runs pass the check together; one counted only on success lets a failure after the spend go free; one that counts only the calls a caller marks lets a caller that forgets the mark escape. An attempt refused before it spends anything is not counted.
     Violates: `if used < limit: call(); record(used + 1)`, or `record()` after `call()` returns. Complies: record the attempt, atomically with the check, before `call()`; keep it when the call fails; do it in the code path every caller takes.
  ```
- Why: REL.4 itself: no clause says when a cap's use must be counted (PRJ.1 pins identity and round checks before the call; P0F.1 and P0F.2 cover input and fallbacks). One mechanism sits behind three findings: a cap charged late (r4-2, cited here), only on success (r4-3, cited under a2) or only by opt-in (r4-4, cited under a3), instead of at the spend. A finding can be cited in one group only, which is why a2 and a3, the two groups that make room for REL.4, carry r4-3 and r4-4. REL.4 does not fit alone (digest 4092 + 119 > 4096): it needs both a2 and a3, and an apply without them is refused.
- Evidence: `review-ratchet:l1:r4:r4-2` — `ratchet/review.py:308` — The daily cap is checked when a review starts, but the counting `review_run` is written only after claude finishes, so concurrent automatic reviews can exceed the cap.
  ```
      if used + 1 > config.max_review_rounds and not over_cap_allowed and not replayed:
          return Refusal(REFUSE_ROUND_CAP, key, round_cap_message(key, used, config.max_review_rounds, len(open_findings(events))))
      if from_file:
          return None
      capped = daily_cap_refusal(config, events, datetime.now().astimezone()) if automatic else None
      if capped is not None:
          return capped
  ```
- Decision: accept by Colar — "三条都接受 (Recommended)"

### rewrite P0E.3

- Before:
  ```markdown
  3. **[P0E.3]** Catch-all handlers are legal only in the cases below, and the line must name the case and the reason. <!-- digest: Catch-all only for: side channel, optional extra, batch isolation, top-level handler, rethrow with cause; name the case on the line --> <!-- ADJUST: the legal-case whitelist. Shorten or extend it, and keep the digest comment in step. -->
     - **Side channel**: masking, metrics or telemetry must not take down the main flow.
     - **Optional extra**: a field the conclusion does not depend on (a usage count recorded as 0).
     - **Batch isolation**: one bad item must not sink the batch; log that item's identity.
     - **Top-level handler**: the outermost layer of a server or worker, which logs the full traceback so the process survives.
     - **Rethrow with cause**: a retry wrapper that raises the last error, or a boundary that translates a parse failure into a client error and keeps the original as its cause. The test is that the exception's identity is not lost. Narrow the type instead where you can.
  ```
- After:
  ```markdown
  3. **[P0E.3]** Catch-all handlers are legal only in the cases below, and the line must name the case and the reason. <!-- digest: Catch-all only for: side channel, optional extra, batch isolation, top-level handler, rethrow with cause; name the case on the line --> <!-- ADJUST: the legal-case whitelist. Shorten or extend it, and keep the digest comment in step. -->
     - **Side channel**: masking, metrics or telemetry must not take down the main flow.
     - **Optional extra**: a field the conclusion does not depend on (a usage count recorded as 0).
     - **Batch isolation**: one bad item must not sink the batch; log that item's identity.
     - **Top-level handler**: the outermost layer of a server or worker, which logs the full traceback so the process survives.
     - **Rethrow with cause**: a retry wrapper that raises the last error, or a boundary that translates a parse failure into a client error and keeps the original as its cause. The test is that the exception's identity is not lost. Narrow the type instead where you can.

     A lint suppression on a broad catch is that line: a bare one, with no case and reason, is not accepted. <!-- ADJUST: e.g. a bare `noqa: BLE001` in ruff or an eslint-disable without a description. -->
  ```
- Why: Pays for REL.4 (a1, drawn from r4-2, r4-3 and r4-4; r4-3 is cited here because a finding may sit in one group only). REL.4 is 115 bytes over the digest budget on its own; this merge frees 75 and a3 frees 52. P0E.4 (a bare lint suppression on a broad catch is not accepted) is already implied by P0E.3, whose line 'must name the case and the reason': a suppression comment is that line. The survivor P0E.3 keeps its digest line and gains P0E.4's sentence and ADJUST note. Lost: P0E.4's own digest line and the ID P0E.4, which is never reused.
- Evidence: `review-ratchet:l1:r4:r4-3` — `ratchet/cli.py:249` — Only a successful review writes an `auto`-triggered review_run, so a billed automatic review that fails is never counted against the daily cap.
  ```
      print("\n".join(findings_table(fresh)) if fresh else "no new findings")
      run_event = review_run_event(config.repo_slug, now_iso(), run_id=run_id, key=key, round_number=round_number,
                                   max_rounds=config.max_review_rounds, over_cap=over_cap, finding_count=finding_count,
                                   level=level, head=head, reviewer_version=reviewer_version,
                                   version_changed=replaced_version is not None, from_file=bool(args.from_file),
                                   trigger=TRIGGER_AUTO if automatic else None,
  [snippet truncated]
  ```
- Decision: accept by Colar — "三条都接受 (Recommended)"

### retire P0E.4

- Before:
  ```markdown
  4. **[P0E.4]** A bare lint suppression on a broad catch is not accepted; it must carry the case and reason. <!-- ADJUST: e.g. a bare `noqa: BLE001` in ruff or an eslint-disable without a description. -->
  ```
- After: (retired)
- Why: Pays for REL.4 (a1, drawn from r4-2, r4-3 and r4-4; r4-3 is cited here because a finding may sit in one group only). REL.4 is 115 bytes over the digest budget on its own; this merge frees 75 and a3 frees 52. P0E.4 (a bare lint suppression on a broad catch is not accepted) is already implied by P0E.3, whose line 'must name the case and the reason': a suppression comment is that line. The survivor P0E.3 keeps its digest line and gains P0E.4's sentence and ADJUST note. Lost: P0E.4's own digest line and the ID P0E.4, which is never reused.
- Evidence: `review-ratchet:l1:r4:r4-3` — `ratchet/cli.py:249` — Only a successful review writes an `auto`-triggered review_run, so a billed automatic review that fails is never counted against the daily cap.
  ```
      print("\n".join(findings_table(fresh)) if fresh else "no new findings")
      run_event = review_run_event(config.repo_slug, now_iso(), run_id=run_id, key=key, round_number=round_number,
                                   max_rounds=config.max_review_rounds, over_cap=over_cap, finding_count=finding_count,
                                   level=level, head=head, reviewer_version=reviewer_version,
                                   version_changed=replaced_version is not None, from_file=bool(args.from_file),
                                   trigger=TRIGGER_AUTO if automatic else None,
  [snippet truncated]
  ```
- Decision: accept by Colar — "三条都接受 (Recommended)"

### rewrite P0E.2

- Before:
  ```markdown
  2. **[P0E.2]** Catch a specific exception type, with its own action. One response for every type is suppressing, not handling.
  ```
- After:
  ```markdown
  2. **[P0E.2]** Catch a specific exception type, with its own action. One response for every type is suppressing, not handling. Where the action is to do nothing, replace the empty handler with an explicit suppression that names the exception type. <!-- digest: Catch a specific exception type, with its own action; if none, an explicit suppression naming the type --> <!-- ADJUST: e.g. `contextlib.suppress(KeyError)` (ruff SIM105). -->
  ```
- Why: Pays for REL.4 (a1, drawn from r4-2, r4-3 and r4-4; r4-4 is cited here because a finding may sit in one group only). This merge frees 52 bytes and a2 frees 75, against the 115 REL.4 is over. P0E.6 (replace an empty handler with an explicit suppression that names the type) is the do-nothing case of P0E.2 (a specific type with its own action). P0E.2 gains P0E.6's sentence and ADJUST note and a digest line that still names the case (71 to 121 bytes, P0E.6's 102 removed). Lost: P0E.6's own digest line and the ID P0E.6, which is never reused.
- Evidence: `review-ratchet:l1:r4:r4-4` — `skills/wrapup/SKILL.md:12` — The cap counts only reviews whose command carries `--auto`, and that flag is added only if the model follows a text instruction. The enforcement is therefore prompt-dependent.
  ```
  **Automatic mode** (`$ARGUMENTS` starts with `auto <worktree>`): the Stop hook asked for this wrap-up and nobody typed it. In step 1 run `ratchet wrapup --repo <worktree> --json --auto` with the Bash tool's `run_in_background` set, as a review takes minutes and a foreground call is cut off at its timeout; `--auto` marks the review as automatic, which the daily cap counts. Wait for the completion notice (do not poll or sleep) and read the output file it names; the JSON object is the last thing in it. Everything below applies, except that `skipped`, `capped`, `daily_capped` and an error get one 
  [snippet truncated]
  ```
- Decision: accept by Colar — "三条都接受 (Recommended)"

### retire P0E.6

- Before:
  ```markdown
  6. **[P0E.6]** Replace an empty handler with an explicit suppression that names the exception type. <!-- ADJUST: e.g. `contextlib.suppress(KeyError)` (ruff SIM105). -->
  ```
- After: (retired)
- Why: Pays for REL.4 (a1, drawn from r4-2, r4-3 and r4-4; r4-4 is cited here because a finding may sit in one group only). This merge frees 52 bytes and a2 frees 75, against the 115 REL.4 is over. P0E.6 (replace an empty handler with an explicit suppression that names the type) is the do-nothing case of P0E.2 (a specific type with its own action). P0E.2 gains P0E.6's sentence and ADJUST note and a digest line that still names the case (71 to 121 bytes, P0E.6's 102 removed). Lost: P0E.6's own digest line and the ID P0E.6, which is never reused.
- Evidence: `review-ratchet:l1:r4:r4-4` — `skills/wrapup/SKILL.md:12` — The cap counts only reviews whose command carries `--auto`, and that flag is added only if the model follows a text instruction. The enforcement is therefore prompt-dependent.
  ```
  **Automatic mode** (`$ARGUMENTS` starts with `auto <worktree>`): the Stop hook asked for this wrap-up and nobody typed it. In step 1 run `ratchet wrapup --repo <worktree> --json --auto` with the Bash tool's `run_in_background` set, as a review takes minutes and a foreground call is cut off at its timeout; `--auto` marks the review as automatic, which the daily cap counts. Wait for the completion notice (do not poll or sleep) and read the output file it names; the JSON object is the last thing in it. Everything below applies, except that `skipped`, `capped`, `daily_capped` and an error get one 
  [snippet truncated]
  ```
- Decision: accept by Colar — "三条都接受 (Recommended)"

## Rename note — 2026-10-31

The tool these rules are run with was renamed from review-ratchet to house-rules (package `house_rules`, command `house-rules`). The rename is a hand edit, not a proposal batch: it adds no clause, retires none and changes no requirement, so it has no revision number, no receipt and no decision to record. What it changed in `CODING-STYLE.md`:

- Prose and the PRJ heading name house-rules instead of review-ratchet, and the ADJUST note names the `house-rules compile` command.
- The `digest:` overrides of PRJ.1 and PRJ.2 now begin `house-rules only:` instead of `review-ratchet only:`, so the compiled digest changed although no requirement did.
- The three retirement markers (P0E.4, P0E.6, R.5) now read `<!-- house-rules-retired: ID -->`, the form the tool reads to keep a retired ID from being reused. Until this edit it did not see these three markers, so those IDs were not reserved against reuse.

The Backfill and Revision entries above are left as written: they use the old name, the old `ratchet/` source paths in their evidence lines and the finding IDs `review-ratchet:l1:...`, which are persistent ledger IDs and are not rewritten. Numbering still continues from the highest Revision heading, so the next batch is Revision 3.

## Labeling note — 2026-10-31

The Labeling section that `house-rules compile` appends to every digest was reworded in the generator (`house_rules/digest.py`), not in `CODING-STYLE.md`. It adds no clause, retires none and changes no clause's requirement, so it has no revision number, no receipt and no decision to record. What changed in the compiled digest:

- The lead now says every finding object in the JSON array MUST carry both keys and that findings are filed by `category`, instead of naming the two keys as extras besides `file`, `line`, `summary` and `failure_scenario`.
- The `category` bullet now sends a finding to `unmapped` also when a clause's exemption applies, and "Prefer `unmapped` over stretching a clause to fit" became "Never stretch a clause to fit."

Why: in the first 24 runs of the generalization check (`samples/generalization/`, round 1), 8 carried no `category` on any finding, and controls covered by a stated exemption were labeled with the clause. The digest shrank from 4082 to 4047 bytes. Numbering still continues from the highest Revision heading, so the next batch is Revision 3.

## Revision 3 — 2026-10-31 (batch 9c7774bfeca198cdb3176e4a2b7255ec9b8d7358dd8f280b673deafdb47a1ea1)

### rewrite REL.1

- Before:
  ```markdown
  1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Keep usable state until replacement is ready and validated; retain or restore it on failure; disposable rebuildable temporary artifacts are exempt -->
     This applies to installed tools, configuration and deployment artifacts with existing usable state. Disposable temporary artifacts that can be discarded and rebuilt are exempt. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
     **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
     **Finding source:** `review-ratchet:l1:r1:r1-4`.
     **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.
  ```
- After:
  ```markdown
  1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Exempt: a cache or build output rebuilt on demand. Otherwise keep usable state until replacement is ready and validated; retain or restore it on failure -->
     This applies to installed tools, configuration and deployment artifacts with existing usable state. A cache or build output rebuilt on demand is exempt, however many entries it holds. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
     **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
     **Finding source:** `review-ratchet:l1:r1:r1-4`.
     **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.
  ```
- Why: Exempt control (cache rebuilt on demand; oracle ok), flagged in 3 of 3 runs: the exemption came last and named no kind. Lead with it and name the kind.
- Evidence: `house-rules:l2:g1-rel1-w1:g1-rel1-w1-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:23` — rebuild() removes the whole cache before re-rendering anything, so a render failure leaves the cache empty or partial.
- Evidence: `house-rules:l2:g1-rel1-w2:g1-rel1-w2-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:25` — rebuild() deletes the whole cache before re-rendering, so any render error leaves the cache partially or fully empty and the previous usable previews are lost (REL.1).
- Evidence: `house-rules:l2:g1-rel1-w3:g1-rel1-w3-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:24` — rebuild() deletes the whole cache directory before re-rendering anything, so a mid-rebuild failure loses every cached preview.
- Decision: accept by Colar — "采纳 (Recommended)"

### rewrite REL.2

- Before:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Persistent ID changes need compatible reads or migration and old-identity checks; display-only changes preserving stored identity are exempt -->
     A display-format change that does not change persistent identity is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- After:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Exempt: a new label or format over the same stored key. Otherwise a stored key (dedup, links, closing) change needs compatible reads or migration and old-identity checks -->
     A new label or display format over the same stored key and fields is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- Why: Exempt control (new label, same stored uid; oracle ok), flagged in 2 of 3 runs: the exemption came last. Lead with it and restore the key qualifier (dedup, links, closing).
- Evidence: `house-rules:l2:g1-rel2-w1:g1-rel2-w1-6` — `samples/generalization/cases/rel-2/code/reading_list.py:20` — Bumping LABEL_STYLE to 2 leaves old rows displaying in a different format from new rows, so one feed mixes `host-0001` and `host#2` labels.
- Evidence: `house-rules:l2:g1-rel2-w2:g1-rel2-w2-7` — `samples/generalization/cases/rel-2/code/reading_list.py:4` — `LABEL_STYLE` is bumped to 2 but old rows are not migrated, and the label text changes from zero-padded `host-0001` to `host#1`.
- Decision: accept by Colar — "采纳 (Recommended)"

## Revision 4 — 2026-10-31 (batch 1e60809fba01badaeddf6f7e76de91e751be361fba33a1c8c3bbf6d85f3dd6de)

### rewrite REL.2

- Before:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Exempt: a new label or format over the same stored key. Otherwise a stored key (dedup, links, closing) change needs compatible reads or migration and old-identity checks -->
     A new label or display format over the same stored key and fields is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- After:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Exempt: a new label or format over the same stored key. Otherwise a stored-ID change needs compatible reads or migration and old-identity checks -->
     A new label or display format over the same stored key and fields is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- Why: Make room to restore the earlier Labeling text, which round 2 showed is needed for the `category` key (written in 1 of 18 with_rule runs with the shorter text). The full clause keeps the dedup/links/closing scope.
- Evidence: `house-rules:l2:g1-rel2-w1:g1-rel2-w1-6` — `samples/generalization/cases/rel-2/code/reading_list.py:20` — Bumping LABEL_STYLE to 2 leaves old rows displaying in a different format from new rows, so one feed mixes `host-0001` and `host#2` labels.
- Evidence: `house-rules:l2:g1-rel2-w2:g1-rel2-w2-7` — `samples/generalization/cases/rel-2/code/reading_list.py:4` — `LABEL_STYLE` is bumped to 2 but old rows are not migrated, and the label text changes from zero-padded `host-0001` to `host#1`.
- Decision: accept by Colar — "采纳 (Recommended)"

## Labeling note, reverted — 2026-10-31

The Labeling rewording recorded in the "Labeling note — 2026-10-31" above is reverted in the generator (`house_rules/digest.py`): the compiled Labeling section is again the one used before that note, byte for byte. Like the note it reverts, this changes no clause and has no revision number.

Why: in round 2 of the generalization check (`samples/generalization/`), recorded with the reworded section, 17 of 18 `with_rule` runs carried no `category` on any finding, against 7 of 21 in round 1 with the earlier section. Revision 4 above shortened the REL.2 digest line first so that the restored section fits the byte budget; the digest is 4092 of 4096 bytes. Numbering still continues from the highest Revision heading, so the next batch is Revision 5.

## Revision 5 — 2026-10-31 (batch e74102c6ba722cd1bf3d83e8ad2a9ac6be6271cf6359216dc17e85ec4f55768f)

### rewrite REL.1

- Before:
  ```markdown
  1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Exempt: a cache or build output rebuilt on demand. Otherwise keep usable state until replacement is ready and validated; retain or restore it on failure -->
     This applies to installed tools, configuration and deployment artifacts with existing usable state. A cache or build output rebuilt on demand is exempt, however many entries it holds. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
     **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
     **Finding source:** `review-ratchet:l1:r1:r1-4`.
     **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.
  ```
- After:
  ```markdown
  1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Keep usable state until replacement is ready and validated; retain or restore it on failure; disposable rebuildable temporary artifacts are exempt -->
     This applies to installed tools, configuration and deployment artifacts with existing usable state. Disposable temporary artifacts that can be discarded and rebuilt are exempt. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
     **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
     **Finding source:** `review-ratchet:l1:r1:r1-4`.
     **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.
  ```
- Why: Round 3 (digest differing from round 1 only in these lines) showed no improvement: exempt controls labeled as often as in round 1 (rel-1 3/3, rel-1b 2/3); restore the round-1 wording.
- Evidence: `house-rules:l2:g1-rel1-w1:g1-rel1-w1-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:23` — rebuild() removes the whole cache before re-rendering anything, so a render failure leaves the cache empty or partial.
- Evidence: `house-rules:l2:g1-rel1-w2:g1-rel1-w2-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:25` — rebuild() deletes the whole cache before re-rendering, so any render error leaves the cache partially or fully empty and the previous usable previews are lost (REL.1).
- Evidence: `house-rules:l2:g1-rel1-w3:g1-rel1-w3-2` — `samples/generalization/cases/rel-1/code/preview_cache.py:24` — rebuild() deletes the whole cache directory before re-rendering anything, so a mid-rebuild failure loses every cached preview.
- Decision: accept by Colar — "回滚到第一轮措辞 (Recommended)"

### rewrite REL.2

- Before:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Exempt: a new label or format over the same stored key. Otherwise a stored-ID change needs compatible reads or migration and old-identity checks -->
     A new label or display format over the same stored key and fields is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- After:
  ```markdown
  2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Persistent ID changes need compatible reads or migration and old-identity checks; display-only changes preserving stored identity are exempt -->
     A display-format change that does not change persistent identity is exempt.
     **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
     **Finding source:** `review-ratchet:l1:r1:r1-5`.
     **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.
  ```
- Why: Round 3 (digest differing from round 1 only in these lines) showed no clear gain: exempt controls labeled in 1 of 6 runs vs 2 of 6 in round 1, violations in 1 of 6 vs 3 of 6; restore the round-1 wording.
- Evidence: `house-rules:l2:g1-rel2-w1:g1-rel2-w1-6` — `samples/generalization/cases/rel-2/code/reading_list.py:20` — Bumping LABEL_STYLE to 2 leaves old rows displaying in a different format from new rows, so one feed mixes `host-0001` and `host#2` labels.
- Evidence: `house-rules:l2:g1-rel2-w2:g1-rel2-w2-7` — `samples/generalization/cases/rel-2/code/reading_list.py:4` — `LABEL_STYLE` is bumped to 2 but old rows are not migrated, and the label text changes from zero-padded `host-0001` to `host#1`.
- Decision: accept by Colar — "回滚到第一轮措辞 (Recommended)"
