# Configuration replacement: from finding to rule decision

This sample teaches one failure mechanism: replacing a configuration can destroy
the last usable copy before a replacement is ready. All inputs are synthetic.
The files are intended for an isolated scratch repository, not production storage.

| File | Role |
|---|---|
| `CODING-STYLE.md` | Small, customizable ERR.1 / REL.1 / reviewer-guidance corpus |
| `unsafe_config.py` | Delete-before-write and swallowed parsing errors |
| `safe_config.py` | Validate and prepare before replacement; propagate parsing errors |
| `heldout_unsafe.py` | Transfer case: serialization fails after opening/truncating the target |
| `heldout_safe.py` | Serialize first, then prepare and replace |
| `check_examples.py` | Deterministic failure/control checks, including mode 0600 preservation |
| `reviewer-prompt.md` | Request a usable native-review output |
| `maintenance-prompt.md` | Compare findings with rules and request a human decision |
| `recorded-review.txt`, `recorded-review-provenance.json` | The raw output of one real native review, and the fingerprints of it and of the files it reviewed |
| `reviewed/` | The exact files that review saw: `config_store.py`, `heldout_safe.py` (before the permission fix) and `heldout_unsafe.py` |
| `make_answers.py` | Offline stand-in for the model and the person in the decision step: writes the assessment and the answers for the recorded review (`--help`; [workshop](../../WORKSHOP.md), section 3) |

## Design the experiment before reviewing

The main pair isolates an injected I/O failure. The transfer pair uses a non-JSON-
serializable value to fail at a different stage. In the recorded acceptance, the
transfer pair was introduced only after the human approved REL.1. Now that it is
published, it is a teaching/regression fixture, not a hidden evaluation set.

To exercise **new-rule discovery**, start a scratch corpus with ERR.1 and R.1 only,
omitting REL.1 and its explanatory lines. Review the unsafe example; a real defect
outside the existing corpus should remain `unmapped`. Ask the maintenance agent
whether the mechanism warrants a rule. It must not add REL.1 merely because this
sample mentions it. You decide on the final wording.

To exercise **existing coverage**, retain the supplied REL.1. The expected rule
decision is covered/no change when its wording already captures the finding.
Repeated findings are not additional reasons to duplicate the clause.

## Replay the recorded review

`recorded-review.txt` is the raw output of the last allowed round (run `live-r3`) of the
original acceptance. The files it names are in `reviewed/`, not the published ones: after
that review the safe examples gained permission-bit preservation, and `config_store.py` is
the earlier form of `safe_config.py`. To replay it without a model:

1. In a new scratch git repository, copy `CODING-STYLE.md`, run
   `house-rules init --rules-file CODING-STYLE.md` and commit.
2. Copy the three files in `reviewed/` to the repository root, so the paths the review
   names exist. Do not use the published `safe_config.py` or `heldout_safe.py` for this.
3. Run `house-rules review --from-file samples/config-replacement/recorded-review.txt --run live-r3`.

No reviewer is called. It records two findings: REL.1 on `heldout_unsafe.py` line 7, and an
`unmapped` one on `config_store.py` line 18 (permission bits not preserved).
`house-rules decision-context` then lists each source as available, with the fingerprint
that `recorded-review-provenance.json` gives, and importing again records nothing new.
The second finding also mentions the original repository's baseline, a `.pending`-file flow
that is not part of this sample. `tests/test_samples.py` runs these steps and checks the
fingerprints, so the record cannot drift from the files again.

To go on from the replay to a rule decision without a model, start from the rules without
REL.1 (so that the first finding is `unmapped`) and let `make_answers.py` write the assessment
and the answers that `prepare-decisions` and `apply-decisions` take. The
[workshop](../../WORKSHOP.md) (section 3) walks through it,
and `tests/test_samples.py` runs the same path.

## Run it

1. Copy the chosen Python files and your selected rule corpus to a new scratch git
   repository. Commit a small baseline so the review target is explicit; then add
   or change the sample files. Do not review unrelated personal projects.
2. Run `house-rules init --rules-file CODING-STYLE.md`, or the setup skill. Verify the
   compiled digest and its import before reviewing.
3. Use the host's native Code Review capability with [this prompt](reviewer-prompt.md).
   The supported wrapper is `house-rules review`; for a native review already completed
   elsewhere, save its output and import once with `review --from-file FILE --run ID`.
4. Give the saved findings to the maintenance agent with [this prompt](maintenance-prompt.md).
   Inspect source, evidence, coverage and exact changes in the native question.
5. Accept, request changes, reject or defer. The agent records your actual response;
   approval of exact text permits application and digest compilation.
6. Review a compliant control and a distinct violating case under the resulting
   rules. Stop at the configured review cap, and preserve unexpected findings.

The native reviewer is nondeterministic: do not expect a fixed count or identical
wording. Check the failure mechanism and attribution rather than matching a prose
string. A compliant control for REL.1 may still have a different correctness defect.

The examples assume ordinary local files and one writer. They preserve permission
bits after a defect found during live acceptance was corrected. They do not establish
ownership/ACL preservation, concurrent-writer correctness or power-loss durability.
Those would require additional requirements and tests.
