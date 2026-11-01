# Pre-registered criteria

These criteria were fixed before any recording was made. They are not changed after seeing a result; version control shows the order.

## Cases

One case per rule, `rel-1` to `rel-4` (clauses REL.1 to REL.4). Each case has two files: a violation of the rule that is new (a different domain and code shape from the finding the rule was learned from), and a control that looks similar but complies or falls under the rule's stated exemption. Each case's `oracle.py` shows by running the code that the violation breaks and the control does not.

In `rel-1`, `rel-3` and `rel-4` the reviewed change adds the two files as new files. In `rel-2` the reviewed change modifies earlier versions of the same two files, which are held in the case's `base/` directory and are present in the base commit the reviewer sees; its change still touches exactly the case's two files. In `rel-2` the violation file changes a stored key scheme without a compatible read of rows stored under the old scheme, and the control file changes only how a label is displayed while the stored identity stays the same.

## Conditions

- `with_rule`: the repo's full `CODING-STYLE.digest.md`.
- `without_rule`: the same digest with the one line of the case's clause removed, and nothing else changed.
- 3 runs per condition per case.
- Reviewer: Claude Code's built-in `/code-review` at level `high`.
- The reviewed change contains exactly the case's two `code/` files: added as new files, or, where the case has `base/` versions of them (`rel-2`), as modifications of those versions.
- Each recording keeps the digest it was made with: the exact digest text of each case and condition is saved next to the recordings, and the provenance of every run records its sha256. A recording is checked against its own saved digest, not against the repo's digest on the day it is replayed, so a later change of `CODING-STYLE.md` does not make recordings unreplayable. If the digest line of the case's clause now differs from the one the `with_rule` runs were recorded with, the report says so in a note for that case and the scores still describe the recorded digests. The code files, the `base/` files and the raw reviewer output of a recording must be unchanged since it was made, or it is refused.

## Matching

- A finding belongs to a file by its `file` field.
- A finding's label is its `category`: the clause ID in kebab case (`rel-1`), or `unmapped`.

## Failed runs

A run fails if the reviewer call fails (non-zero exit, timeout, cannot be started) or its output has no parsable findings array. An explicit empty array (`[]`) is a valid run with 0 findings, not a failed run.

- A failed run is kept and reported.
- Each run slot (case x condition x run number) may be repeated at most once, only because it failed, never because of its result. The repeat is made immediately after the failure.
- Both attempts are kept. The slot is scored from its last attempt, and the report says how many slots needed a repeat and lists every failed attempt.
- A slot whose repeat also fails stays failed.
- A case with a failed `with_rule` slot is INCOMPLETE: it is neither passed nor failed. A failed `without_rule` slot has no pass line to affect and is reported.

## Pass line (per case, `with_rule` runs only)

A case PASSES only if both hold:

1. In at least 2 of 3 runs, a finding on the violation file carries the case's clause label.
2. In 0 of 3 runs, a finding on the control file carries the case's clause label (that is a false positive).

## Also reported, no pass line

For each case and each condition (`with_rule` and `without_rule`):

- the number of runs (of 3) in which the violation file is flagged at all, with any label;
- the number of runs (of 3) with a finding on the violation file carrying the case's clause label;
- the number of runs (of 3) with a finding on the control file of any label, and the number carrying the case's clause label.

## Reporting

- Results are reported whatever they are: the raw recordings, the counts above, and pass or fail per case.
- A failing case is recorded as a limitation. It is not hidden, and it is not re-run until it passes.
- There is no overall accuracy number.
- The sample is small (4 rules x 3 runs). The counts describe the reviewer version that was recorded, not reviewers in general.

## Addendum 1 — rel-3b (added after round 1, before any rel-3b recording)

- Why the case exists: the control of `rel-3` has a real defect against REL.3. `load_checkpoint` reads the file as UTF-8 and catches only a JSON syntax error and wrong shapes, so a checkpoint that is not valid UTF-8 raises `UnicodeDecodeError` on every run. The reviewer labeled that finding REL.3 in 3 of 3 `with_rule` runs, which was correct; the `rel-3` oracle never tested it.
- `rel-3` stands: its round-1 result is reported as it was recorded. It is not changed and not re-run.
- `rel-3b` uses the same violation file as `rel-3` and a corrected control, and exactly the criteria above: the same conditions, matching, failed-run rule and pass line.
- `rel-3b` is reported as its own case next to `rel-3`, not in place of it. Its `case.json` names the case it corrects and what was wrong with that control.

## Addendum 2 — round 2 and held-out cases (added after round 1, before any rules change and before any `rel-1b` or `rel-2b` recording)

Round 1 failed `rel-1` and `rel-2` because the controls, which fall under the stated exemptions of REL.1 and REL.2, were labeled with the clause, and it showed that in 8 of 24 runs no finding carried a `category` at all. The rules are going to be changed and the check recorded again. This addendum fixes how, before either happens.

### Rounds

- A round is a set of recordings made on one `with_rule` digest. Its recordings are kept under `recordings/round-N/`, and its `round.json` names that digest's sha256 and the round's cases.
- Round 1 is every recording made before this addendum (`rel-1`, `rel-2`, `rel-3`, `rel-3b`, `rel-4`), moved unchanged into `recordings/round-1/`. Its scores are unchanged.
- A case may be added to an existing round only on that round's digest. A new round must use a digest that differs from every earlier round's.
- Each round is scored and reported on its own; rounds are put side by side, never pooled.

### Held-out cases `rel-1b` and `rel-2b`

- `rel-1b` (REL.1) and `rel-2b` (REL.2) are new cases of the same kind as `rel-1` and `rel-2`: a violation of the clause, and a control that falls under the clause's stated exemption. Their `case.json` marks them `held_out`.
- They were written after round 1, by a writer who did not see the drafts of the new wording, and the drafts were written before these cases existed. Neither their files nor their recordings are used as evidence for the rule change.
- Both are recorded into round 1 on the round-1 digest before any rule changes, and again in round 2.
- The control of `rel-1b` is a derived lookup file that is rebuilt on demand, not a temporary file. Whether the round-1 wording ("disposable rebuildable temporary artifacts are exempt") covers it is arguable. The case is kept as written, and this is reported next to its result.

### Round 2

- Cases: `rel-1`, `rel-2`, `rel-3b`, `rel-4`, `rel-1b`, `rel-2b`, 3 runs per condition (36 runs). `rel-3` is not re-run (Addendum 1); `rel-3b` stands in for it.
- Only the digest changes. Held fixed: the case files, the conditions, the reviewer command and level, the number of runs, the matching, the failed-run rule and the pass line. `without_rule` is built as before: the round-2 digest with the one line of the case's clause removed. The reviewer's Claude Code version is recorded; if it differs from round 1, the comparison says so.
- The digest changes in two ways, both made through the normal routes and recorded in `CODING-STYLE.changelog.md`:
  1. A shorter, stricter labeling instruction, generated by `house_rules/digest.py`. It applies to every case and to both conditions.
  2. New wording of the REL.1 and REL.2 exemptions, made through the proposal flow and decided by a person. The evidence is the five round-1 control findings of `rel-1` and `rel-2` that carried the clause label, imported into a separate ledger and marked `false_positive`.
- What each case can show:
  - `rel-1` and `rel-2` were tuned on their own controls, so their round-2 result is a check of the fix, not of generalization.
  - `rel-1b` and `rel-2b` are the held-out check of the new exemption wording.
  - The clauses of `rel-3b` and `rel-4` do not change, so their round-2 result reflects the labeling change alone.
  - In the four REL.1 and REL.2 cases the labeling change and the wording change act together; this check cannot separate them.
- Keeping or rolling back the rule change is a separate decision by a person after both rounds are seen. The report records which was made, and both rounds are reported either way.

### Secondary count: cited (declared after round 1)

- For each file and condition, the number of runs (of 3) in which a finding on that file has a `summary` that names the case's clause (`REL.4` or `rel-4`). The `category` and the failure scenario are not read.
- It is reported for every round, round 1 included. It was added after round 1 had been seen, because some runs named the clause in the summary but carried no `category`. It has no pass line and is never part of a verdict.
