# Coding Style (DRAFT template)

<!-- ADJUST: a starting point, not a standard. Every knob below is marked ADJUST. Change thresholds, tool codes and examples to what your repo enforces, and delete the clauses you will not hold people to. After editing, run `house-rules compile` to regenerate the digest. -->

> Readers: engineers and coding agents who write code, and reviewers (human or model) who judge it.
> Role: the single source of truth for review standards. A reviewer does not invent thresholds; they live here.
> Layering: keep cross-project rules here and project-specific ones (stack pitfalls, known debt) in a second rules file listed after this one in `rules_files`. Later files add clauses; they never override.
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
Clause IDs: `P0E.n` exceptions, `P0F.n` fallbacks, `P1.n` complexity, `P2.n` abstraction, `P3.n` comments, `R.n` reviewer conduct. A linter finding that matches no clause is recorded as `<tool>:<code>`.

## P0 · Exceptions: keep the guarded block small, and give every broad catch an identity

**Why**: the wider the `try`, the more different failures collapse into one handler. A timeout (retry), a missing key (a bug) and a failed write (alert) become the same log line, and a `return None` after it makes the failure look like success.

1. **[P0E.1]** Put only the line you expect to fail inside `try`; move the rest after the block. <!-- ADJUST: language tool code, e.g. ruff TRY300 flags a return inside try. -->
2. **[P0E.2]** Catch a specific exception type, with its own action. One response for every type is suppressing, not handling.
3. **[P0E.3]** Catch-all handlers are legal only in the cases below, and the line must name the case and the reason. <!-- digest: Catch-all only for: side channel, optional extra, batch isolation, top-level handler, rethrow with cause; name the case on the line --> <!-- ADJUST: the legal-case whitelist. Shorten or extend it, and keep the digest comment in step. -->
   - **Side channel**: masking, metrics or telemetry must not take down the main flow.
   - **Optional extra**: a field the conclusion does not depend on (a usage count recorded as 0).
   - **Batch isolation**: one bad item must not sink the batch; log that item's identity.
   - **Top-level handler**: the outermost layer of a server or worker, which logs the full traceback so the process survives.
   - **Rethrow with cause**: a retry wrapper that raises the last error, or a boundary that translates a parse failure into a client error and keeps the original as its cause. The test is that the exception's identity is not lost. Narrow the type instead where you can.
4. **[P0E.4]** A bare lint suppression on a broad catch is not accepted; it must carry the case and reason. <!-- ADJUST: e.g. a bare `noqa: BLE001` in ruff or an eslint-disable without a description. -->
5. **[P0E.5]** Log inside a handler with the call that keeps the stack trace, not the one that drops it. <!-- ADJUST: e.g. `logger.exception` rather than `logger.error` (ruff TRY400); `console.error(err)` with the error object. -->
6. **[P0E.6]** Replace an empty handler with an explicit suppression that names the exception type. <!-- ADJUST: e.g. `contextlib.suppress(KeyError)` (ruff SIM105). -->

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

5. **[R.5]** Tells of generated code, reported under the matching P clause: one try around everything, null checks on values that cannot be null, one-implementation base classes, docstrings that repeat the name, "just in case" defaults, boilerplate foreign to this repo. <!-- digest: Generated-code tells, report under the P clause: one big try, needless null checks, single-implementation base class, echo docstring, "just in case" default -->
6. **[R.6]** Read the surrounding code before judging a change. Something wrong in isolation can be right given the system's invariants.
7. **[R.7]** A new gate or rule needs a failing example that proves it fires. <!-- ADJUST: drop this clause if you do not write your own lint gates. -->
   A gate that runs green proves nothing, because a gate that always returns green also runs green. The proof is to build what it should catch and watch it turn red. Give every rule at least one positive example (must fail) and one negative (must pass), starting with the exceptions the rule states explicitly, since those are what a later tightening breaks first. Reverting to the old behavior must turn it green again, so the red is caused by the new part. Pin each positive to the specific rule that fired. With several rules, disable them one at a time and confirm only that rule's example goes green. Commit the tests; a one-off manual run proves nothing later.
