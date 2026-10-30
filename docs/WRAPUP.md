# Wrap-up

A wrap-up closes a stretch of work. `ratchet wrapup` counts, deterministically, how much changed since the last review, and runs `ratchet review` only when that reaches a threshold. If the review finds blind spots (findings no rule covers), the plugin asks one question about them. The budget is fixed: at most one review and at most one question per wrap-up, nothing at all below the thresholds.

```sh
ratchet wrapup                      # measure; review if enough changed; list the new blind spots
ratchet wrapup --dry-run            # measure and say what it would do, or why it would be refused; writes nothing, runs nothing
ratchet wrapup --base main          # measure from a commit you name
ratchet wrapup --from-file r.txt    # a saved review instead of calling claude (the thresholds still apply)
ratchet wrapup --json               # one JSON object on stdout; the human report goes to stderr
ratchet wrapup --report-only        # review nothing; report the open blind spots of the latest review run
```

It also takes `--level` (the review effort, default `high`) and `--repo`, `--ledger`, `--config`. In the plugin, `/review-ratchet:wrapup` (or "wrap up", "收口") runs it and asks the question. With `wrapup_auto` on, the plugin also starts it by itself after commits, and the only thing left for you is the question: see [Automatic wrap-up](#automatic-wrap-up).

## The base

The work is measured in the worktree wrapup runs in (`--repo`, else the current directory), which in a linked worktree is not the main one; the ledger and `.ratchet/` stay with the main worktree, while the rules files, digest and changelog it leaves out of the count are that worktree's copies. The base is the first of these that holds, and the output says which:

| `base_reason` | base | when |
|---|---|---|
| `--base` | the commit `REF` names | `--base REF` was given. A ref that is not a commit is an error |
| `last-review-tree` | the `tree` of the newest `review_run` of this branch that has a usable `head` | that `head` is a commit and an ancestor of HEAD (a review with `head` `none` or no `head` is passed over, and so is one whose `head` is not an ancestor: history a rebase rewrote, a commit this clone no longer has), and the `tree` object is still in the repo. The `tree` is the working tree as that review found it, uncommitted and untracked files included, so what the review read is not counted again whether or not it was committed since |
| `last-review` | the `head` of that same review | the fallback when it recorded no `tree` (an older ledger, or taking the tree failed) or the tree object is gone (`git gc` removes objects nothing refers to). The output says so on a `wrapup: note:` line: work the review left uncommitted counts again, as it did before trees were recorded |
| `merge-base` | where HEAD forked from the default branch | the fork point a review round key uses: `default_branch` from the config, else origin's HEAD branch, else `main`, else `master`. On the default branch itself that is HEAD, so only uncommitted work counts |
| `head` | HEAD | none of the above exists. Only uncommitted work counts; pass `--base REF` to count committed work. A branch with no commit yet is measured from the empty tree: every file is new |

**Only a review of the same branch is the last review.** The ledger is shared by every worktree of the repo, and a review's `tree` holds the uncommitted work of the worktree it ran in. The main worktree's review, taken as the base in a linked worktree, would count the main worktree's uncommitted work there as change. Two worktrees never have one branch checked out at once, so the branch tells their reviews apart. It is read from the review's `round_key`, `<branch>@<fork point>` ([Review rounds](LEDGER.md#review-rounds)): `feature@e72a47386034` is a review of `feature`, and the name ends at the last `@`, so a review of `feat@2` is not one of `feat`. Passed over, then, as if they were not in the ledger:

- reviews of other branches;
- a `--target` review, whose key `target:<T>` names a change and no branch;
- every review, when HEAD is detached: it is on no branch, so none is its last review and the base is the fork point. `ratchet review` itself needs a branch to count rounds on, or `--target`, so a wrap-up that reaches its threshold there stops at the review with that message. This is deliberately the cautious side: a base too far back reviews once more than needed, a base from another branch counts work that is not this one's.

A review of another branch made later does not hide an older review of this one: the newest review *of this branch* decides.

## What counts

Lines and files that differ from the base in the working tree:

- tracked files, from `git diff --numstat <base>`: committed, staged and unstaged work together, lines added plus lines deleted. A rename is one file and the lines that changed with it. A binary file is one file and no lines;
- untracked files that git does not ignore (`git ls-files --others --exclude-standard`), by reading them: a last line without a newline counts, a file git would call binary (a NUL byte in its first 8000 bytes) has no lines, a symlink or a directory (an embedded repo) is one file and no lines;
- not the files ratchet itself writes (the ledger, the label page and batch, `.ratchet/`; the list `init` puts in `.gitignore`), tracked or not. Otherwise a repo that ignores none of them would count every review's own output as work;
- not the files a rule change writes: the config's `rules_files`, its `digest`, and `CODING-STYLE.changelog.md` at the top of the worktree being measured. Adopting one rule would otherwise count its text three times (rules file, digest, changelog) and come within a file of the threshold. The same test leaves them out against every kind of base, tree or commit. Inject targets (`CLAUDE.md` with a digest block) are the user's own files and still count, and so does a file of those names in another directory or a rules file outside the repo.

Files are distinct paths. Ignored files are not counted.

Against a `last-review-tree` base the same rules hold, with one difference in how: both sides are git trees made the same way (`ratchet/state_tree.py`: a temporary copy of the worktree's own index, `git add -A`, `git write-tree`; the real index and the working tree are never touched), and the count is `git diff --numstat <review's tree> <tree now>`. The files ratchet writes are filtered from that diff as above. An untracked file over 1 MiB is not read into the object store: it is one file with no lines, like a binary, and counted when it first shows up (an edit to it is not seen). A symlink in the diff is one line (its target), as git counts it.

**Measuring writes nothing to the repo.** The tree of now exists only to be counted, so its objects (the blobs of new or edited files, the trees above them) go to a temporary object directory: `GIT_OBJECT_DIRECTORY` is that directory, `GIT_ALTERNATE_OBJECT_DIRECTORIES` is the repo's own (the one `git rev-parse --git-path objects` names, which a linked worktree shares with the main one), and the diff runs in the same environment. The directory is deleted when the count is done. So `--dry-run`, and a wrap-up below both thresholds, leave `.git/objects` without a new file. (Git refreshes the modification time of an object it finds already stored when a command would write the same content again; that is all it does to the existing ones.) Only the review's own snapshot, the tree taken when `ratchet review` starts and kept for the next wrap-up to compare with, is written to the repo: unreferenced objects that `git gc` removes in time.

## Thresholds

`.ratchet.json`, both positive integers:

| key | default | meaning |
|---|---|---|
| `wrapup_min_changed_lines` | `200` | review when this many lines changed |
| `wrapup_min_files` | `5` | review when this many files changed |

Either one is enough, and "reached" includes the number itself (200 lines review, 199 do not). Below both nothing is reviewed, the cap is not consulted, and the output gives the numbers: `wrapup: skipped, changed lines 38 < wrapup_min_changed_lines 200 and changed files 3 < wrapup_min_files 5; no review run`.

## The review

Above a threshold wrapup runs `ratchet review` itself with the same defaults (level `high`, the same round cap, the same ingest), so the run is an ordinary `review_run` whose `head` is the next wrap-up's base. A refused round (the cap is reached) ends the wrap-up with exit code 3. wrapup has no `--over-cap`: that stays an explicit decision, `ratchet review --over-cap`. `--dry-run` says beforehand whether the review would be refused ([Dry run](#dry-run)).

Afterwards it reads the ledger: the run's own open findings that no rule covers (`unmapped`, as `count --by-rule` counts them, legacy aliases applied) are the blind spots of this review; the count of every open `unmapped` finding follows. A finding that was merged into another on the same file and line (`also_matches`) is not listed.

A review reads the whole branch, so a blind spot nobody fixed comes back under every new run id. One that was already dealt with is not asked about again: of the run's blind spots, those that repeat a known one are reported as repeats, and the rest are the **new** blind spots, the only ones the question is about. A blind spot is known when an earlier run has an `unmapped` finding that flags the same code in the same file (the exact rule is under [`--json`](#--json)) and that finding is

- still open: the question was asked about it, or will be; or
- marked `false_positive` or `dismissed`: it was answered. (`fixed` does not make a blind spot known. The same code reported again may not be fixed after all, so that one is asked about.)

Only the report changes. The ledger keeps the repeat as the open finding it is, and it stays in `open_unmapped`; no outcome is written for it, not even when the one it repeats was marked `false_positive`.

```
$ ratchet wrapup
wrapup: 270 changed line(s) in 3 file(s) since e72a47386034 (where this branch forked from main)
wrapup: changed lines 270 >= wrapup_min_changed_lines 200; running a review
review round 1/3 for feature@e72a47386034
saved raw review output to .ratchet/reviews/r1.txt
id    rule_id   severity    location         summary
r1-1  unmapped  blocker     svc/retry.py:12  The retry loop never increments its counter, so it spins fo…
r1-2  unmapped  should-fix  svc/cache.py:40  A cache hit returns the stored dict itself, so callers can…
r1-3  P1.1      should-fix  svc/api.py:7     Nesting depth of five in the handler.
review: +3 finding(s) from 3 item(s) (0 merged by file+line, 2 unmapped, 0 labeled from the text, 0 without a usable severity), 0 already recorded
open: 3. A commit with a `Fixes-Finding: <id>` trailer closes a finding.
wrapup: 2 new unmapped finding(s) from run r1, which no rule covers:
  r1-1  svc/retry.py:12  blocker  The retry loop never increments its counter, so it spins forever on a…
  r1-2  svc/cache.py:40  should-fix  A cache hit returns the stored dict itself, so callers can mutate…
wrapup: next, /review-ratchet:maintain drafts rule proposals from them (at most 3 per wrap-up)
wrapup: open unmapped findings: 2
```

The user marked `r1-2` not a real problem, and the work went on. The next wrap-up's review reports `r1-1` and `r1-2` again, in other words, and one blind spot that is new:

```
$ ratchet label r1-2 --outcome false_positive --note "wrap-up: not a real problem"
label: 1 human outcome(s), false_positive (r1-2)
$ ratchet wrapup
wrapup: 240 changed line(s) in 1 file(s) since 6c85162c0a29 (the working tree as review r1 found it)
wrapup: changed lines 240 >= wrapup_min_changed_lines 200; running a review
review round 2/3 for feature@d9e799c23a83
saved raw review output to .ratchet/reviews/r2.txt
id    rule_id   severity    location         summary
r2-1  unmapped  blocker     svc/retry.py:12  retry_step_12 is assigned and never read; the counter never…
r2-2  unmapped  should-fix  svc/cache.py:40  Returning the cached object lets callers mutate it.
r2-3  unmapped  should-fix  svc/queue.py:30  The queue drops an item when the buffer is full and says no…
review: +3 finding(s) from 3 item(s) (0 merged by file+line, 3 unmapped, 0 labeled from the text, 0 without a usable severity), 0 already recorded
open: 5. A commit with a `Fixes-Finding: <id>` trailer closes a finding.
wrapup: 1 new unmapped finding(s) from run r2, which no rule covers:
  r2-3  svc/queue.py:30  should-fix  The queue drops an item when the buffer is full and says nothing.
wrapup: 2 of run r2's unmapped finding(s) repeat a blind spot already known from an earlier run (still open there, or marked false_positive or dismissed; same file, same code around the flagged line); not asked about again:
  r2-1  svc/retry.py:12  is a repeat of r1-1
  r2-2  svc/cache.py:40  is a repeat of r1-2
wrapup: next, /review-ratchet:maintain drafts rule proposals from them (at most 3 per wrap-up)
wrapup: open unmapped findings: 4
```

`r2-1` repeats `r1-1`, which is still open, and `r2-2` repeats `r1-2`, which was answered. Only `r2-3` is asked about. Both repeats are in the ledger and open (4 open in all: `r1-1`, `r2-1`, `r2-2`, `r2-3`). The JSON object has the two in `repeated_unmapped` and `r2-3` in `new_unmapped`.

## `--json`

Stdout is exactly one JSON object; everything else, the review's own output included, goes to stderr. Fields:

| field | meaning |
|---|---|
| `status` | `skipped` (below both thresholds), `reviewed`, `capped` (the round cap refused the review; exit code 3), `dry-run` (`--dry-run`, and a review is due; `would_refuse` says whether it would start), or `reported` (`--report-only`) |
| `base`, `base_reason` | the base and which rule chose it; both null for `reported`. `base` is a commit for every `base_reason` except `last-review-tree`, where it is a git tree id (the working tree as the last review found it), not a commit; null for a branch with no commit |
| `changed_lines`, `changed_files` | the counts; null for `reported` |
| `thresholds` | `{"min_changed_lines": 200, "min_files": 5}` as configured |
| `run` | the run id of this wrap-up's review; for `reported`, of the latest review run. Otherwise null |
| `new_unmapped` | the run's open blind spots that are not repeats, most severe first: `id` (the full ledger id), `display_id`, `file`, `line`, `severity`, `summary`, `failure_scenario`. Empty unless `reviewed` or `reported`. The question is asked about these only |
| `repeated_unmapped` | the full ids of the run's open unmapped findings that repeat a known blind spot: an `unmapped` finding of an earlier run on the same file and the same code around the flagged line (the whole snippet, the flagged line and up to three lines each side as the review saw them, compared line by line with whitespace collapsed, wherever the code has moved to), that is still open or has the outcome `false_positive` or `dismissed`. One `return None` marked not real does not make another `return None` of the file a repeat, and code edited within three lines of the flagged one is other code: it is asked about again. With no window to compare on either side (no snippet, only blank lines) the same line number stands in; with one on one side only they are not the same code. Summaries are never compared. An earlier finding that was `fixed` does not make a repeat, and neither does one of the same run. The ledger keeps the repeats, open; they are in `open_unmapped`, no outcome is written for them, and they are not asked about again |
| `repeat_of` | `{full id of a repeat: full id of the earlier finding it repeats}`, one entry per id in `repeated_unmapped`; the earliest known finding in ledger order when several match. `{}` when nothing repeats. The human report lists each pair (`r2-1 ... is a repeat of r1-1`) |
| `open_unmapped` | every open `unmapped` finding in the ledger |
| `reason` | the threshold that was reached, or the numbers that fell short, or the refusal. In a dry run it is still the threshold that was reached; the refusal is in `would_refuse` |
| `would_refuse` | `null`, or `{kind, round_key, message}`: why a real run would be refused. Only a dry run whose `status` is `dry-run` can fill it. It is `null` for every other status, for a real run, and when the review would start. `kind` is the first check that fails, `rules`, `round_key`, `round_cap` or `claude`, in that order; `round_key` is `null` for the first two (there is no key to name) and the round key for the other two; `message` is the text a real run stops with ([Dry run](#dry-run)) |

## Dry run

`--dry-run` measures as usual, and below both thresholds it prints the `skipped` line. When a review is due it also makes the checks a real review makes before it calls `claude`, so it can say whether the review would start:

```
$ ratchet wrapup --dry-run
wrapup: 270 changed line(s) in 3 file(s) since e72a47386034 (where this branch forked from main)
[dry-run] wrapup: changed lines 270 >= wrapup_min_changed_lines 200; would run `claude -p '/code-review high'`; nothing run or written
```

With `--from-file r.txt` the clause is `would ingest r.txt as a review`. When the review would be refused, the line is

```
[dry-run] wrapup: <reason>; a real run would be refused: <message>; nothing run or written
```

`<reason>` is still the threshold that was reached, not the refusal. `<message>` is the text the real run would stop with, and the same text is `would_refuse.message` in the JSON object. The exit code is `0` either way: a dry run reports a refusal, it does not fail on it.

The checks run in the order a real review makes them, and the first that fails gives the `kind`:

| `kind` | refused when | `round_key` |
|---|---|---|
| `rules` | the rules do not load: a rules file is missing or invalid, or a rules layer outside this repository is not on this machine ([Outer layers](COMPILE.md#outer-layers)) | `null` |
| `round_key` | the change has no round to count on: HEAD is detached, `default_branch` is not a branch, or the branch shares no history with it | `null` |
| `round_cap` | the change's review rounds are used up. The message ends with "ratchet wrapup has no --over-cap: `ratchet review --over-cap` runs one more round, recorded" | the round key |
| `claude` | the review command is not found on the PATH. A `--from-file` wrap-up runs none, so it never gets this kind | the round key |

A refused round records no `review_run`, so the base does not move: the next wrap-up is refused the same way until the cause is gone. That is why the dry run looks ahead.

## Exit codes

`0` skipped, reviewed, dry run (a foreseen refusal included), or reported. `1` an error: not a git repo, a `--base` that is not a commit, an unreadable file, `claude` failing (the review's own errors), `--report-only` with no review run recorded. `3` the round cap refused the review.

## The question

The skill ([`skills/wrapup/SKILL.md`](../skills/wrapup/SKILL.md)) is the only place it is defined; `/review-ratchet:review` ends in the same step (it gets the same object from `ratchet wrapup --json --report-only`, and no threshold applies to an explicit review). No new blind spot means no question, one sentence. Otherwise one `AskUserQuestion` call with two questions:

1. Which of these are not real problems? Multi-select, the 4 most severe blind spots at most (the rest stay open for the label page). A single blind spot is asked as a single-select. Each one marked is recorded as `false_positive` with one `ratchet label <display_id>... --outcome false_positive --note "wrap-up: not a real problem"`: all of them or none, and no label page, batch or scratch file. Nothing else is recorded: an unmarked one stays open, and so does everything when the answer is empty.
2. Turn the rest into rules now? `Now (Recommended)` starts [maintain](../skills/maintain/SKILL.md) on them (at most 3 proposals per wrap-up, each still decided by the user); `Next wrap-up` records nothing and leaves them open.

The question is about `new_unmapped` only. The repeats (`repeated_unmapped`) are never put in it, so a blind spot the user has answered, or has been asked about and left open, does not come back as a question under every new run id; the skill mentions how many there were in a sentence. A repeat the user does want to turn down can still be labeled by hand with `ratchet label`.

`ratchet label-page` + `ratchet apply-labels` (the batch route, for labeling many findings by hand or in a host without a question tool) and `ratchet distill --drafts` remain as fallbacks. They are not the default path.

## Automatic wrap-up

With the plugin the wrap-up starts by itself. You commit as usual. When a turn ends and the work has reached a threshold, Claude runs the wrap-up review, and asks [the question](#the-question) only if the review found a blind spot that is new. Below the thresholds nothing runs and nothing is shown. How this fits into a working day: [INTEGRATION.md](INTEGRATION.md).

### How it runs

Three parts work together. The git post-commit hook notes commits, the plugin's Stop hook decides, and the `wrapup` skill runs the review and asks. The commit hook only records, so that a commit never waits for a review; the end of the turn is where the work is judged.

1. **Commit.** The post-commit hook that `ratchet init` installs ends with `ratchet auto note`. In a Claude Code session whose repo has `wrapup_auto` on, it appends the commit to a local file for that session ([The commit note](ONBOARDING.md#the-commit-note)). It records and decides nothing, and it cannot fail a commit.
2. **The turn ends.** The plugin registers one Stop hook ([`hooks/hooks.json`](../hooks/hooks.json); it runs `hooks/run.sh auto stop`, with a 30 second timeout). `run.sh` first looks, with shell builtins alone, for this session's file in the [state directory](#the-state-directory). A turn in which nothing was committed has none and ends there, in about 10 ms, without starting Python.
3. **The decision.** Otherwise `run.sh` finds a Python 3.10 or newer and runs `ratchet auto stop`, which reads the hook's JSON from stdin. For each worktree a commit was noted in (once, however many commits) it makes the decision `ratchet wrapup --dry-run` makes ([Dry run](#dry-run)): measure the work since the last review, compare it with the thresholds, and, when a review is due, foresee whether it would be refused. A worktree below both thresholds ends here, and the hook says nothing.
4. **Claude goes on with the turn.** For a worktree whose review is due and would start, the hook answers with one line of `additionalContext`, shell-quoting a path that holds spaces:

   ```
   review-ratchet: /work/app has 270 changed line(s) in 3 file(s) since its last review, past the wrap-up threshold (200 lines or 5 files). Run the automatic wrap-up now: /review-ratchet:wrapup auto /work/app (Skill tool: review-ratchet:wrapup, args: auto /work/app; equivalent: ratchet wrapup --repo /work/app).
   ```

   The hook runs no review itself. The line goes to Claude, who takes it up in the same turn instead of ending it. Claude Code shows it as Stop hook feedback, not as a hook error.
5. **The review.** The [`wrapup` skill](../skills/wrapup/SKILL.md) in automatic mode runs `ratchet wrapup --repo <worktree> --json` as a background command, as a review can take minutes and a foreground call is cut off at its timeout. It waits for the completion notice, without polling, and reads the JSON object at the end of the output file. `skipped`, `capped` and an error get one sentence and no question, and `/review-ratchet:setup` is not offered.
6. **The question.** On `reviewed` the skill goes on as in [The question](#the-question): one popup, about the new blind spots only. No new blind spot means one sentence and no popup. `Now` starts [maintain](../skills/maintain/SKILL.md) on them: at most 3 proposals, each still decided by the user.

### Turning it on and off

- **On.** `wrapup_auto` is a boolean in `.ratchet.json`, `false` when absent. `ratchet init` asks for it and writes it ([The `wrapup_auto` key](ONBOARDING.md#the-wrapup_auto-key)); `/review-ratchet:setup` settles it with you first. `init --yes` turns it on, and `init --no-wrapup-auto` writes `false` without asking. It takes both halves: the git post-commit hook that notes commits (not installed with `--no-hooks`) and the plugin's Stop hook. A repository set up before the automatic wrap-up existed gets the question and the updated post-commit block by running `/review-ratchet:setup` again.
- **Off.** Set `"wrapup_auto": false`, or delete the key. The post-commit line stays but notes nothing, and records noted before are dropped at the next Stop. Disabling or uninstalling the plugin removes the Stop hook. `/review-ratchet:wrapup` and `ratchet wrapup` work either way.
- **One setting per repository.** The main worktree's `.ratchet.json` decides for its linked worktrees too. The Stop hook runs at the end of every turn of every session while the plugin is enabled, in repositories that never heard of ratchet as well; there the session has no file and the hook ends at once.

### When it stays quiet

Nothing is said and nothing runs in these cases:

| situation | what happens to the records |
|---|---|
| The work is below both thresholds | They are used up. The next commit notes the worktree again, and the measurement counts everything since the last review |
| The commit was made outside a Claude Code session (`CLAUDE_CODE_SESSION_ID` is not set), for example from a terminal of your own | It is never noted: the git hook skips `ratchet auto note` in the shell. The next wrap-up counts that work all the same |
| Inside ratchet's own review (`RATCHET_REVIEW_CHILD=1`) | Neither the note nor the Stop hook acts. ratchet sets the variable in the `claude` process of its reviews, so a review cannot start another |
| Another Stop hook has continued the turn (`stop_hook_active` in the input) | This Stop is skipped and the records stay for a later one |
| A headless session, which Claude Code marks with `CLAUDE_CODE_SESSION_ATTENDED=0` (as under `claude -p`; see the limits below), unless `RATCHET_AUTO_HEADLESS=1` | They stay |
| A subagent, a workflow or a teammate is still running, or a background shell that runs `ratchet wrapup` or `ratchet review` | The judgement is deferred: they stay for the Stop after the task. Such a task may still commit, or review beside this one |
| The worktree is gone, or its repo no longer has `wrapup_auto` on | They are dropped |

A task counts as over when its status is one of `completed`, `complete`, `done`, `finished`, `succeeded`, `failed`, `error`, `killed`, `stopped`, `cancelled`, `canceled`, `aborted`, `exited` or `terminated`, in any case. A task with no status, or one the hook does not know, counts as running. Other background tasks (a dev server, a monitor, a cloud session, an MCP task) may never end and never defer.

A failure is silent too, because a Stop hook that failed at the end of every turn would be switched off. There is no Python 3.10 or newer, or no `bin/ratchet` in the plugin: `run.sh` exits 0 without a word. An exception becomes one line in `hook.log`, and so does input that is neither empty nor a JSON object. A worktree that cannot be judged (a damaged config, a deleted branch) is logged and skipped, and does not hide the others. `run.sh` also turns the exit code 2 of `ratchet` into 1, since a Stop hook that exits 2 makes Claude go on with the turn.

### Refusals are announced once

A review can be due and still be refused: the rules do not load, the change has no round key, the round cap is used up, or `claude` is not on the PATH ([Dry run](#dry-run)). Claude is not asked to run it. The hook tells the person instead, in a `systemMessage` that only they see, one line per worktree and with the whitespace of the message folded to single spaces:

```
review-ratchet: the wrap-up review of /work/app is due but would be refused: <message>
```

Claude does not go on with the turn because of it. A refusal is told once for each repo, worktree, kind and change. The change is named by its round key; the two kinds that have none, `rules` and `round_key`, are told apart by their message. So the round cap is one message and not one per turn. `announced.json` remembers it. The file is written before the message is shown: a hook killed in between misses one message and never repeats one. An entry older than 30 days no longer counts, so a refusal that lasts that long is told again. Once the cause is gone, the Stop after the next commit asks for the review as usual. One answer can hold both fields, when one worktree is due and another is refused.

### The state directory

Everything the automatic wrap-up keeps lives in one directory on this machine, outside every repository:

```
<state dir>/
  pending/<session id>.jsonl          one line per commit noted in that session
  claimed/<session id>.<pid>.jsonl    the records a Stop hook is judging right now
  announced.json                      the refusals already told: {key: time}
  hook.log                            one line per failure
```

- **Where.** The first of `$RATCHET_STATE_DIR`, `$XDG_STATE_HOME/review-ratchet` and `~/.local/state/review-ratchet` that is set and not empty. The directories are created with mode `0700`.
- **Claiming.** Before it judges, the Stop hook moves the session's pending file into `claimed/` with one atomic rename, and deletes it when done. Of two Stops that meet (two windows of one session) one judges and the other finds nothing. A hook killed in the middle (at its 30 second timeout, say) leaves the file behind. That costs at most one trigger: the next commit notes the worktree again, and the measurement counts everything since the last review.
- **Pruning.** Each time a commit is noted, the files in `pending/` and `claimed/` that nothing has written to for 7 days are deleted. An entry of `announced.json` older than 30 days no longer counts and is dropped the next time the file is written. `hook.log` is never trimmed. It holds one line per failure, `<UTC time> <what failed>`, and for an exception the line ends with `(at <file>:<line>)`, where it was raised.

### Privacy

- **Local only.** The state directory holds worktree paths, commit shas, timestamps and session ids, and none of it is written to a repository file. The ledger (`.style-findings.jsonl`) and `.ratchet/` (saved reviews and evidence, proposal batches) are local as well and git-ignored, because they can hold code excerpts.
- **Committed.** The rules, the digest and `CODING-STYLE.changelog.md`. A changelog entry carries the evidence behind a rule change (file and line, a snippet of the code) and the decision, in the words of whoever made it. In a public repository, read an entry before you commit it.
- **The review itself** is Claude Code's `/code-review`. The code it reads goes to the model provider under Claude Code's own settings, as with any review.

### Cost, limits and versions

- **Cost.** Each automatic run is one `/code-review` at level `high`, billed as any review is, plus the short extra turn that runs the skill. It counts as one round of the change against `max_review_rounds` (default 3, [Review rounds](LEDGER.md#review-rounds)). At the cap the person is told once, as above, and no further review of that change starts by itself: raising `max_review_rounds` lifts it, and `ratchet review --over-cap` runs one more round by hand. Below the thresholds nothing is billed, as measuring is local git work.
- **`/clear`** gives the session a new id. Records noted under the old id are never seen by the new session's Stop (they are deleted after 7 days). The next commit notes the worktree again, and the measurement counts all the work since the last review, so a `/clear` delays a review to the next commit and loses no change.
- **Continuations.** The line of `additionalContext` makes Claude go on with the turn, which counts toward Claude Code's limit of 8 continuations in a row, as a blocking Stop hook does.
- **Headless detection** rests on `CLAUDE_CODE_SESSION_ATTENDED`, which Claude Code does not document. A smoke test on Claude Code 2.1.312 found it `0` under `claude -p`; in an attended VS Code session it was `1`. A session without the variable counts as attended. A headless session is skipped unless `RATCHET_AUTO_HEADLESS=1`; with that set, the automatic wrap-up works headless. In that smoke test the Stop hook continued the turn, Claude ran `/review-ratchet:wrapup auto <worktree>`, and one review was recorded.
- **Versions.** `additionalContext` on a Stop hook needs Claude Code 2.1.163 or newer. `background_tasks` in the hook's input, which the deferral reads, needs 2.1.145 or newer; on an older version nothing is deferred. The hook needs Python 3.10 or newer and stays silent without it.

### Environment variables

| variable | meaning |
|---|---|
| `RATCHET_STATE_DIR` | The state directory. It wins over `XDG_STATE_HOME`, and an empty value counts as unset |
| `XDG_STATE_HOME` | Without `RATCHET_STATE_DIR`, the state directory is `$XDG_STATE_HOME/review-ratchet`, else `~/.local/state/review-ratchet` |
| `RATCHET_AUTO_HEADLESS` | `1` lets the Stop hook judge in a headless session too (a pipeline, a smoke test) |
| `RATCHET_PY` | The Python for `hooks/run.sh`, tried first (3.10 or newer), and for the portable git hooks |
| `RATCHET_REVIEW_CHILD` | ratchet sets it to `1` in the `claude` process of its own reviews, to stop a review from starting a wrap-up. Do not set it by hand: with `1` the note and the Stop hook do nothing in that process |
| `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_SESSION_ATTENDED` | Set by Claude Code and only read here: the id names the session's file, and `0` marks a headless session |

### If nothing happens

- `wrapup_auto` must be `true` in the main worktree's `.ratchet.json`.
- The commit must have been made in a Claude Code session, with the post-commit hook installed (`ratchet init` proves it runs). After it, `<state dir>/pending/<session id>.jsonl` has a new line.
- The work must have reached a threshold. `ratchet wrapup --dry-run` in that worktree prints what the Stop hook decides.
- The plugin must be enabled in a Claude Code that meets the versions above, in a session that is not headless (a headless one needs `RATCHET_AUTO_HEADLESS=1`).
- Any failure of the note or the hook is a line in `hook.log`.

## What the critic reads (probe, Claude Code 2.1.284, 2026-10-29)

Does the built-in `/code-review`, called without a target as `ratchet review` calls it, read only uncommitted changes, or the branch's commits too? Two real runs of `claude -p "/code-review high"` (stdin closed, from the repo root) in a throwaway repo, `main` with one clean commit and a `feature` branch:

1. Clean working tree; the only bug (a loop that never increments its counter) is the one commit on `feature`. Reported, at the right line.
2. Bug A in the first of two commits on `feature` (the later commit is clean), bug B an uncommitted edit of a tracked file, bug C in a new untracked file. All three reported; the review names the commit, calls B an "uncommitted change" and C "untracked".

So the default scope is the branch's commits since it forked from the default branch, plus uncommitted edits, plus untracked files: the set wrapup counts. No `--target` is needed, and snippets stay available. Not probed: a clean tree on the default branch itself. (An unplanned run in a repo whose only change was an untracked file of numbers on `main` answered "no code changes to review".)

## Limits

- **The snapshot is taken when the review starts.** Edits made while the review runs are measured by the next wrap-up. A tree is unreferenced objects: after two weeks `git gc` may remove it, and the wrap-up then falls back to the review's HEAD (said on a `note:` line).
- **Reviews are matched by branch name.** A branch that is checked out in another worktree later (the first one let go of it) takes its reviews with it, and their trees hold the uncommitted work of the old worktree: counted as change in the new one. A branch that was renamed, or deleted and made again, starts with no review. A `--target` review records no tree, and is never taken as a base either, as its round key names no branch.
- **The critic reads the whole branch, not the delta.** The thresholds decide when to review, not what is read. An unfixed finding comes back under the new run's id; `repeated_unmapped` takes the exact repeats of a known blind spot out of the question. A blind spot the user marked `false_positive` or `dismissed` and the next review reports again is a repeat; one marked `fixed` and reported again is new. A repeat needs the same code around the flagged line (the snippet's whole window, whitespace collapsed), so code edited within three lines of it is asked about again: that errs towards one question too many, never one blind spot too few. When neither finding has a window (no snippet, only blank lines) the line numbers are compared instead; when only one has, they are not the same code.
- Lines are added plus deleted, whitespace included, and a moved block counts twice.
- **A dry run checks that `claude` can be found, not that the review will succeed.** `ratchet review --dry-run` previews the round cap and a missing `claude` the same way, but a rules file that does not load or a change with no round key is an error there (exit 1), as it is in a real run.
- Heads in a ledger shared by several clones that are not in this history are skipped, so the fork point decides.
