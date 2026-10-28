# Historical finding reconciliation — 2026-10-28

Scope: the ten `r1` findings and six `r2` findings in review-ratchet's local ledger.
The conclusions below describe the implementation verified for this local closeout,
not an already published release. Original findings and their rule attribution are preserved. Rule adoption
and code-defect closure are different events.

Each short ID below expands to `review-ratchet:l1:RUN:SHORT_ID`. Historical line
numbers may have moved; code symbols and regression tests are the current anchors.
Focused verification passed 42 test executions, plus an AST check for `r1-10`.
The `r2-6` tests establish current behavior, not removal of its deferred cleanup.

| Finding | Conclusion | Current implementation and verification |
|---|---|---|
| r1-1 | Fixed in working tree | `cmd_review` recognizes file replay before refusing a new round. `test_review.RoundCapTest.test_a_replayed_run_is_not_refused_at_the_cap` checks replay at 3/3 and unchanged ledger bytes. |
| r1-2 | Fixed in working tree | `Rules.is_reviewer_rule` excludes guidance from category/prose attribution. `test_ingest.IngestTest.test_a_reviewer_rule_cited_in_the_text_is_skipped_for_the_violation_cited_after_it` retains the actual code rule. |
| r1-3 | Valid alias defect fixed; cwd interpretation not adopted | `repo_relative` resolves absolute aliases before checking the repository boundary. Relative paths still follow the documented repo-root contract. `test_gate.IdCompatibilityTest.test_absolute_repo_alias_matches_canonical_path_without_changing_relative_semantics` covers both. |
| r1-4 | Fixed in working tree | `vendor_into` prepares and validates before replacement, restoring old entries on activation error. `test_vendor.RefreshTest` injects copy, validation, marker, activation and restoration failures. Power-loss recovery remains outside the guarantee. |
| r1-5 | Fixed in working tree | `select_new_events` recognizes the original three-digit ordinal when today's importer replays it, preserving the old ID/outcome and rejecting conflicting content. `test_ledger.FoldTest.test_legacy_padded_review_replay_keeps_original_id_and_outcome` and the adjacent conflict test cover this. No historical events are rewritten. |
| r1-6 | Fixed in working tree | `cmd_count` validates flags before printing; `_review_rounds_line` reports unavailable configuration without failing the count. Tests: `test_cli.CountByRuleTest.test_label_source_without_by_rule_is_refused_before_anything_is_printed` and `test_review.RoundCountLineTest.test_a_default_branch_that_is_not_a_branch_does_not_fail_count`. |
| r1-7 | Fixed in working tree | Detached HEAD prints an explicit n/a reason. `test_review.RoundCountLineTest.test_a_detached_head_says_the_rounds_do_not_apply_and_is_no_error`. |
| r1-8 | Fixed in working tree | The hook probe answers at the start of the installed shell block, before user hook code. `test_init.HookProbeTest` verifies that user code is not executed and the real index is not touched. This does not claim arbitrary hooks are sandboxed. |
| r1-9 | Fixed in working tree | `gate_events` caches file lines within one call. `test_gate.IdCompatibilityTest.test_multiple_findings_read_each_file_once_per_gate_call` verifies one read per file for multiple reports. No process-global stale cache is introduced. |
| r1-10 | Fixed in working tree | The cited assignments are now `ingest_result` and `init_result`. AST inspection of `_ingest_review_text` and `cmd_init` confirms neither assigns the generic name `result`; no redundant syntax-only unit test was added. |
| r2-1 | Fixed in working tree | A recorded ID without `--from-file` is rejected before calling the reviewer, including with `--over-cap` or `--dry-run`. The two `test_review.RoundCapTest.test_a_recorded_run_id_is_refused_without_from_file...` tests assert the reviewer was not invoked. |
| r2-2 | Fixed in working tree | In a corpus with no severity data, valid clause categories are retained. `test_ingest.IngestWithoutSeveritiesTest.test_a_category_naming_a_clause_is_the_models_answer`. |
| r2-3 | Fixed in working tree | The same no-severity convention applies to prose attribution. `test_ingest.IngestWithoutSeveritiesTest.test_a_clause_cited_in_the_text_is_attributed` and `test_rules.LoadRulesTest.test_rules_where_no_clause_has_a_severity_have_no_reviewer_rules`. |
| r2-4 | Fixed in working tree | Hook internals use private temporary variables and unset them; documented `RATCHET_PY`/`RATCHET_BIN` values survive. `test_init.PortableBlockRuntimeTest` exercises the runtime branches and pinned/portable preservation checks. |
| r2-5 | Fixed in working tree | `place_block` requires a shebang; the unreachable fallback was removed. `test_init.PlaceBlockTest.test_a_script_without_a_shebang_is_a_programmer_error_because_plan_hook_never_passes_one` and `test_init.HooksTest.test_a_hook_without_a_shebang_is_not_touched`. |
| r2-6 | Nonblocking cleanup deferred | A real reviewer call with a reused ID is already refused; file replay at the cap correctly says it is not a new round and writes no event. `cmd_review` still constructs an unused `review_run_event` on the replay path. Move event construction inside the non-replay branch in a later cleanup; retain the no-new-event tests. Do not claim this redundancy has been removed. |

Code: [CLI](../ratchet/cli.py), [rules](../ratchet/rules.py),
[ingest](../ratchet/ingest.py), [ledger](../ratchet/ledger.py),
[hooks](../ratchet/hooks.py), [gate](../ratchet/gate.py), [vendor](../ratchet/vendor.py).

Tests can be rerun from the checkout with:

```sh
python3 -m unittest discover -s tests
```

## Ledger disposition

After inspecting this report, the author confirmed through a native question that
the 15 verified fixes should be closed and `r2-6` retained. On 2026-10-28, the tool
appended 15 `human/fixed` outcomes with references to this report and verification.
The ledger now has 16 findings, one open, and 33 total events. Original findings and
rule attributions are unchanged. The closures were recorded against the verified
working tree before the local closeout commit; this report accompanies that commit.
Nothing has been pushed. `r2-6` remains open as nonblocking cleanup.
