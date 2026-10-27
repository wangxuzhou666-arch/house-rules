# review-ratchet

review-ratchet keeps an append-only ledger of code-review findings and what became of them, so review quality can only ratchet forward: every finding a reviewer (human, model, or an automated gate) raised is recorded once, and is later closed by a commit that says it fixes it (`Fixes-Finding: <id>` trailer), by a gate that passes over the file again, or by a person's label. The ledger is one JSON-lines file in your repo (`.style-findings.jsonl`, git-ignored), the tool is a single stdlib-only Python 3.10+ command, and per-repo behavior (rules files, tool-code mapping, repo slug) comes from an optional `.ratchet.json`. Recording and attribution are described in [docs/LEDGER.md](docs/LEDGER.md). `compile` turns your rules files into a short, deterministic digest that a coding session or a review loads as context ([docs/COMPILE.md](docs/COMPILE.md)). `init` and `review` get a new repo from nothing to a first clause-labeled finding ([docs/ONBOARDING.md](docs/ONBOARDING.md)). `gate` lets an existing lint or style gate feed the same ledger ([docs/GATES.md](docs/GATES.md)), and `vendor` copies the tool into a repo that cannot depend on an installed checkout ([docs/VENDORING.md](docs/VENDORING.md)).

## Install

As a Claude Code plugin, from the marketplace in this repository:

```sh
claude plugin marketplace add wangxuzhou666-arch/review-ratchet
claude plugin install review-ratchet@review-ratchet
```

From a local checkout, load it for one session without installing:

```sh
claude --plugin-dir /path/to/review-ratchet
```

Without the plugin, run the command straight from the checkout (Python 3.10+, standard library only):

```sh
/path/to/review-ratchet/bin/ratchet init
```

## Quickstart

With the plugin:

1. In your repo, run `/review-ratchet:setup`. It previews and then runs `ratchet init`: a rules file (the bundled template is a draft to adjust), the compiled style digest, an `@import` line in `CLAUDE.md`, git hooks (each is run once to prove it is reached) and `.gitignore` entries. It ends with a ready `git add ... && git commit` line for the files it created: run it, because the pre-commit hook checks the staged copies.
2. Run `/review-ratchet:review`. It runs Claude Code's `/code-review`, records the findings against your rules' clause IDs, and shows them in a table.

Without the plugin, from a terminal in your repo:

```sh
/path/to/review-ratchet/bin/ratchet init            # asks a few questions; --yes accepts the defaults, --dry-run only prints the plan
/path/to/review-ratchet/bin/ratchet review          # runs claude -p "/code-review high", records the findings
/path/to/review-ratchet/bin/ratchet count --by-rule # what is open, by clause
```

Commit the files `init` lists (the line it prints does it). Fix a finding by committing with a `Fixes-Finding: <id>` trailer, where `<id>` is the short id the review table shows (`r3-2`); the post-commit hook closes it. Put `bin/` on your PATH to type `ratchet` instead of the full path; hooks kept in a committed directory such as `.githooks` also find the tool through `PATH`, `$RATCHET_BIN` or `"bin"` in `.ratchet.json`. What `init` changes, and how to undo it: [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Usage

```sh
bin/ratchet init [--yes] [--dry-run]                                       # set up this repo (config, rules file, digest, CLAUDE.md, hooks, .gitignore)
bin/ratchet review [--level high] [--target T] [--from-file F] [--over-cap] # run /code-review, save the raw output, record the findings; 3 rounds per change, then exit 3
bin/ratchet ingest-code-review --source l1 --run R12 < review-output.txt   # JSON array, or text containing a ```json block
bin/ratchet add --source l2 --run R12 --input report.json                  # explicit report shape
bin/ratchet count [--by-rule [--label-source]]                             # totals, open findings, by source / rule / how the rule was labeled
bin/ratchet infer [--all] [--quiet]                                        # close findings from Fixes-Finding trailers and gate passes
bin/ratchet label-page                                                     # local HTML page to label what is still open
bin/ratchet apply-labels < labels.json                                     # record the labels copied from that page
bin/ratchet compile                                                        # rules files -> CODING-STYLE.digest.md (+ marker blocks in inject_targets)
bin/ratchet compile --check [--staged]                                     # write nothing; exit 1 listing every stale output (pre-commit / CI); --staged reads the git index
bin/ratchet gate record --input reports.json                               # a gate's findings [{file, line, code, message}] -> the ledger
bin/ratchet gate pass --files a.py b.py                                    # the gate passed over these files
bin/ratchet vendor --into third_party/ratchet [--check [--against-source]] # copy the tool into a repo / verify that copy
```

Every command but `vendor` takes `--repo PATH`; all but `init` and `vendor` also take `--ledger FILE` and `--config FILE`. Every command that writes takes `--dry-run`, which writes nothing and prints what it would have written (for `compile`, a diff; for `init`, the plan; for `review`, the command it would run).

To give Claude Code's built-in `/code-review` the rules by hand instead of `init`, run `ratchet compile` and add one line to the repo's `CLAUDE.md`:

```
@CODING-STYLE.digest.md
```

Tests: `python3 -m unittest discover -s tests`. Mutation self-test (applies one deliberate bug at a time to a temp copy and checks the suite goes red; `--full` lists every test that caught each one, `--jobs N` runs N at once): `python3 tests/mutation_check.py [--full] [--jobs N]`.

License: [MIT](LICENSE).
