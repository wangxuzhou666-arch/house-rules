import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import support  # noqa: F401  (puts the repo root on sys.path)
from support import FIXTURES, REPO_ROOT, SEVERITY_TABLE, RepoCase

from ratchet import api
from ratchet.errors import RatchetError
from ratchet.gate import finding_hash, repo_relative, rule_code_of

GATE_RULES = ("1. **[P0E.4]** A bare lint suppression on a broad catch is not accepted.\n"
              "2. **[P1.1]** Nesting depth of five or more is a violation.\n"
              "3. **[P2.3]** No config layer for a one-off need.\n"
              "4. **[R.1]** Standards come from this file.\n\n" + SEVERITY_TABLE)

# Sources behind tests/fixtures/downstream_gate_ledger.jsonl, an open-findings ledger written the way an existing gate
# writes one (its hash recipe reproduced by hand, not by this package). The ids in it must come out of `gate record` unchanged.
SOURCES = {
    "src/a.py": "import os\nimport os\n\n\ndef load(path):\n    try:\n        return open(path).read()\n    except Exception:  # noqa: BLE001\n        return None\n",
    "src/b.py": "def broken(:\n    pass\n",
}
REPORTS = [
    {"file": "src/a.py", "line": 2, "code": "ruff:F811", "message": "redefinition of unused `os` from line 1"},
    {"file": "src/a.py", "line": 8, "code": "gate:NOQA", "message": "bare noqa on a broad catch"},
    {"file": "src/b.py", "line": 0, "code": "gate:SYNTAX", "message": "cannot parse the file"},
]
FIXTURE_IDS = ["demo-repo:gate:73491140740a", "demo-repo:gate:b1fee62becba", "demo-repo:gate:33f7e493304c"]


def downstream_hash(rel: str, code: str, text: str) -> str:
    """The recipe an existing gate uses for the id of a gate finding, written out independently of ratchet.gate."""
    return hashlib.sha1(f"{rel}|{code}|{''.join(text.split())}".encode()).hexdigest()[:12]


class GateCase(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_rules(GATE_RULES)
        self.write_config(rules_files=["rules.md"], tool_rule_map={"gate:NOQA": "P0E.4"})
        for rel, text in SOURCES.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def record(self, reports: object, *argv: str) -> str:
        return self.cli_ok("gate", "record", *argv, stdin=json.dumps(reports))

    def finding_events(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "finding"]


class HashTest(unittest.TestCase):
    def test_the_recipe_is_pinned_by_a_known_value(self) -> None:
        self.assertEqual(finding_hash("src/a.py", "F811", "import os"), "73491140740a")

    def test_it_matches_the_downstream_recipe_for_awkward_text(self) -> None:
        for text in ("", "x = 1", "\tif  a   and b:\n", "名前 = 'ü'", "a\x0cb", "  # noqa: BLE001  "):
            with self.subTest(text=text):
                self.assertEqual(finding_hash("src/é.py", "BLE001", text), downstream_hash("src/é.py", "BLE001", text))

    def test_whitespace_does_not_change_the_id_but_file_code_and_text_do(self) -> None:
        base = finding_hash("a.py", "F811", "import   os")
        self.assertEqual(base, finding_hash("a.py", "F811", "\timport os  "))
        self.assertEqual(len({base, finding_hash("b.py", "F811", "import os"), finding_hash("a.py", "F401", "import os"),
                              finding_hash("a.py", "F811", "import sys")}), 4)

    def test_a_tool_prefix_is_not_part_of_the_hashed_code(self) -> None:
        self.assertEqual(rule_code_of("ruff:F811"), "F811")
        self.assertEqual(rule_code_of("gate:SYNTAX"), "SYNTAX")
        self.assertEqual(rule_code_of("P1.1"), "P1.1")


class IdCompatibilityTest(GateCase):
    def test_reports_for_findings_an_existing_gate_already_holds_open_add_nothing(self) -> None:
        self.ledger.write_bytes((FIXTURES / "downstream_gate_ledger.jsonl").read_bytes())
        before = self.ledger_bytes()
        out = self.record(REPORTS)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertIn("+0 finding(s), 3 already open", out)
        for finding_id in FIXTURE_IDS:
            self.assertIn(f"{finding_id}  ", out)
        self.assertEqual(out.count("already open"), 4)  # three lines and the summary

    def test_the_ids_are_those_of_the_fixture_even_without_an_existing_ledger(self) -> None:
        self.record(REPORTS)
        self.assertEqual([event["id"] for event in self.finding_events()], FIXTURE_IDS)
        for event in self.finding_events():
            self.assertEqual(event["display_id"], event["id"].rsplit(":", 1)[1])
            self.assertEqual(event["run_id"], "gate")

    def test_a_rule_code_and_the_line_text_rebuild_the_id(self) -> None:
        self.record(REPORTS)
        for event in self.finding_events():
            lines = SOURCES[event["file"]].splitlines()
            text = lines[event["line"] - 1] if event["file"] == "src/a.py" else ""
            self.assertEqual(event["id"], f"demo-repo:gate:{downstream_hash(event['file'], event['rule_code'], text)}")

    def test_the_slug_in_the_id_is_the_configured_one(self) -> None:
        self.write_config(rules_files=["rules.md"], repo_slug="other-slug")
        self.record([REPORTS[0]])
        self.assertTrue(self.finding_events()[0]["id"].startswith("other-slug:gate:"))
        self.assertEqual(self.finding_events()[0]["repo"], "other-slug")

    def test_a_moved_line_keeps_the_id_and_a_changed_line_does_not(self) -> None:
        self.record([REPORTS[0]])
        (self.repo / "src/a.py").write_text("# new first line\n" + SOURCES["src/a.py"], encoding="utf-8")
        out = self.record([{**REPORTS[0], "line": 3}])
        self.assertIn("already open", out)
        (self.repo / "src/a.py").write_text(SOURCES["src/a.py"].replace("import os\nimport os", "import os\nimport sys"), encoding="utf-8")
        self.record([REPORTS[0]])
        self.assertEqual(len(self.finding_events()), 2)


class RecordTest(GateCase):
    def test_a_finding_has_the_fields_of_a_gate_finding(self) -> None:
        out = self.record([REPORTS[0]])
        self.assertIn("gate record: +1 finding(s), 0 already open", out)
        (event,) = self.finding_events()
        self.assertEqual({key: event[key] for key in ("source", "run_id", "file", "line", "rule_code", "rule_id", "category",
                                                      "severity", "summary", "verdict", "rule_id_source", "repo")},
                         {"source": "gate", "run_id": "gate", "file": "src/a.py", "line": 2, "rule_code": "F811",
                          "rule_id": "ruff:F811", "category": "ruff", "severity": "should-fix",
                          "summary": "redefinition of unused `os` from line 1", "verdict": "CONFIRMED",
                          "rule_id_source": "tool", "repo": "demo-repo"})
        self.assertEqual(event["sha"], "none")
        self.assertEqual(event["snippet"], "import os\nimport os\n\n")

    def test_the_snippet_is_two_lines_either_side_and_clipped_at_the_file_edges(self) -> None:
        self.record([{**REPORTS[1], "line": 8}, {**REPORTS[0], "line": 1}])
        by_line = {event["line"]: event["snippet"] for event in self.finding_events()}
        self.assertEqual(by_line[8], "    try:\n        return open(path).read()\n    except Exception:  # noqa: BLE001\n        return None")
        self.assertEqual(by_line[1], "import os\nimport os\n")

    def test_the_sha_is_head(self) -> None:
        head = self.commit("base")
        self.record([REPORTS[0]])
        self.assertEqual(self.finding_events()[0]["sha"], head[:8])

    def test_a_tool_code_is_mapped_to_a_clause_through_the_config(self) -> None:
        self.record([REPORTS[1]])
        (event,) = self.finding_events()
        self.assertEqual((event["rule_id"], event["rule_code"], event["rule_id_source"], event["category"], event["severity"]),
                         ("P0E.4", "NOQA", "tool", "P0E", "blocker"))

    def test_a_clause_id_as_the_code_is_used_as_it_is(self) -> None:
        self.record([{"file": "src/a.py", "line": 2, "code": "P1.1", "message": "too deep"}])
        (event,) = self.finding_events()
        self.assertEqual((event["rule_id"], event["rule_code"], event["category"], event["severity"]),
                         ("P1.1", "P1.1", "P1", "should-fix"))
        self.assertNotIn("rule_id_source", event)

    def test_a_clause_the_rules_do_not_define_is_refused_and_nothing_is_written(self) -> None:
        code, _, err = self.cli("gate", "record", stdin=json.dumps([REPORTS[0], {**REPORTS[0], "code": "P9.9"}]))
        self.assertEqual(code, 1)
        self.assertIn("unknown rule_id 'P9.9'", err)
        self.assertFalse(self.ledger.exists())

    def test_a_tool_code_without_a_mapping_is_recorded_as_the_tool_id(self) -> None:
        self.record([{**REPORTS[0], "code": "eslint:no-undef"}])
        event, = self.finding_events()
        self.assertEqual((event["rule_id"], event["rule_code"], event["category"]), ("eslint:no-undef", "no-undef", "eslint"))

    def test_the_failure_scenario_comes_from_the_config_by_rule_id_else_a_plain_default(self) -> None:
        self.write_config(rules_files=["rules.md"], tool_rule_map={"gate:NOQA": "P0E.4"},
                          scenarios={"P0E.4": "A bare suppression hides which case it is."})
        self.record([REPORTS[1], REPORTS[0]])
        scenarios = {event["rule_id"]: event["failure_scenario"] for event in self.finding_events()}
        self.assertEqual(scenarios["P0E.4"], "A bare suppression hides which case it is.")
        self.assertEqual(scenarios["ruff:F811"], "The gate flagged ruff:F811: redefinition of unused `os` from line 1")

    def test_an_explicit_severity_wins_and_a_bad_one_is_refused(self) -> None:
        self.record([{**REPORTS[2], "severity": "blocker"}])
        self.assertEqual(self.finding_events()[0]["severity"], "blocker")
        code, _, err = self.cli("gate", "record", stdin=json.dumps([{**REPORTS[0], "severity": "critical"}]))
        self.assertEqual(code, 1)
        self.assertIn("severity 'critical'", err)

    def test_line_zero_means_no_position_and_is_stored_as_line_one(self) -> None:
        self.record([REPORTS[2]])
        self.assertEqual(self.finding_events()[0]["line"], 1)

    def test_a_line_past_the_end_of_the_file_hashes_as_empty_text(self) -> None:
        self.record([{**REPORTS[0], "line": 500}])
        self.assertEqual(self.finding_events()[0]["id"], f"demo-repo:gate:{downstream_hash('src/a.py', 'F811', '')}")

    def test_the_same_report_twice_in_one_input_is_one_finding(self) -> None:
        out = self.record([REPORTS[0], REPORTS[0]])
        self.assertEqual(len(self.finding_events()), 1)
        self.assertIn("+1 finding(s), 1 already open", out)

    def test_recording_again_adds_nothing_while_the_finding_is_open(self) -> None:
        self.record(REPORTS)
        before = self.ledger_bytes()
        self.record(REPORTS)
        self.assertEqual(self.ledger_bytes(), before)

    def test_a_finding_that_was_closed_and_comes_back_is_reopened(self) -> None:
        self.record([REPORTS[0]])
        self.cli_ok("gate", "pass", "--files", "src/a.py")
        self.cli_ok("infer")
        self.assertIn("0 open", self.cli_ok("count"))
        self.record([REPORTS[0]])
        self.assertIn("1 open", self.cli_ok("count"))
        self.assertEqual(len(self.finding_events()), 2)

    def test_they_count_as_gate_findings(self) -> None:
        self.record(REPORTS)
        self.assertIn("3 open (gate 3 / l1 0 / l2 0)", self.cli_ok("count"))

    def test_all_items_are_checked_before_anything_is_written(self) -> None:
        for bad, message in (({**REPORTS[1], "file": "src/missing.py"}, "item 2: cannot read src/missing.py"),
                             ({**REPORTS[1], "line": "8"}, "item 2: field 'line'"),
                             ({**REPORTS[1], "line": -1}, "item 2: field 'line'"),
                             ({**REPORTS[1], "code": ""}, "item 2: field 'code'"),
                             ({**REPORTS[1], "message": "  "}, "item 2: field 'message'"),
                             ({"file": "src/a.py", "line": 1, "code": "ruff:F811"}, "item 2: field 'message'")):
            with self.subTest(message=message):
                code, _, err = self.cli("gate", "record", stdin=json.dumps([REPORTS[0], bad]))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertFalse(self.ledger.exists())

    def test_the_input_may_be_an_object_with_findings(self) -> None:
        self.record({"findings": [REPORTS[0]]})
        self.assertEqual(len(self.finding_events()), 1)

    def test_input_can_be_stdin_dash_a_file_or_the_json_itself(self) -> None:
        path = self.tmp / "reports.json"
        path.write_text(json.dumps([REPORTS[0]]), encoding="utf-8")
        for argv, stdin in ((("--input", "-"), json.dumps([REPORTS[0]])), (("--input", str(path)), ""),
                            (("--input", json.dumps([REPORTS[0]])), ""), ((), json.dumps([REPORTS[0]]))):
            with self.subTest(argv=argv):
                self.ledger.unlink(missing_ok=True)
                self.cli_ok("gate", "record", *argv, stdin=stdin)
                self.assertEqual(len(self.finding_events()), 1)

    def test_a_missing_input_file_is_an_error(self) -> None:
        code, _, err = self.cli("gate", "record", "--input", str(self.tmp / "absent.json"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read input", err)

    def test_dry_run_prints_the_events_and_writes_nothing(self) -> None:
        out = self.record(REPORTS, "--dry-run")
        self.assertIn("[dry-run] gate record: +3 finding(s)", out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("{")]), 3)
        self.assertFalse(self.ledger.exists())

    def test_paths_are_stored_relative_to_the_repo_root(self) -> None:
        self.record([{**REPORTS[0], "file": str(self.repo / "src" / "a.py")}, {**REPORTS[0], "line": 1, "file": "./src/../src/a.py"}])
        self.assertEqual({event["file"] for event in self.finding_events()}, {"src/a.py"})

    def test_a_path_outside_the_repo_is_refused(self) -> None:
        outside = self.tmp / "elsewhere.py"
        outside.write_text("x = 1\n", encoding="utf-8")
        for file in (str(outside), "../elsewhere.py"):
            with self.subTest(file=file):
                code, _, err = self.cli("gate", "record", stdin=json.dumps([{**REPORTS[0], "file": file}]))
                self.assertEqual(code, 1)
                self.assertIn("is outside the repo", err)

    def test_it_works_from_a_subdirectory(self) -> None:
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "ratchet"), "gate", "record"], cwd=self.repo / "src",
                              input=json.dumps([REPORTS[0]]), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.finding_events()), 1)

    def test_without_rules_files_tool_codes_still_work(self) -> None:
        self.write_config()
        self.record([REPORTS[0]])
        self.assertEqual(self.finding_events()[0]["rule_id"], "ruff:F811")


class RepoRelativeTest(unittest.TestCase):
    def test_forms(self) -> None:
        root = Path("/repo")
        self.assertEqual(repo_relative(root, "a/b.py"), "a/b.py")
        self.assertEqual(repo_relative(root, "./a//b.py"), "a/b.py")
        self.assertEqual(repo_relative(root, "/repo/a/b.py"), "a/b.py")
        with self.assertRaises(RatchetError):
            repo_relative(root, "/other/a.py")
        with self.assertRaises(RatchetError):
            repo_relative(root, "../a.py")


class PassTest(GateCase):
    def test_it_writes_a_gate_pass_event(self) -> None:
        head = self.commit("base")
        out = self.cli_ok("gate", "pass", "--files", "src/b.py", "src/a.py", "src/a.py")
        self.assertIn("gate pass: 2 file(s)", out)
        (event,) = self.events()
        self.assertEqual({key: event[key] for key in ("v", "kind", "repo", "sha", "files")},
                         {"v": 1, "kind": "gate_pass", "repo": "demo-repo", "sha": head[:8], "files": ["src/a.py", "src/b.py"]})
        self.assertRegex(event["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_on_an_unborn_branch_the_sha_is_none(self) -> None:
        self.cli_ok("gate", "pass", "--files", "src/a.py")
        self.assertEqual(self.events()[0]["sha"], "none")

    def test_paths_are_normalized_like_findings_so_they_close_them(self) -> None:
        self.record(REPORTS)
        self.cli_ok("gate", "pass", "--files", "./src/a.py")
        self.cli_ok("infer")
        self.assertIn("1 open", self.cli_ok("count"))  # src/b.py is still open

    def test_the_command_needs_files(self) -> None:
        with self.assertRaises(SystemExit):
            self.cli("gate", "pass")

    def test_dry_run_writes_nothing(self) -> None:
        out = self.cli_ok("gate", "pass", "--files", "src/a.py", "--dry-run")
        self.assertIn("[dry-run] gate pass: 1 file(s)", out)
        self.assertFalse(self.ledger.exists())

    def test_a_file_outside_the_repo_is_refused(self) -> None:
        code, _, err = self.cli("gate", "pass", "--files", "../a.py")
        self.assertEqual(code, 1)
        self.assertIn("is outside the repo", err)

    def record(self, reports: object) -> str:
        return self.cli_ok("gate", "record", stdin=json.dumps(reports))


class ApiTest(GateCase):
    def test_the_public_names_are_the_documented_ones(self) -> None:
        self.assertEqual(sorted(api.__all__), ["GateRecording", "RatchetError", "ledger_path", "record_gate_findings", "record_gate_pass"])
        for name in api.__all__:
            self.assertTrue(hasattr(api, name), name)

    def test_ledger_path(self) -> None:
        self.assertEqual(api.ledger_path(self.repo), self.ledger)
        self.write_config(ledger="findings.jsonl")
        self.assertEqual(api.ledger_path(str(self.repo)), self.repo / "findings.jsonl")

    def test_record_gate_findings_returns_every_id_and_what_it_wrote(self) -> None:
        first = api.record_gate_findings(REPORTS, repo=self.repo)
        self.assertEqual(first.ids, FIXTURE_IDS)
        self.assertEqual([event["id"] for event in first.recorded], FIXTURE_IDS)
        second = api.record_gate_findings(REPORTS, repo=self.repo)
        self.assertEqual((second.ids, second.recorded), (FIXTURE_IDS, []))
        self.assertEqual(len(self.finding_events()), 3)

    def test_the_cli_and_the_api_write_the_same_events(self) -> None:
        api.record_gate_findings(REPORTS, repo=self.repo)
        from_api = [{k: v for k, v in event.items() if k != "ts"} for event in self.events()]
        self.ledger.unlink()
        self.record(REPORTS)
        self.assertEqual([{k: v for k, v in event.items() if k != "ts"} for event in self.events()], from_api)

    def test_record_gate_pass_returns_the_event_it_wrote(self) -> None:
        event = api.record_gate_pass(["src/b.py", "src/a.py"], repo=self.repo)
        self.assertEqual(self.events(), [event])
        self.assertEqual(event["files"], ["src/a.py", "src/b.py"])

    def test_the_repo_defaults_to_the_current_directory(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(api.record_gate_findings(REPORTS[:1]).ids, FIXTURE_IDS[:1])

    def test_problems_raise_ratchet_error_and_write_nothing(self) -> None:
        with self.assertRaises(RatchetError):
            api.record_gate_findings([REPORTS[0], {**REPORTS[0], "code": "P9.9"}], repo=self.repo)
        with self.assertRaises(RatchetError):
            api.record_gate_pass([], repo=self.repo)
        self.assertFalse(self.ledger.exists())

    def test_an_unwritable_ledger_is_a_ratchet_error_too(self) -> None:
        self.write_config(rules_files=["rules.md"], ledger="no-such-dir/ledger.jsonl")
        with self.assertRaises(RatchetError):
            api.record_gate_findings(REPORTS[:1], repo=self.repo)

    def record(self, reports: object) -> str:
        return self.cli_ok("gate", "record", stdin=json.dumps(reports))


if __name__ == "__main__":
    unittest.main()
