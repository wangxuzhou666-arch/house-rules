# Suggested native-review request

Adapt the following to your repository. Use the host's native Code Review tool or
skill; this request defines scope and output, not a replacement reviewer.

> Review the specified code change for correctness, using this repository's
> configured coding-style digest as the local standard. Read surrounding code and
> relevant full clauses before deciding whether a rule applies. Report a general
> correctness defect as `unmapped` if no local rule covers it. Do not invent a rule,
> threshold, executed test or code location. Do not change code or rules during review.
>
> For each actionable finding, identify the triggering input/state, what the code
> does, and the resulting failure. Distinguish observed checks from inference. Use
> the repository's risk policy and include a specific file and one-based line.
>
> Return a JSON array, optionally in one fenced JSON block, with `file`, `line`,
> `summary`, `failure_scenario`, `category` and `severity`. `category` is a configured
> clause's kebab form, such as `rel-1`, or `unmapped`. `severity` is `blocker`,
> `should-fix` or `nice`. Return `[]` if no actionable finding is supported. Additional
> explanation can follow the JSON. Preserve legitimate exceptions and do not force
> a finding on each file.

Change the explanatory style freely. If you change field names or risk labels,
normalize them before import and retain the untouched original with its mapping.
See [customization](../../docs/CUSTOMIZATION.md). The canonical schema does not
force every reviewer into the same prose or personal coding style.
