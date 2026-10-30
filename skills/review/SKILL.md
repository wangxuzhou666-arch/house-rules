---
description: Run Claude Code's /code-review on the current repo and record its findings in the review-ratchet ledger. Use when the user asks to review changes with review-ratchet, to log or track review findings, or to run "ratchet review"; not for a plain code review that should leave the ledger alone. When the review finds a blind spot no rule covers, it ends with the same single question as a wrap-up.
argument-hint: "[low|medium|high|xhigh|max] [target]"
allowed-tools: Read, Write, Bash(ratchet *), AskUserQuestion, Skill
---

# Review with review-ratchet

`ratchet` is the plugin's `bin/ratchet`. If the shell cannot find it, use `${CLAUDE_PLUGIN_ROOT}/bin/ratchet`. Arguments given to this skill: `$ARGUMENTS`.

1. Read the arguments: the first word is the level (`low`, `medium`, `high`, `xhigh` or `max`; default `high`), anything after it is the target (a PR number, branch or path; default is the current diff).
2. Run `ratchet review --level <level>`, adding `--target <target>` if given. It takes about 30 seconds at `high` and longer above that. If the repo has no rules yet, offer `/review-ratchet:setup` first.
   A change gets at most 3 review rounds (review, fix, re-review; `max_review_rounds` in `.ratchet.json`). `review` prints `review round <r>/<max> for <key>` first, and `ratchet count` ends with the rounds used.
3. Show the findings table exactly as printed, and name the file where the raw review output was saved.
4. Run `ratchet count --by-rule` and summarize what is open. On the last round (round 3 unless the config says otherwise), fix **blockers only**: the rest stays open in the ledger for the periodic distill step; say so instead of fixing it.
   If `review` is refused (`round cap reached ...`, exit code 3), stop. Do not pass `--over-cap` on your own: tell the user the cap is reached, how many findings are open, and ask whether they want one more round (an `--over-cap` run is recorded in the ledger).
5. Decision step: run `ratchet wrapup --json --report-only` and follow step 3 of [wrapup](../wrapup/SKILL.md) with the `new_unmapped` it prints. That is the only place the question is defined: no new blind spot means no question, otherwise one question, and [maintain](../maintain/SKILL.md) starts only if the user picks `Now`. A plain review changes no rules.
6. `ratchet label-page` (a local HTML page; the user marks findings fixed, dismissed or false positive and you pass the copied JSON to `ratchet apply-labels`) is a fallback for labeling many findings by hand or when there is no question tool. Do not offer it by default.
7. Remind the user that a commit whose message ends with `Fixes-Finding: <id>` closes that finding.
