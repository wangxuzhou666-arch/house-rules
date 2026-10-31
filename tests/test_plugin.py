import json
import os
import re
import shlex
import unittest
from pathlib import Path

import support  # noqa: F401  (puts the repo root on sys.path)
from support import REPO_ROOT

PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
AUTO_STOP = REPO_ROOT / "house_rules" / "auto_stop.py"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
OWNER = "wangxuzhou666-arch"
REPOSITORY = f"https://github.com/{OWNER}/house-rules"
SKILLS = REPO_ROOT / "skills"
DESCRIPTION_LIMIT = 1536  # Claude Code truncates description + when_to_use at this length in the skill listing


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match, "SKILL.md must start with a frontmatter block"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))

    def test_name_and_version(self) -> None:
        self.assertEqual(self.manifest["name"], "house-rules")
        self.assertEqual(self.manifest["version"], "0.1.0")

    def test_it_carries_a_description_and_an_author_without_an_email(self) -> None:
        self.assertTrue(self.manifest["description"].strip())
        self.assertTrue(self.manifest["author"]["name"].strip())
        self.assertNotIn("email", self.manifest["author"])
        self.assertNotIn("@", PLUGIN_JSON.read_text(encoding="utf-8"))

    def test_it_names_the_author_the_license_the_repository_and_keywords(self) -> None:
        self.assertEqual(self.manifest["author"], {"name": OWNER})
        self.assertEqual(self.manifest["license"], "MIT")
        self.assertEqual(self.manifest["repository"], REPOSITORY)
        self.assertTrue(self.manifest["keywords"])
        self.assertTrue(all(isinstance(word, str) and word for word in self.manifest["keywords"]))

    def test_only_the_manifest_and_the_marketplace_live_in_the_plugin_directory(self) -> None:
        self.assertEqual(sorted(p.name for p in PLUGIN_JSON.parent.iterdir()), ["marketplace.json", "plugin.json"])

    def test_bin_house_rules_is_executable_because_the_plugin_puts_bin_on_the_path(self) -> None:
        self.assertTrue(os.access(REPO_ROOT / "bin" / "house-rules", os.X_OK))


class MarketplaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
        self.manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))

    def test_it_is_a_marketplace_named_house_rules_with_an_owner_and_no_email(self) -> None:
        self.assertEqual(self.marketplace["name"], "house-rules")
        self.assertEqual(self.marketplace["owner"], {"name": OWNER})
        self.assertTrue(self.marketplace["description"].strip())
        self.assertNotIn("@", MARKETPLACE_JSON.read_text(encoding="utf-8"))

    def test_it_lists_this_repository_as_its_one_plugin_at_the_root(self) -> None:
        (entry,) = self.marketplace["plugins"]
        self.assertEqual(entry["source"], "./")
        self.assertEqual(entry["name"], self.manifest["name"])  # the install id is <entry name>@<marketplace name>
        self.assertTrue(entry["description"].strip())

    def test_the_keys_are_ones_claude_code_reads(self) -> None:
        self.assertLessEqual(set(self.marketplace), {"name", "description", "owner", "plugins"})
        self.assertLessEqual(set(self.marketplace["plugins"][0]), {"name", "source", "description"})


class HooksTest(unittest.TestCase):
    """hooks/hooks.json is loaded by Claude Code from the plugin root on its own; the manifest does not name it."""

    def setUp(self) -> None:
        self.config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        (group,) = self.config["hooks"]["Stop"]
        (self.hook,) = group["hooks"]
        self.group = group

    def test_only_the_stop_event_is_registered(self) -> None:
        self.assertEqual(list(self.config), ["hooks"])
        self.assertEqual(list(self.config["hooks"]), ["Stop"])

    def test_it_has_no_matcher_because_a_stop_hook_takes_none_and_no_condition(self) -> None:
        self.assertEqual(list(self.group), ["hooks"])
        self.assertEqual(set(self.hook), {"type", "command", "timeout"})

    def test_it_is_a_command_with_a_timeout_of_30_seconds(self) -> None:
        self.assertEqual((self.hook["type"], self.hook["timeout"]), ("command", 30))

    def test_the_command_runs_the_plugins_executable_run_sh_with_auto_stop_and_quotes_the_plugin_path(self) -> None:
        command = self.hook["command"]
        self.assertTrue(command.startswith('"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" '), command)  # a shell reads it, and a path may hold a space
        script, *arguments = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT)))
        self.assertEqual((Path(script), arguments), (REPO_ROOT / "hooks" / "run.sh", ["auto", "stop"]))
        self.assertTrue(os.access(script, os.X_OK))

    def test_run_sh_is_a_posix_shell_script(self) -> None:
        self.assertEqual((REPO_ROOT / "hooks" / "run.sh").read_text(encoding="utf-8").splitlines()[0], "#!/bin/sh")

    def test_the_subcommand_it_runs_exists(self) -> None:
        from house_rules.cli import build_parser
        args = build_parser().parse_args(["auto", "stop"])
        self.assertTrue(args.standalone)

    def test_the_manifest_does_not_name_the_hooks_file_which_would_load_it_twice(self) -> None:
        self.assertNotIn("hooks", json.loads(PLUGIN_JSON.read_text(encoding="utf-8")))

    def test_the_skill_the_hook_asks_for_is_one_the_plugin_has_under_the_name_the_hook_gives(self) -> None:
        plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["name"]
        self.assertIn(f"/{plugin}:wrapup auto ", AUTO_STOP.read_text(encoding="utf-8"))
        self.assertIn(f"Skill tool: {plugin}:wrapup, args: auto ", AUTO_STOP.read_text(encoding="utf-8"))
        self.assertEqual(frontmatter((SKILLS / "wrapup" / "SKILL.md").read_text(encoding="utf-8"))["name"], "wrapup")


class DistributionTest(unittest.TestCase):
    def test_the_license_is_mit_with_the_owners_copyright(self) -> None:
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn(f"Copyright (c) 2026 {OWNER}\n", text)
        self.assertIn("Permission is hereby granted, free of charge", text)

    def test_the_readme_gives_the_three_ways_to_install(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for line in (f"claude plugin marketplace add {OWNER}/house-rules", "claude plugin install house-rules@house-rules",
                     "claude --plugin-dir /path/to/house-rules", "/path/to/house-rules/bin/house-rules init"):
            self.assertIn(line, readme)


class SkillTest(unittest.TestCase):
    def skills(self) -> dict[str, str]:
        return {path.parent.name: path.read_text(encoding="utf-8") for path in sorted(SKILLS.glob("*/SKILL.md"))}

    def test_the_plugin_exposes_setup_review_wrapup_and_rule_maintenance(self) -> None:
        self.assertEqual(set(self.skills()), {"setup", "review", "wrapup", "maintain"})

    def test_each_has_a_precise_description_that_says_when_to_use_it(self) -> None:
        for name, text in self.skills().items():
            with self.subTest(skill=name):
                description = frontmatter(text)["description"]
                self.assertIn("Use when", description)
                self.assertLessEqual(len(description), DESCRIPTION_LIMIT)
                self.assertIn("house-rules", description)

    def test_each_is_short(self) -> None:
        for name, text in self.skills().items():
            with self.subTest(skill=name):
                self.assertLess(len(text.splitlines()), 40)

    def test_each_can_run_the_tool_without_asking_and_names_the_fallback_path(self) -> None:
        for name, text in self.skills().items():
            with self.subTest(skill=name):
                self.assertIn("Bash(house-rules *)", frontmatter(text)["allowed-tools"])
                self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/house-rules", text)

    def test_setup_uses_yes_because_a_tool_call_has_no_terminal(self) -> None:
        text = self.skills()["setup"]
        self.assertIn("house-rules init --dry-run", text)
        self.assertIn("house-rules init --yes", text)

    def test_review_runs_review_and_keeps_the_label_page_only_as_a_fallback(self) -> None:
        text = self.skills()["review"]
        self.assertIn("house-rules review --level", text)
        self.assertIn("$ARGUMENTS", text)
        self.assertIn("label-page", text)
        self.assertIn("fallback", text)
        self.assertIn("Do not offer it by default", text)

    def test_review_ends_in_the_wrapup_question_and_does_not_define_it_again(self) -> None:
        text = self.skills()["review"]
        self.assertIn("house-rules wrapup --json --report-only", text)
        self.assertIn("(../wrapup/SKILL.md)", text)
        self.assertNotIn("AskUserQuestion call", text)  # the question is written once, in the wrapup skill
        self.assertNotIn("multiSelect", text)
        self.assertIn("AskUserQuestion", frontmatter(text)["allowed-tools"])  # the user's review-to-question path must not need a prompt

    def test_wrapup_measures_first_and_asks_at_most_one_two_part_question(self) -> None:
        text = self.skills()["wrapup"]
        self.assertIn("house-rules wrapup --json", text)
        for status in ("skipped", "capped", "daily_capped", "reviewed"):
            self.assertIn(f"`{status}`", text)
        self.assertEqual(text.count("AskUserQuestion call"), 1)
        self.assertIn("ONE AskUserQuestion call with two questions", text)
        self.assertIn("multiSelect", text)
        self.assertIn("4 most severe at most", text)
        self.assertIn("`Now (Recommended)`", text)
        self.assertIn("(Recommended)", text)

    def test_wrapup_takes_auto_worktree_and_runs_the_review_in_the_background(self) -> None:
        text = self.skills()["wrapup"]
        self.assertIn("auto <worktree>", frontmatter(text)["argument-hint"])
        self.assertIn("auto <worktree>", frontmatter(text)["description"])
        self.assertIn("house-rules wrapup --repo <worktree> --json --auto", text)  # the review is marked automatic: the daily cap counts it
        self.assertIn("`run_in_background`", text)
        self.assertIn("completion notice", text)
        self.assertIn("one sentence and no question", text)

    def test_wrapup_says_a_daily_cap_in_one_sentence_and_asks_nothing(self) -> None:
        text = self.skills()["wrapup"]
        [bullet] = [line for line in text.splitlines() if line.lstrip().startswith("- `daily_capped`")]
        self.assertIn("exit code 4", bullet)
        self.assertIn("One sentence", bullet)
        self.assertIn("No question", bullet)
        self.assertIn("`/house-rules:wrapup` without `auto`", bullet)  # the way to review the work now, which the cap does not touch
        automatic = next(line for line in text.splitlines() if line.startswith("**Automatic mode**"))
        self.assertIn("`skipped`, `capped`, `daily_capped` and an error get one sentence and no question", automatic)
        typed = next(line for line in text.splitlines() if line.startswith("1. "))  # step 1: what a wrap-up someone typed runs
        self.assertIn("house-rules wrapup --json", typed)
        self.assertNotIn("--auto", typed)  # it is no automatic one: the cap neither counts it nor refuses it

    def test_wrapup_records_only_false_positive_with_one_direct_label_command(self) -> None:
        text = self.skills()["wrapup"]
        self.assertIn('house-rules label <display_id>... --outcome false_positive --note "wrap-up: not a real problem"', text)
        self.assertIn("Use no other outcome", text)
        self.assertNotIn("--input FILE", text)  # the default path opens no batch and writes no scratch file

    def test_wrapup_keeps_the_label_page_and_apply_labels_as_the_batch_fallback(self) -> None:
        text = self.skills()["wrapup"]
        for command in ("house-rules label-page", "house-rules apply-labels", "house-rules distill --drafts"):
            self.assertIn(command, text)
        self.assertIn("the batch route", text)
        self.assertIn("fallbacks, not the default path", text)

    def test_wrapup_never_lifts_the_round_cap_by_itself_and_hands_rules_to_maintain(self) -> None:
        text = self.skills()["wrapup"]
        self.assertIn("Do not run `house-rules review --over-cap` on your own", text)
        self.assertIn("(../maintain/SKILL.md)", text)
        for trigger in ("wrap up", "收口", "/ship"):
            self.assertIn(trigger, frontmatter(text)["description"])

    def test_review_keeps_its_original_triggers(self) -> None:
        description = frontmatter(self.skills()["review"])["description"]
        for trigger in ("review changes with house-rules", "log or track review findings", '"house-rules review"'):
            self.assertIn(trigger, description)

    def test_review_states_the_round_cap_and_never_lets_the_agent_lift_it(self) -> None:
        text = self.skills()["review"]
        self.assertIn("at most 3 review rounds", text)
        self.assertIn("blockers only", text)
        self.assertIn("Do not pass `--over-cap` on your own", text)
        self.assertIn("ask", text)

    def test_every_house_rules_subcommand_a_skill_mentions_exists(self) -> None:
        from house_rules.cli import build_parser
        known = set(build_parser()._subparsers._group_actions[0].choices)
        for name, text in self.skills().items():
            for command in re.findall(r"`house-rules ([a-z][a-z-]*)", text):
                with self.subTest(skill=name, command=command):
                    self.assertIn(command, known)


if __name__ == "__main__":
    unittest.main()
