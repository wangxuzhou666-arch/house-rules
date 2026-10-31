"""Finding ids written before the tool was renamed keep the old repo slug in front: `review-ratchet:l1:r2:r2-6`.

The slug of a finding id is the repo's own (`default_slug`, or `repo_slug` in the config), so once the repo is called house-rules
the ledger holds ids with two prefixes side by side. Nothing may compare an id's prefix with the config's slug: the commands that
read ids must take the old ones as they take the new ones.
"""
import json

import support  # noqa: F401  (puts the repo root on sys.path)
from support import RepoCase, report_item, review_finding, review_round

from house_rules.config import load_config
from house_rules.decision_context import assess, build_context

OLD_SLUG = "review-ratchet"
NEW_SLUG = "house-rules"


def old_finding(display_id: str, **overrides: object) -> dict:
    """An open blind spot (no clause covers it) as a ledger of the old name holds it."""
    return review_finding(display_id, source="l1", run="r2", id=f"{OLD_SLUG}:l1:r2:{display_id}", repo=OLD_SLUG,
                          rule_code="unmapped", rule_id="unmapped", category="unmapped", file="src/a.py", **overrides)


class OldPrefixIdsTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.with_sample_rules(repo_slug=NEW_SLUG)
        (self.repo / "src").mkdir()
        (self.repo / "src/a.py").write_text("first\nsecond\nthird\n", encoding="utf-8")
        self.old, self.other = old_finding("r2-6", line=2), old_finding("r2-7", line=3)
        self.append_raw(review_round(run="r2", repo=OLD_SLUG), self.old, self.other)

    def outcomes(self) -> list[tuple[str, str]]:
        return [(event["id"], event["outcome"]) for event in self.events() if event["kind"] == "outcome"]

    def test_new_findings_get_the_new_prefix_and_count_beside_the_old_ones(self) -> None:
        self.add_findings([report_item(rule_id="unmapped")], run="R9")
        ids = [event["id"] for event in self.events() if event["kind"] == "finding"]
        self.assertEqual(ids[:2], [self.old["id"], self.other["id"]])
        self.assertTrue(ids[2].startswith(f"{NEW_SLUG}:l1:R9:"), ids[2])
        self.assertEqual(self.events()[-1]["repo"], NEW_SLUG)
        self.assertIn("3 total, 3 open", self.cli_ok("count"))

    def test_label_takes_an_old_id_in_full_and_by_its_display_id(self) -> None:
        self.cli_ok("label", self.old["id"], "--outcome", "false_positive")
        self.cli_ok("label", "r2-7", "--outcome", "dismissed")
        self.assertEqual(self.outcomes(), [(self.old["id"], "false_positive"), (self.other["id"], "dismissed")])
        self.assertIn("2 total, 0 open", self.cli_ok("count"))

    def test_wrapup_reports_an_old_id_as_a_blind_spot_until_it_is_labeled(self) -> None:
        report = json.loads(self.cli_ok("wrapup", "--report-only", "--json"))
        self.assertEqual(([item["id"] for item in report["new_unmapped"]], report["open_unmapped"]), ([self.old["id"], self.other["id"]], 2))
        self.cli_ok("label", self.old["id"], "--outcome", "false_positive")
        report = json.loads(self.cli_ok("wrapup", "--report-only", "--json"))
        self.assertEqual(([item["id"] for item in report["new_unmapped"]], report["open_unmapped"]), ([self.other["id"]], 1))

    def test_the_rule_maintenance_flow_reads_an_old_id(self) -> None:
        packet = json.loads(self.cli_ok("decision-context", "--finding", self.old["id"]))
        self.assertEqual([item["finding"]["id"] for item in packet["findings"]], [self.old["id"]])
        config = load_config(self.repo, None, None, self.repo)
        context = build_context(config, self.events())
        entries = [{"finding_ids": [finding["id"]], "verification": {"status": "uncertain", "basis": "No snapshot."}, "action": "keep",
                    "rule_ids": [], "reason": "Not enough evidence for a rule change.", "changes": []} for finding in (self.old, self.other)]
        result = assess(config, self.events(), context, {"v": 1, "context_id": context["context_id"], "assessments": entries})
        self.assertEqual([card["finding_ids"] for card in result["cards"]], [[self.old["id"]], [self.other["id"]]])
