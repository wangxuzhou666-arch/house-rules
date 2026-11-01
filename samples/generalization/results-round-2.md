# Generalization check results, round 2

Round 2, with_rule digest `a57942f0ac67`. Level `high`, 3 runs per condition. PASS is decided in `with_rule` alone: a finding on the violation file carries the case's label in at least 2 runs, and a finding on the control file carries it in 0. `without_rule` has no pass line; it is shown for comparison. Columns count runs, not findings; flagged means a finding on that file with any label, `unmapped` included. `cited` is secondary, declared after round 1, no pass line, never part of a verdict: the runs in which a finding on that file has a summary that names the case's clause (REL.4 or rel-4); the failure scenario is not read.

| case | clause | control | condition | runs ok | violation labeled | violation flagged | control labeled | control flagged | verdict | violation cited (secondary) | control cited (secondary) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rel-1 | REL.1 | exempt | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 3/3 | 2/3 |
| rel-1 | REL.1 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |
| rel-1b | REL.1 | exempt | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 0/3 | 0/3 |
| rel-1b | REL.1 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |
| rel-2 | REL.2 | exempt | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 1/3 | 0/3 |
| rel-2 | REL.2 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |
| rel-2b | REL.2 | exempt | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 3/3 | 1/3 |
| rel-2b | REL.2 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |
| rel-3b | REL.3 | compliant | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 3/3 | 0/3 |
| rel-3b | REL.3 | compliant | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |
| rel-4 | REL.4 | compliant | with_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | FAIL | 3/3 | 0/3 |
| rel-4 | REL.4 | compliant | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - | 0/3 | 0/3 |

PASS 0, FAIL 6, INCOMPLETE 0.

Run slots that needed a repeat after a failed first attempt (both attempts are kept): 0.

Notes:

- rel-1: the rules changed since recording: the REL.1 line of the digest now differs from the recorded one; the scores describe the recorded digests
- rel-1b: the rules changed since recording: the REL.1 line of the digest now differs from the recorded one; the scores describe the recorded digests
- rel-2: the rules changed since recording: the REL.2 line of the digest now differs from the recorded one; the scores describe the recorded digests
- rel-2b: the rules changed since recording: the REL.2 line of the digest now differs from the recorded one; the scores describe the recorded digests
