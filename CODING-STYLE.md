# Coding Style (DRAFT template)

<!-- ADJUST: a starting point, not a standard. Every knob below is marked ADJUST. Change thresholds, tool codes and examples to what your repo enforces, and delete the clauses you will not hold people to. After editing, run `house-rules compile` to regenerate the digest. -->

> Readers: engineers and coding agents who write code, and reviewers (human or model) who judge it.
> Role: the single source of truth for review standards. A reviewer does not invent thresholds; they live here.
> Layering: this repository copy includes the baseline and explicitly scoped project rules. PRJ clauses apply only to house-rules. Local adoption does not publish or synchronize a rule across projects. If rules are split into files, list the shared baseline first and project rules after it in `rules_files`; later files add clauses, never override.
> Target: four kinds of bloat that generated code tends to produce: broad catches that swallow errors, fallbacks that disguise failure, abstractions with one implementation, and comments that restate the code.

## 0. Enforcement tiers

<!-- ADJUST: name the real tools your repo uses for the mechanical tier. -->

| tier | who runs it | when | what it covers |
|---|---|---|---|
| Mechanical | linter plus a small syntax-tree script, in pre-commit | every commit, staged lines only | anything decidable from a rule code or the syntax tree |
| Judgment | the reviewer | every diff | anything that needs the surrounding context |
| Backlog | a periodic audit | per batch or milestone | historical debt, cleaned in bulk; never in pre-commit, or it is red every day and gets bypassed |

Which tier a rule belongs to: if it holds in every session, is unrelated to the task and is unambiguous, make it mechanical; otherwise it is judgment.

<!-- ADJUST: the family list. Add or remove families here and in the severity table under R.4. -->
Clause IDs: `P0E.n` exceptions, `P0F.n` fallbacks, `P1.n` complexity, `P2.n` abstraction, `P3.n` comments, `PRJ.n` house-rules project contracts, `REL.n` state and identity reliability, `R.n` reviewer conduct. A linter finding that matches no clause is recorded as `<tool>:<code>`.

## P0 · Exceptions: keep the guarded block small, and give every broad catch an identity

**Why**: the wider the `try`, the more different failures collapse into one handler. A timeout (retry), a missing key (a bug) and a failed write (alert) become the same log line, and a `return None` after it makes the failure look like success.

1. **[P0E.1]** Put only the line you expect to fail inside `try`; move the rest after the block. <!-- ADJUST: language tool code, e.g. ruff TRY300 flags a return inside try. -->
2. **[P0E.2]** Catch a specific exception type, with its own action. One response for every type is suppressing, not handling. Where the action is to do nothing, replace the empty handler with an explicit suppression that names the exception type. <!-- digest: Catch a specific exception type, with its own action; if none, an explicit suppression naming the type --> <!-- ADJUST: e.g. `contextlib.suppress(KeyError)` (ruff SIM105). -->
3. **[P0E.3]** Catch-all handlers are legal only in the cases below, and the line must name the case and the reason. <!-- digest: Catch-all only for: side channel, optional extra, batch isolation, top-level handler, rethrow with cause; name the case on the line --> <!-- ADJUST: the legal-case whitelist. Shorten or extend it, and keep the digest comment in step. -->
   - **Side channel**: masking, metrics or telemetry must not take down the main flow.
   - **Optional extra**: a field the conclusion does not depend on (a usage count recorded as 0).
   - **Batch isolation**: one bad item must not sink the batch; log that item's identity.
   - **Top-level handler**: the outermost layer of a server or worker, which logs the full traceback so the process survives.
   - **Rethrow with cause**: a retry wrapper that raises the last error, or a boundary that translates a parse failure into a client error and keeps the original as its cause. The test is that the exception's identity is not lost. Narrow the type instead where you can.

   A lint suppression on a broad catch is that line: a bare one, with no case and reason, is not accepted. <!-- ADJUST: e.g. a bare `noqa: BLE001` in ruff or an eslint-disable without a description. -->
<!-- house-rules-retired: P0E.4 -->
5. **[P0E.5]** Log inside a handler with the call that keeps the stack trace, not the one that drops it. <!-- ADJUST: e.g. `logger.exception` rather than `logger.error` (ruff TRY400); `console.error(err)` with the error object. -->
<!-- house-rules-retired: P0E.6 -->

## P0 · Fallbacks: a failure must not pass for a success

**Why**: `return None`, `or default` and an `else` that can never run rewrite "something broke" as "looks fine". It is the other half of a broad catch: one loses the information, the other hides that it is gone.

1. **[P0F.1]** Fail fast: assert on programmer errors, validate input only at the boundary. <!-- ADJUST: boundary means your request handlers, config parsing and external data entry. -->
2. **[P0F.2]** Degrade explicitly: log it and name the fallback value, never a silent `None`.
3. **[P0F.3]** Do not over-protect: no defensive branch for something that fails one time in a hundred.
4. **[P0F.4]** Check for empty only where the value can really be empty; trust the type annotation.

## P1 · Complexity: nesting depth is the signal, line count is not

**Why**: reading code means holding "which conditions am I inside" in working memory, which fits about four chunks. At depth N the innermost line needs N conditions to hold at once. A long but flat function (a table of independent ifs) is long, not complex.

1. **[P1.1]** Nesting depth of 5 or more is a violation. Fix in order: guard clause, extract the inner block, dispatch table. <!-- ADJUST: the depth threshold. Count if / for / while / try / with levels. -->
2. **[P1.2]** Advisory only: over 50 lines and cyclomatic complexity over 15 deserves a look. Data-heavy functions are exempt. (nice) <!-- ADJUST: both thresholds, and the tool (e.g. ruff C901). Keep it advisory unless your repo's data says length predicts defects. -->
3. **[P1.3]** A branch that assigns or returns needs an `else`, except a guard clause.

Meta-rule: a proxy metric does not transfer between domains. Test a threshold against your own repo before adopting it.

## P2 · Abstraction: one implementation is a wrapper

**Why**: an abstraction earns its keep by unifying implementations that already exist. With one, it unifies an imagined future and adds an indirection for nothing.

1. **[P2.1]** A base class, protocol or factory needs at least two real implementations.
2. **[P2.2]** Abstract on the third repetition, never for a hypothetical future need.
3. **[P2.3]** No config layer, plugin system or registry for a one-off need.
4. **[P2.4]** Qualify generic names: `data`, `result`, `info`, `tmp`, `manager`, `handler`, `util`. <!-- ADJUST: your own banned-name list. -->
5. **[P2.5]** Name symmetric pairs symmetrically (`start`/`stop`, `open`/`close`, `send`/`recv`). <!-- ADJUST: boolean naming is judgment, not mechanical. A boolean whose name reads like a noun or verb (`timeout`, `write`) should become `should_timeout` or `is_write_mode`; do not demand a prefix on idioms like `dry_run`, `force` or `truncated`. -->

## P3 · Comments: write why, not what

**Why**: the code already says what it does. Repeating it is noise that buries the comments that matter: why this and not the alternative, which trap, which measured number.

1. **[P3.1]** No comments that restate the code or the function name.
2. **[P3.2]** Comment the non-obvious: a decision, a pitfall, an ordering constraint.
3. **[P3.3]** Comments use the project's agreed language. <!-- ADJUST: pick one. -->
4. **[P3.4]** No debugging leftovers in a commit: stray prints, temporary TODOs, commented-out code.

## PRJ · Project contracts (house-rules only)

These clauses are judgment rules for review execution and finding attribution in house-rules. Both have explicit `should-fix` severity; neither adds a mechanical gate.

1. **[PRJ.1]** Importing a saved review result without executing a reviewer is a replay, not a new review round; every new execution must pass run-identity and round-limit checks before the reviewer is called. (should-fix) <!-- digest: house-rules only: import without execution is replay; reused IDs never exempt new execution from pre-call identity and round checks -->
   Reusing a run ID alone does not grant a replay exemption. Only an import path that does not execute a new review can use that exemption.
   **Why added (proposal A, accepted 2026-10-28):** Existing fallback rules do not define execution identity. The two findings establish complementary boundaries: do not count or block a genuine replay as a new round, and do not let a new reviewer execution bypass its checks by reusing an ID.
   **Finding sources:** `review-ratchet:l1:r1:r1-1`; `review-ratchet:l1:r2:r2-1`.
   **Examples:** Violates: calling the reviewer again with an old run ID to bypass the cap. Complies: importing the same saved output without calling the reviewer or consuming another round.

2. **[PRJ.2]** Attribute code findings only to clauses that define code violations, using the rules file's documented conventions to distinguish them from reviewer guidance. (should-fix) <!-- digest: house-rules only: map code clauses by file conventions for category and body refs; retain valid clauses where severity omission is allowed -->
   Where the file permits code-violation clauses without a declared severity, missing severity alone must not exclude a legitimate clause. Explicit category attribution and attribution inferred from body references must use the same criteria. Follow the file contract; this does not require a new clause-type field or forbid a documented severity-based convention in a mixed file.
   **Why added (proposal B, accepted 2026-10-28):** Excluding reviewer guidance is valid, but a blanket missing-severity filter also excludes valid clauses in files that omit severity declarations. The later counterexamples refine the same rule; two attribution paths are not two independent reasons to add rules.
   **Finding sources:** `review-ratchet:l1:r1:r1-2`; `review-ratchet:l1:r2:r2-2`; `review-ratchet:l1:r2:r2-3`.
   **Examples:** Violates: marking every finding unmapped solely because a custom rules file omits severity. Complies: excluding actual reviewer guidance under the documented file contract while retaining legitimate code clauses in both attribution paths.

## REL · Preserve usable state and persistent identity

These judgment rules are adopted in this repository only. They may generalize, but this adoption does not synchronize them into cross-project rules. All four have explicit `should-fix` severity; none adds a mechanical gate.

1. **[REL.1]** When replacing a usable artifact, preserve its only usable copy until the replacement is prepared and validated; a failed replacement must leave or restore a usable state. (should-fix) <!-- digest: Exempt: a cache or build output rebuilt on demand. Otherwise keep usable state until replacement is ready and validated; retain or restore it on failure -->
   This applies to installed tools, configuration and deployment artifacts with existing usable state. A cache or build output rebuilt on demand is exempt, however many entries it holds. Choose a replacement and recovery strategy appropriate to the artifact; this rule does not assume a single rename makes a multi-file operation atomic.
   **Why added (proposal C, accepted 2026-10-28):** Reporting a copy failure does not preserve the previous working installation. Existing error-handling clauses do not state the required preservation of usable state.
   **Finding source:** `review-ratchet:l1:r1:r1-4`.
   **Examples:** Violates: deleting the old installation before copying its replacement, then losing both on a copy failure. Complies: preparing and validating the replacement before switching, with the old state retained or recoverable if the switch fails.

2. **[REL.2]** Changes to the generation of persistent IDs used for deduplication, linking or closing records require compatible reads or a migration, with verification that old records still identify the same objects. (should-fix) <!-- digest: Exempt: a new label or format over the same stored key. Otherwise a stored key (dedup, links, closing) change needs compatible reads or migration and old-identity checks -->
   A new label or display format over the same stored key and fields is exempt.
   **Why added (proposal D, accepted 2026-10-28):** An ID-format change can create duplicate records and break existing links even when both formats look valid. This is a persistence contract, distinct from naming style and from PRJ.1's distinction between replay and new execution.
   **Finding source:** `review-ratchet:l1:r1:r1-5`.
   **Examples:** Violates: replacing stored `r1-001` with `r1-1` so replay inserts a second record. Complies: compatible lookup or a migration that preserves the object's identity and references, or changing only its displayed label.

3. **[REL.3]** Don't guard only the syntax of a state file you read back: a wrong shape must take the same fallback, or the same named error, as a parse failure. (should-fix) <!-- digest: Don't guard only a state file's syntax: a wrong shape takes the same fallback or named error as a parse failure, else one bad file fails every run -->
   Scope: a file a program wrote on an earlier run and reads on a later one (state, cache, saved records), which a person, an older version or a crash may have left in another shape. Input from the host or the caller is outside this rule.
   Violates: `except ValueError` around `json.loads`, then `.items()` on a list. Complies: an `isinstance` check that takes the parse-failure path, so the next save repairs the file.

4. **[REL.4]** Don't count a cap's use only at the end, on success or by opt-in: record each attempt as it starts, in code every caller passes through. (should-fix) <!-- digest: Don't count a cap's use at the end, on success only or by opt-in: count each attempt as it starts -->
   Scope: a limit on something billed, rate-limited or repeated (a daily review cap, a round limit, a quota). A cap checked when the work starts but counted when it ends lets concurrent runs pass the check together; one counted only on success lets a failure after the spend go free; one that counts only the calls a caller marks lets a caller that forgets the mark escape. An attempt refused before it spends anything is not counted.
   Violates: `if used < limit: call(); record(used + 1)`, or `record()` after `call()` returns. Complies: record the attempt, atomically with the check, before `call()`; keep it when the call fails; do it in the code path every caller takes.

## R · Reviewer conduct

1. **[R.1]** Standards come from this file, not from the reviewer. If nothing applies, say "no standard, suggest X" instead of reporting a violation.
2. **[R.2]** Never rubber-stamp: if nothing is wrong, say which rule the code gets right.
3. **[R.3]** Teach: every finding states the mechanism and the fix, not just the symptom.
4. **[R.4]** Severity follows the family of the violated clause. <!-- ADJUST: the family-to-severity table. A family missing from it is guidance: its clauses carry no severity. -->

   | family | severity |
   |---|---|
   | P0E, P0F | blocker |
   | P1, P2 | should-fix |
   | P3 | nice |

<!-- house-rules-retired: R.5 -->
6. **[R.6]** Read the surrounding code before judging a change. Something wrong in isolation can be right given the system's invariants.
7. **[R.7]** A new gate or rule needs a failing example that proves it fires. <!-- digest: Prove gates with fail/pass, reversal and per-rule mutation checks; isolate unrelated probe effects; explicitly authorize real integration effects --> <!-- ADJUST: drop this clause if you do not write your own lint gates. -->
   A gate that runs green proves nothing, because a gate that always returns green also runs green. The proof is to build what it should catch and watch it turn red. Give every rule at least one positive example (must fail) and one negative (must pass), starting with the exceptions the rule states explicitly, since those are what a later tightening breaks first. Reverting to the old behavior must turn it green again, so the red is caused by the new part. Pin each positive to the specific rule that fired. With several rules, disable them one at a time and confirm only that rule's example goes green. Commit the tests; a one-off manual run proves nothing later.
   When performing this validation or installation probes, isolate writes and external actions unrelated to the validation purpose. Isolating the Git index does not isolate the working tree, network or deployment behavior. Real integration validation must have an explicitly authorized side-effect scope; a probe must not accidentally deploy, push or execute user scripts.
   **Why amended (proposal E, accepted 2026-10-28):** Effective validation needs both evidence that the check works and a boundary around its side effects. This extends the existing testing guidance without adding a code-violation severity or a separate rule.
   **Finding source:** `review-ratchet:l1:r1:r1-8`.
   **Examples:** Violates: an installation probe unexpectedly executes a user's deployment script. Complies: isolated synthetic fixtures, or a real integration check whose side effects are explicitly authorized and bounded.
