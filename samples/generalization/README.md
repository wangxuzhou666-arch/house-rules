# Generalization check

house-rules turns code-review findings into written rules. This check asks one question about four of them (REL.1 to REL.4): does a rule learned from one real finding let the reviewer flag a new instance of the same mechanism, label it with the rule's ID, and leave a similar-looking legitimate counterpart alone?

Each case pairs a new violation with a control (compliant, or covered by the rule's stated exemption). The reviewer (Claude Code's `/code-review`) sees both files together, once with the rule in the digest and once with that clause removed. The scoring is fixed in [CRITERIA.md](CRITERIA.md), written before any recording.

Recordings are filed by round. A round is one digest generation: all its runs were made with the same `with_rule` digest. Round 1 holds seven cases (`rel-1` to `rel-4`, `rel-3b`, and the held-out `rel-1b` and `rel-2b`); its scores are in [results-round-1.md](results-round-1.md).

Three rounds have been recorded; [results-compare.md](results-compare.md) puts them side by side, and CRITERIA.md Addenda 2 and 3 fixed each later round before it was recorded:

- Round 1: the rules as they were. PASS 1 (`rel-3b`), FAIL 6.
- Round 2: a shorter Labeling instruction plus REL.1 and REL.2 rewritten to lead with their exemptions. PASS 0, FAIL 6: in 17 of 18 `with_rule` runs no finding carried a `category` key. The Labeling instruction was then restored.
- Round 3: only the REL.1 and REL.2 rewrites. PASS 1 (`rel-3b`), FAIL 5. Exempt controls of REL.1 were labeled with the clause as often as in round 1 (`rel-1` 3/3, held-out `rel-1b` 2/3), and with the same clause text `rel-4` moved from 1/3 to 3/3 on its violation, a measure of how much 3 runs vary.
- Decision after round 3: the rewrites showed no improvement, so REL.1 and REL.2 were restored to their round-1 wording (Revision 5 in `CODING-STYLE.changelog.md`). The current digest is byte-identical to the one round 1 was recorded with.

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
  recordings/round-<N>/        written by --live, one directory per round
    round.json                 the round: its number, the sha256 of its `with_rule` digest, the cases recorded in it
    <case>/                    one set per case
      <condition>-<n>.txt              the reviewer's raw output for run n (its last attempt)
      <condition>-<n>.json             provenance: command, reviewer version, hashes of the code and base files, sha256 of the digest
      <condition>-<n>.attempt1.txt/.json   the failed first attempt, only for a run that was repeated
      <condition>-<n>[.attempt1].stderr.txt   the reviewer's stderr of a failed attempt, for diagnosis on the recording machine only (git-ignored, never committed)
      <condition>.digest.md            the exact digest text the runs of that condition were made with (one per condition)
  results-round-<N>.json, .md  what --replay computes from one round
  results-compare.md           the rounds side by side per case; written when more than one round has results
```

`<condition>` is `with_rule` or `without_rule`. A case without `base/` adds its two files to the scratch repo as new files. With `base/`, the base commit of the scratch repo holds CLAUDE.md, the digest and the `base/` files (under the names of the `code/` files they are the earlier version of), and the next commit replaces them with the `code/` versions, so the reviewer sees a modification. Every file in `base/` must have a counterpart among the case's two `code/` files and differ from it.

A round is one digest generation. A new round must start on a `with_rule` digest that no earlier round used, so it follows a change of the rules. A case may be added to an existing round only while the digest is still that round's digest. Both are refused before any call to the reviewer.

Do not edit a file under `code/` or `base/` once recordings exist: the recordings belong to those exact files, and replay refuses them otherwise. Editing `CODING-STYLE.md` later is fine: each recording is checked against its own digest snapshot, and replay prints a note (also in `results-round-<N>.json`) when the digest line of a case's clause has changed since it was recorded.

## Check a case without a reviewer

An oracle runs the files under the failure its rule is about (a crash in the middle of a replacement, a wrong-shape state file, rows written under an earlier ID scheme and read with the reviewed code, failing and interleaved deliveries under a limit). It prints one line per fact and exits 0 only if the violation breaks and the control does not. It uses temporary directories and leaves nothing behind.

```
python3.12 -I samples/generalization/cases/rel-1/oracle.py
```

Run it for `rel-1` to `rel-4`.

`rel-3b` is `rel-3` with a corrected control (the control of `rel-3` did not survive a state file that is not valid UTF-8; see Addendum 1 in [CRITERIA.md](CRITERIA.md)); it was recorded in round 1 on the same digest after the other four, run its oracle the same way, and it is reported next to `rel-3`, not in place of it.

`rel-1b` (REL.1) and `rel-2b` (REL.2) are held-out cases for the clauses' exemptions: they were written after round 1, are not used as evidence when the wording of those rules changes, and were recorded into round 1 on the same digest before any rule changed (Addendum 2 in [CRITERIA.md](CRITERIA.md)). Run their oracles the same way.

## Score and record

```
python3.12 samples/generalization/run_check.py                                           # --replay: score every round found; free
python3.12 samples/generalization/run_check.py --round 1                                 # replay one round only
python3.12 samples/generalization/run_check.py --live --round 2                          # print the plan; makes no call, exits 2
python3.12 samples/generalization/run_check.py --live --round 2 --case rel-1 --yes-spend 6   # record; runs the reviewer and costs money
```

- `--replay` (the default) scores the recordings of every round against [CRITERIA.md](CRITERIA.md), prints one table per round, the runs that needed a repeat and any notes, and writes `results-round-<N>.json` and `.md` for each round and, when more than one round has results, `results-compare.md` (per case: violation labeled, control labeled and verdict in each round). `--round N` limits it to that round and writes no comparison. A round is scored for exactly the cases its `round.json` lists. It refuses (exit 1) a recording whose raw output, digest snapshot, code files or base files have been changed since it was made, a `with_rule` snapshot that is not the digest named in `round.json`, and recordings of a case that `round.json` does not list.
- `--live` runs the reviewer and needs `--round N`. Without `--yes-spend N` it only prints the plan. `N` must equal the number of planned calls (cases x 2 conditions x 3 runs; with all seven round-1 cases that is 42); the plan line also gives the maximum number of repeats on top, which `N` covers. A call that fails, or whose output has no findings array, is repeated once at once; both attempts are kept (see Failed runs in CRITERIA.md). The reviewer's stderr is not recorded: it names the recording machine's settings and local paths, so no provenance file holds any of it. For a failed attempt only, the last 5 lines go to the console and all of it to the `.stderr.txt` file above; a run that worked leaves nothing of it. Existing recordings are never overwritten: delete them, or add `--skip-existing` to record only the runs that have none. Once the spending is confirmed, `round.json` is written (or the case is added to it).
- `--case ID` (repeatable) and `--runs N` select part of the check; results files are not written for a partial selection.

The scoring tables have two secondary columns, `violation cited` and `control cited`: the number of runs in which a finding on that file has a `summary` that names the case's clause (`REL.4` or `rel-4`, whole word, any case). They were declared after round 1, have no pass line and never change a verdict. They sit in a separate `secondary` object of `results-round-<N>.json`.
