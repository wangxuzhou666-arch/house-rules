# Onboarding

Goal: from a fresh clone to a first clause-labeled finding in the ledger in a few minutes. Two commands do it: `ratchet init` wires a repo up, `ratchet review` records the first findings.

Requirements: Python 3.10+ (stdlib only), git, and for `review` the `claude` CLI (Claude Code) on PATH.

## `ratchet init`

```sh
ratchet init              # asks a few questions
ratchet init --yes        # accepts the defaults (use this from scripts and from a Claude Code tool call)
ratchet init --dry-run    # prints the plan and a diff per file, writes nothing
```

Options: `--rules-file PATH` (use your own rules file instead of the template), `--template` (copy the bundled template), `--no-hooks`, `--no-claude-md`, `--repo PATH`. `--rules-file` and `--template` exclude each other.

Without `--yes` it needs a terminal; run from a pipe or a tool call it stops and says so rather than guessing. `--dry-run` never asks. Questions are asked only for a step that would change something, so rerunning on a finished repo asks nothing.

`--dry-run` prints, under each step that would change a file, the unified diff of exactly what a real run would write (new files appear as a diff against nothing; the digest a template copy would produce is rendered too). It writes nothing and never runs a hook, so it cannot show the live check below.

It is **idempotent**: each step checks what is already in place, only fills what is missing, and reports `already ok` for the rest. It never overwrites a file it did not create, and every edit to a file you own sits in a marked block that a rerun finds and refreshes in place.

### The steps

| step | what it does | undo |
|---|---|---|
| config | Creates `.ratchet.json` with `rules_files`. If the file exists without `rules_files`, adds that key and keeps the rest. If it already lists rules, leaves it byte for byte. | delete the file |
| rules file | Asks for a path to your own rules file, or offers the bundled template ([`templates/CODING-STYLE.template.md`](../templates/CODING-STYLE.template.md)), copied to `CODING-STYLE.md`. An existing `CODING-STYLE.md` or `docs/CODING-STYLE.md` is the default. The template is a draft: open it and read the `<!-- ADJUST: ... -->` comments, each marks a threshold, whitelist or tool code to fit to your repo. Never overwrites an existing file. | delete the copy |
| compile | Runs `ratchet compile`: writes `CODING-STYLE.digest.md` (see [COMPILE.md](COMPILE.md)). | delete the digest |
| CLAUDE.md | Appends the heading `## Code style digest (review-ratchet)` and the line `@CODING-STYLE.digest.md` (your configured digest path) inside a `<!-- ratchet:begin -->` ... `<!-- ratchet:end -->` block, creating `CLAUDE.md` if absent. The heading is part of the block, so the import has a title of its own wherever it lands in your file. Claude Code expands the import, and the built-in `/code-review` reads `CLAUDE.md`, so reviews are judged against your clause IDs. An existing import line outside the block counts as done. | delete the block |
| git hooks | Installs `pre-commit` and `post-commit`, then runs each once to prove its block is reached (below). | delete the `# ratchet:begin` ... `# ratchet:end` block from each hook |
| .gitignore | Ignores the ledger, the label-page files and `.ratchet/reviews/` in a `# ratchet:begin` ... `# ratchet:end` block. Entries already listed elsewhere are not repeated. | delete the block |

### What it prints at the end

- **The commit hint.** If init created or changed files that git sees as new or modified, it lists them and prints a ready line, for example `git add .ratchet.json CODING-STYLE.md CODING-STYLE.digest.md CLAUDE.md .githooks/pre-commit .githooks/post-commit && git commit -m "chore: adopt review-ratchet"`. Commit them together: the pre-commit hook checks the *staged* copies, so until the rules file and the digest are committed side by side, other commits are blocked. Only files init manages are listed, and a shared file (`CLAUDE.md`, `.gitignore`, a hook) only when it carries init's block. The ledger (`.style-findings.jsonl`) and `.ratchet/` are git-ignored and stay local. Hooks in `.git/hooks` are not tracked and never appear in the hint. A dry run prints no hint.
- **The next steps.** Three lines: run a review, see the counts, label outcomes.
- **`review-ratchet is set up`** instead of the next steps when every step was already `ok` or skipped: a rerun on a finished repo says so and nothing else.

If a step is `FAILED` (see the live check below), the other steps still run, and init then exits 1.

### The hooks

- **pre-commit** runs `ratchet compile --check --staged`. If you staged a rules edit without the regenerated digest, the commit stops and says which staged file is stale; run `ratchet compile` if needed, `git add` the files it names, and commit again. A passing check prints nothing. See [`--check --staged`](COMPILE.md#--check---staged).
- **post-commit** runs `ratchet infer --quiet`. A commit whose message ends with a `Fixes-Finding: <id>` trailer closes that finding in the ledger; the hook prints one line when it closes something and stays silent otherwise. It cannot fail a commit.

Both blocks only act in a repo that has a `.ratchet.json`, so a `core.hooksPath` shared by many repos is safe.

#### Portable and pinned blocks

Which block `init` writes depends on where git looks for hooks (`.git/hooks`, or `core.hooksPath` when set):

| hooks directory | block | why |
|---|---|---|
| inside the work tree, not under `.git` (for example `.githooks`) | **portable** | the directory is meant to be committed, so the block must hold no path of your machine |
| `.git/hooks`, or a directory outside the work tree | **pinned** | nobody else receives that file, so absolute paths are fine and need no setup |

A **portable** block looks everything up when the hook runs:

- the tool is `$RATCHET_BIN` if set, else the `bin` key of `.ratchet.json` (relative to the repo root; see [LEDGER.md](LEDGER.md#config-ratchetjson)), else `ratchet` on `PATH`;
- Python is `$RATCHET_PY` if set, else `python3` on `PATH`, and it must be 3.10 or newer.

If either lookup fails, the hook prints a loud `WARNING ... is NOT running` on stderr and does nothing; it never fails the commit for that, not even under `set -e`. (The pre-commit check still blocks when it can run and finds a stale digest.) `init` says when the tool it runs from is inside the repo (a [vendored copy](VENDORING.md)): it prints the exact `"bin": "<path>"` line to add to `.ratchet.json`, and commits then work for everyone who has the vendored copy. Under the Claude Code plugin, `ratchet` is on `PATH` only inside Claude's Bash tool, so teammates committing from a terminal need `bin`, `RATCHET_BIN`, or `ratchet` on their `PATH`.

A **pinned** block records, when you ran init, the interpreter that ran it (`sys.executable`, not `env python3`: a hook runs with whatever `PATH` git was started from, which may have no Python 3.10+ or the wrong one) and the `bin/ratchet` of that checkout, shown as `RATCHET_PY=` and `RATCHET_BIN=`. If either stops existing (you moved or updated the checkout, the plugin updated, or the virtualenv was deleted), the hook prints the same loud `WARNING` and does nothing. Run `ratchet init` again to refresh the paths.

#### Where the block goes, and the live check

Existing hooks are never replaced. A shell-script hook gets the block added; a hook in another language is left untouched and `init` prints the line to add yourself (a `MANUAL` step). With `core.hooksPath` set, the hooks go there.

The block always goes at the **top** of the hook, right under the shebang line, in a new hook and in an existing one. Anywhere lower, your own code could end the script before it (an `exit` or `exec`, a function that exits, a failing command under `set -e`, an early return when nothing is staged) and the check would be installed but never run. At the top nothing of yours runs first. A block an earlier version appended after your `exit`, or one you moved further down, is moved back to the top by the next `init` and reported as `updated`; once it is at the top a rerun says `already ok`. Lines in your own hook (a `PATH` setup, say) run after the block, so the block (the portable kind; a pinned one carries its own paths) finds ratchet through `$RATCHET_BIN`, the `bin` key of `.ratchet.json`, or the `PATH` git started the hook with.

Every installed block is also **run once**. `init` starts the hook from the repo root with `RATCHET_HOOK_PROBE=1`; the first thing the block does is print `ratchet-hook-probe <hook>` and exit 0, before touching the tool. The step is `ok` only if the hook exited 0 with that line on its stdout. Anything else (the shebang names an interpreter that is missing or cannot run the block, a hook that hangs past 60 seconds) makes the step `FAILED` with the status and the tail of the hook's output; the remaining steps still run, and `init` exits 1.

- **There is no flag to skip it, on purpose.** The failure it catches is a check that is installed and silently never runs. A switch to turn that off would let the same silence back in.
- **What the run costs: none of your code runs.** The block is the first thing in the hook and exits at once when it sees `RATCHET_HOOK_PROBE`, so everything of yours below it stays unexecuted during `init`: no deploy, no push, no lint auto-fix. The probe also uses a throwaway git index equal to `HEAD` (empty on an unborn branch), so it cannot touch yours. A dry run never probes.
- **An early exit below the block is fine.** A hook with `[ -z "$(git diff --cached --name-only)" ] && exit 0` further down passes: the block sits above that line, so on a commit with nothing staged it still runs.

If `core.hooksPath` is set, git ignores `.git/hooks`, so any hook that was running from there stopped. `init` lists the executable ones by name under the first hook step (`warning: core.hooksPath makes git use ..., so these hooks in .git/hooks no longer run: ...`); move their logic into the hooks directory or unset `core.hooksPath`. When `core.hooksPath` comes from a global or system config file, the hook step also says so: every repo using that directory runs the hook, and repos without `.ratchet.json` skip it.

## `ratchet review`

```sh
ratchet review                       # /code-review at level high on the current diff
ratchet review --level max           # low | medium | high | xhigh | max
ratchet review --target 123          # a PR number, branch or path
ratchet review --from-file out.txt   # ingest a review you saved earlier; claude is not run
ratchet review --dry-run             # print the command it would run; with --from-file, show what would be recorded
```

It runs `claude -p "/code-review <level> [target]"` from the repo root (about 30 seconds at `high`), saves the raw output to `.ratchet/reviews/<run>.txt`, ingests the findings as source `l1`, and prints a table: id, rule_id, severity, file:line, summary. The run id defaults to `r<k>`, where k is one more than the number of review runs so far (the ledger's, plus any run whose raw output was saved but recorded nothing), and the findings are `r<k>-1`, `r<k>-2`, ...: short enough to type in a `Fixes-Finding:` trailer. `--run ID` sets it instead. A run id that is already recorded is refused (omit `--run` for a new round); `--from-file <saved review> --run <that id>` re-ingests it instead: the same output records nothing twice, different output is refused. So is a default id that an earlier explicit `--run r<k>` already took (the numbering never skips ahead to hide that).

Each finding's `rule_id` comes from the `category` the model gave. The digest orders it to put a `category` (a clause ID in kebab-case) and a `severity` on every finding, and an answer that is not a known clause is recorded as `unmapped` rather than guessed. If a finding has no usable `category` (none at all, or one naming a Reviewer rule, a clause without a severity that no finding violates), the first clause ID that is not a Reviewer rule cited in its summary (then its failure scenario) is used and the finding is marked `attributed`; `ratchet count --by-rule --label-source` shows how many of each. If no clause in your rules has a severity at all (no tags, no family table), `compile` lists every clause under Reviewer rules and warns; `review` then counts none of them as one, so every clause can be a `category` and be attributed. Severity comes from the model if it gave one, otherwise from the clause's family.

If `claude` is not on PATH, `review` says so and suggests `--from-file`. To use another command or wrapper, set `RATCHET_CLAUDE_CMD` (split like a shell command line; `-p "<prompt>"` is appended).

Close findings by committing a fix with a trailer:

```
fix: narrow the guarded block in the fetch helper

Fixes-Finding: r3-2
```

When many findings pile up, `ratchet label-page` writes a local HTML page to label them; see [LEDGER.md](LEDGER.md#labels).

## With the Claude Code plugin

The repository is also a Claude Code plugin (`.claude-plugin/plugin.json`) and a one-plugin marketplace (`.claude-plugin/marketplace.json`). Install it with `claude plugin marketplace add wangxuzhou666-arch/review-ratchet` and `claude plugin install review-ratchet@review-ratchet`, or load a checkout for one session with `claude --plugin-dir /path/to/review-ratchet`. Its `bin/` directory is on the Bash tool's PATH while the plugin is enabled, so the skills run `ratchet` directly.

- `/review-ratchet:setup` walks through `ratchet init`: it previews with `--dry-run`, explains each line, runs `--yes`, and shows the next steps.
- `/review-ratchet:review [level] [target]` runs `ratchet review`, shows the table and the counts, and offers the label page when findings pile up.

After a plugin update the install path changes. Pinned hooks (the default `.git/hooks`, or a hooks directory outside the repo) record that path, so run `/review-ratchet:setup` again to refresh them; portable hooks record none and need nothing.

## Undo

Everything `init` adds to a file you own is between `ratchet:begin` and `ratchet:end` markers (`<!-- ... -->` in `CLAUDE.md`, `# ...` in `.gitignore` and the hooks). Delete the blocks. Delete `.ratchet.json`, the digest and the rules copy if you want them gone too. A hook file that `init` created and that is now empty apart from `#!/bin/sh` can be removed. The ledger and `.ratchet/` are yours to keep or delete.
