# Suggested rule-maintenance request

Use this after a native review has been saved. The agent follows the
[assessment and decision contract](../../docs/DECISION-CARDS.md); the person should
not have to prepare JSON or manually assemble paths.

> Collect decision-context for the selected finding IDs. Inspect their original
> review, available code snapshot and current code; report missing or moved evidence.
> Compare the complete configured rules, including scope and exceptions.
>
> Judge each scenario as supported, uncertain or refuted, with a reason. Group only
> findings sharing a failure mechanism. Choose covered, keep, add, rewrite, merge
> or retire. Explain why this action fits better than a nearby alternative. A high
> severity or repeated occurrence alone does not justify another rule. A specific
> bug may need a code fix without a rule edit.
>
> Prepare a validated decision batch before asking me. By default, put an action
> title above separate bullets for source, code location, rule coverage, failure
> scenario, evidence, reason, exact clause/diff and scope. Leave a blank line between
> bullets; let long items wrap. Include a short translation if helpful. Use verified
> absolute-path links and line numbers; do not hide material uncertainty.
>
> Use the host's native question tool. Offer accept, adjust, reject or defer, adapting
> to its supported controls. Record my actual answer. If I request adjustment, show
> the revised exact text and obtain a new decision before applying it. Apply only
> approved groups, compile/check the digest, and retain the batch and receipt.
> Accepting a rule does not close the code defect. Do not push.

Replace the bullet layout with your preferred presentation if needed, while retaining
the evidence and approval requirements. Preview-only feedback must not be counted
as a new rule adoption. Personal acceptance/consistency metrics remain separate
from deciding whether this particular change should be applied.
