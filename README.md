English | [中文](README.zh.md)

# review-ratchet

Turn code-review feedback into inspectable proposals for your own coding standards.
Your reviewer finds defects; the model compares them with your rules; you decide
which changes belong in the rulebook. The tool preserves the sources, validates
approved edits and feeds the updated digest into the next review.

```mermaid
flowchart LR
  A[Code change] --> B[Native code review]
  B --> C[Findings + source evidence]
  C --> D[Model: coverage + rule proposal]
  D --> E[Human: native decision question]
  E -->|Approve exact text| F[Rules + digest + decision receipt]
  E -->|Adjust / reject / defer| G[Preserved decision history]
  F --> B
```

The reference implementation is a Python 3.10+ CLI with no third-party runtime
dependencies and a coding-agent plugin. Findings live in a local, append-only
ledger; private review data is not uploaded by the harness. A native reviewer may
send code to its provider under that host's settings. This workflow does not train
model weights or guarantee a review-quality improvement.

Why the loop is built this way (the RL analogy and its limits, what went wrong along
the way, how to carry it to another reviewer, prior art): [docs/METHOD.md](docs/METHOD.md).

## Make it yours

| Bring your own | Blueprint supplied here | Stable contract |
|---|---|---|
| Coding style and risk policy | [Rule template](templates/CODING-STYLE.template.md), [writing guide](docs/RULE-WRITING.md) | Clause IDs, scope, severity and traceable edits |
| Review explanation and grouping | [Reviewer prompt](samples/config-replacement/reviewer-prompt.md) | File, line, failure scenario and normalized attribution |
| Human decision presentation | [Maintenance prompt](samples/config-replacement/maintenance-prompt.md), [bullet layout](docs/DECISION-CARDS.md) | Evidence, reason, exact change and explicit decision |
| Personal evaluation criteria | [Future metrics design](docs/DECISION-METRICS.md) | Original suggestions and actual responses retained |

Point `rules_files` at your `CODING-STYLE.md`; you do not need to adopt the author's
rules. Change presentation through your repository instructions or maintenance
skill. Custom review field names need an adapter to the canonical import format;
there is no arbitrary schema or popup-layout configuration engine today.
See [customization and its boundaries](docs/CUSTOMIZATION.md).

## Try the sample

The [configuration-replacement sample](samples/config-replacement/README.md) contains
a small rule corpus, intentionally faulty code, compliant controls, a transfer pair,
and reusable prompts. After cloning or downloading this repository:

```sh
python3 samples/config-replacement/check_examples.py
```

It injects a write failure and tests a serialization failure in disposable files.
The unsafe examples lose the previous configuration; the safe examples preserve it.
No model call or human approval is simulated by that command. To verify a clean
installation, hooks, review replay and finding closure without a model call:

```sh
python3 scripts/check_acceptance.py --output-dir /absolute/new/scratch-directory
```

For a real review, use the native reviewer with the sample's reviewer prompt, then
the maintenance prompt to obtain a source-backed decision question. The sample
explains how to start without REL.1 and evaluate a proposed addition, or start with
REL.1 and recognize existing coverage. Read the [acceptance record](docs/ACCEPTANCE.md)
for observed results and limitations; the examples are not a general benchmark.

## Install

As a Claude Code plugin, from the marketplace in this repository:

```sh
claude plugin marketplace add wangxuzhou666-arch/review-ratchet
claude plugin install review-ratchet@review-ratchet
```

The marketplace commands require a published repository revision. For an unpublished
checkout, use the local loading route below; local acceptance does not verify a remote install.

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
3. When you finish a stretch of work, run `/review-ratchet:wrapup` (or say "wrap up"). It counts what changed since the last review, reviews once only if enough did (`wrapup_min_changed_lines`, default 200, or `wrapup_min_files`, default 5), and asks one question only when the review found something no rule covers. See [wrap-up](docs/WRAPUP.md).
4. To maintain the rules from those findings, run `/review-ratchet:maintain`. The agent gathers code evidence, proposes scoped changes, and asks through the host's native question tool. Accepted changes update the rules and digest; rejection, deferral and requests for revision are also recorded. No custom UI or manual JSON copying is required. See [the workflow and assessment contract](docs/DECISION-CARDS.md).

Without the plugin, from a terminal in your repo:

```sh
/path/to/review-ratchet/bin/ratchet init            # asks a few questions; --yes accepts the defaults, --dry-run only prints the plan
/path/to/review-ratchet/bin/ratchet review          # runs claude -p "/code-review high", records the findings
/path/to/review-ratchet/bin/ratchet wrapup          # counts the work since the last review; reviews once only if enough changed
/path/to/review-ratchet/bin/ratchet count --by-rule # what is open, by clause
```

Commit the files `init` lists (the line it prints does it). Fix a finding by committing with a `Fixes-Finding: <id>` trailer, where `<id>` is the short id the review table shows (`r3-2`); the post-commit hook closes it. Put `bin/` on your PATH to type `ratchet` instead of the full path; hooks kept in a committed directory such as `.githooks` also find the tool through `PATH`, `$RATCHET_BIN` or `"bin"` in `.ratchet.json`. What `init` changes, and how to undo it: [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Usage

Bring your own `CODING-STYLE.md`, risk policy and native-question presentation. The
bundled style is a blueprint; the evidence and approval contracts remain stable.
See [customization](docs/CUSTOMIZATION.md), [rule writing](docs/RULE-WRITING.md) and
the future [personal decision metrics](docs/DECISION-METRICS.md) direction.

```sh
bin/ratchet init [--yes] [--dry-run]                                       # set up this repo (config, rules file, digest, CLAUDE.md, hooks, .gitignore)
bin/ratchet review [--level high] [--target T] [--from-file F] [--over-cap] # run /code-review, save the raw output, record the findings; 3 rounds per change, then exit 3
bin/ratchet wrapup [--level high] [--base REF] [--from-file F] [--json] [--dry-run] # count what changed since the last review; review once if enough did (docs/WRAPUP.md); exit 3 at the round cap
bin/ratchet ingest-code-review --source l1 --run R12 < review-output.txt   # JSON array, or text containing a ```json block
bin/ratchet add --source l2 --run R12 --input report.json                  # explicit report shape
bin/ratchet count [--by-rule [--label-source]]                             # totals, open findings, by source / rule / how the rule was labeled
bin/ratchet decision-context [--finding ID] [--assessment FILE]            # evidence packet or validated model assessment, read-only
bin/ratchet prepare-decisions --assessment FILE [--finding ID]            # persist analysis and proposed edits for native human questions
bin/ratchet apply-decisions --input FILE                                  # record actual answers and apply accepted groups
bin/ratchet decision-summary                                             # author-specific accept/revise/reject/defer counts, not accuracy
bin/ratchet rollback-proposals --batch ID [--dry-run]                      # restore the rules, digest and changelog of an applied batch, unless they were edited since
bin/ratchet recover-proposals                                              # restore the files of an interrupted apply or rollback (takes no --dry-run: it only restores)
bin/ratchet infer [--all] [--quiet]                                        # close findings from Fixes-Finding trailers and gate passes
bin/ratchet label r3-2 r3-5 --outcome false_positive [--note TEXT]         # record an outcome by id (the short ids the review table shows), no label page; all or nothing
bin/ratchet label-page                                                     # local HTML page to label what is still open
bin/ratchet apply-labels < labels.json                                     # record the labels copied from that page
bin/ratchet compile                                                        # rules files -> CODING-STYLE.digest.md (+ marker blocks in inject_targets)
bin/ratchet compile --check [--staged]                                     # write nothing; exit 1 listing every stale output (pre-commit / CI); --staged reads the git index
bin/ratchet gate record --input reports.json                               # a gate's findings [{file, line, code, message}] -> the ledger
bin/ratchet gate pass --files a.py b.py                                    # the gate passed over these files
bin/ratchet vendor --into third_party/ratchet [--check [--against-source]] # copy the tool into a repo / verify that copy
```

Every command but `vendor` takes `--repo PATH`; all but `init` and `vendor` also take `--ledger FILE` and `--config FILE`. Every command that writes takes `--dry-run`, except `recover-proposals`, which only restores what an interrupted write left half done. `--dry-run` writes nothing and prints what it would have written (for `compile`, a diff; for `init`, the plan; for `review`, the command it would run; for `wrapup`, the measurement and whether it would review).

To give Claude Code's built-in `/code-review` the rules by hand instead of `init`, run `ratchet compile` and add one line to the repo's `CLAUDE.md`:

```
@CODING-STYLE.digest.md
```

Tests: `python3 -m unittest discover -s tests`. Mutation self-test (applies one deliberate bug at a time to a temp copy and checks the suite goes red; `--full` lists every test that caught each one, `--jobs N` runs N at once): `python3 tests/mutation_check.py [--full] [--jobs N]`.

Contributing: synthetic failing examples, regression pairs and rule proposals are welcome; findings from real private repositories are not. See [CONTRIBUTING.md](CONTRIBUTING.md).

License: [MIT](LICENSE).
