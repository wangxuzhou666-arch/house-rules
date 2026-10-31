# Ledger reference

The ledger is a JSON-lines file, one event per line, append-only. Default location: `.style-findings.jsonl` in the root of the repo's main worktree (so every linked worktree shares one ledger). The skips file and `.ratchet/` are shared the same way, while the files git tracks are each worktree's own ([Linked worktrees](ONBOARDING.md#linked-worktrees)). The machine-readable definition is [`schemas/finding.v1.schema.json`](../schemas/finding.v1.schema.json); the tool validates every event against the same rules before it writes and when it reads, and refuses a ledger with a bad line rather than reading part of it.

## Events

Four kinds share the file. The current state of a finding is the **fold of its events in file order**: a finding is open until an `outcome` for its id appears. Nothing is ever rewritten. `review_run` events take no part in that fold.

### `finding`

| field | notes |
|---|---|
| `v`, `kind` | `1`, `"finding"` |
| `id` | l1/l2: `<repo>:<source>:<run_id>:<display_id>`. gate: `<repo>:gate:<12 hex>` |
| `display_id` | short handle for humans; the value used in `Fixes-Finding:` trailers (`r7-2`: run `r7`, second finding). gate: the same 12 hex |
| `run_id` | the review run; `gate` for gate findings |
| `repo` | repo slug, `[a-z0-9-]+` |
| `source` | `gate` (an automated check), `l1` or `l2` (two tiers of review; the tool treats them identically) |
| `ts` | when the event was **written**, local time, `YYYY-MM-DDTHH:MM:SS`, no timezone |
| `sha` | short HEAD at write time, or `none` on an unborn branch. It is the HEAD of the worktree the command runs in (`--repo`, else the current directory): `ratchet review`, `add`, `ingest-code-review`, `gate` and the Python API all read it there, so from a linked worktree it is that worktree's HEAD |
| `file`, `line` | `line` is an integer >= 1 |
| `rule_code` | what the detector or reviewer called it (tool code, or the category text the model gave) |
| `rule_id` | see [Rule ids](#rule-ids) |
| `category` | free-form grouping label. New events carry the clause family (`P0E`), the tool name (`ruff`) or `unmapped`; older ledgers use a fixed set such as `exception` |
| `severity` | `blocker`, `should-fix` or `nice` |
| `summary`, `failure_scenario` | non-empty text |
| `verdict` | `CONFIRMED` or `PLAUSIBLE` |
| `snippet` | optional code context. `ratchet review` fills it with the flagged line and its neighbours ([Code snippets](#code-snippets)); `add` records the text it is given; `ingest-code-review` records none |
| `snippet_source` | optional, written by `ratchet review` only: `review`, `worktree` or `unavailable`, where the snippet was read from ([Code snippets](#code-snippets)). Absent on every other finding |
| `discovered_at` | optional; when the reviewer produced the finding, if that differs from `ts` |
| `also_matches` | optional; other findings on the same file+line collapsed into this one (`rule_id`, `severity`, `summary`, `failure_scenario` each) |
| `rule_id_source` | optional: `model` (a model's category), `human`, `tool` (mapped through `tool_rule_map`), `attributed` (the model gave no usable category, so the clause id was read out of its summary or failure scenario; see `ingest-code-review`). A finding that could not be labeled at all is `unmapped` with source `model`. Absent on older events |

A `finding` event whose id already has an outcome **re-opens** it when its source is `gate` (the violation came back). A repeated l1/l2 event is ignored, and one that reuses an id for a different file/line/summary is refused (it usually means a run id was reused for a different review).

### `outcome`

`{v, kind, id, outcome, outcome_by, evidence, ts}`: `outcome` is `fixed`, `dismissed` or `false_positive`; `outcome_by` is `auto:commit`, `auto:gate_pass` or `human`; `evidence` is non-empty text (`commit <sha8> <subject>`, `gate_pass <ts> sha <sha> covers <file>`, or the person's note).

### `gate_pass`

`{v, kind, repo, ts, sha, files}`: an automated gate passed over `files` (a non-empty list of paths). A gate writes these with `ratchet gate pass` or `ratchet.api.record_gate_pass` ([GATES.md](GATES.md)), or appends them (and `gate` findings) itself: write each batch with a single `O_APPEND` write so concurrent writers cannot interleave half lines. A gate finding's id is `<repo>:gate:` plus the first 12 hex of `sha1(file|rule_code|line text with all whitespace removed)`, so re-detections of the same issue fold into one finding even when the line moves. `ratchet gate record` writes exactly this id ([GATES.md](GATES.md)).

### `review_run`

`{v, kind, repo, ts, run, round_key, round, max_rounds, over_cap, finding_count, source}` and the optional `reviewer`, `reviewer_version`, `level`, `head`, `version_changed`, `tree`, `trigger`: one review round of one change; see [Review rounds](#review-rounds) and [Critic version](#critic-version). `run` is the review's run id; `round` is 1 + the `review_run` events with the same `round_key` before this one; `max_rounds` is the `max_review_rounds` in force; `over_cap` is true when `round > max_rounds`; `finding_count` is how many findings the review contained, new to the ledger or not; `source` is `claude` (`ratchet review` ran it) or `file` (`--from-file`). It is written in the same append as the review's new findings, for every review that produced one (including a review with no findings), and never by `--dry-run` or by a review that failed. The kind is additive: a ledger without it validates and folds as before, and its older reviews count as no rounds.

A review run by `ratchet review` also records which critic ran it and on what. The six fields are optional, so `review_run` events written before them stay valid and have none. New events always carry the first four, `null` where the value is not known; `version_changed` is there only when true, and `tree` only when it could be taken:

| field | notes |
|---|---|
| `reviewer` | `claude-code` when `ratchet review` ran the review; `null` for `--from-file`, whose reviewer is not known (`source` stays `file`) |
| `reviewer_version` | the stripped stdout of `claude --version` (for example `2.1.45 (Claude Code)`), asked once per review before it starts; `null` for `--from-file`, and when the version could not be read |
| `level` | the review level, `low` to `max`; `null` for `--from-file` |
| `head` | the full sha of the commit HEAD pointed at before the reviewer started (for `--from-file`: when the file was read), or `none` on a branch with no commit. Uncommitted changes are not part of it. For a review `ratchet review` ran it is the `head` of that run's saved evidence, so the two always agree. `review` reviews the worktree it is run in (`--repo`, else the current directory), so from a linked worktree this is that worktree's HEAD, and its branch and fork point name the round key; the ledger, `.ratchet/`, the config and the skips file stay in the main worktree, while the rules files and the code under review are that worktree's own |
| `version_changed` | written only when `true`: `reviewer_version` differs from the latest earlier `review_run` that has one, so this is the first review on the new version |
| `tree` | optional (absent on older events, and when taking it failed, which the review says on stderr): the 40-hex id of a git tree holding the worktree as the review found it when it started (`--from-file`: when the file was read): tracked files as on disk, uncommitted work included, and untracked files git does not ignore (one over 1 MiB is a stand-in, see [WRAPUP.md](WRAPUP.md)). `ratchet wrapup` diffs it against the tree of now, so reviewed work is not counted again once committed. A review of a commit range ([WRAPUP.md](WRAPUP.md#on-the-default-branch)) read committed work only, so it records instead the tree of the commit its `head` names, which holds none of the work still uncommitted: the next wrap-up counts that work again. The wrap-up takes the newest `review_run` of the branch it runs on, the branch being what `round_key` (`<branch>@<fork point>`) names: the tree holds the uncommitted work of the worktree it was taken in, and a `target:` key names no branch ([WRAPUP.md](WRAPUP.md#the-base)). Nothing refers to the tree, so `git gc` may remove it; a reader treats a missing object as absent. Not written for `--dry-run`, a replay, or a `--target` review (its `target:` key names no branch, so no wrap-up measures from it) |

`trigger` is a seventh optional field, and says who asked for the review rather than which critic ran it. It is written only by `ratchet wrapup --auto`, as `"trigger": "auto"` (the automatic wrap-up: the Stop hook asks Claude for it, and a script may call it too), and is absent on a review run by `ratchet review` or by `ratchet wrapup` without `--auto`, and on older events. It is a record only: the daily cap does not read the ledger. It counts the slots of its own book, `.ratchet/auto-reservations.jsonl`, which a slot enters when the review is asked for or started and not when it ends ([WRAPUP.md](WRAPUP.md#the-daily-cap)). A reader takes only `auto` as meaningful and ignores any other value; validation is structural (a non-empty string), so a later trigger never makes a ledger unreadable.

## Review rounds

A change gets at most `max_review_rounds` (default 3) reviews: review, fix, re-review. Fixes can create new findings, so each extra pass adds cost and fixes to re-check (whether later passes are worth less has not been measured; see [METHOD.md](METHOD.md#fixes-can-create-new-findings)), and the third round is the last one in which blockers are fixed; whatever else is still open stays in the ledger for the periodic distill step.

`ratchet review` names the change with a **round key**, counts the `review_run` events carrying it, and numbers this review 1 + that. Past the cap it refuses **before** running `claude` (so a refused round costs nothing), prints how many findings are still open, and exits with code **3**; `--over-cap` runs it anyway and records `over_cap: true`. A `--from-file` review is a round like any other. `--dry-run` shows the key and the round and writes nothing (it says when the real run would be refused: past the cap, or with no `claude` on the PATH, which a `--from-file` review does not need). Re-ingesting a run id that is already recorded with `--from-file` is a replay, not a round: the cap never refuses it, it adds no `review_run` event, and it prints `review: replay of run <id> (already recorded; not another round)` instead of the round line. Only `--from-file` can replay: `--run <recorded id>` without it would be a fresh, billed `claude` run, so it is refused up front, before the cap and before `claude` runs. `ratchet count` ends with `review rounds on <key>: <used>/<max>`.

The round key is:

- **`target:<T>`** with `--target T`.
- **`<branch>@<fork point>`** otherwise: the current branch and the first 12 hex digits of the commit where it forked from the default branch (`git merge-base`). The default branch is the config key `default_branch`, else the branch `origin/HEAD` points at, else `main`, else `master`; when none of these exists there is no fork point to name, and `review` asks for `default_branch` or `--target`.
- **`<branch>@unborn`** before the first commit.

Two consequences, both intended. On the default branch itself the fork point is HEAD, so each new commit there starts a new key and gets its own rounds. Merging the default branch into a feature branch moves its fork point, so the count starts over. On a detached HEAD there is no branch: `review` fails and asks for `--target` (and `count` ends with `review rounds: n/a (detached HEAD)`).

**A linked worktree has its own key.** The key is read in the worktree being reviewed (`--repo`, else the current directory), not in the main one. A linked worktree is on its own branch, so it has its own fork point and its own rounds, counted apart from the main worktree's in the one shared ledger; the ledger, `.ratchet/`, the config and the skips file stay in the main worktree, while the rules files and the code under review are that worktree's own. A `--from-file` review is keyed by the worktree it is run in as well, and `ratchet count` ends with the rounds of the worktree it is run in: the key a review there would get.

## Critic version

The same rules read by a different critic find different things, so every review `ratchet review` runs records which critic it was. Before the review starts it asks the command it is about to run for its version (`claude --version`, or your `RATCHET_CLAUDE_CMD` wrapper with `--version` appended, with a ten-second limit) and stores the stripped output as `reviewer_version`. It asks once per review, and never for `--dry-run`, a refused round, a replay or a `--from-file` review. It asks *before* the review so that a long review is recorded under the version that was running, not the one installed by the time it finished.

- **A new version is announced.** When `reviewer_version` is not null and differs from the latest earlier `review_run` that has one, the run prints `critic version changed: <old> -> <new> (first review on this version)` (before the findings table) and writes `version_changed: true`. The first review with any recorded version announces nothing: there is nothing to change from. A review without a version (`--from-file`, or an unreadable version) is skipped when looking for the earlier one, so it neither announces anything nor hides a change. A review that fails records nothing and leaves the announcement for the next one.
- **An unreadable version is said out loud.** If `--version` fails, times out or prints nothing, the run prints one line on stderr, `warning: critic version not recorded (<reason>); this review's reviewer_version is null`, and goes ahead: the review is recorded with `reviewer_version: null`.
- **`ratchet count` lists findings per version** once the review findings in the ledger come from two or more distinct versions (see the `count` command below). A finding gets the version of the `review_run` whose `run` is the finding's `run_id`. Review findings whose run recorded no version (an older ledger, a `--from-file` review, an unreadable version, a run with no `review_run`) are listed together as `no version recorded`; `gate` findings are not part of it. With one version, or none, `count` prints exactly what it printed before.

The listing puts the counts side by side and nothing more. Different versions are different instruments, so a difference between two rows is not evidence about the rules or the code, and the tool computes none.

## Code snippets

`ratchet review` is the one command that reads code for a finding. `snippet` is the flagged line with up to three lines either side (at most 7 lines and about 600 characters), `snippet_source` says where the text came from:

| `snippet_source` | the snippet is |
|---|---|
| `review` | cut from the file **as it was before the review started**, from the evidence the run captured in `.ratchet/reviews/<run>.evidence.json`, not from the working tree as it is after the review (the reviewer, or you, may have edited it since). That evidence holds the files changed since HEAD and the untracked ones; for a range review on the default branch ([WRAPUP.md](WRAPUP.md#on-the-default-branch)) it holds instead the files changed in the range, as committed at the review's HEAD. At most 100 files and 2 MiB of text. A file it does not hold had no change since HEAD when the review started, so it is cut from the commit the run's `head` names (what `git show <head>:<file>` prints): that is the code the reviewer read, not a guess, and it is read under the same refusals as the working tree (see `unavailable`). A file marked `assume-unchanged` or `skip-worktree` hides its edits from git and so from this test: its snippet is HEAD's version |
| `worktree` | cut from the working tree **when a `--from-file` review was imported**. Such a review left no evidence, so this may differ from the code the reviewer saw |
| `unavailable` | there is no snippet (`snippet` is absent). The file does not exist or is not UTF-8 text under 256 KiB, the line is past its end, the file is outside the repo, a symlink, under `.git` or a `.env*` file (in any letter case), or the evidence saw it change but could not keep it (over the evidence budget, binary, deleted), or it changed beyond the evidence's 100 files, or the commit `head` names holds no regular file of that name (untracked and git-ignored, a symlink, a directory, a submodule), or the review began before the first commit. A `--target` review is always `unavailable`: its evidence is marked unverified against the target, because the review looked at a PR or another branch and the local files may be other code than the finding is about |

When the window is longer than the budget, the line farthest from the flagged one is dropped first (the one above, on a tie), and a flagged line longer than the budget on its own is cut. A snippet that is less than the whole window ends with the line `[snippet truncated]`; a window cut short only by the start or end of the file is the whole window and says nothing.

`add` records the `snippet` it is given and no `snippet_source`; `ingest-code-review` records neither. Re-importing a recorded run with `--from-file` is a replay and keeps the snippets the first import recorded. A finding without `snippet_source` (written before the field existed, or by another command) is valid. `review --dry-run --from-file` prints the findings it would record, snippets included; a dry run of a review that would run `claude` runs and records nothing, so it has no snippets to show.

## Rule ids

`rule_id` is one of:

- a **clause id** defined in one of the configured `rules_files` (for example `P0E.2`, `R.7`);
- `unmapped`: a real finding that matches no clause. Use it instead of the nearest-looking clause, so clause statistics stay honest;
- a **tool id**, `<tool>:<code>` (for example `ruff:F811`, `gate:SYNTAX`, `eslint:no-undef`).

`add` rejects an id that is none of these (`unknown rule_id 'P9.9'`); nothing is coerced. `ingest-code-review` is the one place unknown input becomes `unmapped`, because the model's `category` is a best-effort extra. The ledger schema only checks the *shape* of a rule id; clause existence is checked against the rules files when an event is created, never when old events are read, so a rules file can change without invalidating history.

### Rules files

A clause is defined by a line that **starts** with its id, in either notation:

```
1. **[P0E.1]** Keep the guarded block to the line expected to fail.       <- bracketed (list item, bullet or heading)
| P0E.1 | blocker | Keep the guarded block to the line expected to fail. |   <- table row, id in the first cell
```

An id mentioned mid-line is a cross reference and defines nothing; lines inside code fences are ignored; defining an id twice is an error; a rules file that defines nothing is an error. An optional severity tag on the defining line sets the clause's default severity: `(blocker)`, `[should-fix]`, `` `nice` ``, a table cell, or `severity: blocker`. A clause without a tag takes the severity of its family from the `| family | severity |` table, if the rules files have one ([COMPILE.md](COMPILE.md#severity-table)); with neither it has no default severity (new findings then get `should-fix` unless the reviewer supplied one). A clause defined in an earlier rules file cannot be redefined by a later one.

The id notation is the config key `clause_id_pattern`, a regex for one id. Default: `[A-Z][A-Z0-9]*\.[0-9]+`, which matches `P0E.2`, `P3.1`, `R.7`.

## Config: `.ratchet.json`

At the repo root (main worktree), or any file passed with `--config`. Every key is optional; unknown keys are an error. Relative paths resolve against the repo root. In a linked worktree the files git tracks (`rules_files`, `digest`, `inject_targets`) resolve against that worktree instead; a path that leads outside the repo (a shared `../rules.md`, an absolute path, a path under `~`, which stands for the home directory of whoever runs the tool) is one file for every worktree and is looked up from the main worktree. A rules file outside the repo is an outer layer: ratchet reads it and never writes it, and the commands that need the whole rule set stop when it is not on the machine ([COMPILE.md](COMPILE.md#outer-layers)). `ledger` and `skips_file` always resolve against the main worktree, and a `.ratchet.json` in a linked worktree is ignored.

| key | default | meaning |
|---|---|---|
| `repo_slug` | origin remote basename, else the repo directory name | `[a-z0-9-]+`; goes into finding ids |
| `ledger` | `.style-findings.jsonl` | ledger path; `--ledger` overrides it |
| `rules_files` | `[]` | markdown files holding clause definitions (a linked worktree reads its own copies). A file outside the repo is a read-only outer layer that has to be on the machine, see [COMPILE.md](COMPILE.md#outer-layers) |
| `clause_id_pattern` | `[A-Z][A-Z0-9]*\.[0-9]+` | regex for one clause id |
| `tool_rule_map` | `{}` | `{"ruff:F811": "P2.3"}`: a reported tool id is recorded as that clause (`rule_code` keeps the tool id, `rule_id_source` is `tool`). Values must be defined clauses |
| `scenarios` | `{}` | `{rule_id: text}`: the `failure_scenario` used when a reported finding has none |
| `skips_file` | none | JSON-lines file of gate skips, see below. Unset means no skip exclusion. Read from the main worktree, like the ledger |
| `label_page_threshold` | `20` | `infer` suggests a label page, and `label-page --auto` generates one, when more findings than this are open |
| `legacy_unmapped_alias` | `[]` | see [Legacy ledgers](#legacy-ledgers) |
| `digest`, `digest_max_chars`, `digest_max_bytes`, `inject_targets` | see [COMPILE.md](COMPILE.md#config-keys) | settings for `compile` |
| `max_review_rounds` | `3` | integer >= 1: how many reviews one change gets, see [Review rounds](#review-rounds) |
| `default_branch` | origin's HEAD branch, else `main`, else `master` | the branch a change's fork point is measured from (a local branch name; a remote-only one is found as `origin/<name>`) |
| `wrapup_min_changed_lines` | `200` | integer >= 1: `ratchet wrapup` reviews when at least this many lines changed since the last review. Reaching this or `wrapup_min_files` is enough, see [WRAPUP.md](WRAPUP.md#thresholds) |
| `wrapup_min_files` | `5` | integer >= 1: `ratchet wrapup` reviews when at least this many files changed since the last review. Reaching this or `wrapup_min_changed_lines` is enough |
| `wrapup_auto` | `false` | `true` or `false` and nothing else (`config 'wrapup_auto' must be true or false`): turns on the automatic wrap-up review after commits made in a Claude Code session. Each automatic run costs one `/code-review`. `ratchet init` asks and writes it ([ONBOARDING.md](ONBOARDING.md#the-wrapup_auto-key)). What it turns on, from the commit to the question: [WRAPUP.md](WRAPUP.md#automatic-wrap-up) |
| `wrapup_auto_max_per_day` | `2` | integer >= 1: the most automatic wrap-up reviews (`ratchet wrapup --auto`, which the Stop hook asks for) that may be asked for or started in any 24 hours in this repo, all its worktrees together; a review that fails still counts. A review you run yourself (`ratchet review`, `ratchet wrapup` without `--auto`) is neither counted nor refused. Past it the Stop hook does not ask and `ratchet wrapup --auto` is refused before `claude` runs: [WRAPUP.md](WRAPUP.md#the-daily-cap) |
| `bin` | none | path to the `ratchet` executable, relative to the repo root (for example `third_party/ratchet/bin/ratchet` for a [vendored copy](VENDORING.md)). Only the portable git hooks read it, when `$RATCHET_BIN` is unset ([ONBOARDING.md](ONBOARDING.md#portable-and-pinned-blocks)); the commands themselves ignore it beyond requiring a non-empty string |

## Commands

- **`add --source l1|l2 --run ID [--input FILE]`**: a JSON array (or `{"findings": [...]}`) of items with `id`, `file`, `line`, `summary`, `verdict`, `rule_id`, and `failure_scenario` (unless `scenarios` has a default for the rule). Optional: `severity`, `snippet`, `discovered_at`, and `status` (`fixed`, `skipped`, `no_change_needed`), which also writes a `human` outcome (`fixed`, `dismissed`, `false_positive`).
- **`ingest-code-review --source l1|l2 --run ID [--input FILE] [--verdict CONFIRMED|PLAUSIBLE] [--discovered-at TS]`**: input is the JSON array a code review prints (`file`, `line`, `summary`, `failure_scenario` per object), or the review text, from which the first ```` ```json ```` fenced array is taken. Two optional extras are used when they are usable: `category` and `severity` (otherwise the clause's tagged severity, else `should-fix`). The digest's Labeling section tells the reviewing model to put both on every finding object ([COMPILE.md](COMPILE.md#what-the-digest-contains)). A `category` is a kebab clause id such as `p0e-6`, matched case-insensitively against the known ids; anything else becomes `unmapped`, and the finding is `rule_id_source: model` either way. **Without a usable category** (missing, empty, not a string, or naming a Reviewer rule), the first clause id that is not a Reviewer rule found in the `summary`, and failing that in the `failure_scenario`, is used and the finding is `rule_id_source: attributed`: models that skip the key still tend to cite `(P0E.2, ...)` in prose. A clause id is a whole token in any of the forms `P0E.2`, `p0e-2`, `P0E-2`, `P0E_2` (case-insensitive); `P1.1` is not found inside `P1.10`, and an id no rules file defines is not a mention. A Reviewer rule (a clause without a severity, such as `R.6`; the clauses `compile` lists under Reviewer rules) is not a mention either: no finding violates one, so naming it in the prose or in `category` attributes nothing. Rules with no severity anywhere (no tag, no family table) have no Reviewer rules in this sense: `compile` lists every clause there but warns, and every clause stays something a finding can violate. No mention at all is `unmapped` (a `category` that named such a clause keeps what the model wrote in `rule_code`). Any other category the model did give is never second-guessed from the prose. Findings on the same file+line collapse: the most severe becomes the finding and the rest go into `also_matches`. `display_id`s are `<run>-1`, `<run>-2`, ... so ingesting the same review twice is a no-op. The command prints each new finding's `display_id`, which is what a fixing commit puts in its trailer. If the review's JSON is invalid only because a model quoted code in a string value without escaping the quotes (`cfg["key"]`), the quotes are escaped inside the one-line string fields of the schema (`file`, `summary`, `failure_scenario`, `category`, `severity`) and the summary line says how many; any other defect is an error as before.
- **`count [--by-rule [--label-source]]`**: totals and open counts; when the review findings come from two or more critic versions, a `by critic version:` block follows (`  <version>: <n> total, <m> open` per version in the order each first reviewed, then `  no version recorded: ...` if any review finding lacks one; see [Critic version](#critic-version)); a last line gives the review rounds used on the current change (`review rounds on <key>: 2/3`; `review rounds: n/a (<why>)` on a detached HEAD or when no default branch is known; `review rounds: unavailable (<reason>)` when the lookup fails, say because `default_branch` is not a branch: `count` only reads and never fails for it); `--by-rule` breaks open findings down by rule id (legacy alias applied); `--label-source` adds, per rule, how its findings were labeled: `P0E.2: 3 (model 2, attributed 1)`. `none` is an event from before `rule_id_source` existed.
- **`infer [--all] [--quiet]`**: closes findings, see below. `--quiet` prints nothing unless a finding was closed or a trailer needs attention (the post-commit hook uses it).
- **`review [--level L] [--target T] [--from-file F] [--run ID] [--over-cap]`** and **`init`**: the capture and onboarding commands; see [ONBOARDING.md](ONBOARDING.md). `review` stops with exit code 3 once the change's review rounds are used up, see [Review rounds](#review-rounds). It also gives each finding a code snippet ([Code snippets](#code-snippets)) and records the critic version ([Critic version](#critic-version)).
- **`gate record`**, **`gate pass`**: a lint or style gate feeding the ledger; see [GATES.md](GATES.md).
- **`vendor`**: copy the tool into another repo; see [VENDORING.md](VENDORING.md).
- **`label ID... --outcome fixed|dismissed|false_positive [--note TEXT]`**: records one `human` outcome for each finding named, in one call, with no label page and no batch. An `ID` is read the way a commit trailer is ([Commit trailers](#commit-trailers)): a full finding id, else a `display_id` such as the `r3-2` the review table shows. All or nothing, and every problem is reported at once: an id that matches no finding, a `display_id` shared by several findings (the error lists their full ids), a finding that already has an outcome (a label never overwrites one) and the same finding named twice each refuse the whole call and write nothing. The event is the one `apply-labels` writes, built by the same function; `--note` is its evidence (without one, the evidence is `ratchet label`). `--dry-run` prints the events it would append. For many findings to label by hand, `label-page` and `apply-labels` are the batch route.
- **`label-page`**, **`apply-labels`**: see below.
- **`compile [--check] [--strict]`**: writes the style digest from `rules_files`; see [COMPILE.md](COMPILE.md).

## Attribution

### Commit trailers

A commit closes a finding when its message ends with a trailer naming it:

```
fix: narrow the guarded block in the fetch helper

Fixes-Finding: R12-3
Fixes-Finding: R12-4, R12-7
```

The value is a `display_id` or a full finding id; several trailers, and comma- or space-separated lists, are fine. Trailers are read with git's own trailer parser (`git log --format=%(trailers:key=Fixes-Finding)`), so they only count in the message's last paragraph and the key is case-insensitive. By default the history of `HEAD` is scanned, the HEAD of the worktree `infer` runs in (so the post-commit hook sees the commit that was just made there); `--all` scans every ref, the branches of every worktree included.

- **No time comparison.** The trailer is the author's stated intent, and a fix may be committed before the finding is written to the ledger.
- **Subjects are never parsed.** Text like `feat(parser): ... (S1)` closes nothing.
- An id that matches no finding, or a `display_id` shared by several findings (use the full id), is reported on stderr and closes nothing.
- A finding is closed once; running `infer` again adds nothing. If a gate finding re-opens, the commit that closed its earlier occurrence is not reused.

### Gate passes

An open `gate` finding is closed (`auto:gate_pass`) by the first `gate_pass` event **after it in the file** whose `files` lists the finding's file. Findings listed in the skips file are excluded: a gate that only inspects changed lines lets a skipped violation stay in the file, so a later unrelated edit passing the gate says nothing about it. l1/l2 findings are never closed this way.

The skips file (`skips_file`) is JSON lines; each row may carry `finding_ids` (a list of ids that were skipped past the gate) and `reason_kind`. A file that does not exist yet means no skips. Findings in a row with `reason_kind: "false_positive"` are only pre-selected on the label page, never labeled automatically. The skips file is shared state like the ledger whose ids it lists: every worktree reads the one in the main worktree. ratchet only reads it; a gate that appends to it should write that copy, not one in its own worktree.

### Labels

`label-page` writes `.ratchet-label-page.html` and `.ratchet-label-batch.json` next to the ledger: one card per open finding, with `skip / fixed / dismissed / false_positive` choices and a note. "Copy JSON" produces `{batch_id, labels: [{id, outcome, note}]}`; pipe it to `apply-labels`. The batch is validated as a whole (right batch id, every id open and in the batch, no duplicates, valid outcomes) and a single bad label rejects everything. Accepted labels are written as `human` outcomes, with the note (or the batch id) as evidence.

## Dry run

`add`, `ingest-code-review`, `infer`, `label`, `label-page`, `apply-labels`, `wrapup` and `compile` accept `--dry-run`: all validation and inference still runs and nothing is written. The ledger commands print the events that would be appended as JSON lines; `compile` prints a diff; `wrapup` prints what it measured and what it would do, and runs no review; when a review is due it also says whether a real run would be refused ([WRAPUP.md](WRAPUP.md#dry-run)).

## Legacy ledgers

Ledgers written before this schema (all fields of the first v1 shape) load and fold unchanged; the newer fields (`discovered_at`, `also_matches`, `rule_id_source`, `snippet_source`, and on `review_run` `reviewer`, `reviewer_version`, `level`, `head`, `version_changed`, `tree`, `trigger`) are optional. Events are never rewritten.

The original importer generated three-digit review ordinals such as `R1-001`.
Replaying that run with the current `R1-1` format recognizes the legacy finding,
preserves its ID and outcome, and still rejects conflicting file/line/summary content.
New runs use unpadded ordinals. This does not retroactively merge duplicates already
written by an intermediate version; inspect such duplicates before labeling them.

Some projects used one clause as a catch-all for reviewer findings that fit no clause. If yours did, list it:

```json
{ "legacy_unmapped_alias": ["R.1"] }
```

When reading, an `l1`/`l2` finding whose stored `rule_id` is listed is **treated as `unmapped`** in aggregates (`count --by-rule`) and on the label page. The stored events keep their original `rule_id`. The default is an empty list: nothing is aliased unless the config says so. The alias does not apply to `gate` findings.
