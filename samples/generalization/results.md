# Generalization check results

Level `high`, 3 runs per condition. PASS is decided in `with_rule` alone: a finding on the violation file carries the case's label in at least 2 runs, and a finding on the control file carries it in 0. `without_rule` has no pass line; it is shown for comparison. Columns count runs, not findings; flagged means a finding on that file with any label, `unmapped` included.

| case | clause | control | condition | runs ok | violation labeled | violation flagged | control labeled | control flagged | verdict |
|---|---|---|---|---|---|---|---|---|---|
| rel-1 | REL.1 | exempt | with_rule | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | FAIL |
| rel-1 | REL.1 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - |
| rel-2 | REL.2 | exempt | with_rule | 3/3 | 2/3 | 3/3 | 2/3 | 3/3 | FAIL |
| rel-2 | REL.2 | exempt | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - |
| rel-3 | REL.3 | compliant | with_rule | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | FAIL |
| rel-3 | REL.3 | compliant | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - |
| rel-4 | REL.4 | compliant | with_rule | 3/3 | 1/3 | 3/3 | 0/3 | 3/3 | FAIL |
| rel-4 | REL.4 | compliant | without_rule | 3/3 | 0/3 | 3/3 | 0/3 | 3/3 | - |

PASS 0, FAIL 4, INCOMPLETE 0.

Run slots that needed a repeat after a failed first attempt (both attempts are kept): 0.
