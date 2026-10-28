# Personal decision metrics — future work

Status: design direction, not an implemented benchmark or automatic training loop.
Today, `decision-summary` reports accept/revise/reject/defer/pending counts, separates
initial proposals from revisions and change proposals from no-change recommendations,
and reports rolled-back batches. It does not calculate a consistency score.

## Preserve the decision history

The existing batch and receipt retain the original proposal, finding and rule context,
the operator-recorded actual human response, approved text and application state.
Revisions can link to a parent batch. A future report should derive its numbers from
those records, not maintain a second editable tally. Native questions still depend
on the host truthfully recording the response; the CLI does not authenticate a person.

A future personal report could show:

| Field | Purpose |
|---|---|
| Person/project and time window | Establish whose standards and which sample are measured |
| Proposal ID, parent and rule version | Keep revisions of one proposal together |
| Suggested action and exact text | Preserve what the model originally recommended |
| Actual decision and adjustment reason | Preserve acceptance, correction or rejection |
| Applied version and later rollback | Distinguish approval from a lasting change |
| Finding, code snapshot and review source | Let the person inspect the original evidence |
| Counts, denominator and exclusions | Make the result reproducible |

## Separate acceptance from consistency

One proposed first-pass acceptance rate is:

`initial change proposals accepted unchanged / initial change proposals with an accept, revise or reject decision`

Report deferrals and pending proposals separately, with the total number presented.
Do not count preview clicks, revised resubmissions or duplicate findings as additional
initial successes. A merged proposal is one decision unit, not one vote per edited
clause. Report eventual acceptance after revision and later rollback separately.
The current counts are useful inputs, but this reporting policy is not implemented.

Acceptance means the proposal suited this person's decision at that time. It does
not establish that the finding was correct, the rule prevented future bugs, or an
independent human would make the same choice. A model could raise acceptance by
offering only easy suggestions while missing important opportunities.

A later consistency study could compare model recommendations against independently
recorded decisions by the same person on a held-out set, including cases where no
rule change is warranted. Record rule and prompt versions, disagreements, task mix,
sample size, uncertainty, session boundaries and changes in the person's preferences.
Evaluate action agreement and wording acceptance separately. This requires an explicit
evaluation protocol beyond the existing decision counter.

90% may be a personal research target once the denominator and sample are defined;
it is not a universal threshold or a current product claim. Different people may
legitimately prefer different rules. Neither a high score nor an old acceptance
authorizes a new edit: every proposed rule change still needs a human decision.
