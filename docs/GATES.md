# Gates

A gate is an automated check that runs on every commit (a linter, a syntax-tree script, a style checker). `ratchet gate` lets one feed the ledger, so its findings are tracked like a reviewer's: recorded once, closed when the gate later passes over the file.

- `ratchet gate record` records findings the gate raised (source `gate`).
- `ratchet gate pass` records that the gate passed over some files. `ratchet infer` then closes the open gate findings in those files ([LEDGER.md](LEDGER.md#gate-passes)).

A gate written in Python can import the same operations instead of running the command; see [the Python API](#python-api).

## `gate record`

```sh
ratchet gate record --input reports.json
some-linter --format=json | convert-to-reports | ratchet gate record
```

`--input` is a JSON file, the JSON itself (text starting with `[` or `{`), or `-` for stdin; without it stdin is read. The JSON is a list (or `{"findings": [...]}`) of reports:

| key | meaning |
|---|---|
| `file` | path of the flagged file. Relative to the repo root (not the caller's cwd), or absolute inside it; absolute symlink aliases are resolved before the boundary check; stored relative with `/` |
| `line` | integer >= 0. `0` means "no position" (a syntax error that has none) and is stored as line 1 |
| `code` | `<tool>:<code>` (`ruff:F811`, `gate:DEPTH`) or a clause id (`P1.1`) |
| `message` | what the gate said, non-empty; becomes the finding's `summary` |
| `severity` | optional: `blocker`, `should-fix` or `nice` |

Every report is checked, and all files are read, before anything is written; one bad report rejects the lot. A report with the same id as a finding that is already open is not written again (the output says `already open`); one whose earlier finding was closed re-opens it, which is how a violation that comes back is tracked.

```
  demo:gate:73491140740a  src/a.py:2  ruff:F811  new
gate record: +1 finding(s), 0 already open
```

### How a report becomes a finding

- **`rule_id`**: `code` is looked up in the config's `tool_rule_map` first, so `{"ruff:BLE001": "P0E.3"}` records every `ruff:BLE001` as clause `P0E.3` (`rule_id_source: tool`). A `<tool>:<code>` that is not mapped is recorded as that tool id. A clause id must be defined in the rules files, otherwise the report is refused; nothing is coerced.
- **`rule_code`**: the code the gate used, without the tool prefix: `F811` for `ruff:F811`, `P1.1` for `P1.1`.
- **`severity`**: the report's, else the clause's (tag or family table), else `should-fix`.
- **`failure_scenario`**: the config's `scenarios[rule_id]`, else `The gate flagged <rule_id>: <message>`.
- **`snippet`**: the flagged line and two lines either side, read from the working tree.
- `verdict` is `CONFIRMED`, `run_id` is `gate`, `sha` is HEAD (`none` before the first commit).

### The finding id

```
id         = <repo_slug>:gate:<hash>
display_id = <hash>
hash       = first 12 hex of sha1( "<file>|<rule_code>|<line text with all whitespace removed>" )
```

`file` is the stored (repo-relative) path, `rule_code` is the unprefixed code above, and the line text is line `line` of the file as `str.splitlines()` sees it (empty when `line` is 0 or past the end). Because whitespace is removed, re-indenting the line or moving it keeps the id, so a re-detection folds into one finding.

This is the id recipe a gate that wrote the ledger by hand would already be using, and it is fixed: changing it would orphan every open gate finding. A gate that has its own check codes (`DEPTH`, `NOQA`) reports them as `gate:DEPTH` and `gate:NOQA` and maps them to clauses with `tool_rule_map`; the hash then uses `DEPTH` and `NOQA`. The tests build a ledger with the recipe written out independently and check that `gate record` reproduces its ids.

## `gate pass`

```sh
ratchet gate pass --files src/a.py src/b.py
```

Writes `{kind: "gate_pass", repo, ts, sha: HEAD, files}`: sorted, de-duplicated, repo-relative paths. Call it when the gate passes, with the files it checked. Open gate findings in those files are closed by the next `ratchet infer` (the post-commit hook runs it).

A gate that only inspects changed lines can pass over a file that still holds a violation it let through on purpose. List the ids of those findings in the config's `skips_file` and a later pass will not close them ([LEDGER.md](LEDGER.md#gate-passes)).

## Python API

```python
from ratchet.api import ledger_path, record_gate_findings, record_gate_pass
```

Three functions are the stable surface; nothing else in the package is promised to keep its name or signature. `repo` is any path inside the repo (default: the current directory); config, rules and ledger location come from its `.ratchet.json`, exactly as for the command line.

| call | does |
|---|---|
| `ledger_path(repo=None) -> Path` | where the repo's ledger is (it may not exist yet) |
| `record_gate_findings(findings, repo=None) -> GateRecording` | what `gate record` does. `findings` is an iterable of the report dicts above. `GateRecording.ids` lists the id of every report, including ones already open; `GateRecording.recorded` the events appended just now |
| `record_gate_pass(files, repo=None) -> dict` | what `gate pass` does; returns the event it appended |

Failures (a bad report, an unknown clause, a ledger that cannot be written) raise `ratchet.errors.RatchetError`, which `ratchet.api` re-exports. A gate whose verdict must not depend on the ledger catches that one type and warns:

```python
try:
    record_gate_findings(reports)
except RatchetError as exc:
    print(f"warning: ledger not updated: {exc}", file=sys.stderr)
```

If the gate runs from a vendored copy ([VENDORING.md](VENDORING.md)), put the directory that holds `ratchet/` on `sys.path` before importing.
