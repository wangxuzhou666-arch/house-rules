"""The `wrapup_auto` switch: the config key, what `ratchet init` asks and writes for it, and the setup skill that offers it.

Only the switch is covered here; what a `true` starts reads `Config.wrapup_auto` and is tested where it lives.
"""
import json
import re
import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
import test_init
import test_worktree
from support import REPO_ROOT, STYLE_RULES, RepoCase, git, make_config

from ratchet.cli import build_parser
from ratchet.config import DEFAULT_WRAPUP_AUTO, load_config
from ratchet.onboard import WRAPUP_AUTO_QUESTION

SETUP_SKILL = REPO_ROOT / "skills" / "setup" / "SKILL.md"
RULES_QUESTION = "Rules file:"
CLAUDE_MD_QUESTION = "to CLAUDE.md?"
HOOKS_QUESTION = "Install git hooks"


class ConfigKeyTest(RepoCase):
    def wrapup_auto(self) -> bool:
        return load_config(self.repo, None, None, self.repo).wrapup_auto

    def test_it_is_off_when_the_config_does_not_say(self) -> None:
        self.assertIs(self.wrapup_auto(), False)  # no config file at all
        self.write_config(rules_files=["rules.md"])
        self.assertIs(self.wrapup_auto(), False)
        self.assertIs(DEFAULT_WRAPUP_AUTO, False)
        self.assertIs(make_config().wrapup_auto, False)

    def test_true_and_false_are_read_as_they_are(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                self.write_config(wrapup_auto=value)
                self.assertIs(self.wrapup_auto(), value)

    def test_it_is_a_known_key_and_a_misspelling_of_it_is_not(self) -> None:
        self.write_config(wrapup_auto=True)
        self.assertEqual(self.cli("count")[0], 0)
        self.write_config(wrapup_automatic=True)
        code, _, err = self.cli("count")
        self.assertEqual(code, 1)
        self.assertIn("unknown config key", err)

    def test_only_true_or_false_is_accepted(self) -> None:
        for bad in (0, 1, "yes", "true", None, 1.0, [True], {}):
            with self.subTest(value=bad):
                self.write_config(wrapup_auto=bad)
                code, _, err = self.cli("count")
                self.assertEqual(code, 1)
                self.assertIn("config 'wrapup_auto' must be true or false", err)


class InitWritesTheKeyTest(test_init.InitCase):
    """A repo with no config yet: what init asks and what it writes."""

    def switch(self) -> object:
        return json.loads(self.text(".ratchet.json"))["wrapup_auto"]

    def test_yes_writes_true_after_rules_files_with_the_indentation_init_always_uses(self) -> None:
        out = self.init("--yes")
        self.assertEqual(self.text(".ratchet.json"), '{\n  "rules_files": [\n    "CODING-STYLE.md"\n  ],\n  "wrapup_auto": true\n}\n')
        self.assertEqual(self.statuses(out)["config"], "added")
        self.assertIn(".ratchet.json: rules_files ['CODING-STYLE.md'], wrapup_auto true", out)

    def test_it_goes_after_every_key_the_config_already_has(self) -> None:
        self.write_config(repo_slug="mine", label_page_threshold=5)
        self.init("--yes")
        self.assertEqual(list(json.loads(self.text(".ratchet.json"))), ["repo_slug", "label_page_threshold", "rules_files", "wrapup_auto"])

    def test_no_wrapup_auto_writes_false_and_asks_nothing(self) -> None:
        code, out, err = self.ask("\n\n\n", "--no-wrapup-auto")  # the rules file, CLAUDE.md and the hooks: three answers are enough
        self.assertEqual(code, 0, err)
        self.assertNotIn(WRAPUP_AUTO_QUESTION, out)
        self.assertIs(self.switch(), False)
        self.assertIn("wrapup_auto false", out)

    def test_no_wrapup_auto_with_yes_writes_false(self) -> None:
        self.init("--yes", "--no-wrapup-auto")
        self.assertIs(self.switch(), False)

    def test_y_turns_it_on(self) -> None:
        code, _, err = self.ask("\ny\n\n\n")
        self.assertEqual(code, 0, err)
        self.assertIs(self.switch(), True)

    def test_n_turns_it_off_and_init_goes_on_to_the_next_question(self) -> None:
        code, out, err = self.ask("\nn\n\n\n")
        self.assertEqual(code, 0, err)
        self.assertIs(self.switch(), False)
        self.assertEqual(self.statuses(out)["CLAUDE.md"], "added")
        self.assertTrue(self.hook("pre-commit").is_file())

    def test_enter_takes_the_default_which_is_on(self) -> None:
        code, _, err = self.ask("\n\n\n\n")
        self.assertEqual(code, 0, err)
        self.assertIs(self.switch(), True)

    def test_the_question_comes_after_the_rules_one_and_before_the_claude_md_and_hooks_ones(self) -> None:
        _, out, _ = self.ask("\n\n\n\n")
        positions = [out.index(text) for text in (RULES_QUESTION, WRAPUP_AUTO_QUESTION, CLAUDE_MD_QUESTION, HOOKS_QUESTION)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(f"{WRAPUP_AUTO_QUESTION} [Y/n] ", out)
        self.assertIn("costs one /code-review", out)

    def test_with_the_rules_file_given_it_is_the_first_question(self) -> None:
        _, out, _ = self.ask("\n\n\n", "--template")
        self.assertNotIn(RULES_QUESTION, out)
        self.assertLess(out.index(WRAPUP_AUTO_QUESTION), out.index(CLAUDE_MD_QUESTION))
        self.assertIs(self.switch(), True)


class ConfigThatExistsTest(test_init.InitCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_rules(STYLE_RULES)

    def put_config(self, text: str) -> None:
        (self.repo / ".ratchet.json").write_text(text, encoding="utf-8")

    def test_a_config_that_has_the_key_is_left_byte_for_byte_whichever_the_value(self) -> None:
        for value in ("true", "false"):
            with self.subTest(value=value):
                config = f'{{"rules_files":["rules.md"],   "wrapup_auto": {value}}}'
                self.put_config(config)
                out = self.init("--yes")
                self.assertEqual(self.text(".ratchet.json"), config)
                self.assertEqual(self.statuses(out)["config"], "already ok")

    def test_init_only_fills_in_a_missing_key_so_the_flag_does_not_overwrite_one_that_is_there(self) -> None:
        config = '{"rules_files":["rules.md"],"wrapup_auto":true}'
        self.put_config(config)
        self.init("--yes", "--no-wrapup-auto")
        self.assertEqual(self.text(".ratchet.json"), config)

    def test_a_config_that_has_the_key_asks_nothing_about_it(self) -> None:
        self.put_config('{"rules_files": ["rules.md"], "wrapup_auto": false}')
        self.init("--yes")  # everything else is in place, so no question is left to ask
        code, out, err = self.ask("")
        self.assertEqual(code, 0, err)
        self.assertNotIn(WRAPUP_AUTO_QUESTION, out)

    def test_a_config_without_it_gets_the_key_last_and_its_other_keys_are_kept(self) -> None:
        self.put_config('{"rules_files":["rules.md"],   "repo_slug": "mine", "label_page_threshold": 5}')
        out = self.init("--yes")
        kept = {"rules_files": ["rules.md"], "repo_slug": "mine", "label_page_threshold": 5}
        self.assertEqual(self.text(".ratchet.json"), json.dumps({**kept, "wrapup_auto": True}, indent=2) + "\n")
        self.assertEqual(self.statuses(out)["config"], "updated")
        self.assertIn(".ratchet.json: wrapup_auto true", out)
        self.assertEqual(self.statuses(self.init("--yes"))["config"], "already ok")

    def test_the_commit_init_asks_for_carries_the_config_it_changed(self) -> None:
        self.put_config('{"rules_files": ["rules.md"]}')
        git(self.repo, "add", ".ratchet.json", "rules.md")
        self.commit("adopt the old way")
        out = self.init("--yes")
        (staged,) = [line for line in out.splitlines() if line.strip().startswith("git add ")]
        self.assertIn(" .ratchet.json", staged.split(" && ")[0])

    def test_a_config_without_it_is_asked_at_a_terminal_and_the_rules_question_is_not(self) -> None:
        self.put_config('{"rules_files": ["rules.md"]}')
        code, out, err = self.ask("n\n\n\n")  # the wrap-up, CLAUDE.md and the hooks
        self.assertEqual(code, 0, err)
        self.assertNotIn(RULES_QUESTION, out)
        self.assertIs(json.loads(self.text(".ratchet.json"))["wrapup_auto"], False)

    def test_a_config_without_it_gets_false_from_the_flag_without_a_question(self) -> None:
        self.put_config('{"rules_files": ["rules.md"]}')
        code, out, err = self.ask("\n\n", "--no-wrapup-auto")  # CLAUDE.md and the hooks
        self.assertEqual(code, 0, err)
        self.assertNotIn(WRAPUP_AUTO_QUESTION, out)
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"rules_files": ["rules.md"], "wrapup_auto": False})

    def test_a_config_with_the_key_and_no_rules_files_is_asked_for_the_rules_only(self) -> None:
        self.put_config('{"wrapup_auto": false}')
        code, out, err = self.ask(f"{self.repo / 'rules.md'}\n\n\n")  # the rules file, CLAUDE.md and the hooks
        self.assertEqual(code, 0, err)
        self.assertNotIn(WRAPUP_AUTO_QUESTION, out)
        self.assertEqual(json.loads(self.text(".ratchet.json")), {"wrapup_auto": False, "rules_files": ["rules.md"]})


class InitDryRunTest(test_init.InitCase):
    def test_a_fresh_repo_shows_the_choice_and_writes_nothing(self) -> None:
        before = self.files()
        out = self.init("--dry-run")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.statuses(out)["config"], "would add")
        self.assertIn(".ratchet.json: rules_files ['CODING-STYLE.md'], wrapup_auto true", out)
        self.assertIn('+  "wrapup_auto": true', out)

    def test_no_wrapup_auto_shows_false(self) -> None:
        out = self.init("--dry-run", "--no-wrapup-auto")
        self.assertIn("wrapup_auto false", out)
        self.assertIn('+  "wrapup_auto": false', out)
        self.assertNotIn("wrapup_auto true", out)

    def test_an_old_config_shows_the_key_it_would_add_and_stays_as_it_is(self) -> None:
        self.write_rules(STYLE_RULES)
        self.write_config(rules_files=["rules.md"])
        before = self.files()
        out = self.init("--dry-run")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.statuses(out)["config"], "would update")
        self.assertIn(".ratchet.json: wrapup_auto true", out)
        self.assertIn('+  "wrapup_auto": true', out)

    def test_a_config_with_the_key_shows_no_change(self) -> None:
        self.init("--yes")
        out = self.init("--dry-run")
        self.assertEqual(self.statuses(out)["config"], "already ok")
        self.assertNotIn("wrapup_auto", out)


class LinkedWorktreeTest(test_init.InitCase, test_worktree.LinkedCase):
    """The config is shared by every worktree and init writes it from the main one only, so a linked one gets a hint."""

    def hints(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if "wrapup_auto is not set in" in line]

    def test_no_config_is_written_and_one_line_says_how_to_turn_it_on(self) -> None:
        before = self.main_text(".ratchet.json")
        out = self.ok_in_tree("init", "--yes")
        self.assertEqual(self.main_text(".ratchet.json"), before)
        self.assertNotIn("wrapup_auto", before)
        self.assertEqual(self.statuses(out)["config"], "already ok")
        (hint,) = self.hints(out)
        self.assertIn(str(self.repo / ".ratchet.json"), hint)
        self.assertIn("run `ratchet init` there", hint)
        self.assertIn('"wrapup_auto": true', hint)

    def test_a_dry_run_gives_the_same_hint_and_writes_nothing(self) -> None:
        before = self.main_text(".ratchet.json")
        out = self.ok_in_tree("init", "--dry-run")
        self.assertEqual(len(self.hints(out)), 1)
        self.assertEqual(self.main_text(".ratchet.json"), before)

    def test_no_wrapup_auto_gives_no_hint_and_still_writes_nothing(self) -> None:
        before = self.main_text(".ratchet.json")
        out = self.ok_in_tree("init", "--yes", "--no-wrapup-auto")
        self.assertEqual(self.hints(out), [])
        self.assertEqual(self.main_text(".ratchet.json"), before)

    def test_a_config_that_has_the_key_gives_no_hint(self) -> None:
        self.write_config(rules_files=["rules.md"], inject_targets=[test_worktree.REVIEWER], wrapup_auto=False)
        self.assertEqual(self.hints(self.ok_in_tree("init", "--yes")), [])

    def test_at_a_terminal_it_asks_nothing_about_it(self) -> None:
        code, out, err = self.ask("\n\n", "--repo", str(self.tree))  # CLAUDE.md and the hooks; a third question would run out of answers
        self.assertEqual(code, 0, err)
        self.assertNotIn(WRAPUP_AUTO_QUESTION, out)
        self.assertEqual(len(self.hints(out)), 1)

    def test_it_reads_the_switch_the_main_worktree_holds(self) -> None:
        self.assertIs(load_config(self.tree, None, None, self.tree).wrapup_auto, False)
        self.write_config(rules_files=["rules.md"], inject_targets=[test_worktree.REVIEWER], wrapup_auto=True)
        self.assertIs(load_config(self.tree, None, None, self.tree).wrapup_auto, True)


class SetupSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SETUP_SKILL.read_text(encoding="utf-8")

    def choices(self) -> list[str]:
        """The bullets of step 2, the things the skill settles with the user before it runs anything."""
        step = re.search(r"^2\. .*?(?=^3\. )", self.text, flags=re.DOTALL | re.MULTILINE)
        assert step, "the setup skill has lost its step 2"
        return re.findall(r"^   - .*$", step.group(0), flags=re.MULTILINE)

    def test_it_settles_three_choices_and_the_third_is_the_automatic_wrap_up(self) -> None:
        self.assertIn("Settle three choices", self.text)
        rules, hooks, wrapup = self.choices()
        self.assertTrue(rules.startswith("   - Rules:"))
        self.assertTrue(hooks.startswith("   - Git hooks:"))
        self.assertTrue(wrapup.startswith("   - Automatic wrap-up:"))

    def test_the_wrap_up_choice_names_what_it_costs_and_the_flag_that_leaves_it_off(self) -> None:
        wrapup = self.choices()[2]
        self.assertIn("after commits made in a Claude Code session", wrapup)
        self.assertIn("costs one `/code-review`", wrapup)
        self.assertIn("`--yes` turns it on", wrapup)
        self.assertIn("add `--no-wrapup-auto`", wrapup)

    def test_the_choice_is_settled_before_the_preview_and_the_real_run(self) -> None:
        self.assertLess(self.text.index("Automatic wrap-up:"), self.text.index("ratchet init --dry-run"))
        self.assertLess(self.text.index("ratchet init --dry-run"), self.text.index("ratchet init --yes"))

    def test_the_flag_it_names_is_one_init_takes_and_is_off_unless_given(self) -> None:
        parser = build_parser()
        self.assertIs(parser.parse_args(["init", "--no-wrapup-auto"]).no_wrapup_auto, True)
        self.assertIs(parser.parse_args(["init"]).no_wrapup_auto, False)


if __name__ == "__main__":
    unittest.main()
