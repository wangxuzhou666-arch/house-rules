---
description: Run Claude Code's /code-review on the current repo and record its findings in the review-ratchet ledger. Use when the user asks to review changes with review-ratchet, to log or track review findings, or to run "ratchet review"; not for a plain code review that should leave the ledger alone.
argument-hint: "[low|medium|high|xhigh|max] [target]"
allowed-tools: Bash(ratchet *)
---

# Review with review-ratchet

`ratchet` is the plugin's `bin/ratchet`. If the shell cannot find it, use `${CLAUDE_PLUGIN_ROOT}/bin/ratchet`. Arguments given to this skill: `$ARGUMENTS`.

1. Read the arguments: the first word is the level (`low`, `medium`, `high`, `xhigh` or `max`; default `high`), anything after it is the target (a PR number, branch or path; default is the current diff).
2. Run `ratchet review --level <level>`, adding `--target <target>` if given. It takes about 30 seconds at `high` and longer above that. If the repo has no rules yet, offer `/review-ratchet:setup` first.
   A change gets at most 3 review rounds (review, fix, re-review; `max_review_rounds` in `.ratchet.json`). `review` prints `review round <r>/<max> for <key>` first, and `ratchet count` ends with the rounds used.
3. Show the findings table exactly as printed, and name the file where the raw review output was saved.
4. Run `ratchet count --by-rule` and summarize what is open. On the last round (round 3 unless the config says otherwise), fix **blockers only**: the rest stays open in the ledger for the periodic distill step; say so instead of fixing it.
   If `review` is refused (`round cap reached ...`, exit code 3), stop. Do not pass `--over-cap` on your own: tell the user the cap is reached, how many findings are open, and ask whether they want one more round (an `--over-cap` run is recorded in the ledger).
5. If `review` said `open > N` or more than 20 findings are open, offer `ratchet label-page`: it writes a local HTML page; the user marks each finding fixed, dismissed or false positive, copies the JSON, and you pass it to `ratchet apply-labels`.
6. Remind the user that a commit whose message ends with `Fixes-Finding: <id>` closes that finding.
7. When the user requested the review-to-rules loop, continue with [maintain](../maintain/SKILL.md) using these findings. It prepares evidence and asks for decisions through the host's native question tool. A plain review ends here without changing rules.
