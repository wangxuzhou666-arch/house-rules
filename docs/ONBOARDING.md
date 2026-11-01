# Onboarding

Goal: from a fresh clone to a first clause-labeled finding in the ledger in a few minutes. Two commands do it: `house-rules init` wires a repo up, `house-rules review` records the first findings.

Requirements: Python 3.10+ (stdlib only), git, and for `review` the `claude` CLI (Claude Code) on PATH.

## `house-rules init`

```sh
house-rules init              # asks a few questions
house-rules init --yes        # accepts the defaults (use this from scripts and from a Claude Code tool call)
house-rules init --dry-run    # prints the plan and a diff per file, writes nothing
```

Options: `--rules-file PATH` (use your own rules file instead of the template), `--template` (copy the bundled template), `--no-hooks`, `--no-claude-md`, `--no-wrapup-auto` (answer no to the automatic wrap-up question without asking it, see [the `wrapup_auto` key](#the-wrapup_auto-key)), `--repo PATH`. `--rules-file` and `--template` exclude each other.

Without `--yes` it needs a terminal; run from a pipe or a tool call it stops and says so rather than guessing. `--dry-run` never asks. There are up to four questions, in this order: the rules file, the automatic wrap-up, the `CLAUDE.md` import, the git hooks. A question is asked only for a step that would change something, so rerunning on a finished repo asks nothing.

`--dry-run` prints, under each step that would change a file, the unified diff of exactly what a real run would write (new files appear as a diff against nothing; the digest a template copy would produce is rendered too). It writes nothing and never runs a hook, so it cannot show the live check below. The start point step shows no diff: it records a commit in a local file, and a dry run says only `would add`.

It is **idempotent**: each step checks what is already in place, only fills what is missing, and reports `already ok` for the rest. It never overwrites a file it did not create, and every edit to a file you own sits in a marked block that a rerun finds and refreshes in place.

### The steps

| step | what it does | undo |
|---|---|---|
| config | Writes `.house-rules.json` with `rules_files` and `wrapup_auto`, the answer to the automatic wrap-up question ([below](#the-wrapup_auto-key)). If the file exists, adds whichever of the two it lacks and keeps the other keys; a file that has both is left byte for byte. In a linked worktree it is never created or edited, see [Linked worktrees](#linked-worktrees). | delete the file |
| rules file | Asks for a path to your own rules file, or offers the bundled template ([`templates/CODING-STYLE.template.md`](../templates/CODING-STYLE.template.md)), copied to `CODING-STYLE.md`. An existing `CODING-STYLE.md` or `docs/CODING-STYLE.md` is the default. The template is a draft: open it and read the `<!-- ADJUST: ... -->` comments, each marks a threshold, whitelist or tool code to fit to your repo. Never overwrites an existing file. A rules file outside the repository is saved in the config as an absolute path and is read-only for house-rules ([Outer layers](COMPILE.md#outer-layers)). | delete the copy |
| compile | Runs `house-rules compile`: writes `CODING-STYLE.digest.md` (see [COMPILE.md](COMPILE.md)). | delete the digest |
| CLAUDE.md | Appends the heading `## Code style digest (house-rules)` and the line `@CODING-STYLE.digest.md` (your configured digest path) inside a `<!-- house-rules:begin -->` ... `<!-- house-rules:end -->` block, creating `CLAUDE.md` if absent. The heading is part of the block, so the import has a title of its own wherever it lands in your file. Claude Code expands the import, and the built-in `/code-review` reads `CLAUDE.md`, so reviews are judged against your clause IDs. An existing import line outside the block counts as done. | delete the block |
| git hooks | Installs `pre-commit` and `post-commit`, then runs each once to prove its block is reached (below). | delete the `# house-rules:begin` ... `# house-rules:end` block from each hook |
| .gitignore | Ignores the ledger, the label-page files and the whole `.house-rules/` directory (saved reviews, proposal batches, receipts: local state that can hold code excerpts) in a `# house-rules:begin` ... `# house-rules:end` block. Entries already listed elsewhere are not repeated. `CODING-STYLE.changelog.md`, the readable record of applied rule changes, is not ignored: commit it with the rules. A block written by an earlier version, which listed only `.house-rules/reviews/`, is refreshed in place on the next run and reported as `updated`. | delete the block |
| start point | Records where the default branch stands now, in `.house-rules/start-points.json`, as the commit its first wrap-up measures from, so that the commits made after init count ([The start point](WRAPUP.md#the-start-point)). On that branch the fork point is HEAD itself, which counts nothing that is committed. A usable start point already recorded stays where it is. It is the default branch that is recorded, whichever branch init runs on. Skipped, with the reason, when the repo has no commit yet (the first rerun after a commit records it) or the default branch cannot be found. A dry run writes nothing. | delete the file |

The start point step prints one of these lines (`would add` instead of `added` in a dry run):

```
  start point      added        main is measured from 8be23cec1070, where it stands now: the first wrap-up there counts the commits made after it
  start point      already ok   main is measured from 8be23cec1070
  start point      skipped      no commit yet
  start point      skipped      no default branch found (no origin/HEAD, main or master): set default_branch in .house-rules.json and run init again
  start point      skipped      config default_branch 'develop' is not a branch of this repo
```

### The `wrapup_auto` key

`wrapup_auto` turns on the automatic wrap-up review after commits made in a Claude Code session. It is a JSON boolean in `.house-rules.json`, `false` when absent; any other value is an error (`config 'wrapup_auto' must be true or false`). After the rules file, `init` asks:

```
Turn on the automatic wrap-up review after commits made in a Claude Code session? Each automatic run costs one /code-review. [Y/n]
```

Enter answers yes. `--yes` and `--dry-run`, which ask nothing, take that default; `--no-wrapup-auto` writes `false` and does not ask. What the config step does depends on what the file holds:

| `.house-rules.json` | the config step |
|---|---|
| missing, or holds neither `rules_files` nor `wrapup_auto` | asks, then writes both keys, `wrapup_auto` after the keys that were there |
| holds `wrapup_auto` but not `rules_files` | adds `rules_files`, keeps the value of `wrapup_auto`, asks nothing |
| holds `rules_files` but not `wrapup_auto` (main worktree) | asks, adds only `wrapup_auto`; the step is `updated`, with the detail `.house-rules.json: wrapup_auto true` |
| holds both | leaves the file byte for byte, `already ok`; `--no-wrapup-auto` does not overwrite the value |

Whenever it writes, `init` serializes the whole file again with two-space indentation. The other keys and their order stay, but the layout is tidied. A dry run shows the change in its diff, for example `+  "wrapup_auto": true`. In a linked worktree `init` neither asks this question nor writes the config: see [Linked worktrees](#linked-worktrees).

### What it prints at the end

- **The commit hint.** If init created or changed files that git sees as new or modified, it lists them and prints a ready line, for example `git add .house-rules.json CODING-STYLE.md CODING-STYLE.digest.md CLAUDE.md .githooks/pre-commit .githooks/post-commit && git commit -m "chore: adopt house-rules"`. Commit them together: the pre-commit hook checks the *staged* copies, so until the rules file and the digest are committed side by side, other commits are blocked. Only files init manages are listed, and a shared file (`CLAUDE.md`, `.gitignore`, a hook) only when it carries init's block. In a linked worktree the listed files are that worktree's. The ledger (`.style-findings.jsonl`) and the whole `.house-rules/` directory are git-ignored and stay local. `CODING-STYLE.changelog.md` is not: it appears the first time a rule change is applied (see [DECISION-CARDS.md](DECISION-CARDS.md#the-rule-change-record)), and you commit it with the rules. Hooks in `.git/hooks` are not tracked and never appear in the hint. A dry run prints no hint.
- **The next steps.** Three lines: run a review (`house-rules review --level high`), see what is open by clause (`house-rules count --by-rule`), and close a stretch of work with `house-rules wrapup` (`/house-rules:wrapup` in Claude Code; see [WRAPUP.md](WRAPUP.md)). The third line goes on to name `house-rules label <id> --outcome fixed`, which records what became of a finding, and `house-rules label-page`, which does many at once.
- **`house-rules is set up`** instead of the next steps when every step was already `ok` or skipped: a rerun on a finished repo says so and nothing else.

If a step is `FAILED` (see the live check below), the other steps still run, and init then exits 1.

### The hooks

`init` asks `Install git hooks (pre-commit: compile --check --staged; post-commit: infer, and in a Claude Code session with wrapup_auto on, noting the commit for the automatic wrap-up)?` and installs two:

- **pre-commit** runs `house-rules compile --check --staged`. If you staged a rules edit without the regenerated digest, the commit stops and says which staged file is stale; run `house-rules compile` if needed, `git add` the files it names, and commit again. A passing check prints nothing, except that a shared rules layer which is not on this machine gets a warning and does not stop the commit ([Check mode](COMPILE.md#check-mode)). See [`--check --staged`](COMPILE.md#--check---staged).
- **post-commit** runs `house-rules infer --quiet`. A commit whose message ends with a `Fixes-Finding: <id>` trailer closes that finding in the ledger; the hook prints one line when it closes something and stays silent otherwise. Then it runs `house-rules auto note`, which does something only in a Claude Code session with `wrapup_auto` on: it notes the commit for the automatic wrap-up ([below](#the-commit-note)). It cannot fail a commit.

Both blocks only act in a repo that has a `.house-rules.json`, so a `core.hooksPath` shared by many repos is safe.

#### The commit note

The post-commit block ends with one more line, in the portable and the pinned block alike (the pre-commit block has none):

```sh
[ -z "${CLAUDE_CODE_SESSION_ID:-}" ] || "$_house_rules_py" "$_house_rules_bin" auto note || true
```

Outside a Claude Code session the shell skips it before Python starts, and `|| true` keeps a failure from ending the hook under `set -e`. `house-rules auto note` records the commit only when all four of these hold, and otherwise exits 0 without a word:

- `CLAUDE_CODE_SESSION_ID` matches `[A-Za-z0-9_-]{1,128}`;
- `HOUSE_RULES_REVIEW_CHILD` is not `1`. house-rules sets it to `1` for the `claude` process of its own reviews, so a commit made there is not noted; `0`, an empty value or `yes` do not stop the note;
- `wrapup_auto` is exactly `true` in the main worktree's `.house-rules.json` (a linked worktree's own copy is ignored);
- `HEAD` exists.

The note only records. It appends one JSON line to `<state dir>/pending/<session id>.jsonl`:

```json
{"worktree": "<top of the worktree the commit was made in>", "head": "<full 40-hex sha>", "ts": "2026-10-30T09:12:41+00:00"}
```

`worktree` is the absolute path of the top of the worktree the commit was made in (a linked worktree records its own), `head` the full sha, `ts` the UTC time to the second.

The state dir is the first of these that is set and not empty: `$HOUSE_RULES_STATE_DIR`, `$XDG_STATE_HOME/house-rules`, `~/.local/state/house-rules`. The note creates it and `pending/` with mode `0700`, and the files in them with `0600`. Each time a commit is noted, the files in `pending/` and in `claimed/` (where the Stop hook keeps the records it is judging) that nothing has written to for 7 days are deleted. The record stays on this machine because the paths and shas are private: nothing goes into the repository or its history. What reads the records at the end of the turn, and what else the state dir holds: [Automatic wrap-up](WRAPUP.md#automatic-wrap-up).

A failure reaches neither the terminal nor the commit. It is one line in `<state dir>/hook.log`, `<UTC time> auto note: <ExceptionType>: <message> (at <file>:<line>)`, and that file is never trimmed.

#### Portable and pinned blocks

Which block `init` writes depends on where git looks for hooks (`.git/hooks`, or `core.hooksPath` when set):

| hooks directory | block | why |
|---|---|---|
| inside the work tree, not under `.git` (for example `.githooks`) | **portable** | the directory is meant to be committed, so the block must hold no path of your machine |
| `.git/hooks`, or a directory outside the work tree | **pinned** | nobody else receives that file, so absolute paths are fine and need no setup |

A **portable** block looks everything up when the hook runs:

- the tool is `$HOUSE_RULES_BIN` if set, else the `bin` key of `.house-rules.json` (relative to the repo root; see [LEDGER.md](LEDGER.md#config-house-rulesjson)), else `house-rules` on `PATH`;
- Python is `$HOUSE_RULES_PY` if set, else `python3` on `PATH`, and it must be 3.10 or newer.

If either lookup fails, the hook prints a loud `WARNING ... is NOT running` on stderr and does nothing; it never fails the commit for that, not even under `set -e`. (The pre-commit check still blocks when it can run and finds a stale digest.) `init` says when the tool it runs from is inside the repo (a [vendored copy](VENDORING.md)): it prints the exact `"bin": "<path>"` line to add to `.house-rules.json`, and commits then work for everyone who has the vendored copy. Under the Claude Code plugin, `house-rules` is on `PATH` only inside Claude's Bash tool, so teammates committing from a terminal need `bin`, `HOUSE_RULES_BIN`, or `house-rules` on their `PATH`.

A **pinned** block records, when you ran init, the interpreter that ran it (`sys.executable`, not `env python3`: a hook runs with whatever `PATH` git was started from, which may have no Python 3.10+ or the wrong one) and the `bin/house-rules` of that checkout, shown as `HOUSE_RULES_PY=` and `HOUSE_RULES_BIN=`. If either stops existing (you moved or updated the checkout, the plugin updated, or the virtualenv was deleted), the hook prints the same loud `WARNING` and does nothing. Run `house-rules init` again to refresh the paths.

#### Where the block goes, and the live check

Existing hooks are never replaced. A shell-script hook gets the block added; a hook in another language is left untouched and `init` prints the line to add yourself (a `MANUAL` step). For post-commit it prints a second line, `and, for the automatic wrap-up (it does something only in a Claude Code session with wrapup_auto on): <prefix>auto note`; `<prefix>` is the interpreter and the `bin/house-rules` of this checkout for a pinned block, and empty for a portable one. Pre-commit has no second line. With `core.hooksPath` set, the hooks go there.

The block always goes at the **top** of the hook, right under the shebang line, in a new hook and in an existing one. Anywhere lower, your own code could end the script before it (an `exit` or `exec`, a function that exits, a failing command under `set -e`, an early return when nothing is staged) and the check would be installed but never run. At the top nothing of yours runs first. A block an earlier version appended after your `exit`, or one you moved further down, is moved back to the top by the next `init` and reported as `updated`; once it is at the top a rerun says `already ok`. A post-commit block written before the commit note existed is brought up to date the same way: the next `init` rewrites it in place and reports `updated`, a second run says `already ok`, and nothing outside the block changes. Lines in your own hook (a `PATH` setup, say) run after the block, so the block (the portable kind; a pinned one carries its own paths) finds house-rules through `$HOUSE_RULES_BIN`, the `bin` key of `.house-rules.json`, or the `PATH` git started the hook with.

Every installed block is also **run once**. `init` starts the hook from the top of the worktree with `HOUSE_RULES_HOOK_PROBE=1`; the first thing the block does is print `house-rules-hook-probe <hook>` and exit 0, before touching the tool (and before the commit note, so the probe leaves no record). The step is `ok` only if the hook exited 0 with that line on its stdout. Anything else (the shebang names an interpreter that is missing or cannot run the block, a hook that hangs past 60 seconds) makes the step `FAILED` with the status and the tail of the hook's output; the remaining steps still run, and `init` exits 1.

- **There is no flag to skip it, on purpose.** The failure it catches is a check that is installed and silently never runs. A switch to turn that off would let the same silence back in.
- **What the run costs: none of your code runs.** The block is the first thing in the hook and exits at once when it sees `HOUSE_RULES_HOOK_PROBE`, so everything of yours below it stays unexecuted during `init`: no deploy, no push, no lint auto-fix. The probe also uses a throwaway git index equal to `HEAD` (empty on an unborn branch), so it cannot touch yours. A dry run never probes.
- **An early exit below the block is fine.** A hook with `[ -z "$(git diff --cached --name-only)" ] && exit 0` further down passes: the block sits above that line, so on a commit with nothing staged it still runs.

If `core.hooksPath` is set, git ignores `.git/hooks`, so any hook that was running from there stopped. `init` lists the executable ones by name under the first hook step (`warning: core.hooksPath makes git use ..., so these hooks in .git/hooks no longer run: ...`); move their logic into the hooks directory or unset `core.hooksPath`. When `core.hooksPath` comes from a global or system config file, the hook step also says so: every repo using that directory runs the hook, and repos without `.house-rules.json` skip it.

## `house-rules review`

```sh
house-rules review                       # /code-review at level high on the current diff
house-rules review --level max           # low | medium | high | xhigh | max
house-rules review --target 123          # a PR number, branch or path
house-rules review --from-file out.txt   # ingest a review you saved earlier; claude is not run
house-rules review --dry-run             # print the command it would run, and whether a real run would be refused; with --from-file, show what would be recorded
```

It runs `claude -p "/code-review <level> [target]"` from the root of the worktree you run it in (about 30 seconds at `high`; the ledger and `.house-rules/` stay in the main worktree, while the rules it labels findings with and saves as evidence are that worktree's own), saves the raw output to `.house-rules/reviews/<run>.txt`, ingests the findings as source `l1`, and prints a table: id, rule_id, severity, file:line, summary. On the default branch, when the work is measured from a commit that is not HEAD (the start point or the last review's head), the prompt carries a commit range after the level and the review reads those commits only ([On the default branch](WRAPUP.md#on-the-default-branch)). The run id defaults to `r<k>`, where k is one more than the number of review runs so far (the ledger's, plus any run whose raw output was saved but recorded nothing), and the findings are `r<k>-1`, `r<k>-2`, ...: short enough to type in a `Fixes-Finding:` trailer. `--run ID` sets it instead. A run id that is already recorded is refused (omit `--run` for a new round); `--from-file <saved review> --run <that id>` re-ingests it instead: the same output records nothing twice, different output is refused. So is a default id that an earlier explicit `--run r<k>` already took (the numbering never skips ahead to hide that).

Each finding's `rule_id` comes from the `category` the model gave. The digest orders it to put a `category` (a clause ID in kebab-case) and a `severity` on every finding, and an answer that is not a known clause is recorded as `unmapped` rather than guessed. If a finding has no usable `category` (none at all, or one naming a Reviewer rule, a clause without a severity that no finding violates), the first clause ID that is not a Reviewer rule cited in its summary (then its failure scenario) is used and the finding is marked `attributed`; `house-rules count --by-rule --label-source` shows how many of each. If no clause in your rules has a severity at all (no tags, no family table), `compile` lists every clause under Reviewer rules and warns; `review` then counts none of them as one, so every clause can be a `category` and be attributed. Severity comes from the model if it gave one, otherwise from the clause's family.

Each finding carries a short snippet of the code it points at, as the reviewer read it when the review started ([LEDGER.md](LEDGER.md#code-snippets)). The ledger also records which `claude` version ran the review; the first review on a new version prints `reviewer version changed: <old> -> <new> (first review on this version)` before the table, because a different reviewer is a different instrument.

If `claude` is not on PATH, `review` says so and suggests `--from-file`; it checks before it prints the `review round` line. To use another command or wrapper, set `HOUSE_RULES_CLAUDE_CMD` (split like a shell command line; `-p "<prompt>"` is appended).

`house-rules review --dry-run` also says what a real run would be refused for. Two refusals are previewed and the dry run carries on past them, to print what it would run (or, with `--from-file`, record) and exit 0: the round cap, and a missing `claude`. Their line comes after `review round N/M for KEY` and before `[dry-run] review: would run ...`:

```
[dry-run] a real run would be refused: the round cap is reached; --over-cap runs it anyway, recorded
[dry-run] a real run would be refused: `claude` not found on PATH; install Claude Code, or run the review elsewhere and pass its saved output with --from-file FILE
```

(The command in the second line is the first word of the one in use, so a `HOUSE_RULES_CLAUDE_CMD` wrapper is named there. A `--from-file` review runs no `claude`, so it never gets that line.) A rules file that does not load, and a change that has no round key (a detached HEAD, say), are not previewed: they are the same errors a real run stops on, exit 1.

Close findings by committing a fix with a trailer:

```
fix: narrow the guarded block in the fetch helper

Fixes-Finding: r3-2
```

When many findings pile up, `house-rules label-page` writes a local HTML page to label them; see [LEDGER.md](LEDGER.md#labels).

## Linked worktrees

A repo can have several worktrees (`git worktree add`). house-rules keeps its shared state in the main worktree and works on the files git tracks in the worktree a command runs in, so every worktree feeds one ledger and compiles its own copy of the rules. A command runs in the worktree `--repo` points into, else the one the current directory is in (a subdirectory is fine). In the main worktree the shared state and the tracked files are in the same place, and nothing below applies.

- **Shared, in the main worktree:** the ledger, the skips file (`skips_file`), the label page and batch (next to the ledger), everything under `.house-rules/` (saved reviews and their evidence, the start points, the book and lock of the daily cap, proposal batches, receipts, the journal of an interrupted write, the lock and `version.json`) and the config. A `.house-rules.json` in a linked worktree is ignored; the main worktree's decides.
- **The worktree's own:** the files git tracks that house-rules reads or writes: the rules files (`rules_files`), the digest, the inject targets, `CODING-STYLE.changelog.md`, `CLAUDE.md` and `.gitignore`, and the code that is reviewed, measured, gated or put in a decision context.

A file the config names that lies outside the repo (a shared `../rules.md`, an absolute path, a path under `~`) is one file for every worktree and is looked up from the main worktree. The digest header names it the same way from every worktree, so worktrees with the same rules compile to the same bytes.

What each command does in a linked worktree:

- `house-rules compile` writes that worktree's digest and injected blocks. `house-rules compile --check` compares them with its rules, and `--staged` with its own index ([COMPILE.md](COMPILE.md#--check---staged)).
- `house-rules review` runs `claude` there, labels the findings with that worktree's rules, saves them as evidence and counts the review rounds of its branch ([LEDGER.md](LEDGER.md#review-rounds)); the raw review and the evidence go to the main worktree's `.house-rules/`. `house-rules wrapup` measures that worktree's changes ([WRAPUP.md](WRAPUP.md#the-base)).
- `house-rules add`, `house-rules ingest-code-review` and `house-rules gate` stamp that worktree's HEAD on what they write and resolve rule ids against its rules; `gate` also reads its files ([GATES.md](GATES.md#in-a-linked-worktree)). The Python API does the same for the worktree its `repo` is in.
- `house-rules infer` reads the `Fixes-Finding:` trailers in the history of that worktree's HEAD, so a commit made on another worktree's branch is not seen until it is merged; `--all` reads every ref ([LEDGER.md](LEDGER.md#commit-trailers)).
- `house-rules decision-context` and the proposal commands read that worktree's rules and code. A rule change is applied, rolled back and recovered in one worktree: see [Worktrees](DECISION-CARDS.md#worktrees).
- The hooks `init` installs run in the worktree a commit is made in: pre-commit checks that worktree's index and files, and post-commit runs `house-rules infer --quiet` on its HEAD and, in a Claude Code session with `wrapup_auto` on in the main worktree's config, notes the commit under that worktree's own path ([The commit note](#the-commit-note)). A hook block acts only when the top of that worktree holds a `.house-rules.json`, so keep the config committed.

**`house-rules init` in a linked worktree** sets up that worktree's own files: the digest, the `CLAUDE.md` import, the `.gitignore` block and the hooks; its start point step records into the main worktree's `.house-rules/`. Its heading names the worktree it runs in (`house-rules init: <path>`), and a second line says the config and the ledger stay shared with the main worktree. The commit it asks for lists that worktree's files. Hooks in the default `.git/hooks` are shared by every worktree, so one `init` serves them all; a `core.hooksPath` that points into the repo names a directory in each worktree, and `init` writes the portable blocks into the one it runs in.

`init` never writes `.house-rules.json` from a linked worktree. If the main worktree's config lists no `rules_files`, it stops without writing anything (`--yes` and `--dry-run` alike) and tells you to run `house-rules init` in the main worktree first.

It does not ask the automatic wrap-up question there either, since `wrapup_auto` is the main worktree's to write. When that config has no `wrapup_auto` and `--no-wrapup-auto` was not given, the config step prints one line under it, and with the key present it prints nothing:

```
wrapup_auto is not set in <path of the main worktree's .house-rules.json>, which every worktree shares and init writes from the main worktree only: run `house-rules init` there, or add "wrapup_auto": true to it, to turn on the automatic wrap-up review
```

## With the Claude Code plugin

The repository is also a Claude Code plugin (`.claude-plugin/plugin.json`) and a one-plugin marketplace (`.claude-plugin/marketplace.json`). Install it with `claude plugin marketplace add wangxuzhou666-arch/house-rules` and `claude plugin install house-rules@house-rules`, or load a checkout for one session with `claude --plugin-dir /path/to/house-rules`. Its `bin/` directory is on the Bash tool's PATH while the plugin is enabled, so the skills run `house-rules` directly.

- `/house-rules:setup` walks through `house-rules init`: it settles three choices with you first (the rules file, the git hooks, the automatic wrap-up), previews with `--dry-run`, explains each line, runs `--yes` with the flags those choices need, and shows the next steps.
- `/house-rules:review [level] [target]` runs `house-rules review`, shows the table and the counts, and offers the label page when findings pile up.

After a plugin update the install path changes. Pinned hooks (the default `.git/hooks`, or a hooks directory outside the repo) record that path, so run `/house-rules:setup` again to refresh them; portable hooks record none and need nothing.

## Undo

Everything `init` adds to a file you own is between `house-rules:begin` and `house-rules:end` markers (`<!-- ... -->` in `CLAUDE.md`, `# ...` in `.gitignore` and the hooks). Delete the blocks. Delete `.house-rules.json`, the digest and the rules copy if you want them gone too. A hook file that `init` created and that is now empty apart from `#!/bin/sh` can be removed. The ledger and `.house-rules/` are yours to keep or delete.
