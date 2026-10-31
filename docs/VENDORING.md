# Vendoring

Some repos run their tests in throwaway worktrees, or copy scripts into temporary repos, where an installed checkout of house-rules is not on the path. They need the tool inside the repo. `house-rules vendor` copies it there and records enough to tell later whether the copy was edited or has fallen behind.

```sh
/path/to/house-rules/bin/house-rules vendor --into third_party/house-rules
git add third_party/house-rules
```

`--into` is any directory; it is created if needed, and it does not have to be inside a git repo (so `vendor` can run from anywhere). `--dry-run` prints what would be copied and writes nothing.

## What is copied

| path | |
|---|---|
| `house_rules/` | the package (Python files and the label page template) |
| `bin/house-rules` | the launcher; it keeps its execute bit |
| `schemas/` | the ledger's JSON Schema |
| `templates/` | the bundled rules template that `house-rules init --template` copies |
| `LICENSE` | |
| `VENDORED_FROM.json` | written last, see below |

Nothing else: no tests, docs or plugin files. The template is vendored because it is small and `init --template` reads it, so `init` works from a vendored copy. A copy made before `templates/` was vendored lacks it: `init` then stops with a message naming `--rules-file PATH` (your own rules file), re-running `house-rules vendor`, or running from a full checkout. Bytecode caches (`__pycache__`, `*.pyc`) and `.DS_Store` are never copied or hashed, so running the vendored copy does not count as editing it.

Run a vendored copy as `python3 third_party/house-rules/bin/house-rules ...`. If you run `house-rules init` from it and your git hooks directory is inside the repo (for example `.githooks`), the hooks look for the tool through `$HOUSE_RULES_BIN`, `bin` in `.house-rules.json`, then `PATH`; init prints the line to add, `"bin": "third_party/house-rules/bin/house-rules"` ([ONBOARDING.md](ONBOARDING.md#portable-and-pinned-blocks)). From Python with `third_party` on `sys.path` and `from house_rules.api import ...` ([GATES.md](GATES.md#python-api)).

Running `vendor` again prepares a fresh copy and marker in a temporary sibling directory, checks the copy against the planned content hash, then replaces the owned entries. Files the source dropped disappear and hand edits are lost. Anything else in the directory, including other `bin/` tools, is left alone. A directory that has `house_rules/`, `bin/house-rules`, `schemas/`, `templates/` or `LICENSE` but no `VENDORED_FROM.json` is not a vendored copy; `vendor` refuses it and changes nothing. `--into` may not be the source checkout or a folder inside it.

Copying or validation failure leaves the old files intact. An error during replacement restores the moved entries and old marker. If restoration itself fails, the error names the retained recovery directory; restore its `old/` entries before retrying. These are multiple file moves, not one atomic directory swap: concurrent use, process termination and power loss during replacement are not covered by automatic recovery. Run updates while the vendored tool is idle. Successful updates remove the temporary backup.

## `VENDORED_FROM.json`

```json
{
  "source_commit": "9f1c2a7e0b3d4a5c6e7f8091a2b3c4d5e6f70812",
  "content_sha256": "3b7e..."
}
```

- `source_commit`: the full HEAD commit of the house-rules checkout the copy came from, when the vendored files in that checkout match HEAD. Otherwise `uncommitted`: the checkout has changes to those files, no commit yet, is not a git repository, or sits inside some other repository (whose HEAD says nothing about it). `content_sha256` identifies such a copy.
- `content_sha256`: sha256 over one line per file, in path order, `<path> NUL <sha256 of the file's bytes> LF`, for every file in the table above except the marker. It changes when a byte, a file name, or the set of files changes; file modes and timestamps do not count.

Commit the marker with the copy.

## `--check`

```sh
house-rules vendor --check --into third_party/house-rules                    # was the copy edited?
house-rules vendor --check --against-source --into third_party/house-rules   # ... and has the source moved on?
```

Writes nothing; exit 0 when the copy is intact, exit 1 with one line per problem on stderr otherwise.

- **Edited**: the files now in the directory hash differently from `content_sha256`. A changed, added, removed or renamed file all count.
- **Stale** (`--against-source`): the source checkout this `house-rules` runs from hashes differently from `content_sha256`, so the copy is not what `vendor` would produce today. Run `--check --against-source` with the source checkout's own `bin/house-rules`.

Both checks can fail at once. Run `vendor` again to fix either. Without `--against-source` a copy that is merely behind the source passes: the check is about the copy, so it works in CI where no source checkout exists.

A missing or malformed `VENDORED_FROM.json` is an error (exit 1).
