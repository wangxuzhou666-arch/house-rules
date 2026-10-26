# review-ratchet

review-ratchet keeps an append-only ledger of code-review findings and what became of them, so review quality can only ratchet forward: every finding a reviewer (human, model, or an automated gate) raised is recorded once, and is later closed by a commit that says it fixes it (`Fixes-Finding: <id>` trailer), by a gate that passes over the file again, or by a person's label. The ledger is one JSON-lines file in your repo (`.style-findings.jsonl`, git-ignored), the tool is a single stdlib-only Python 3.10+ command, and per-repo behavior (rules files, tool-code mapping, repo slug) comes from an optional `.ratchet.json`. Phase 1 covers recording and attribution; see [docs/LEDGER.md](docs/LEDGER.md) for the event schema, config keys and conventions. Phase 2 (`compile`) turns the same rules files into a short, deterministic digest that a coding session or a review loads as context; see [docs/COMPILE.md](docs/COMPILE.md).

## Usage

```sh
bin/ratchet ingest-code-review --source l1 --run R12 < review-output.txt   # JSON array, or text containing a ```json block
bin/ratchet add --source l2 --run R12 --input report.json                  # explicit report shape
bin/ratchet count [--by-rule]                                              # totals, open findings, by source / rule
bin/ratchet infer [--all]                                                  # close findings from Fixes-Finding trailers and gate passes
bin/ratchet label-page                                                     # local HTML page to label what is still open
bin/ratchet apply-labels < labels.json                                     # record the labels copied from that page
bin/ratchet compile                                                        # rules files -> CODING-STYLE.digest.md (+ marker blocks in inject_targets)
bin/ratchet compile --check                                                # write nothing; exit 1 listing every stale output (pre-commit / CI)
```

Every command takes `--repo PATH`, `--ledger FILE` and `--config FILE`; every command that writes takes `--dry-run`, which writes nothing and prints what it would have written (for `compile`, a diff).

To give Claude Code's built-in `/code-review` the rules, run `ratchet compile` and add one line to the repo's `CLAUDE.md`:

```
@CODING-STYLE.digest.md
```

Tests: `python3 -m unittest discover -s tests`. Mutation self-test (applies one deliberate bug at a time to a temp copy and checks the suite goes red; `--full` lists every test that caught each one): `python3 tests/mutation_check.py [--full]`.
