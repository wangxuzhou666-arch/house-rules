# Acceptance records

These records separate deterministic checks, actual model review and actual human
decisions. They are engineering acceptance records, not a quality benchmark.

## v1 checks (2026-11-01)

Run on the revision prepared for the first public release. The record of 2026-10-28
follows unchanged.

| Check | Result |
|---|---|
| Unit and integration suite | 2,097 tests passed |
| Offline acceptance script ([Clean installation](#clean-installation)) | passed |
| Mutation definitions | 1,533 defined; each matches exactly one place in its file |
| Full mutation run | not completed for this release (below) |
| `claude plugin validate` | passes with two warnings; `--strict` fails on one of them (below) |
| Generalization check | three rounds recorded; the replay is offline and reproduces the committed results |
| Automatic wrap-up | exercised live twice: headless with a stand-in reviewer, and in an editor session with the real reviewer (below) |
| Remote marketplace install | not checked yet; it needs the first push |

**Mutation.** The last complete run is the one of 2026-10-28 below: 328 of 328
caught. Mutants were added in groups with the code they guard, and each group was
run against the suite when it was added: a targeted run, not a full one. A full run
of all 1,533 was started for this release with eight workers and stopped after about
9.5 hours, near the 350th mutant; at that rate the whole run would have taken about
40 hours on the 8-core laptop it ran on. The runner prints its results only when it
finishes, so the stopped run left no result, and none is claimed here.

**Plugin validation.** `claude plugin validate .` (Claude Code 2.1.316) passes with
two warnings. The first says that `CLAUDE.md` at the plugin root is not loaded as
project context. That file is not meant as plugin context: it is this repository's
own import of its digest, so that reviews of this repository use its rules.
`--strict` turns this warning into an error, so strict validation fails. The second,
a missing icon, is a plugin-directory check that does not change the result.

**Generalization check.** [samples/generalization](../samples/generalization/README.md)
holds 7 cases recorded in three rounds, 114 reviewer calls with Claude Code 2.1.284:
round 1 PASS 1 / FAIL 6, round 2 PASS 0 / FAIL 6, round 3 PASS 1 / FAIL 5. The rule
wording was then restored to round 1's. Replaying the recordings costs nothing, and a
test pins the round-1 scores. What the results mean is in
[METHOD.md](METHOD.md#does-a-rule-generalize).

**Automatic wrap-up, live.** These are the author's observations of 2026-10-30 and
2026-10-31, made under the project's earlier name; the sessions are not published.
In a headless `claude -p` session (Claude Code 2.1.312) with the plugin loaded through
`--plugin-dir` and a stand-in reviewer, Claude committed over 200 lines into a linked
worktree with `git -C`. The Stop hook continued the turn, Claude invoked the wrap-up
skill in automatic mode, the review ran in the background, and the ledger gained one
review run. In an editor session with the real built-in `/code-review`, commits
triggered three automatic wrap-ups in turn, the three review rounds of one branch
(9, 8 and 6 findings). No review started another, and each left one review run in
the ledger.

## v0 record (2026-10-28)

### The rule feedback loop

An isolated synthetic repository began with ERR.1 and reviewer guidance. Native
review identified swallowed parsing errors and delete-before-write loss of usable
configuration. The latter was `unmapped`.

The model proposed retaining ERR.1 and adding a scoped REL.1. A real native question
presented the exact clause. The author accepted both suggestions; tools stored the
response and applied one rule edit plus its digest. A subsequent native review
attributed the replacement defect to REL.1. No simulated acceptance is counted as
this human decision.

The last allowed review round introduced compliant controls and a transfer case
not used to draft the clause:

| Check | Observed result |
|---|---|
| Write fails after deleting the old file | Deterministic unsafe example loses the old content; prepared replacement preserves it |
| Serialization fails after opening/truncating the target | Transfer violation reported under REL.1; deterministic check reproduces loss |
| Serialize/validate and prepare before replacement | Native reviewer did not report REL.1 against either compliant control |
| Other correctness issues | Reviewer separately reported missing permission-bit preservation as `unmapped` |

That additional defect was not suppressed to make the demonstration look clean.
The published safe examples now preserve existing mode bits and default a new file
to mode 0600. Deterministic checks cover this correction. The original repository's
three-round cap was respected: the permission correction was **not** submitted to
a fourth model review there.

Inspect the [recorded review](../samples/config-replacement/recorded-review.txt)
and [reviewed-source fingerprints](../samples/config-replacement/recorded-review-provenance.json).
The source fingerprints describe the pre-permission-fix versions, and those exact files
are kept in `samples/config-replacement/reviewed/` (a test checks each against its
fingerprint). Published examples include the later correction, so do not describe their
exact current bytes as the ones reviewed in that run. Once published, the transfer pair is a regression sample,
not a hidden test set. No accuracy percentage follows from these few observations.

### Clean installation

The offline acceptance script was run from a new output directory. It copied the
distribution to a separate location and created new git repositories. It checked:

- Custom rule-file setup and bundled-template setup, without mixing the two corpora.
- Generated digest/imports and actual pre/post-commit hook probes.
- Synthetic review import, replay idempotence, evidence and proposal preparation.
- A stale staged digest blocking a commit, then successful compilation and commit.
- An actual synthetic fixing commit closing the imported finding through its hook.
- Deterministic failure/control examples.

This offline route does not fabricate a human approval receipt. Reproduce it with:

```sh
python3 scripts/check_acceptance.py --output-dir /absolute/new/scratch-directory
```

The directory must be new. It retains `commands.json`, `result.json`, the copied
distribution and fixture repos. Use a local scratch location; all input code is
synthetic. It creates commits only in those fixture repositories, never in the
source checkout, and does not push or call a model. The commands it runs get no
`GIT_*` or `CLAUDE*` variable and a `HOUSE_RULES_STATE_DIR` inside the output
directory, so running it from a Claude Code session leaves no commit note in the
real state directory ([The commit note](ONBOARDING.md#the-commit-note)).

A separate live check loaded the copied plugin through `--plugin-dir` and invoked
`/house-rules:review high` in the clean custom-style repository. It reached the
native reviewer, saved raw output and a code snapshot, and stored four findings
from six reported items (two were folded into same-location evidence). A fixing
commit in that fixture closed all four live findings. Together with the earlier
synthetic finding, its ledger then contained five total and zero open findings.

Local plugin loading and the CLI route are verified. Remote marketplace download
is not: this working revision has not been pushed. The actual native-review path
tested here is Claude Code; other hosts' import paths are supported but their native
UI integrations have not been exercised by this acceptance.

### Remaining scope

Final local checks: all 328 mutation definitions were executed and caught. The
818-test unit/integration suite passed before mutation and again after restoring
the original source. Each mutant used the same discovered suite with affected
modules and matching test names prioritized, stopping at the first failure; no
tests were removed. The run used eight isolated workers and verified that every
mutation matched exactly one source location.

The full run exposed a coverage gap for a corpus with no configured rules.
Expanded rule-lookup and review-import regressions now verify that punctuation in
unmapped prose does not cause an empty-pattern lookup. Both pass with the original
implementation and fail when the guard is removed. The complete 328-mutant run was
then repeated successfully. Strict digest, skill-format and relative
documentation-link checks also passed.

The [16-finding reconciliation](RECONCILIATION.md) records 15 verified
fixes and one nonblocking cleanup item. Publication, remote installation, broad
longitudinal effectiveness, automatic rule retirement and personal consistency
scoring remain separate work. A rule change still requires a human decision.
