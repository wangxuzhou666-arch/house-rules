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
