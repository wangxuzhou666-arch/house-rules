# Acceptance record — 2026-10-28

The following separates deterministic checks, actual model review and actual human
decisions. It is an engineering acceptance record, not a quality benchmark.

## The rule feedback loop

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
The source fingerprints describe the pre-permission-fix versions. Published examples
include the later correction, so do not describe their exact current bytes as the
ones reviewed in that run. Once published, the transfer pair is a regression sample,
not a hidden test set. No accuracy percentage follows from these few observations.

## Clean installation

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
source checkout, and does not push or call a model.

A separate live check loaded the copied plugin through `--plugin-dir` and invoked
`/review-ratchet:review high` in the clean custom-style repository. It reached the
native reviewer, saved raw output and a code snapshot, and stored four findings
from six reported items (two were folded into same-location evidence). A fixing
commit in that fixture closed all four live findings. Together with the earlier
synthetic finding, its ledger then contained five total and zero open findings.

Local plugin loading and the CLI route are verified. Remote marketplace download
is not: this working revision has not been pushed. The actual native-review path
tested here is Claude Code; other hosts' import paths are supported but their native
UI integrations have not been exercised by this acceptance.

## Remaining scope

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
