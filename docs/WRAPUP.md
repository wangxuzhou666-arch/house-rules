# Wrap-up

A wrap-up closes a stretch of work. `ratchet wrapup` counts, deterministically, how much changed since the last review, and runs `ratchet review` only when that reaches a threshold. If the review finds blind spots (findings no rule covers), the plugin asks one question about them. The budget is fixed: at most one review and at most one question per wrap-up, nothing at all below the thresholds.

```sh
ratchet wrapup                      # measure; review if enough changed; list the new blind spots
ratchet wrapup --dry-run            # measure and say what it would do; writes nothing, runs nothing
ratchet wrapup --base main          # measure from a commit you name
ratchet wrapup --from-file r.txt    # a saved review instead of calling claude (the thresholds still apply)
ratchet wrapup --json               # one JSON object on stdout; the human report goes to stderr
ratchet wrapup --report-only        # review nothing; report the open blind spots of the latest review run
```

It also takes `--level` (the review effort, default `high`) and `--repo`, `--ledger`, `--config`. In the plugin, `/review-ratchet:wrapup` (or "wrap up", "收口") runs it and asks the question.

## The base

The work is measured in the worktree wrapup runs in (`--repo`, else the current directory), which in a linked worktree is not the main one; the ledger and `.ratchet/` stay with the main worktree. The base is the first of these that holds, and the output says which:

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
- not the files a rule change writes: the config's `rules_files`, its `digest`, and `CODING-STYLE.changelog.md` at the repo root. Adopting one rule would otherwise count its text three times (rules file, digest, changelog) and come within a file of the threshold. The same test leaves them out against every kind of base, tree or commit. Inject targets (`CLAUDE.md` with a digest block) are the user's own files and still count, and so does a file of those names in another directory or a rules file outside the repo.

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

Above a threshold wrapup runs `ratchet review` itself with the same defaults (level `high`, the same round cap, the same ingest), so the run is an ordinary `review_run` whose `head` is the next wrap-up's base. A refused round (the cap is reached) ends the wrap-up with exit code 3. wrapup has no `--over-cap`: that stays an explicit decision, `ratchet review --over-cap`.

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
| `status` | `skipped` (below both thresholds), `reviewed`, `capped` (the round cap refused the review; exit code 3), `dry-run`, or `reported` (`--report-only`) |
| `base`, `base_reason` | the base and which rule chose it; both null for `reported`. `base` is a commit for every `base_reason` except `last-review-tree`, where it is a git tree id (the working tree as the last review found it), not a commit; null for a branch with no commit |
| `changed_lines`, `changed_files` | the counts; null for `reported` |
| `thresholds` | `{"min_changed_lines": 200, "min_files": 5}` as configured |
| `run` | the run id of this wrap-up's review; for `reported`, of the latest review run. Otherwise null |
| `new_unmapped` | the run's open blind spots that are not repeats, most severe first: `id` (the full ledger id), `display_id`, `file`, `line`, `severity`, `summary`, `failure_scenario`. Empty unless `reviewed` or `reported`. The question is asked about these only |
| `repeated_unmapped` | the full ids of the run's open unmapped findings that repeat a known blind spot: an `unmapped` finding of an earlier run on the same file and the same code around the flagged line (the whole snippet, the flagged line and up to three lines each side as the review saw them, compared line by line with whitespace collapsed, wherever the code has moved to), that is still open or has the outcome `false_positive` or `dismissed`. One `return None` marked not real does not make another `return None` of the file a repeat, and code edited within three lines of the flagged one is other code: it is asked about again. With no window to compare on either side (no snippet, only blank lines) the same line number stands in; with one on one side only they are not the same code. Summaries are never compared. An earlier finding that was `fixed` does not make a repeat, and neither does one of the same run. The ledger keeps the repeats, open; they are in `open_unmapped`, no outcome is written for them, and they are not asked about again |
| `repeat_of` | `{full id of a repeat: full id of the earlier finding it repeats}`, one entry per id in `repeated_unmapped`; the earliest known finding in ledger order when several match. `{}` when nothing repeats. The human report lists each pair (`r2-1 ... is a repeat of r1-1`) |
| `open_unmapped` | every open `unmapped` finding in the ledger |
| `reason` | the threshold that was reached, or the numbers that fell short, or the refusal |

## Exit codes

`0` skipped, reviewed, dry run, or reported. `1` an error: not a git repo, a `--base` that is not a commit, an unreadable file, `claude` failing (the review's own errors), `--report-only` with no review run recorded. `3` the round cap refused the review.

## The question

The skill ([`skills/wrapup/SKILL.md`](../skills/wrapup/SKILL.md)) is the only place it is defined; `/review-ratchet:review` ends in the same step (it gets the same object from `ratchet wrapup --json --report-only`, and no threshold applies to an explicit review). No new blind spot means no question, one sentence. Otherwise one `AskUserQuestion` call with two questions:

1. Which of these are not real problems? Multi-select, the 4 most severe blind spots at most (the rest stay open for the label page). A single blind spot is asked as a single-select. Each one marked is recorded as `false_positive` with one `ratchet label <display_id>... --outcome false_positive --note "wrap-up: not a real problem"`: all of them or none, and no label page, batch or scratch file. Nothing else is recorded: an unmarked one stays open, and so does everything when the answer is empty.
2. Turn the rest into rules now? `Now (Recommended)` starts [maintain](../skills/maintain/SKILL.md) on them (at most 3 proposals per wrap-up, each still decided by the user); `Next wrap-up` records nothing and leaves them open.

The question is about `new_unmapped` only. The repeats (`repeated_unmapped`) are never put in it, so a blind spot the user has answered, or has been asked about and left open, does not come back as a question under every new run id; the skill mentions how many there were in a sentence. A repeat the user does want to turn down can still be labeled by hand with `ratchet label`.

`ratchet label-page` + `ratchet apply-labels` (the batch route, for labeling many findings by hand or in a host without a question tool) and `ratchet distill --drafts` remain as fallbacks. They are not the default path.

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
- `--dry-run` does not consult the round cap; `ratchet review --dry-run` does.
- Heads in a ledger shared by several clones that are not in this history are skipped, so the fork point decides.
