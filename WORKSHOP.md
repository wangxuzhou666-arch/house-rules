English | [中文](WORKSHOP.zh.md)

# Workshop: from a review finding to a rule, and whether the rule holds

A hands-on walk through house-rules in six short sections, 45 to 60 minutes in all. You read a rules file, replay a recorded review, turn a gap the review exposed into a rule, look at the one measurement this project has, set up the automation and its limits, and try to improve a rule.

The main line is free and deterministic: it replays recordings and calls no model. The two steps that could spend money (one in section 5, one in section 6) say so before you run them, and you can skip both.

What this workshop does not give you is evidence that an agent writes better code under these rules. The project does not claim that. It gives you a way to write down what a reviewer keeps finding, decide rule by rule what becomes a rule, and check whether a rule changes what the reviewer does. Sections 4 and 6 show how hard that last check is.

Alone or in a group, each section ends with a checkpoint to say out loud. Sections 4 and 6 add questions to discuss. The commands themselves run in seconds; the time goes on reading the output and doing the exercises.

## Before you start

You need `git` 2.28 or newer (with a name and email set, because you will commit), Python 3.10 or newer, and a shell. The tool uses the Python standard library only. Claude Code is not needed for the free path.

```sh
git clone https://github.com/wangxuzhou666-arch/house-rules
cd house-rules
export HR_SRC="$PWD"
export PATH="$HR_SRC/bin:$PATH"
house-rules --help
```

- Clone it; do not download a zip. Section 4 needs a git repository and stops without one.
- Use one terminal for the whole workshop, because the two `export` lines live in it. If your `python3` is older than 3.10, put a newer one first on `PATH` in this terminal.
- Sections 1, 2, 3 and 5 work in a scratch repository, `hr-demo`, next to your clone. Sections 4 and 6 work in the clone itself, and section 6 undoes its edits at the end.
- The output blocks below were recorded in one run. Hashes, dates, paths and a few byte counts will differ in yours; the shape should not.

Words used throughout:

| Word | Meaning here |
|---|---|
| rules file | Your standards as clauses with stable ids, such as `ERR.1`: `CODING-STYLE.md` |
| digest | The short form of the rules file, compiled into `CODING-STYLE.digest.md`. The coding session and the reviewer both read it, so it has a byte budget |
| reviewer | A code review that is not part of this project: Claude Code's built-in `/code-review`, or any review whose output you import |
| finding | One defect the reviewer reports, with a file, a line and a label (a clause id, or `unmapped`) |
| blind spot | A real defect that no clause covers, so its label is `unmapped` |
| ledger | The local file `.style-findings.jsonl` that records findings and what became of them. It is git-ignored |

---

## 1. The rules file is your ruler (about 8 minutes)

**Goal.** Meet the three things everything else hangs on: a rules file with clause ids, the digest compiled from it, and the digest's byte budget. See the pre-commit hook refuse a commit whose digest is stale.

**Commands.**

```sh
cd "$HR_SRC"
mkdir ../hr-demo && cd ../hr-demo && git init -q -b main
# the sample rules without REL.1: lines 6 to 9 hold REL.1 and its notes, so ERR.1 and R.1 stay
sed '6,9d' "$HR_SRC/samples/config-replacement/CODING-STYLE.md" > CODING-STYLE.md
cat CODING-STYLE.md
house-rules init --dry-run --no-wrapup-auto --rules-file CODING-STYLE.md
house-rules init --yes --no-wrapup-auto --rules-file CODING-STYLE.md
house-rules compile --check --strict
wc -c CODING-STYLE.digest.md
git add .house-rules.json CODING-STYLE.md CODING-STYLE.digest.md CLAUDE.md .gitignore
git commit -q -m "chore: adopt house-rules"
```

The corpus is two clauses and a note:

```text
# Sample project rules

This is a small teaching corpus, not a required style for other repositories.

- **[ERR.1]** Propagate configuration parsing errors; do not silently substitute a successful-looking default. (should-fix)
- **[R.1]** Review correctness as well as these rules. Use unmapped for a real defect no code rule covers.
```

`--dry-run` prints what `init` would do, one diff per file, and writes nothing. The real run writes the config, the digest, an `@import` of the digest in `CLAUDE.md` (so Claude Code and `/code-review` read it), two git hooks and `.gitignore` entries. Then read the digest:

```sh
cat CODING-STYLE.digest.md
```

Now try the guard. Edit the rules file, stage only that edit and commit:

```sh
sed -i.bak 's/small teaching corpus/small teaching corpus for the workshop/' CODING-STYLE.md && rm CODING-STYLE.md.bak
git add CODING-STYLE.md
git commit -m "docs: say what the corpus is for"
```

The commit is refused. Regenerate the digest, stage it and commit again:

```sh
house-rules compile
git add CODING-STYLE.digest.md
git commit -m "docs: say what the corpus is for"
```

**What you should see.**

```text
  config           added        .house-rules.json: rules_files ['CODING-STYLE.md'], wrapup_auto false
  compile          added        digest CODING-STYLE.digest.md: 925 bytes
  CLAUDE.md        added        @CODING-STYLE.digest.md under `## Code style digest (house-rules)`, in a new CLAUDE.md ...
  hook pre-commit  added        .git/hooks/pre-commit (runs with <python>); live check: the block ran
  hook post-commit added        .git/hooks/post-commit (runs with <python>); live check: the block ran
  .gitignore       added        .style-findings.jsonl, .house-rules-label-page.html, .house-rules-label-batch.json, .house-rules/
  start point      skipped      no commit yet
compile --check: 1 output(s) up to date
     925 CODING-STYLE.digest.md
```

`init` also prints a ready `git add ... && git commit` line (the commands above do the same) and three `next` hints. The digest has one line per clause (`ERR.1 | should-fix | Propagate configuration parsing errors`), one per reviewer rule, and a generated Labeling section that tells the reviewer to answer with a `category` (the clause id in kebab-case, or `unmapped`) and a `severity`. The refused commit prints:

```text
stale: CODING-STYLE.digest.md (digest, the STAGED version is out of date)
this check reads what is staged, not the working tree: run `house-rules compile` if the working tree is stale too, then `git add CODING-STYLE.digest.md`
```

**Swap in your own.** Point `init` at a rules file of your own (`house-rules init --rules-file docs/CODING-STYLE.md` in your repository), or write three clauses of your own in this shape, in a scratch copy of the sample:

```text
- **[ID]** What must hold. (should-fix) <!-- digest: a shorter text for the digest -->
```

Run `compile --check --strict` and read the line your reviewer will actually see. The digest is loaded into every session, which is why it is capped (4096 bytes by default) and why a long clause needs a short `digest:` text.

**Checkpoint.** You can say (1) what one digest line contains, (2) why the digest has a byte budget, and (3) what the pre-commit hook refuses, and why it checks the staged copy.

**Go deeper.** [Your CODING-STYLE.md](docs/CUSTOMIZATION.md#your-coding-stylemd) · [Phrasing and the digest budget](docs/RULE-WRITING.md#phrasing-and-the-digest-budget) · [Compile reference](docs/COMPILE.md) · [What `init` changes and how to undo it](docs/ONBOARDING.md)

---

## 2. The reviewer, by replay (about 10 minutes)

**Goal.** See a real review arrive in the ledger without calling a model. The recording is the raw output of one real `/code-review`, made on three small files with a known defect. You import it as if you had just run it, and learn what a finding is, what `unmapped` means, and what "replay, not another round" means.

The recording was made when the rules already had REL.1. Yours does not, and that is the point: the same defect now looks like a blind spot.

**Commands.**

```sh
cd "$HR_SRC/../hr-demo"
# the three files that review saw (the published safe_config.py and heldout_safe.py are newer: do not use them here)
cp "$HR_SRC"/samples/config-replacement/reviewed/*.py .
cat heldout_unsafe.py
house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run live-r3
house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run live-r3
house-rules count --by-rule --label-source
```

The second import is on purpose.

**What you should see.** `heldout_unsafe.py` opens the settings file for writing, which empties it, and only then serializes the values. The first import:

```text
review round 1/3 for main@<12 hex>
saved raw review output to .house-rules/reviews/live-r3.txt
id         rule_id   severity    location             summary
live-r3-1  unmapped  should-fix  heldout_unsafe.py:7  path.open("w") truncates the existing settings file before…
live-r3-2  unmapped  nice        config_store.py:18   The replacement is written to a fresh temp file and os.repl…
review: +2 finding(s) from 2 item(s) (0 merged by file+line, 2 unmapped, 0 labeled from the text, 0 without a usable severity), 0 already recorded
open: 2. A commit with a `Fixes-Finding: <id>` trailer closes a finding.
```

The second import records nothing: `review: replay of run live-r3 (already recorded; not another round)` and `no new findings`. Then `count`:

```text
findings: 2 total, 2 open (gate 0 / l1 2 / l2 0); events: 3
  unmapped: 2 (model 2)
review rounds on main@<12 hex>: 1/3
```

Finding 1 is a real delete-before-write defect that your two rules do not cover. The recorded text says `"category": "rel-1"`, a clause your corpus does not have, so the importer cannot resolve the label and files it as `unmapped`: a blind spot. Finding 2 (the replacement file loses the old file's permission bits) is a different, smaller bug.

**Swap in your own.** Import a review of your own. Save any reviewer's output as a JSON array of objects with `file`, `line`, `summary`, `failure_scenario`, `category` (a kebab-case clause id, or `unmapped`) and `severity` (`blocker`, `should-fix` or `nice`), then:

```bash
house-rules review --from-file mine.json --run mine-1
```

Other field names need an adapter of your own; there is no mapping setting. The example object is in [Your review output](docs/CUSTOMIZATION.md#your-review-output).

**Checkpoint.** You can explain why a second import costs nothing and does not use a round, what `unmapped` means and why the digest tells the reviewer to prefer it to stretching a clause, and that a change gets at most 3 review rounds (you used 1).

**Go deeper.** [Review rounds](docs/LEDGER.md#review-rounds) · [Rule ids](docs/LEDGER.md#rule-ids) · [Review output from elsewhere](docs/INTEGRATION.md#review-output-from-elsewhere) · [the sample's README](samples/config-replacement/README.md)

---

## 3. A blind spot becomes a rule (about 10 minutes)

**Goal.** Walk the decision step from end to end: evidence, an assessment, a proposal, a recorded human answer, then a rule edit with a new digest and a changelog entry.

In Claude Code the model writes the assessment and a native popup asks you (`/house-rules:maintain`). Here no model is called, so a small helper, [`make_answers.py`](samples/config-replacement/make_answers.py), plays both parts and fills in the ids that differ per repository. Everything else is the real tool. The helper proposes for finding 1 a new clause, REL.1, and for finding 2 "keep, no rule", and it answers accept and defer.

**Commands.**

```sh
cd "$HR_SRC/../hr-demo"
mkdir .house-rules/work
house-rules decision-context > .house-rules/work/context.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" assessment .house-rules/work/context.json > .house-rules/work/assessment.json
house-rules decision-context --assessment .house-rules/work/assessment.json > .house-rules/work/cards.json
house-rules prepare-decisions --assessment .house-rules/work/assessment.json > .house-rules/work/prepared.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" answers .house-rules/work/prepared.json > .house-rules/work/answers.json
house-rules apply-decisions --dry-run --input .house-rules/work/answers.json
```

The tool reads these files only from inside the repository, and `.house-rules/` is git-ignored, so the scratch files neither get committed nor count as work in section 5. Open `assessment.json` (what the model says), `cards.json` (what the person would be shown: the finding, the code, the exact before and after text) and `answers.json` (what the person says). Nothing has changed yet.

Now apply the answers, check the result and commit it:

```sh
house-rules apply-decisions --input .house-rules/work/answers.json
house-rules compile --check --strict
house-rules decision-summary
git diff --stat
wc -c CODING-STYLE.digest.md
git add CODING-STYLE.md CODING-STYLE.digest.md CODING-STYLE.changelog.md
git commit -q -m "rules: add REL.1"
```

**What you should see.** The dry run and the real run print the same object, with `"status": "preview"` and then `"applied"`:

```text
{
  "accepted": 1,
  "answer_counts": { "accept": 1, "defer": 1 },
  "batch_id": "<64 hex>",
  "changed": ["CODING-STYLE.md", "CODING-STYLE.digest.md", ".house-rules/proposals/version.json", "CODING-STYLE.changelog.md"],
  "rejected": 0,
  "status": "applied"
}
```

`compile --check` exits 0, `decision-summary` reports one accepted change and one deferred non-change, and `git diff --stat` shows `CODING-STYLE.md | 3 +++` and `CODING-STYLE.digest.md | 3 ++-`. The digest grows from 925 to 1063 bytes and gains a `REL.1 | should-fix | Preserve usable configuration until ...` line. `CODING-STYLE.changelog.md` is new: the exact clause text, why, the evidence (finding id, `heldout_unsafe.py:7`, the code snippet) and the decision in words: `Decision: accept by workshop learner — "Yes, add REL.1 as written."`

Two rules of the tool are worth showing. A group with no explicit answer is never applied: a default is not consent. And accepting a rule does not mark the finding it came from as fixed; closing it is a separate act (section 5).

**Swap in your own.** Do this after the dry run and before the real `apply-decisions`; nothing is written until then. Change the clause to your own wording, check it, and prepare a new batch:

```bash
sed 's/Preserve the existing usable configuration until/Keep the old configuration until/' .house-rules/work/assessment.json > .house-rules/work/assessment-b.json
house-rules decision-context --assessment .house-rules/work/assessment-b.json > .house-rules/work/cards-b.json
house-rules prepare-decisions --assessment .house-rules/work/assessment-b.json > .house-rules/work/prepared-b.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" answers .house-rules/work/prepared-b.json --confirmed-by "Your Name" > .house-rules/work/answers-b.json
house-rules apply-decisions --dry-run --input .house-rules/work/answers-b.json
```

Then apply `answers-b.json` instead of `answers.json`. To see what a different answer does, change `accept` to `revise` in a copy of the answers and dry-run that: `changed` comes back empty, because the tool records a request to revise and applies nothing (its counters file the answer under `rejected`). For the real thing, `/house-rules:maintain` does the model's part and asks through the host's native question tool (Claude Code needed; not part of this free path).

**Checkpoint.** You can say who did what: the tool kept the evidence and validated; the model proposed; the person decided; nothing changed without an `accept`. You can find the exact clause text and the decision in `CODING-STYLE.changelog.md`.

**Go deeper.** [Reusable assessment prompt](docs/DECISION-CARDS.md#reusable-assessment-prompt) · [Native human decisions](docs/DECISION-CARDS.md#native-human-decisions) · [Write rules that can be applied and traced](docs/RULE-WRITING.md) · [the sample's maintenance prompt](samples/config-replacement/maintenance-prompt.md)

---

## 4. Does your judgment generalize? (about 10 minutes)

**Goal.** Meet the only measurement in the project and read it without over-reading it. The question: after a rule is learned from one finding, does it make the reviewer flag a new instance of the same mechanism, label it with the rule's id, and leave a legitimate look-alike alone? Everything here replays recorded reviews: free, deterministic, no model.

**Commands.**

```sh
cd "$HR_SRC"
python3 -I samples/generalization/cases/rel-1/oracle.py
cat samples/generalization/cases/rel-1/code/*.py
python3 samples/generalization/run_check.py --round 1
python3 samples/generalization/run_check.py
git status --short
```

The oracle runs both files under an injected failure and prints one line per fact. It decides what is true from behavior, not from a model. The two files are the violation (`site_publisher.py`, which deletes the live directory before rendering) and its control (`preview_cache.py`, a cache that is rebuilt on demand, which the rule exempts). `run_check.py --round 1` scores the recorded reviews of round 1; with no argument it scores all three rounds and compares them. Both exit 0 even when verdicts are FAIL.

**What you should see.** The oracle ends with nine `ok` lines, such as:

```text
ok   site_publisher: injected render failure reaches the caller: True (expected True)
ok   site_publisher: earlier pages still served after the failed publish: False (expected False)
ok   preview_cache: every preview is obtainable with current content after the failed rebuild: True (expected True)
```

`False (expected False)` is the violation losing its pages, as intended. Round 1 prints one table; here are four of its rows (the two trailing `cited` columns, a secondary count with no pass line, are left out):

```text
case    clause  control    condition     runs ok  violation labeled  violation flagged  control labeled  control flagged  verdict
rel-1   REL.1   exempt     with_rule     3/3      3/3                3/3                3/3              3/3              FAIL
rel-1   REL.1   exempt     without_rule  3/3      0/3                3/3                0/3              3/3              -
rel-3b  REL.3   compliant  with_rule     3/3      2/3                3/3                0/3              3/3              PASS
rel-3b  REL.3   compliant  without_rule  3/3      0/3                3/3                0/3              3/3              -
...
PASS 1  FAIL 6  INCOMPLETE 0
```

and the run ends with `wrote samples/generalization/results-round-1.json and samples/generalization/results-round-1.md`. The full replay prints a table for each of the three rounds, then `notes:` under rounds 2 and 3 saying the rules changed since recording; those rounds were recorded with rewritten wording that was later rolled back (section 6). On an unchanged tree `git status --short` prints nothing: the replay reproduces the committed result files byte for byte.

How to read a row. Each case has 3 runs per condition. `with_rule` reviews saw the repository's digest; `without_rule` reviews saw the same digest minus that one clause. *Labeled* means a finding on that file carries the clause id as its category; *flagged* means a finding on that file with any label. The pass line, on `with_rule` only: the violation labeled in at least 2 of 3 runs and the control labeled in 0 of 3.

Read the two conditions side by side. Without the rule, the violation is flagged 3 of 3 and labeled 0 of 3: the reviewer finds the bug anyway, and the rule changes the label. With the rule, `rel-1` labels the exempt control as often as the violation (3 of 3 and 3 of 3), so it fails the pass line. In round 1 only `rel-3b` passes. Say it as plainly as the README does: the check is small (4 rules, 7 cases, 3 runs per condition, one reviewer version, one author) and mostly says "not yet".

**Swap in your own.** Offline, with no reviewer: choose one rule of your own, write a violation file and a look-alike control that complies or falls under the rule's stated exemption, and write an `oracle.py` in the shape of `samples/generalization/cases/rel-1/oracle.py`: inject a failure, print one line per fact, exit 0 only if the violation breaks and the control does not. The oracle is what keeps the control honest. In this project the control of `rel-3` turned out to be defective: the reviewer was right and the case was wrong, which is why `rel-3b` exists. Recording your case with a real reviewer is a paid step; section 6 describes it.

**Checkpoint.** You can read one row and say what each count is, state the pass line, and explain why "violation flagged 3 of 3 without the rule" neither shows the rule is useless nor that it works.

**Questions to discuss.** What would a rule have to do to count as "generalizing"? Why does the check keep a control next to every violation?

**Go deeper.** [Generalization check: layout and `--live`](samples/generalization/README.md) · [Pre-registered criteria and addenda](samples/generalization/CRITERIA.md)

---

## 5. Automation, and what it costs (about 10 minutes)

**Goal.** See the pieces that make the loop run by itself (hooks, thresholds, caps, the plugin) and the settings that bound the spend. Every command is offline; the one optional step that can spend money is marked.

**Commands.** In the scratch repo from section 3. First, would a wrap-up review run now? Measuring uses local git only and bills nothing.

```sh
cd "$HR_SRC/../hr-demo"
house-rules wrapup --dry-run
seq 1 25 | sed 's/.*/value_& = &/' >> config_store.py
house-rules wrapup --dry-run
cat > .house-rules.json <<'EOF'
{
  "rules_files": ["CODING-STYLE.md"],
  "wrapup_auto": false,
  "wrapup_min_changed_lines": 20
}
EOF
house-rules wrapup --dry-run
```

Next, close one finding with a commit trailer and record one judgment by hand:

```sh
cp heldout_safe.py heldout_unsafe.py
git add heldout_unsafe.py
git commit -m "fix: serialize before opening the file" -m "Fixes-Finding: live-r3-1"
house-rules count --by-rule --label-source
house-rules label live-r3-2 --outcome dismissed --note "real, but outside this sample"
house-rules count --by-rule --label-source
```

Last, the round cap. Three more imports use new run ids; the fourth is refused before any reviewer would run. This adds duplicate findings to the ledger, which is fine in a scratch repo:

```sh
for r in again-1 again-2 again-3 again-4; do house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run $r; done
```

**What you should see.** The first dry run measures your rule change as nothing (the rules file, digest and changelog are not counted as work) and skips:

```text
wrapup: 0 changed line(s) in 0 file(s) since <tree> (the working tree as review live-r3 found it)
wrapup: skipped, changed lines 0 < wrapup_min_changed_lines 200 and changed files 0 < wrapup_min_files 5; no review run
wrapup: open unmapped findings: 2
```

After the 25 added lines it still skips (`25 < 200`, `1 < 5`). After you lower the line threshold to 20 it counts the config edit as well and reports a review as due; the numbers will be near these:

```text
wrapup: 32 changed line(s) in 2 file(s) since <tree> (the working tree as review live-r3 found it)
[dry-run] wrapup: changed lines 32 >= wrapup_min_changed_lines 20; would run `claude -p '/code-review high <range>'`; nothing run or written
```

Either threshold is enough. The trailer commit prints `infer: +1 fixed (auto:commit), +0 fixed (auto:gate_pass); open 1`, and `count` now shows `1 open` instead of the `2 open` of section 2; `label` prints `label: 1 human outcome(s), dismissed (live-r3-2)` and `count` then shows `0 open`. The first import of the cap loop shows something new: the same recorded text now resolves to `REL.1` on `heldout_unsafe.py:7`, because the clause exists. Runs 2 and 3 exit 0; run 4 is refused with exit status 3:

```text
house-rules: error: round cap reached for main@<12 hex>: 3/3 rounds used; 6 findings still open (house-rules count). Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded).
```

**The controls.** These are the knobs that bound what the automation can do and spend. All live in `.house-rules.json` ([the table with defaults](docs/LEDGER.md#config-house-rulesjson)):

| Knob | Default | What it bounds |
|---|---|---|
| `wrapup_min_changed_lines`, `wrapup_min_files` | 200, 5 | Below both, no wrap-up review runs and nothing is billed. Either one is enough |
| `max_review_rounds` | 3 | Reviews per change; the next is refused before the reviewer runs (`--over-cap` overrides, recorded) |
| `wrapup_auto` | off unless `init` turns it on | Whether a commit can start a wrap-up by itself |
| `wrapup_auto_max_per_day` | 2 | Automatic reviews asked for or started in any rolling 24 hours; past it, exit 4 and one notice. The book is `.house-rules/auto-reservations.jsonl` |
| `digest_max_bytes` | 4096 | How large the digest may grow; a rule that would push it over is refused |

**With the plugin (needs Claude Code; not part of the free path).** Install it, run setup once per repository, and commit as usual. When the work since the last review crosses a threshold, the Stop hook starts one wrap-up review in the background and asks you at most one question, only for a new blind spot. This needs Claude Code 2.1.163 or newer.

```bash
claude plugin marketplace add wangxuzhou666-arch/house-rules
claude plugin install house-rules@house-rules
```

Then `/house-rules:setup` in your repository. The other skills are `/house-rules:review`, `/house-rules:wrapup` and `/house-rules:maintain`.

**Optional, costs money.** One real review of a repository of your own: `house-rules review --level high` (or `/house-rules:wrapup`). In the author's runs a two-file change cost roughly $0.2 and took about 30 seconds; a very large range cost far more (one 9,000-line range took millions of tokens). That is an estimate, not a recorded figure, and your plan, model and change decide the real one.

**Swap in your own.** Put your own numbers in `.house-rules.json` and re-run `wrapup --dry-run` until you can predict whether a review would run for the last stretch of work in your own repository.

**Checkpoint.** You can say what starts a review, what stops it (the thresholds, the round cap, the daily cap), where the daily-cap book lives, and what a commit trailer does.

**Go deeper.** [Thresholds](docs/WRAPUP.md#thresholds) · [The daily cap](docs/WRAPUP.md#the-daily-cap) · [Cost, limits and versions](docs/WRAPUP.md#cost-limits-and-versions) · [If nothing happens](docs/WRAPUP.md#if-nothing-happens) · [Cost](docs/INTEGRATION.md#cost) · [With and without the plugin](docs/INTEGRATION.md#with-and-without-the-plugin)

---

## 6. Tune your ruler (about 10 minutes)

**Goal.** Try to improve a rule, and learn why the replay cannot tell you whether you did. This is where the project's own tuning attempt, and what went wrong in it, becomes your discussion material.

**Commands.** In the clone, whose `CODING-STYLE.md` has REL.1. Reword the REL.1 digest line, recompile, replay round 1 again, and see what changed. The edits are undone in the last step.

```sh
cd "$HR_SRC"
python3 - <<'EOF'
p = "CODING-STYLE.md"
t = open(p, encoding="utf-8").read()
old = "Keep usable state until replacement is ready and validated; retain or restore it on failure; disposable rebuildable temporary artifacts are exempt"
new = "Never delete the old state before the new one is ready; restore it on failure; rebuildable temporary files are exempt"
assert t.count(old) == 1
open(p, "w", encoding="utf-8").write(t.replace(old, new))
EOF
house-rules compile
python3 samples/generalization/run_check.py --round 1 | tail -8
git status --short
git diff samples/generalization/results-round-1.md
python3 samples/generalization/run_check.py --live --round 4 --case rel-1
```

**What you should see.** The digest shrinks (`compile: 1 written, 0 unchanged; digest CODING-STYLE.digest.md: 4053 bytes (limit 4096)`). The replay prints the same scores as before, plus a note per affected case:

```text
notes:
  rel-1: the rules changed since recording: the REL.1 line of the digest now differs from the recorded one; the scores describe the recorded digests
  rel-1b: the rules changed since recording: ...
```

`git status` shows `CODING-STYLE.md`, `CODING-STYLE.digest.md` and `samples/generalization/results-round-1.*` modified. The replay writes the note into the tracked result files (that is all `git diff` shows there: a `Notes:` list under the unchanged scores), which is why the last step undoes it. The live command only plans; it makes no call and exits 2:

```text
not started: pass --yes-spend 6 to confirm exactly this many calls
live plan: 6 reviewer call(s) planned into round 4 (1 case(s) x 2 conditions x 3 run(s) at /code-review high), plus at most 6 more as repeats ...; this spends real money
```

The point: a replay scores each recording against the digest it was made with, so editing a rule can never move a score. Only a new live round can show whether your edit helped, and that costs real reviews: 6 calls for one case, 42 for all seven.

**Optional, costs money.** If you have Claude Code and want the answer for one case, the plan command above with `--yes-spend 6` runs it, while your edit is still in place; then replay and read the new round:

```bash
python3 samples/generalization/run_check.py --live --round 4 --case rel-1 --yes-spend 6
python3 samples/generalization/run_check.py
```

At roughly $0.2 per review (the author's estimate) that is about $1.2, and a few minutes. The recordings stay in your clone; write the result down whichever way it falls.

**Undo.** Restore your clone (any recordings you made stay as untracked files):

```sh
git checkout -- CODING-STYLE.md CODING-STYLE.digest.md samples/generalization
```

**Discussion: what the project's own tuning attempt found.** The author rewrote REL.1 and REL.2 so their digest lines lead with the exemption, shortened the Labeling instruction, and recorded two more rounds (72 more reviews). The three rounds are all in `results-compare.md`.

1. **An exemption written into the digest did not stop the mislabel.** The exempt control of `rel-1` was labeled REL.1 in 3 of 3 runs in round 1 and again in 3 of 3 in round 3 with the new wording (the held-out `rel-1b`: 2 of 3 both times). The reviewer reports problems in the control with or without the rule; the rule changes the label.
2. **Shortening the Labeling instruction made `category` vanish.** This is an inference from the counts, not a tested cause. Count the `with_rule` runs whose output has no `category` anywhere:

   ```sh
   cd "$HR_SRC/samples/generalization/recordings"
   for r in 1 2 3; do echo "round $r: $(grep -L '"category"' round-$r/*/with_rule-?.txt | wc -l | tr -d ' ') of $(ls round-$r/*/with_rule-?.txt | wc -l | tr -d ' ') runs have no category"; done
   ```

   You get 7 of 21, then 17 of 18, then 6 of 18: the label went missing in round 2 and came back when the old text did. The label, not the finding, is the weak link.
3. **Three runs per condition is noisy.** The clause text of `rel-4` did not change between rounds 1 and 3, yet its violation went from labeled 1 of 3 to 3 of 3.

The outcome: the rewrites showed no improvement, so the author rolled REL.1 and REL.2 back to the round-1 wording; the current digest is byte-identical to round 1's. Ask the group: what would you test next? What would make a 3-run result trustworthy: more runs, a fixed model, a second reviewer, a different label?

**Swap in your own.** Take one rule of your own from section 1 or 3. Reword its digest line, then write down, before any paid run, what the reviewer will label and what it will do with your control. If you have budget, record one case of your own with `--live` and note the result honestly, pass or fail.

**Checkpoint.** You can explain why a replay cannot confirm a rule edit, what it costs to find out, and the three lessons in your own words.

**Go deeper.** [Criteria and addenda 2 and 3](samples/generalization/CRITERIA.md) · [Why the loop is built this way, and its limits](docs/METHOD.md) · [Contributing: send a synthetic failing example, a regression pair, a generalization case or a rule proposal, never real private findings](CONTRIBUTING.md)

---

## What you have now

A rules file with stable clause ids, a digest the reviewer and the coding session read, a ledger of findings and what became of them, a changelog of every rule change with its evidence and the decision, and a way to ask whether a rule holds. You do not have evidence that the agent writes better code, and this project does not claim any.

To clean up: `rm -rf "$HR_SRC/../hr-demo"`. The clone is unchanged unless you recorded a live case in section 6.

Next: [README](README.md) for installing the plugin, [Fitting house-rules into daily work](docs/INTEGRATION.md) for daily use, and [Why the loop is built this way](docs/METHOD.md) for the design and its limits.
