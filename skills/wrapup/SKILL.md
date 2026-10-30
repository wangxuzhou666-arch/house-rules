---
name: wrapup
description: Wrap up a stretch of work with review-ratchet: count what changed since the last review, run one code review only when enough did, and ask at most one question about any new blind spots (findings no rule covers). Use when the user says wrap up, close out, finish up, 收口 or 收尾, when /ship or another workflow reaches its closing step, after /review-ratchet:review has run, or when a review-ratchet Stop hook message asks for the automatic wrap-up (`auto <worktree>`).
argument-hint: "[auto <worktree>] [--level low|medium|high|xhigh|max] [--base REF]"
allowed-tools: Read, Write, Bash(ratchet *), AskUserQuestion, Skill
---

# Wrap up with review-ratchet

`ratchet` is the plugin's `bin/ratchet`. If the shell cannot find it, use `${CLAUDE_PLUGIN_ROOT}/bin/ratchet`. Arguments given to this skill: `$ARGUMENTS`. A wrap-up costs at most one review and one question.

**Automatic mode** (`$ARGUMENTS` starts with `auto <worktree>`): the Stop hook asked for this wrap-up and nobody typed it. In step 1 run `ratchet wrapup --repo <worktree> --json` with the Bash tool's `run_in_background` set, as a review takes minutes and a foreground call is cut off at its timeout. Wait for the completion notice (do not poll or sleep) and read the output file it names; the JSON object is the last thing in it. Everything below applies, except that `skipped`, `capped` and an error get one sentence and no question, and `/review-ratchet:setup` is not offered.

1. Run `ratchet wrapup --json`, adding the `--level` and `--base` flags found in `$ARGUMENTS`, if any. Stdout is one JSON object; the human report (the measurement and, if it ran, the review's own output) is on stderr: relay it as printed. If the repo has no rules yet, offer `/review-ratchet:setup` first.
   - **Exit code 1** (an error: `claude` failed, its output held no JSON, HEAD is detached, a `--base` that is not a commit, ...): stdout is empty, so there is no `status` to read. Tell the user the error from stderr exactly as printed, then stop: no retry, no question, nothing recorded.
2. Branch on `status`:
   - `skipped`: one sentence with `changed_lines` and `changed_files` against `thresholds`, saying no review ran (and `open_unmapped` if above 0). No question; stop.
   - `capped` (exit code 3): the round cap is reached. Do not run `ratchet review --over-cap` on your own: say how many findings are open and ask whether the user wants one more round. Stop.
   - `reviewed`: continue with `new_unmapped`.
3. **Decision step** (the one definition; [review](../review/SKILL.md) points here, and gets the same object from `ratchet wrapup --json --report-only`). If `new_unmapped` is empty, say so in a sentence and ask nothing. Ask only about `new_unmapped`: `repeated_unmapped` lists the ids of this run's findings that repeat a blind spot already known, one an earlier run left open or one already marked `false_positive` or `dismissed` (the review reads the whole branch, or on the default branch the commits since the start point or the last review, so an unfixed blind spot can come back). A repeat is the same file with the same code around the flagged line, not just the same line of text. Say which repeats which, from `repeat_of` ({this run's finding id: the earlier finding it repeats}; a display id is the part of a full id after its last `:`), in a sentence such as "r2-1 repeats r1-1, r2-2 repeats r1-2: not asked again", and never put one in the question. Otherwise make ONE AskUserQuestion call with two questions:
   - Header `Not real?`, multiSelect: "Which of these are not real problems?" One option per blind spot, the 4 most severe at most (label `display_id file:line`, description the summary); say how many were left out. For exactly one blind spot ask it single-select instead: `Real problem, keep open (Recommended)` / `Not a real problem`.
   - Header `Rules now?`, single-select: "Turn the rest into rules now?" `Now (Recommended)` (runs maintain: at most 3 proposals, and you still decide each) / `Next wrap-up` (nothing is recorded; they stay open).
4. Record only the answers: run `ratchet label <display_id>... --outcome false_positive --note "wrap-up: not a real problem"` once, naming each blind spot marked not real by its `display_id` (a full `id` from `new_unmapped` works too). It writes all of them or none. Use no other outcome. A default or an empty answer records nothing.
5. If the answer to the second question was `Now` and blind spots are still open, continue with [maintain](../maintain/SKILL.md) on them. If it was `Next wrap-up`, say they stay open and stop.
6. `ratchet label-page` + `ratchet apply-labels` (the batch route: many findings to label by hand, or no question tool) and `ratchet distill --drafts` are fallbacks, not the default path.
