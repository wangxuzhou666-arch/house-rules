# Bring your own review standards

house-rules is a workflow blueprint: review findings become evidence-backed
rule proposals, a person decides, and approved rules feed the next review. The
bundled coding style and decision layout are starting points, not universal standards.

| Layer | Customize | Keep for the workflow |
|---|---|---|
| Coding style | Language, families, priorities, scope, exceptions and examples | Stable clause IDs and parseable definitions |
| Native review | Reviewer, explanation style and additional analysis | Original output plus a normalized finding with source and failure scenario |
| Decision question | Labels, ordering, length, language and native host presentation | Evidence, rationale, exact proposed change, explicit human decision |
| Decision history | Future personal reports and evaluation criteria | Original proposal, actual response, applied revision and provenance |

## Your CODING-STYLE.md

Use an existing file instead of accepting the bundled template:

```sh
house-rules init --rules-file docs/CODING-STYLE.md
house-rules compile --check --strict
```

The equivalent configuration points at your files:

```json
{
  "rules_files": ["docs/CODING-STYLE.md", "docs/PROJECT-RULES.md"]
}
```

Keep files inside the repository for the native maintenance workflow: its evidence
reader and proposal writer enforce that boundary. Multiple files add distinct IDs;
a later file cannot silently redefine an earlier clause. Do not load the bundled
rules alongside yours unless you intend to adopt both. See [compile](COMPILE.md).

Risk severity and enforcement method are separate choices. `blocker`, `should-fix`
and `nice` are the internal severity values; use your own family names, such as
`DATA.1`, `API.1` or `DOC.1`, and define their meaning. A mechanical check may enforce
a low-risk preference; a high-risk issue may require contextual judgment. Project
scope and personal preferences should be stated explicitly, not inferred from an ID.

For a mixed file, give code-violation clauses explicit severities (or a family
severity table), and leave reviewer guidance untagged. With no severity data at all,
the importer permits all defined clauses and defaults findings to `should-fix` when
the reviewer supplies no severity; this is a compatibility mode, not a precise risk
policy. See [rule writing](RULE-WRITING.md) for a small reusable structure.

## Your review output

The native reviewer remains responsible for reviewing code. You may ask it for a
table, grouped explanations, additional risk analysis or a different vocabulary.
The importer currently expects a JSON array, either alone or in a fenced JSON block:

```json
[
  {
    "file": "src/config_store.py",
    "line": 6,
    "summary": "Deleting the old configuration before writing its replacement loses usable state.",
    "failure_scenario": "Writing fails after deletion, leaving no usable configuration.",
    "category": "rel-1",
    "severity": "should-fix"
  }
]
```

These are illustrative values, not a finding from the reader's repository. A
`category` names a real clause in that repository, or `unmapped` for a correctness
defect outside its rules. The importer adds identity, timestamps and attribution
metadata. It does not certify the model's claim. Paths and lines must be verified.

Custom field names need a normalization step before import; there is no arbitrary
schema-mapping setting today. For example, `location.path` maps to `file`, `impact`
plus its trigger maps to `failure_scenario`, and your risk labels map to the three
supported severities. Preserve the untouched original and an explicit link to the
normalized file; never present a rewritten report as verbatim reviewer output.
The current CLI does not automatically manage that two-file mapping. Additional
custom fields are not automatically preserved as ledger fields; keep them in the
original report. See the [ledger contract](LEDGER.md).

For supported output, use `house-rules review --from-file review.txt --run RUN_ID`.
For a different format, supply the normalized file and retain its original beside
it. Imports cannot recreate missing historical code snapshots. The digest's generated
Labeling section describes the canonical format; change the reviewer instructions
or adapter, not the generated digest by hand.

## Your native decision question

The [default decision brief](DECISION-CARDS.md#native-human-decisions) uses an action
title and separate bullets with blank lines. You may change the presentation through
repository instructions or a customized maintenance skill. There is no layout editor
or layout configuration key; rendering still depends on the host's question tool.

Every presentation must make the source, failure scenario, evidence limits, rule
coverage, recommendation and exact change inspectable before the decision. If the
host has no native question tool, use chat. A selected default is never approval.
Retain actual accept/adjust/reject/defer responses; a proposed adjustment requires
confirmation of its final wording. The JSON decision contract remains stable even
when the visible question changes. Preview feedback is not another rule adoption.

## Personal feedback over time

`house-rules decision-summary` currently reports decision counts. A personal acceptance
rate or consistency benchmark is future work, described separately in
[decision metrics](DECISION-METRICS.md). No shared target, including 90%, is enforced.
