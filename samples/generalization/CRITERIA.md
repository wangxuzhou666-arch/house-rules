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
