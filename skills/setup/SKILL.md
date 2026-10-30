---
description: Set up review-ratchet in the current git repo (rules file, compiled style digest, CLAUDE.md import, git hooks, .gitignore entries). Use when the user asks to set up, install, initialize or onboard review-ratchet, or to start a findings ledger for this repo.
argument-hint: "[--rules-file PATH]"
allowed-tools: Bash(ratchet *) Bash(git rev-parse *)
---

# Set up review-ratchet

`ratchet` is the plugin's `bin/ratchet`. If the shell cannot find it, use `${CLAUDE_PLUGIN_ROOT}/bin/ratchet`. Arguments given to this skill: `$ARGUMENTS`.

1. Run `git rev-parse --show-toplevel`. If it fails, tell the user review-ratchet needs a git repo and stop.
2. Settle three choices with the user, skipping any they already stated:
   - Rules: copy the bundled template (a draft they should edit; its `<!-- ADJUST -->` comments mark every knob) or use their own rules file whose clauses look like `1. **[P0E.1]** text`. For their own file, add `--rules-file PATH` below.
   - Git hooks: a pre-commit check that the digest is fresh, and a post-commit step that closes findings from `Fixes-Finding:` trailers and, when the automatic wrap-up (next) is on, also notes the commit for it. To skip them, add `--no-hooks`.
   - Automatic wrap-up: whether to review by itself after commits made in a Claude Code session, once the work since the last review is big enough. Each automatic run costs one `/code-review`. `--yes` turns it on; to leave it off, add `--no-wrapup-auto`.
3. Preview with `ratchet init --dry-run <flags>` and explain each printed line in one sentence: config, rules file, compile, CLAUDE.md, hooks, .gitignore.
4. When the user agrees, run `ratchet init --yes <flags>` (init asks questions in a terminal, so `--yes` is required here) and show its output, including the commit line and the three "next" lines. If a hook step is FAILED, init exits 1: the hook did not answer the live check even though init put the block right under its shebang (usually a shebang line that names an interpreter which is missing). Relay the message, fix that line, then run init again.
5. Tell the user:
   - Commit the files init lists with the `git add ... && git commit` line it prints (config, rules file, digest, `CLAUDE.md`, and the hooks when they live in a tracked directory). The ledger and `.ratchet/` are git-ignored.
   - Hooks in `.git/hooks` or in a directory outside the repo record the absolute paths of this plugin install and of the Python that ran `init`. After a plugin update, run this skill again to refresh them; until then a hook prints a warning and does nothing. Hooks in a tracked directory such as `.githooks` record no paths; they find `ratchet` through `$RATCHET_BIN`, `"bin"` in `.ratchet.json`, or `PATH`, so teammates need one of those.
   - To undo, delete the `ratchet:begin` / `ratchet:end` blocks (see `${CLAUDE_PLUGIN_ROOT}/docs/ONBOARDING.md`).
