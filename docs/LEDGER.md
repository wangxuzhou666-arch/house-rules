# Ledger reference

The ledger is a JSON-lines file, one event per line, append-only. Default location: `.style-findings.jsonl` in the root of the repo's main worktree (so every linked worktree shares one ledger). The machine-readable definition is [`schemas/finding.v1.schema.json`](../schemas/finding.v1.schema.json); the tool validates every event against the same rules before it writes and when it reads, and refuses a ledger with a bad line rather than reading part of it.

## Events

Three kinds share the file. The current state of a finding is the **fold of its events in file order**: a finding is open until an `outcome` for its id appears. Nothing is ever rewritten.

### `finding`

| field | notes |
|---|---|
| `v`, `kind` | `1`, `"finding"` |
| `id` | l1/l2: `<repo>:<source>:<run_id>:<display_id>`. gate: `<repo>:gate:<12 hex>` |
| `display_id` | short handle for humans; the value used in `Fixes-Finding:` trailers. gate: the same 12 hex |
| `run_id` | the review run; `gate` for gate findings |
| `repo` | repo slug, `[a-z0-9-]+` |
| `source` | `gate` (an automated check), `l1` or `l2` (two tiers of review; the tool treats them identically) |
| `ts` | when the event was **written**, local time, `YYYY-MM-DDTHH:MM:SS`, no timezone |
| `sha` | short HEAD at write time, or `none` on an unborn branch |
| `file`, `line` | `line` is an integer >= 1 |
| `rule_code` | what the detector or reviewer called it (tool code, or the category text the model gave) |
| `rule_id` | see [Rule ids](#rule-ids) |
| `category` | free-form grouping label. New events carry the clause family (`P0E`), the tool name (`ruff`) or `unmapped`; older ledgers use a fixed set such as `exception` |
| `severity` | `blocker`, `should-fix` or `nice` |
| `summary`, `failure_scenario` | non-empty text |
| `verdict` | `CONFIRMED` or `PLAUSIBLE` |
| `snippet` | optional code context |
| `discovered_at` | optional; when the reviewer produced the finding, if that differs from `ts` |
| `also_matches` | optional; other findings on the same file+line collapsed into this one (`rule_id`, `severity`, `summary`, `failure_scenario` each) |
| `rule_id_source` | optional: `model` (a model's category), `human`, `tool` (mapped through `tool_rule_map`), `attributed` (reserved for a later attribution step). Absent on older events |

A `finding` event whose id already has an outcome **re-opens** it when its source is `gate` (the violation came back). A repeated l1/l2 event is ignored, and one that reuses an id for a different file/line/summary is refused (it usually means a run id was reused for a different review).

### `outcome`

`{v, kind, id, outcome, outcome_by, evidence, ts}`: `outcome` is `fixed`, `dismissed` or `false_positive`; `outcome_by` is `auto:commit`, `auto:gate_pass` or `human`; `evidence` is non-empty text (`commit <sha8> <subject>`, `gate_pass <ts> sha <sha> covers <file>`, or the person's note).

### `gate_pass`

`{v, kind, repo, ts, sha, files}`: an automated gate passed over `files` (a non-empty list of paths). This tool does not write these; the gate that runs in your pre-commit hook or CI does. It may append them (and `gate` findings) itself: write each batch with a single `O_APPEND` write so concurrent writers cannot interleave half lines. A gate finding's id is `<repo>:gate:` plus the first 12 hex of `sha1(file|rule_code|line text with all whitespace removed)`, so re-detections of the same issue fold into one finding even when the line moves.

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

At the repo root (main worktree), or any file passed with `--config`. Every key is optional; unknown keys are an error. Relative paths resolve against the repo root.

| key | default | meaning |
|---|---|---|
| `repo_slug` | origin remote basename, else the repo directory name | `[a-z0-9-]+`; goes into finding ids |
| `ledger` | `.style-findings.jsonl` | ledger path; `--ledger` overrides it |
| `rules_files` | `[]` | markdown files holding clause definitions |
| `clause_id_pattern` | `[A-Z][A-Z0-9]*\.[0-9]+` | regex for one clause id |
| `tool_rule_map` | `{}` | `{"ruff:F811": "P2.3"}`: a reported tool id is recorded as that clause (`rule_code` keeps the tool id, `rule_id_source` is `tool`). Values must be defined clauses |
| `scenarios` | `{}` | `{rule_id: text}`: the `failure_scenario` used when a reported finding has none |
| `skips_file` | none | JSON-lines file of gate skips, see below. Unset means no skip exclusion |
| `label_page_threshold` | `20` | `infer` suggests a label page, and `label-page --auto` generates one, when more findings than this are open |
| `legacy_unmapped_alias` | `[]` | see [Legacy ledgers](#legacy-ledgers) |
| `digest`, `digest_max_chars`, `digest_max_bytes`, `inject_targets` | see [COMPILE.md](COMPILE.md#config-keys) | settings for `compile` |

## Commands

- **`add --source l1|l2 --run ID [--input FILE]`**: a JSON array (or `{"findings": [...]}`) of items with `id`, `file`, `line`, `summary`, `verdict`, `rule_id`, and `failure_scenario` (unless `scenarios` has a default for the rule). Optional: `severity`, `snippet`, `discovered_at`, and `status` (`fixed`, `skipped`, `no_change_needed`), which also writes a `human` outcome (`fixed`, `dismissed`, `false_positive`).
- **`ingest-code-review --source l1|l2 --run ID [--input FILE] [--verdict CONFIRMED|PLAUSIBLE] [--discovered-at TS]`**: input is the JSON array a code review prints (`file`, `line`, `summary`, `failure_scenario` per object), or the review text, from which the first ```` ```json ```` fenced array is taken. Two optional extras are used when they are usable: `category` (a kebab clause id such as `p0e-6`, matched case-insensitively against the known ids; anything else becomes `unmapped`) and `severity` (otherwise the clause's tagged severity, else `should-fix`). Findings on the same file+line collapse: the most severe becomes the finding and the rest go into `also_matches`. `display_id`s are `<run>-001`, `<run>-002`, ... so ingesting the same review twice is a no-op. The command prints each new finding's `display_id`, which is what a fixing commit puts in its trailer.
- **`count [--by-rule]`**: totals and open counts; `--by-rule` breaks open findings down by rule id (legacy alias applied).
- **`infer [--all]`**: closes findings, see below.
- **`label-page`**, **`apply-labels`**: see below.
- **`compile [--check] [--strict]`**: writes the style digest from `rules_files`; see [COMPILE.md](COMPILE.md).

## Attribution

### Commit trailers

A commit closes a finding when its message ends with a trailer naming it:

```
fix: narrow the guarded block in the fetch helper

Fixes-Finding: R12-003
Fixes-Finding: R12-004, R12-007
```

The value is a `display_id` or a full finding id; several trailers, and comma- or space-separated lists, are fine. Trailers are read with git's own trailer parser (`git log --format=%(trailers:key=Fixes-Finding)`), so they only count in the message's last paragraph and the key is case-insensitive. By default `HEAD`'s history is scanned; `--all` scans every ref.

- **No time comparison.** The trailer is the author's stated intent, and a fix may be committed before the finding is written to the ledger.
- **Subjects are never parsed.** Text like `feat(parser): ... (S1)` closes nothing.
- An id that matches no finding, or a `display_id` shared by several findings (use the full id), is reported on stderr and closes nothing.
- A finding is closed once; running `infer` again adds nothing. If a gate finding re-opens, the commit that closed its earlier occurrence is not reused.

### Gate passes

An open `gate` finding is closed (`auto:gate_pass`) by the first `gate_pass` event **after it in the file** whose `files` lists the finding's file. Findings listed in the skips file are excluded: a gate that only inspects changed lines lets a skipped violation stay in the file, so a later unrelated edit passing the gate says nothing about it. l1/l2 findings are never closed this way.

The skips file (`skips_file`) is JSON lines; each row may carry `finding_ids` (a list of ids that were skipped past the gate) and `reason_kind`. A file that does not exist yet means no skips. Findings in a row with `reason_kind: "false_positive"` are only pre-selected on the label page, never labeled automatically.

### Labels

`label-page` writes `.ratchet-label-page.html` and `.ratchet-label-batch.json` next to the ledger: one card per open finding, with `skip / fixed / dismissed / false_positive` choices and a note. "Copy JSON" produces `{batch_id, labels: [{id, outcome, note}]}`; pipe it to `apply-labels`. The batch is validated as a whole (right batch id, every id open and in the batch, no duplicates, valid outcomes) and a single bad label rejects everything. Accepted labels are written as `human` outcomes, with the note (or the batch id) as evidence.

## Dry run

`add`, `ingest-code-review`, `infer`, `label-page`, `apply-labels` and `compile` accept `--dry-run`: all validation and inference still runs and nothing is written. The ledger commands print the events that would be appended as JSON lines; `compile` prints a diff.

## Legacy ledgers

Ledgers written before this schema (all fields of the first v1 shape) load and fold unchanged; the three newer fields are optional. Events are never rewritten.

Some projects used one clause as a catch-all for reviewer findings that fit no clause. If yours did, list it:

```json
{ "legacy_unmapped_alias": ["R.1"] }
```

When reading, an `l1`/`l2` finding whose stored `rule_id` is listed is **treated as `unmapped`** in aggregates (`count --by-rule`) and on the label page. The stored events keep their original `rule_id`. The default is an empty list: nothing is aliased unless the config says so. The alias does not apply to `gate` findings.
