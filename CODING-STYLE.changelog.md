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
