# Contributing

house-rules keeps coding rules as versioned text with clause IDs, logs review findings
to a ledger, and turns findings into rule changes that a person approves. Take what is
useful for your own repo. If you want to give something back, this is what helps.

## What you can send

- **Synthetic failing examples.** A small, self-contained snippet that review should
  flag, or one where a rule fires when it should not. Say which clause it should hit,
  or that it should stay `unmapped`.
- **Rule proposals.** A clause in the shape [docs/RULE-WRITING.md](docs/RULE-WRITING.md)
  describes: stable ID, obligation, severity, scope, exception. Include the example that
  motivated it and say why the existing clauses do not already cover it.
- **Regression pairs.** A violating snippet that must be reported, and a compliant
  snippet that must stay clean. [samples/config-replacement](samples/config-replacement/README.md)
  is the model. The two sides should differ in the behavior that matters, not in
  variable names.
- **Generalization cases.** A violation file, a legitimate control (compliant, or covered by
  the rule's stated exemption) and an `oracle.py` that runs both under the failure the rule is
  about and exits 0 only if the violation breaks and the control does not, in the shape of
  [samples/generalization/cases/rel-1](samples/generalization/cases/rel-1/). Say which clause
  it tests. Standard library only.
- **Bug fixes with tests.** Show the failing test first, then the fix.
- **Docs.** Corrections, clearer wording, translations. `README.zh.md`,
  `docs/RULE-WRITING.zh.md` and `templates/CODING-STYLE.template.zh.md` mirror their English
  originals; if you change one side, say so in the PR so the other can follow. The Chinese
  template must keep the same clause IDs and severities as the English one.

## What not to send

By default, do not upload findings, ledger rows, review transcripts or code from a real
private repository, even with the names removed. Rewrite the mechanism as a synthetic
reproduction that stands on its own: new names, the smallest code that shows the failure,
nothing recognizable from the original project. The ledger (`.style-findings.jsonl`) and
`.house-rules/` are git-ignored on purpose; do not force-add them. No secrets, tokens or
machine-specific paths either. If you think a real case is worth including, open an
issue and describe it instead of attaching it.

## Rule changes need a person

A rule change is never merged automatically: not by a bot, not on a model's proposal,
not because the tests are green. Adding, rewording, retiring a clause or changing its
severity needs a human decision. In a PR, put the exact new text, the failure it prevents
and why existing clauses do not cover it, and expect the maintainer to decide. This is
the same contract the tool itself follows: the model proposes, a person approves
([docs/DECISION-CARDS.md](docs/DECISION-CARDS.md)). If you edit a rules file, run
`bin/house-rules compile` so the digest is not stale; `bin/house-rules compile --check` tells you.

A new **mechanical gate** (a lint rule, a syntax-tree check, a hook: anything that
blocks a commit or fails CI) needs more than the decision. R.7 in
[CODING-STYLE.md](CODING-STYLE.md) (it is also in the
[template](templates/CODING-STYLE.template.md)) says why: a gate that always returns green
also runs green, so green proves nothing. Send:

1. a failing example that proves the gate fires, and a compliant example that must stay
   clean, starting with the exceptions the rule states;
2. each failing example pinned to the specific rule that fired;
3. a mutation check: a deliberate bug that makes the gate wrong, added to
   [tests/mutation_check.py](tests/mutation_check.py) as a `Mutant(name, path, old, new)`
   where `old` occurs exactly once in the file, and a test that goes red because of it.

Reverting to the old behavior must turn the gate green again, so the red is caused by
your change. Tests and probes use synthetic fixtures in temporary directories; they must
not push, deploy or touch your real repository.

## Dev loop

Python 3.10 or newer, standard library only. There is nothing to install.

```sh
python3 -m unittest discover -s tests -q
python3 tests/mutation_check.py [--full] [--jobs N]
```

The unit tests are the everyday check. Run them before every PR.

The mutation self-test applies one deliberate bug at a time to a temporary copy of the
repo (your checkout is never touched) and fails if the suite stays green. **It has no
`--help` and does no argument parsing: any invocation starts a full run, and a full run
(hundreds of mutants, each running the suite) can take hours.** `--jobs N` runs N mutants
at once; `--full` runs the whole suite per mutant and lists every test that caught it. Run it when
you added or changed a gate, a parser or a check, not for a docs or typo change. It
first checks that every `Mutant` still matches exactly one place in its file and stops
with exit code 2 if not, so if you edited a line a mutant targets, update that mutant.

Code under `house_rules/` is reviewed against this repo's own [CODING-STYLE.md](CODING-STYLE.md),
which `CLAUDE.md` imports as a compiled digest. Skim it before a larger change.

## License

Contributions are accepted under the project's [MIT license](LICENSE).
