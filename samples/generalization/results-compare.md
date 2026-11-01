# Generalization check: rounds compared

Rounds: round 1 (with_rule digest `f85fe1df1803`), round 2 (with_rule digest `a57942f0ac67`), round 3 (with_rule digest `9eebabb0c6c3`). Every cell lists the rounds in this order, separated by `->`; `-` means the case was not recorded in that round. Counts are runs of 3 in `with_rule`, verdicts are the ones of the round's own results file.

| case | clause | violation labeled (with_rule) | control labeled (with_rule) | verdict |
|---|---|---|---|---|
| rel-1 | REL.1 | 3/3 -> 0/3 -> 3/3 | 3/3 -> 0/3 -> 3/3 | FAIL -> FAIL -> FAIL |
| rel-1b | REL.1 | 2/3 -> 0/3 -> 2/3 | 2/3 -> 0/3 -> 2/3 | FAIL -> FAIL -> FAIL |
| rel-2 | REL.2 | 2/3 -> 0/3 -> 0/3 | 2/3 -> 0/3 -> 0/3 | FAIL -> FAIL -> FAIL |
| rel-2b | REL.2 | 1/3 -> 0/3 -> 1/3 | 0/3 -> 0/3 -> 1/3 | FAIL -> FAIL -> FAIL |
| rel-3 | REL.3 | 3/3 -> - -> - | 3/3 -> - -> - | FAIL -> - -> - |
| rel-3b | REL.3 | 2/3 -> 0/3 -> 3/3 | 0/3 -> 0/3 -> 0/3 | PASS -> FAIL -> PASS |
| rel-4 | REL.4 | 1/3 -> 0/3 -> 3/3 | 0/3 -> 0/3 -> 1/3 | FAIL -> FAIL -> FAIL |

Reviewer version: round 1: `2.1.284 (Claude Code)`; round 2: `2.1.284 (Claude Code)`; round 3: `2.1.284 (Claude Code)`.
