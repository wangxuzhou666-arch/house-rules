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

## Run it

1. Copy the chosen Python files and your selected rule corpus to a new scratch git
   repository. Commit a small baseline so the review target is explicit; then add
   or change the sample files. Do not review unrelated personal projects.
2. Run `ratchet init --rules-file CODING-STYLE.md`, or the setup skill. Verify the
   compiled digest and its import before reviewing.
3. Use the host's native Code Review capability with [this prompt](reviewer-prompt.md).
   The supported wrapper is `ratchet review`; for a native review already completed
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
