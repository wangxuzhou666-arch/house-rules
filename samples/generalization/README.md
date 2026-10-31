# Generalization check

house-rules turns code-review findings into written rules. This check asks one question about four of them (REL.1 to REL.4): does a rule learned from one real finding let the reviewer flag a new instance of the same mechanism, label it with the rule's ID, and leave a similar-looking legitimate counterpart alone?

Each case pairs a new violation with a control (compliant, or covered by the rule's stated exemption). The reviewer (Claude Code's `/code-review`) sees both files together, once with the rule in the digest and once with that clause removed. The scoring is fixed in [CRITERIA.md](CRITERIA.md), written before any recording. There are no results here yet.

## Layout

```
samples/generalization/
  CRITERIA.md        pre-registered conditions, failed-run rule and pass line
  cases/rel-N/
    code/            the two files the reviewer sees (plain Python, stdlib only)
    base/            optional (rel-2): earlier versions of those files, same names; the reviewed change modifies them
    oracle.py        behavioural ground truth for the pair
    case.json        clause, learned-from findings, which file is which (never shown to the reviewer)
  run_check.py       records and scores the runs
  recordings/<case>/ written by --live, one set per case (nothing is recorded yet)
    <condition>-<n>.txt              the reviewer's raw output for run n (its last attempt)
    <condition>-<n>.json             provenance: command, reviewer version, hashes of the code and base files, sha256 of the digest
    <condition>-<n>.attempt1.txt/.json   the failed first attempt, only for a run that was repeated
    <condition>-<n>[.attempt1].stderr.txt   the reviewer's stderr of a failed attempt, for diagnosis on the recording machine only (git-ignored, never committed)
    <condition>.digest.md            the exact digest text the runs of that condition were made with (one per condition)
  results.json, results.md           what --replay computes from the recordings (written once something is recorded)
```

`<condition>` is `with_rule` or `without_rule`. A case without `base/` adds its two files to the scratch repo as new files. With `base/`, the base commit of the scratch repo holds CLAUDE.md, the digest and the `base/` files (under the names of the `code/` files they are the earlier version of), and the next commit replaces them with the `code/` versions, so the reviewer sees a modification. Every file in `base/` must have a counterpart among the case's two `code/` files and differ from it.

Do not edit a file under `code/` or `base/` once recordings exist: the recordings belong to those exact files, and replay refuses them otherwise. Editing `CODING-STYLE.md` later is fine: each recording is checked against its own digest snapshot, and replay prints a note (also in `results.json`) when the digest line of a case's clause has changed since it was recorded.

## Check a case without a reviewer

An oracle runs the files under the failure its rule is about (a crash in the middle of a replacement, a wrong-shape state file, rows written under an earlier ID scheme and read with the reviewed code, failing and interleaved deliveries under a limit). It prints one line per fact and exits 0 only if the violation breaks and the control does not. It uses temporary directories and leaves nothing behind.

```
python3.12 -I samples/generalization/cases/rel-1/oracle.py
```

Run it for `rel-1` to `rel-4`.

## Score and record

```
python3.12 samples/generalization/run_check.py                          # --replay: score the committed recordings; free
python3.12 samples/generalization/run_check.py --live                   # print the plan; makes no call, exits 2
python3.12 samples/generalization/run_check.py --live --yes-spend 24    # record; runs the reviewer and costs money
```

- `--replay` (the default) scores the recordings against [CRITERIA.md](CRITERIA.md), prints the table, the runs that needed a repeat and any notes, and writes `results.json` and `results.md`. It refuses (exit 1) a recording whose raw output, digest snapshot, code files or base files have been changed since it was made.
- `--live` runs the reviewer. Without `--yes-spend N` it only prints the plan. `N` must equal the number of planned calls (cases x 2 conditions x 3 runs = 24); the plan line also gives the maximum number of repeats on top, which `N` covers. A call that fails, or whose output has no findings array, is repeated once at once; both attempts are kept (see Failed runs in CRITERIA.md). The reviewer's stderr is not recorded: it names the recording machine's settings and local paths, so no provenance file holds any of it. For a failed attempt only, the last 5 lines go to the console and all of it to the `.stderr.txt` file above; a run that worked leaves nothing of it. Existing recordings are never overwritten: delete them, or add `--skip-existing` to record only the runs that have none.
- `--case ID` (repeatable) and `--runs N` select part of the check; results files are not written for a partial selection.
