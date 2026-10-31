# Fitting review-ratchet into daily work

The goal is to set it up once and then work as you do now. The review-to-rules loop runs by itself, and you are asked a question only when a review finds a blind spot (a finding no rule covers) that it has not asked you about before. This page is the map; each step links to the reference that has the detail.

## The workflow

1. **Install the plugin** ([README](../README.md#install)). The automatic wrap-up needs Claude Code 2.1.163 or newer.
2. **Run `/review-ratchet:setup` once in each repository.** It creates the rules file, the compiled digest, the git hooks and the `.gitignore` entries, records the [start point](WRAPUP.md#the-start-point) of the default branch (where its first wrap-up measures from), and asks whether to turn on the automatic wrap-up. Commit the files it lists ([Onboarding](ONBOARDING.md)).
3. **Commit as usual.** With the automatic wrap-up on, each commit made in a Claude Code session is noted in a small file on your machine ([the commit note](ONBOARDING.md#the-commit-note)).
4. **A turn ends after a commit, and the work since the last review has reached a threshold** (200 changed lines or 5 files by default). Claude runs the wrap-up review in the background. Below the threshold nothing runs ([Automatic wrap-up](WRAPUP.md#automatic-wrap-up)).
5. **Answer the one question, if there is one.** It is asked only about blind spots that are new: which of them are not real problems, and should the rest become rules now. Nothing new, nothing asked.
6. **Decide the rule changes.** `Now` starts `/review-ratchet:maintain`. It gathers code evidence and proposes at most 3 scoped changes; you accept, adjust, reject or defer each one ([Decision cards](DECISION-CARDS.md)). Accepted changes update the rules, the digest and `CODING-STYLE.changelog.md`. Commit them with your work, and the next review reads the new digest.
7. **Close findings with commits.** A fix committed with a `Fixes-Finding: <id>` trailer closes the finding ([Commit trailers](LEDGER.md#commit-trailers)).

You set up once and answer the occasional question. The noting, the measuring, the review and the record-keeping run by themselves. `/review-ratchet:review` and `/review-ratchet:wrapup` stay available for the times you want a review now.

## With and without the plugin

The plugin carries the Stop hook, which is the only thing that starts a wrap-up by itself. Without the plugin nothing triggers automatically, and you run the wrap-up yourself: at the end of a stretch of work, or from your own closing step, such as a ship or release command.

```sh
ratchet wrapup --dry-run   # what it would do, and why; runs and writes nothing
ratchet wrapup --json      # one JSON object on stdout; the human report goes to stderr
```

`wrapup` reviews only past the threshold, so calling it at every close is cheap. The object's `status` says what happened (`skipped`, `reviewed` or `capped`; exit code 3 means the round cap refused the review), and `new_unmapped` lists the blind spots that are new ([`--json`](WRAPUP.md#--json)). Show them to whoever decides, record a "not a real problem" with `ratchet label <id>... --outcome false_positive`, and turn the rest into rules with `/review-ratchet:maintain`.

Without the plugin, answer the init question about the automatic wrap-up with no (`ratchet init --no-wrapup-auto`). Otherwise the post-commit hook notes the commits made in a Claude Code session, and nothing reads the notes.

## Review output from elsewhere

A review run somewhere else (another tool, CI, a colleague's machine) can be ingested instead of calling `claude`: `ratchet review --from-file out.txt`, or `ratchet wrapup --from-file out.txt`, where the thresholds still apply. The file holds the JSON array a code review prints, or text that contains a fenced `json` array ([Commands](LEDGER.md#commands)). The findings get the same ids, rule labels and round as a native review, and ingesting the same output twice records nothing twice.

## Linked worktrees

A commit made with `git -C <linked worktree> commit` is noted for that worktree, since the hook runs at the top of the worktree the commit was made in. The wrap-up measures that worktree against the last review of its own branch. The ledger and `.ratchet.json` are the main worktree's and shared by every worktree: a `.ratchet.json` in a linked worktree is ignored, so `wrapup_auto` is set once. Keep the config committed, because a hook acts only where the top of the worktree holds one ([Linked worktrees](ONBOARDING.md#linked-worktrees)).

## Rule layers and promotion

`rules_files` is ordered, global first and the project's own last ([Layering](COMPILE.md#layering)). A layer outside the repository, such as a file shared by several projects, is read-only for ratchet: it reads the clauses and never writes them ([Outer layers](COMPILE.md#outer-layers)).

- **A blind spot goes into the project's own layer.** Proposals write a rules file of this repository, and one aimed at a shared file is refused ([Rules outside this repository](DECISION-CARDS.md#rules-outside-this-repository)).
- **Promotion to the shared layer is a separate step.** Make it in the repository that owns the shared file, as ratchet has no command for it: strip names, paths and other private details, decide it on its own merits, and leave the code evidence behind.
- **A shared layer has to be on the machine.** Without it the commands that need the whole rule set stop. The wrap-up's dry run reports that as a refusal of kind `rules`, and the automatic wrap-up tells you once.

## Privacy

- **Local only:** the pending records and `hook.log` in the state directory, the ledger (`.style-findings.jsonl`) and `.ratchet/` (saved reviews and evidence, proposal batches). `init` git-ignores the last two because they can hold code excerpts ([Privacy](WRAPUP.md#privacy)).
- **Committed:** the rules, the digest and `CODING-STYLE.changelog.md`. A changelog entry carries the evidence behind a rule change (file and line, a snippet of the code) and the decision, in the words of whoever made it. In a public repository, read an entry before you commit it.
- **The review itself** is Claude Code's `/code-review`: the code it reads goes to the model provider under Claude Code's settings.

## Cost

Below the thresholds nothing runs, as measuring is local git work. Past them, one wrap-up is one `/code-review`, at most one question and at most 3 proposals. A change gets at most `max_review_rounds` reviews (default 3); after that the person is told once, and no further review of that change starts by itself. A repo gets at most `wrapup_auto_max_per_day` automatic reviews (default 2) in any 24 hours: past that the person is told once and the next one waits until the window makes room, while a wrap-up you start yourself is neither counted nor held back ([The daily cap](WRAPUP.md#the-daily-cap)). Tune it with `wrapup_min_changed_lines`, `wrapup_min_files`, `max_review_rounds` and `wrapup_auto_max_per_day` ([Config](LEDGER.md#config-ratchetjson)), or turn the automatic wrap-up off with `"wrapup_auto": false`.
