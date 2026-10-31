"""Scoring and recording harness for the generalization check.

The question: after a rule was learned from one finding, does the reviewer flag a NEW instance of the same mechanism, label
it with that rule, and leave a similar legitimate control alone? CRITERIA.md fixes the reading before anything is recorded;
this file only counts, and records what the reviewer said so that the count can be redone for free.

    python3.12 samples/generalization/run_check.py                          # --replay: score the committed recordings, free
    python3.12 samples/generalization/run_check.py --live --yes-spend 24    # record: runs the reviewer, COSTS MONEY

Layout, all under samples/generalization/:
  cases/<id>/case.json, oracle.py, code/<violation>, code/<control>    written by hand; never shown to the reviewer
  cases/<id>/base/<file>                  optional: the earlier version of a code file, so that the review sees a modification
  recordings/<id>/<condition>-<n>.txt     the reviewer's raw stdout (the last attempt of that run)
  recordings/<id>/<condition>-<n>.json    provenance: which code, base files and digest produced that text, with which command
  recordings/<id>/<condition>-<n>.attempt1.txt/.json    the failed first attempt of a run that was repeated (both are kept)
  recordings/<id>/<condition>-<n>[.attempt1].stderr.txt    the reviewer's stderr of a FAILED attempt, for diagnosis on this machine only:
                                          it names the recording machine's settings and paths, so it is git-ignored and no provenance holds it
  recordings/<id>/<condition>.digest.md   the exact digest text those runs were made with: replay checks against this, not the repo's
  results.json, results.md                what --replay computes from them (no clock in either)

Two conditions per case: `with_rule` (the repo's full digest) and `without_rule` (the same digest compiled from the rules
with that case's clause block removed). Each is reviewed `--runs` times (3) at `/code-review high`, in a scratch git repo
whose reviewed change is exactly the case's two code files: added, or modified from their `base/` versions.
A run whose call fails, or whose output has no findings array, is repeated once at once; both attempts are kept (CRITERIA.md).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

# house_rules is imported from the checkout this file sits in, whatever the current directory is.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from house_rules.config import Config, load_config  # noqa: E402
from house_rules.digest import display_path, plan_outputs  # noqa: E402
from house_rules.errors import HouseRulesError  # noqa: E402
from house_rules.fsutil import read_text, write_text  # noqa: E402
from house_rules.git import run_git  # noqa: E402
from house_rules.ingest import extract_review_items_lenient  # noqa: E402
from house_rules.ledger import UNMAPPED  # noqa: E402
from house_rules.review import (  # noqa: E402
    REVIEW_CHILD_ENV, TIMEOUT_SECONDS, claude_command, critic_version, require_claude,
)
from house_rules.rules import Rules, kebab_key, load_rules, parse_rules_text  # noqa: E402
from house_rules.sources import DiskReader  # noqa: E402

LEVEL = "high"  # CRITERIA.md: every recorded review is `/code-review high`
CONDITIONS = ("with_rule", "without_rule")
DEFAULT_RUNS = 3  # CRITERIA.md assumes three runs per condition
# CRITERIA.md, PASS per case, in with_rule only:
MIN_LABELED_RUNS = 2  # (1) a finding on the violation file carries the case's label in at least this many runs
MAX_CONTROL_LABELED_RUNS = 0  # (2) ... and a finding on the control file carries it in no more than this many
CONTROL_KINDS = ("compliant", "exempt")
RECORDING_VERSION = 1
MAX_ATTEMPTS = 2  # CRITERIA.md: a run slot is repeated at most once, and only because its first attempt failed
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"  # a headless review must not look like a continuation of the caller's session
EXIT_NOT_CONFIRMED = 2  # --live without the matching --yes-spend
STDERR_TAIL_LINES = 5  # how much of a failed attempt's stderr goes to the console; the side file has all of it

OK, FAILED, MISSING = "ok", "failed", "missing"  # what became of one run
EMPTY, RECORDED, INTERRUPTED = "empty", "recorded", "interrupted"  # a run slot on disk: nothing / a final recording / only a failed first attempt
PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"  # the verdict of one case
VIOLATION, CONTROL = "violation", "control"

# The id of the model, if the review text happens to name one (the output of /code-review does not promise to).
_MODEL_ID_RE = re.compile(r"\bclaude-[a-z]+-[0-9][a-z0-9.\-]*", re.IGNORECASE)
_GIT_IDENTITY = {"GIT_AUTHOR_NAME": "generalization", "GIT_AUTHOR_EMAIL": "generalization@example.invalid",
                 "GIT_COMMITTER_NAME": "generalization", "GIT_COMMITTER_EMAIL": "generalization@example.invalid"}


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def gen_dir(self) -> Path:
        return self.root / "samples" / "generalization"

    @property
    def cases_dir(self) -> Path:
        return self.gen_dir / "cases"

    @property
    def recordings_dir(self) -> Path:
        return self.gen_dir / "recordings"

    @property
    def results_json(self) -> Path:
        return self.gen_dir / "results.json"

    @property
    def results_md(self) -> Path:
        return self.gen_dir / "results.md"

    def recording(self, case_id: str, condition: str, run: int, *, failed_first_attempt: bool = False) -> tuple[Path, Path]:
        """(raw output, provenance) of one run: its last attempt, or with `failed_first_attempt` the failed attempt before a repeat."""
        stem = f"{condition}-{run}" + (".attempt1" if failed_first_attempt else "")
        return self.recordings_dir / case_id / f"{stem}.txt", self.recordings_dir / case_id / f"{stem}.json"

    def snapshot(self, case_id: str, condition: str) -> Path:
        """The digest text the runs of one case and condition were made with."""
        return self.recordings_dir / case_id / f"{condition}.digest.md"


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


# ---- cases ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    id: str
    clause: str
    control_kind: str
    violation: str  # path relative to the case directory, such as `code/store_a.py`
    control: str
    code: dict[str, bytes]  # the two files above by that path, as read when the case was loaded
    base: dict[str, bytes]  # their earlier versions in base/, by path such as `base/store_a.py`; may hold one, both or none

    @property
    def label(self) -> str:
        """The clause as the reviewer is asked to write it in `category`: `REL.1` -> `rel-1`."""
        return kebab_key(self.clause)

    def code_sha256(self) -> dict[str, str]:
        return {rel: sha256_hex(data) for rel, data in self.code.items()}

    def base_sha256(self) -> dict[str, str]:
        return {rel: sha256_hex(data) for rel, data in self.base.items()}


def _case_error(path: Path, problem: str) -> HouseRulesError:
    return HouseRulesError(f"{path}: {problem}")


def code_path_of(base_path: str) -> str:
    """The file a base file is the earlier version of: `base/a.py` -> `code/a.py`."""
    return "code/" + base_path.removeprefix("base/")


def read_base(case_dir: Path, code: dict[str, bytes]) -> dict[str, bytes]:
    """The files under base/ by path relative to the case. Each is the earlier version of one of the case's two code files, under
    the same name, and differs from it: the reviewed change is then a modification of that file."""
    base_dir = case_dir / "base"
    if not base_dir.exists():
        return {}
    if not base_dir.is_dir():
        raise _case_error(base_dir, "must be a directory")
    base: dict[str, bytes] = {}
    for file in sorted(path for path in base_dir.rglob("*") if path.is_file()):
        rel = file.relative_to(case_dir).as_posix()
        if code_path_of(rel) not in code:
            raise _case_error(file, f"has no counterpart among the case's two code files (it would be {code_path_of(rel)})")
        data = file.read_bytes()
        if data == code[code_path_of(rel)]:
            raise _case_error(file, f"is the same as {code_path_of(rel)}, which would then not be part of the reviewed change")
        base[rel] = data
    return base


def read_case(case_dir: Path) -> Case:
    path = case_dir / "case.json"
    text = read_text(path)
    if text is None:
        raise _case_error(path, "not found")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _case_error(path, f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("v") != 1:
        raise _case_error(path, 'must be an object with "v": 1')
    if raw.get("id") != case_dir.name:
        raise _case_error(path, f'"id" is {raw.get("id")!r}, but the directory is {case_dir.name!r}')
    for key in ("clause", "violation", "control", "control_kind"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise _case_error(path, f'"{key}" must be a non-empty string')
    if raw["control_kind"] not in CONTROL_KINDS:
        raise _case_error(path, f'"control_kind" must be one of {list(CONTROL_KINDS)}')
    code: dict[str, bytes] = {}
    for key in (VIOLATION, CONTROL):
        rel = PurePosixPath(raw[key])
        if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("code",) or len(rel.parts) < 2:
            raise _case_error(path, f'"{key}" must be a path inside code/, got {raw[key]!r}')
        file = case_dir / rel
        if not file.is_file():
            raise _case_error(path, f'"{key}" names {raw[key]!r}, which is not a file of the case')
        code[rel.as_posix()] = file.read_bytes()
    violation, control = raw[VIOLATION], raw[CONTROL]
    if PurePosixPath(violation).name == PurePosixPath(control).name:
        # findings are matched to a file by its name (the reviewer writes the path in several spellings)
        raise _case_error(path, "the violation and the control file must have different names")
    return Case(case_dir.name, raw["clause"].strip(), raw["control_kind"], violation, control, code, read_base(case_dir, code))


def load_cases(cases_dir: Path, wanted: Sequence[str]) -> list[Case]:
    """The cases under `cases_dir` in name order, or only those named in `wanted`."""
    case_dirs = sorted(path.parent for path in cases_dir.glob("*/case.json"))
    if not case_dirs:
        raise HouseRulesError(f"no cases found: expected {cases_dir}/<id>/case.json")
    known = [path.name for path in case_dirs]
    unknown = sorted(set(wanted) - set(known))
    if unknown:
        raise HouseRulesError(f"unknown case {unknown}; the cases are {known}")
    return [read_case(path) for path in case_dirs if not wanted or path.name in wanted]


# ---- digests --------------------------------------------------------------------------------------------------------

class OverlayReader(DiskReader):
    """The working tree, except that the files in `replaced` read as the given bytes: how the rules are compiled with one
    clause's block gone, without touching the real file."""

    def __init__(self, replaced: dict[Path, bytes]) -> None:
        self._replaced = replaced

    def read(self, path: Path) -> bytes | None:
        if path in self._replaced:
            return self._replaced[path]
        return super().read(path)


def load_repo_config(root: Path) -> Config:
    config = load_config(root, None, None, root)
    if not config.rules_files:
        raise HouseRulesError(f"{root}: the config names no rules_files, so there is no digest to take a clause out of")
    return dataclasses.replace(config, inject_targets=())  # only the digest file matters here


def rendered_digest(config: Config, rules: Rules, reader: DiskReader) -> str:
    """The digest `house-rules compile` writes for these rules."""
    return plan_outputs(config, rules, reader)[0].new


def remove_clause_block(text: str, line_number: int) -> str:
    """`text` without the clause defined on `line_number`: its defining line, and for a list item the indented and blank lines
    that follow (its explanation, examples and the blank line after it). A table row is one line."""
    lines = text.splitlines(keepends=True)
    start = line_number - 1
    end = start + 1
    if not lines[start].lstrip().startswith("|"):
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
            end += 1
    return "".join(lines[:start] + lines[end:])


def _mentions(text: str, clause_id: str) -> bool:
    parts = kebab_key(clause_id).split("-")
    pattern = r"(?<![A-Za-z0-9])" + r"[.\-_]".join(re.escape(part) for part in parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def clause_lines(digest: str | None, clause_id: str) -> list[str]:
    """The lines of a digest that list `clause_id` (one under Violations; none when there is no digest)."""
    return [line for line in (digest or "").splitlines() if line.startswith(f"{clause_id} | ")]


def _without_position(rules: Rules) -> dict[str, object]:
    return {clause_id: dataclasses.replace(clause, line=0) for clause_id, clause in rules.clauses.items()}


def check_only_that_clause_went(full: Rules, reduced: Rules, clause_id: str) -> None:
    """Every other clause, the family severity table and the order of the clauses must be as before."""
    expected = {key: value for key, value in _without_position(full).items() if key != clause_id}
    if (list(_without_position(reduced)) != list(expected) or _without_position(reduced) != expected
            or reduced.family_severity != full.family_severity):
        raise HouseRulesError(f"taking {clause_id}'s block out of the rules changed more than that clause (a neighbouring clause "
                              "or the severity table sat inside the block): give the clause a block of its own first")


def check_digest_lost_only(full: str, reduced: str, clause_id: str) -> None:
    """CRITERIA.md: the same digest with the one line of the clause removed, and nothing else changed. The one thing a compile may
    change on its own is the Labeling example, which is taken from the first clause listed under Violations: when that is the
    clause removed, the example moves to the next one, and the Labeling section is not compared."""
    full_lines, reduced_lines = full.splitlines(), reduced.splitlines()
    kept = [line for line in full_lines if not line.startswith(f"{clause_id} | ")]
    if len(kept) != len(full_lines) - 1:
        raise HouseRulesError(f"the digest must list {clause_id} exactly once, under Violations")
    heading = "## Labeling"
    first_listed = full_lines[full_lines.index("## Violations") + 1].startswith(f"{clause_id} | ")
    if first_listed:
        kept, reduced_lines = kept[:kept.index(heading)], reduced_lines[:reduced_lines.index(heading)]
    if reduced_lines != kept:
        raise HouseRulesError(f"the digest without {clause_id} differs from the digest with it by more than that clause's line")


def digest_without(config: Config, rules: Rules, full: str, clause_id: str) -> str:
    """The digest compiled from the rules with the block of `clause_id` removed. Its header comment names the rules file by a hash
    of its bytes, which the removal changes; it is given the header of the shipped digest, so that the two differ by that clause."""
    clause = rules.clauses.get(clause_id)
    if clause is None:
        raise HouseRulesError(f"clause {clause_id} is not defined in the rules")
    if rules.severity_of(clause_id) is None:
        raise HouseRulesError(f"clause {clause_id} is a Reviewer rule; a case is about a clause a finding can violate")
    owner = next(source for source in rules.sources
                 if clause_id in parse_rules_text(source.text, str(source.path), config.clause_id_pattern))
    reader = OverlayReader({owner.path: remove_clause_block(owner.text, clause.line).encode("utf-8")})
    reduced = load_rules(config, reader)
    check_only_that_clause_went(rules, reduced, clause_id)
    compiled = rendered_digest(config, reduced, reader)
    digest = full.split("\n", 1)[0] + "\n" + compiled.split("\n", 1)[1]
    check_digest_lost_only(full, digest, clause_id)
    if _mentions(digest, clause_id):
        raise HouseRulesError(f"the digest without {clause_id} still mentions it (another clause refers to it); "
                              "without_rule would not be without the rule")
    return digest


@dataclass(frozen=True)
class Digests:
    full: str  # with_rule: the digest the repo ships
    without: dict[str, str]  # clause id -> the digest compiled without that clause's block

    def for_condition(self, condition: str, clause_id: str) -> str:
        return self.full if condition == "with_rule" else self.without[clause_id]


def build_digests(config: Config, clause_ids: Iterable[str]) -> Digests:
    rules = load_rules(config)
    full = rendered_digest(config, rules, DiskReader())
    shipped = read_text(config.digest_path)
    if shipped != full:
        raise HouseRulesError(f"{display_path(config.digest_path, config.worktree)} is not what `house-rules compile` writes from "
                              "the rules now: run it first, so that with_rule is the digest the repo really ships")
    return Digests(full, {clause_id: digest_without(config, rules, full, clause_id) for clause_id in sorted(set(clause_ids))})


# ---- reading a review -----------------------------------------------------------------------------------------------

def parse_review(text: str, exit_code: int | None, error: str | None) -> tuple[list[dict] | None, str | None]:
    """(findings, None) for a review that ran and printed a JSON array of findings (an empty array is a review that found
    nothing), else (None, why not). The reading of the text is the one `ingest-code-review` uses."""
    if exit_code != 0:
        return None, error or f"the reviewer exited with {exit_code}"
    try:
        items, _ = extract_review_items_lenient(text)
    except HouseRulesError as exc:
        return None, f"no JSON findings in the output ({str(exc).splitlines()[0]})"
    else:
        return items, None


def finding_label(item: dict) -> str:
    """The clause the reviewer named in `category`, in kebab-case; a finding that names none is `unmapped`."""
    category = item.get("category")
    text = category.strip() if isinstance(category, str) else ""
    return kebab_key(text) if text else UNMAPPED


def which_file(reported: object, case: Case) -> str | None:
    """VIOLATION, CONTROL or None for the `file` of a finding. Compared by file name: a reviewer writes the same file as
    `code/a.py`, `./code/a.py`, `a.py` or an absolute path, and load_cases made sure the two names differ."""
    if not isinstance(reported, str):
        return None
    name = PurePosixPath(reported.strip().replace("\\", "/")).name
    if name == PurePosixPath(case.violation).name:
        return VIOLATION
    if name == PurePosixPath(case.control).name:
        return CONTROL
    return None


@dataclass(frozen=True)
class RunScore:
    run: int
    status: str  # OK, FAILED or MISSING
    reason: str | None  # why it failed or is missing
    findings: int
    violation_labels: tuple[str, ...]  # the label of every finding on the violation file
    control_labels: tuple[str, ...]  # the label of every finding on the control file
    other_file_findings: int  # findings on any other file: ignored
    reviewer_version: str | None
    model_id: str | None
    attempts: int  # reviewer calls recorded for this run: 0, 1, or 2 (the first failed and was repeated)
    first_failure: str | None  # why the first attempt failed, when it did


def _unscored(run: int, status: str, reason: str, prov: dict, attempts: int = 0, first_failure: str | None = None) -> RunScore:
    return RunScore(run, status, reason, 0, (), (), 0, _text_or_none(prov.get("reviewer_version")), _text_or_none(prov.get("model_id")),
                    attempts, first_failure)


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def score_items(items: list[dict], case: Case, run: int, prov: dict, attempts: int, first_failure: str | None) -> RunScore:
    labels: dict[str, list[str]] = {VIOLATION: [], CONTROL: []}
    others = 0
    for item in items:
        side = which_file(item.get("file"), case)
        if side is None:
            others += 1
        else:
            labels[side].append(finding_label(item))
    return RunScore(run, OK, None, len(items), tuple(labels[VIOLATION]), tuple(labels[CONTROL]), others,
                    _text_or_none(prov.get("reviewer_version")), _text_or_none(prov.get("model_id")), attempts, first_failure)


def tally(runs: Sequence[RunScore], label: str) -> dict[str, int]:
    """Counts of runs (not findings) for one condition of one case."""
    scored = [run for run in runs if run.status == OK]
    return {
        "runs_ok": len(scored),
        "runs_failed": sum(1 for run in runs if run.status == FAILED),
        "runs_missing": sum(1 for run in runs if run.status == MISSING),
        "violation_labeled_runs": sum(1 for run in scored if label in run.violation_labels),
        "violation_flagged_runs": sum(1 for run in scored if run.violation_labels),
        "control_labeled_runs": sum(1 for run in scored if label in run.control_labels),
        "control_flagged_runs": sum(1 for run in scored if run.control_labels),
    }


def verdict(with_rule: dict[str, int]) -> str:
    """CRITERIA.md: decided in with_rule alone, and only when every run of it produced findings to read (a run that failed
    even after its repeat leaves the case INCOMPLETE)."""
    if with_rule["runs_failed"] or with_rule["runs_missing"]:
        return INCOMPLETE
    if with_rule["violation_labeled_runs"] >= MIN_LABELED_RUNS and with_rule["control_labeled_runs"] <= MAX_CONTROL_LABELED_RUNS:
        return PASS
    return FAIL


# ---- replay ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Attempt:
    prov: dict
    raw: str


def changed_files(name: str, recorded: object, current: dict[str, str], kind: str) -> list[str]:
    """What differs between the file hashes a recording names and the files of the case now (`kind`: "files" or "base files")."""
    recorded_hashes = recorded if isinstance(recorded, dict) else {}
    found = [f"{name}: {rel} is not the file that was reviewed (recorded {str(recorded_hashes.get(rel))[:12]}, now {now[:12]})"
             for rel, now in current.items() if recorded_hashes.get(rel) != now]
    if set(recorded_hashes) != set(current):
        found.append(f"{name}: the review covered the {kind} {sorted(recorded_hashes)}, the case has {sorted(current)}")
    return found


def read_attempt(layout: Layout, case: Case, condition: str, run: int, snapshot_sha256: str | None, *, failed_first_attempt: bool,
                 expected_attempt: int) -> tuple[Attempt | None, list[str]]:
    """(one recorded attempt of a run, what is wrong with it); (None, []) when it has no recording. It is checked against the code
    and base files in the repo now and against the digest snapshot of its own case and condition, not against the repo's digest."""
    raw_path, prov_path = layout.recording(case.id, condition, run, failed_first_attempt=failed_first_attempt)
    name = f"{case.id}/{condition}-{run}" + (".attempt1" if failed_first_attempt else "")
    raw = read_text(raw_path)
    prov_text = read_text(prov_path)
    if raw is None or prov_text is None:
        return None, []
    try:
        prov = json.loads(prov_text)
    except json.JSONDecodeError as exc:
        return None, [f"{name}.json: not valid JSON ({exc})"]
    if not isinstance(prov, dict) or prov.get("v") != RECORDING_VERSION:
        return None, [f"{name}.json: not a version {RECORDING_VERSION} record"]
    problems = [f"{name}: recorded {key} {prov.get(key)!r}, expected {wanted!r}"
                for key, wanted in (("case", case.id), ("condition", condition), ("run", run), ("level", LEVEL), ("attempt", expected_attempt))
                if prov.get(key) != wanted]
    problems += changed_files(name, prov.get("code_sha256"), case.code_sha256(), "files")
    problems += changed_files(name, prov.get("base_sha256"), case.base_sha256(), "base files")
    snapshot_name = f"{case.id}/{condition}.digest.md"
    if snapshot_sha256 is None:
        problems.append(f"{name}: its digest snapshot {snapshot_name} is missing")
    elif prov.get("digest_sha256") != snapshot_sha256:
        problems.append(f"{name}: the digest it ran with is not the one in its snapshot {snapshot_name} (recorded "
                        f"{str(prov.get('digest_sha256'))[:12]}, snapshot {snapshot_sha256[:12]})")
    if prov.get("raw_sha256") != sha256_hex(raw):
        problems.append(f"{name}.txt: the raw output was changed after it was recorded")
    return Attempt(prov, raw), problems


def load_run(layout: Layout, case: Case, condition: str, run: int, snapshot_sha256: str | None) -> tuple[RunScore, list[str]]:
    """(the score of one recorded run, what is wrong with its provenance). The run is scored from its last attempt. A run with
    no recording is MISSING."""
    name = f"{case.id}/{condition}-{run}"
    repeated = any(path.exists() for path in layout.recording(case.id, condition, run, failed_first_attempt=True))
    final, problems = read_attempt(layout, case, condition, run, snapshot_sha256, failed_first_attempt=False,
                                   expected_attempt=MAX_ATTEMPTS if repeated else 1)
    first, first_problems = (read_attempt(layout, case, condition, run, snapshot_sha256, failed_first_attempt=True, expected_attempt=1)
                             if repeated else (None, []))
    problems += first_problems
    first_failure = None
    if first is not None:
        first_items, first_failure = parse_review(first.raw, first.prov.get("exit_code"), _text_or_none(first.prov.get("error")))
        if first_items is not None:
            problems.append(f"{name}.attempt1: the run was repeated although its first attempt did not fail")
    if final is None:
        if problems:
            return _unscored(run, MISSING, "unreadable provenance", {}), problems
        if first is not None:
            return _unscored(run, MISSING, "the repeat of a failed first attempt is not recorded (--live --skip-existing makes it)", {},
                             1, first_failure), []
        return _unscored(run, MISSING, "no recording", {}), []
    attempts = 2 if first is not None else 1
    if problems:
        return _unscored(run, MISSING, "provenance mismatch", final.prov, attempts, first_failure), problems
    items, failure = parse_review(final.raw, final.prov.get("exit_code"), _text_or_none(final.prov.get("error")))
    if items is None:
        return _unscored(run, FAILED, str(failure), final.prov, attempts, first_failure), []
    return score_items(items, case, run, final.prov, attempts, first_failure), []


def snapshot_problems(case: Case, condition: str, snapshot: str | None) -> list[str]:
    """What is wrong with the text of a digest snapshot: the with_rule one must list the clause (once), the without_rule one must
    not name it."""
    name = f"{case.id}/{condition}.digest.md"
    if snapshot is None:
        return []
    if condition == "with_rule" and len(clause_lines(snapshot, case.clause)) != 1:
        return [f"{name}: does not list {case.clause} exactly once, so it is not the digest of a with_rule run"]
    if condition == "without_rule" and _mentions(snapshot, case.clause):
        return [f"{name}: still names {case.clause}, so it is not the digest of a without_rule run"]
    return []


def rules_changed_note(case: Case, recorded_digest: str | None, current_digest: str | None) -> str | None:
    """A note when the digest line of the case's clause is not what the with_rule runs were recorded with. The scores are still
    those of the recorded digests; the repo's current digest never decides whether a recording can be replayed."""
    if recorded_digest is None or clause_lines(recorded_digest, case.clause) == clause_lines(current_digest, case.clause):
        return None
    return (f"{case.id}: the rules changed since recording: the {case.clause} line of the digest now differs from the recorded one; "
            "the scores describe the recorded digests")


def condition_row(runs: Sequence[RunScore], label: str) -> dict:
    row: dict = dict(tally(runs, label))
    row["runs"] = [
        {"run": run.run, "status": run.status, "findings": run.findings, "violation_labels": list(run.violation_labels),
         "control_labels": list(run.control_labels), "other_file_findings": run.other_file_findings, "attempts": run.attempts,
         "first_attempt_failure": run.first_failure}
        if run.status == OK else {"run": run.run, "status": run.status, "reason": run.reason, "attempts": run.attempts,
                                  "first_attempt_failure": run.first_failure}
        for run in runs]
    return row


def repeat_summary(case_rows: Sequence[dict]) -> dict:
    """How many run slots needed a repeat, and every failed attempt on disk (the first attempt of a repeated run, and a last
    attempt that failed too)."""
    repeated, failed = 0, []
    for case in case_rows:
        for condition in CONDITIONS:
            for run in case["conditions"][condition]["runs"]:
                where = {"case": case["id"], "condition": condition, "run": run["run"]}
                if run["first_attempt_failure"] is not None:
                    repeated += 1
                    failed.append({**where, "attempt": 1, "reason": run["first_attempt_failure"]})
                if run["status"] == FAILED:
                    failed.append({**where, "attempt": run["attempts"], "reason": run["reason"]})
    return {"slots_repeated": repeated, "failed_attempts": failed}


def replay(layout: Layout, cases: Sequence[Case], current_digest: str | None, runs: int) -> dict:
    """Score every recording of `cases`. Refuses (HouseRulesError) if a recording is not of the code, base files and digest snapshot
    it was made with. `current_digest` (the digest the repo ships now, None if it has none) only decides whether to add a note."""
    problems: list[str] = []
    scored: dict[tuple[str, str], list[RunScore]] = {}
    snapshots: dict[tuple[str, str], str | None] = {}
    for case in cases:
        for condition in CONDITIONS:
            snapshot = read_text(layout.snapshot(case.id, condition))
            snapshots[(case.id, condition)] = snapshot
            problems += snapshot_problems(case, condition, snapshot)
            loaded = [load_run(layout, case, condition, run, sha256_hex(snapshot) if snapshot is not None else None)
                      for run in range(1, runs + 1)]
            scored[(case.id, condition)] = [score for score, _ in loaded]
            problems += [problem for _, found in loaded for problem in found]
    if problems:
        raise HouseRulesError("refusing to score: these recordings are not of the code, base files and digest snapshots they were "
                              "made with (record them again, or restore the files):\n  " + "\n  ".join(problems))
    case_rows = []
    for case in cases:
        rows = {condition: condition_row(scored[(case.id, condition)], case.label) for condition in CONDITIONS}
        note = rules_changed_note(case, snapshots[(case.id, "with_rule")], current_digest)
        case_rows.append({"id": case.id, "clause": case.clause, "label": case.label, "control_kind": case.control_kind,
                          "verdict": verdict(rows["with_rule"]), "notes": [note] if note else [], "conditions": rows})
    every_run = [run for runs_of in scored.values() for run in runs_of]
    return {
        "v": 1,
        "level": LEVEL,
        "runs_per_condition": runs,
        "pass_rule": {"violation_labeled_runs_at_least": MIN_LABELED_RUNS, "control_labeled_runs_at_most": MAX_CONTROL_LABELED_RUNS,
                      "decided_in": "with_rule"},
        "reviewer_versions": sorted({run.reviewer_version for run in every_run if run.reviewer_version}),
        "model_ids": sorted({run.model_id for run in every_run if run.model_id}),
        "cases": case_rows,
        "repeats": repeat_summary(case_rows),
        "totals": {name: sum(1 for row in case_rows if row["verdict"] == name) for name in (PASS, FAIL, INCOMPLETE)},
    }


def _fraction(count: int, results: dict) -> str:
    return f"{count}/{results['runs_per_condition']}"


def table_rows(results: dict) -> list[list[str]]:
    header = ["case", "clause", "control", "condition", "runs ok", "violation labeled", "violation flagged", "control labeled",
              "control flagged", "verdict"]
    rows = [header]
    for case in results["cases"]:
        for condition in CONDITIONS:
            counts = case["conditions"][condition]
            rows.append([case["id"], case["clause"], case["control_kind"], condition,
                         _fraction(counts["runs_ok"], results), _fraction(counts["violation_labeled_runs"], results),
                         _fraction(counts["violation_flagged_runs"], results), _fraction(counts["control_labeled_runs"], results),
                         _fraction(counts["control_flagged_runs"], results), case["verdict"] if condition == "with_rule" else "-"])
    return rows


def run_notes(results: dict) -> list[str]:
    return [f"{case['id']} {condition}-{run['run']}: {run['status']} ({run['reason']})"
            for case in results["cases"] for condition in CONDITIONS for run in case["conditions"][condition]["runs"]
            if run["status"] != OK]


def repeat_notes(results: dict) -> list[str]:
    """One line per run that needed a repeat: why its first attempt failed, and what the repeat gave."""
    return [f"{case['id']} {condition}-{run['run']}: attempt 1 failed ({run['first_attempt_failure']}); the repeat is {run['status']}"
            + (f" ({run['reason']})" if run["status"] != OK else "")
            for case in results["cases"] for condition in CONDITIONS for run in case["conditions"][condition]["runs"]
            if run["first_attempt_failure"] is not None]


def case_notes(results: dict) -> list[str]:
    return [note for case in results["cases"] for note in case["notes"]]


def render_text_table(results: dict) -> str:
    rows = table_rows(results)
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]
    totals = results["totals"]
    lines += ["", f"PASS {totals[PASS]}  FAIL {totals[FAIL]}  INCOMPLETE {totals[INCOMPLETE]}",
              "labeled = a finding carries the case's clause as its category; flagged = a finding on that file, any label "
              "(unmapped included); counts are runs, not findings",
              f"run slots that needed a repeat after a failed first attempt (both attempts are kept): {results['repeats']['slots_repeated']}"]
    for heading, notes in (("repeated runs:", repeat_notes(results)), ("runs without findings:", run_notes(results)),
                           ("notes:", case_notes(results))):
        lines += (["", heading, *[f"  {note}" for note in notes]] if notes else [])
    return "\n".join(lines)


def render_markdown(results: dict) -> str:
    rows = table_rows(results)
    lines = ["# Generalization check results", "",
             f"Level `{results['level']}`, {results['runs_per_condition']} runs per condition. PASS is decided in `with_rule` alone: "
             f"a finding on the violation file carries the case's label in at least {MIN_LABELED_RUNS} runs, and a finding on the "
             f"control file carries it in {MAX_CONTROL_LABELED_RUNS}. `without_rule` has no pass line; it is shown for comparison. "
             "Columns count runs, not findings; flagged means a finding on that file with any label, `unmapped` included.", "",
             "| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    totals = results["totals"]
    lines += ["", f"PASS {totals[PASS]}, FAIL {totals[FAIL]}, INCOMPLETE {totals[INCOMPLETE]}.", "",
              f"Run slots that needed a repeat after a failed first attempt (both attempts are kept): {results['repeats']['slots_repeated']}."]
    for heading, notes in (("Repeated runs:", repeat_notes(results)), ("Runs without findings:", run_notes(results)),
                           ("Notes:", case_notes(results))):
        lines += (["", heading, "", *[f"- {note}" for note in notes]] if notes else [])
    return "\n".join(lines) + "\n"


def has_recordings(results: dict) -> bool:
    return any(run["status"] != MISSING for case in results["cases"] for row in case["conditions"].values() for run in row["runs"])


def write_results(layout: Layout, results: dict) -> None:
    write_text(layout.results_json, json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    write_text(layout.results_md, render_markdown(results))


# ---- live -----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    case: Case
    condition: str
    run: int

    @property
    def key(self) -> tuple[str, str, int]:
        return self.case.id, self.condition, self.run


def plan_jobs(cases: Sequence[Case], runs: int) -> list[Job]:
    """Every (case, condition, run), run by run. The two conditions of a case follow each other, and which one goes first
    alternates, so that whatever drifts over the hours (load, model, time of day) falls on both alike."""
    jobs: list[Job] = []
    for run in range(1, runs + 1):
        for index, case in enumerate(cases):
            order = CONDITIONS if (index + run) % 2 == 0 else CONDITIONS[::-1]
            jobs += [Job(case, condition, run) for condition in order]
    return jobs


def make_review_repo(repo: Path, digest_name: str, digest: str, case: Case) -> tuple[str, str]:
    """A git repo in `repo` whose base commit is CLAUDE.md importing the digest, plus the digest and the case's base files (the
    earlier versions) under the names of the code files they are the earlier version of, and whose next commit puts the case's two
    code files at the same relative names: added, or the base files modified. (base sha, head sha)."""
    env = {**_GIT_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}  # no hooks or signing of the user's
    run_git(["init", "-q", "-b", "main"], repo, env_extra=env)
    write_text(repo / "CLAUDE.md", f"## Code style digest (house-rules)\n@{digest_name}\n")
    write_text(repo / digest_name, digest)
    for rel, data in case.base.items():
        (repo / code_path_of(rel)).parent.mkdir(parents=True, exist_ok=True)
        (repo / code_path_of(rel)).write_bytes(data)
    run_git(["add", "-A"], repo, env_extra=env)
    run_git(["commit", "-q", "-m", "base: rules"], repo, env_extra=env)
    base = run_git(["rev-parse", "HEAD"], repo).stdout.strip()
    for rel, data in case.code.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    run_git(["add", "-A"], repo, env_extra=env)
    run_git(["commit", "-q", "-m", "change the code under review"], repo, env_extra=env)
    return base, run_git(["rev-parse", "HEAD"], repo).stdout.strip()


@dataclass(frozen=True)
class ReviewerOutput:
    stdout: str
    exit_code: int | None  # None: it never exited (timeout, or not runnable)
    stderr: str  # the reviewer's own remarks on its settings and paths: shown and kept for a failed attempt only, never recorded
    error: str | None


def review_env() -> dict[str, str]:
    """What `house-rules review` gives its claude (the review marker), and no session id."""
    env = {key: value for key, value in os.environ.items() if key != SESSION_ID_ENV}
    env[REVIEW_CHILD_ENV] = "1"
    return env


def _partial_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_reviewer(argv: list[str], cwd: Path) -> ReviewerOutput:
    """Like review.run_claude, but a failure comes back as data: the raw output of a failed run is kept."""
    try:
        proc = subprocess.run(argv, cwd=cwd, env=review_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return ReviewerOutput(_partial_output(exc.stdout), None, "", f"timed out after {TIMEOUT_SECONDS}s")
    except OSError as exc:
        return ReviewerOutput("", None, "", f"cannot run {argv[0]}: {exc}")
    return ReviewerOutput(proc.stdout, proc.returncode, proc.stderr, None)


def review_once(layout: Layout, config: Config, job: Job, digest: str, attempt: int) -> tuple[str, dict, str]:
    """Review the case's change once. Returns the raw output, the provenance and the stderr, none of them saved yet. The stderr
    is not part of the provenance: it carries the recording machine's settings and local paths, and the repo is public."""
    scratch = Path(tempfile.mkdtemp(prefix="house-rules-generalization-"))
    try:
        if scratch.resolve().is_relative_to(layout.root.resolve()):
            raise HouseRulesError(f"the temporary directory {scratch} is inside the repo; set TMPDIR to a place outside it")
        digest_name = display_path(config.digest_path, config.worktree)
        base, head = make_review_repo(scratch, digest_name, digest, job.case)
        review_range = f"{base}..{head}"
        argv = claude_command(LEVEL, review_range)
        try:
            version, version_error = critic_version(scratch), None
        except HouseRulesError as exc:
            version, version_error = None, str(exc)
        output = run_reviewer(argv, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)  # a leftover scratch dir is only clutter, never a reason to lose the recording
    items, failure = parse_review(output.stdout, output.exit_code, output.error)
    model = _MODEL_ID_RE.search(output.stdout)
    prov = {
        "v": RECORDING_VERSION, "case": job.case.id, "condition": job.condition, "run": job.run, "attempt": attempt, "level": LEVEL,
        "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "command": argv, "range": review_range,
        "reviewer_version": version, "reviewer_version_error": version_error,
        "code_sha256": job.case.code_sha256(), "base_sha256": job.case.base_sha256(), "digest_sha256": sha256_hex(digest),
        "clause_removed": job.case.clause if job.condition == "without_rule" else None,
        "exit_code": output.exit_code, "error": output.error,
        "model_id": model.group() if model else None, "raw_sha256": sha256_hex(output.stdout),
        "status": OK if items is not None else FAILED, "failure": failure,
    }
    return output.stdout, prov, output.stderr


def keep_failed_stderr(raw_path: Path, layout: Layout, prov: dict, stderr: str) -> None:
    """Show the last lines of a failed attempt's stderr on the console and keep all of it next to its recording, to find out
    why it failed. Only a failed attempt gets this: a run that worked leaves no trace of its stderr."""
    if not stderr.strip():
        return
    side_path = raw_path.with_suffix(".stderr.txt")  # <condition>-<n>[.attempt1].stderr.txt
    write_text(side_path, stderr)
    tail = "\n".join(stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
    print(f"{prov['case']}/{prov['condition']}-{prov['run']} attempt {prov['attempt']} failed; the end of its stderr "
          f"(all of it is in {display_path(side_path, layout.root)}):\n{tail}", file=sys.stderr, flush=True)


def save_recording(layout: Layout, job: Job, raw: str, prov: dict, stderr: str, *, failed_first_attempt: bool) -> None:
    raw_path, prov_path = layout.recording(job.case.id, job.condition, job.run, failed_first_attempt=failed_first_attempt)
    write_text(raw_path, raw)  # the provenance is written last: a recording without it counts as missing
    if prov["status"] == FAILED:
        keep_failed_stderr(raw_path, layout, prov, stderr)
    write_text(prov_path, json.dumps(prov, indent=2, ensure_ascii=False) + "\n")


def record_slot(layout: Layout, config: Config, job: Job, digest: str, state: str) -> dict:
    """Review one run slot and save every attempt. A failed attempt is kept and the slot is reviewed once more, at once; a repeat
    is never made because of what a review said, nor more than once (MAX_ATTEMPTS). A slot left with only its failed first attempt
    (`state` INTERRUPTED) gets just the repeat. Returns the provenance of the last attempt."""
    attempt = 2 if state == INTERRUPTED else 1
    while True:
        raw, prov, stderr = review_once(layout, config, job, digest, attempt)
        if prov["status"] == OK or attempt == MAX_ATTEMPTS:
            save_recording(layout, job, raw, prov, stderr, failed_first_attempt=False)
            return prov
        save_recording(layout, job, raw, prov, stderr, failed_first_attempt=True)
        attempt += 1


def slot_state(layout: Layout, job: Job) -> str:
    """EMPTY (nothing on disk), RECORDED (the last attempt is), or INTERRUPTED (only a failed first attempt is)."""
    if any(path.exists() for path in layout.recording(job.case.id, job.condition, job.run)):
        return RECORDED
    if any(path.exists() for path in layout.recording(job.case.id, job.condition, job.run, failed_first_attempt=True)):
        return INTERRUPTED
    return EMPTY


def check_snapshots(layout: Layout, targets: Sequence[Job], digests: Digests) -> None:
    """A digest snapshot already on disk for the case and condition of any of `targets` must be the digest a new run would use: all
    runs of one case and condition are made with one digest."""
    for job in targets:
        saved = read_text(layout.snapshot(job.case.id, job.condition))
        if saved is not None and saved != digests.for_condition(job.condition, job.case.clause):
            raise HouseRulesError(f"{job.case.id}/{job.condition}: the digest now is not the one its earlier runs were recorded with "
                                  f"({display_path(layout.snapshot(job.case.id, job.condition), layout.root)}); all runs of a case and "
                                  "condition use one digest. Delete that snapshot and the recordings of that condition to record it "
                                  "again with the rules as they are now")


def plan_line(jobs: Sequence[Job], states: dict[tuple[str, str, int], str], cases: int, runs: int) -> str:
    repeats = sum(1 for job in jobs if states[job.key] != INTERRUPTED)
    return (f"live plan: {len(jobs)} reviewer call(s) planned ({cases} case(s) x {len(CONDITIONS)} conditions x {runs} run(s) at "
            f"/code-review {LEVEL}), plus at most {repeats} more as repeats (one per run at most, only for a run whose call fails or "
            f"whose output has no findings array); --yes-spend must equal the {len(jobs)} planned calls, repeats are covered by it; "
            "this spends real money")


def run_live(layout: Layout, config: Config, cases: Sequence[Case], digests: Digests, runs: int, yes_spend: int | None,
             skip_existing: bool) -> int:
    planned = plan_jobs(cases, runs)
    states = {job.key: slot_state(layout, job) for job in planned}
    touched = [job for job in planned if states[job.key] != EMPTY]
    if touched and not skip_existing:
        names = ", ".join(f"{job.case.id}/{job.condition}-{job.run}" for job in touched)
        raise HouseRulesError(f"recordings already exist: {names}. They are evidence and are never overwritten: delete them, "
                              "or pass --skip-existing to record only the runs that have none (a run left with a failed first "
                              "attempt only gets its repeat)")
    jobs = [job for job in planned if states[job.key] != RECORDED]
    if not jobs:
        print("nothing to record: every run already has a recording")
        return 0
    targets = list({(job.case.id, job.condition): job for job in jobs}.values())  # one job of every case and condition
    check_snapshots(layout, targets, digests)
    require_claude(claude_command(LEVEL, "<range>"))
    print(plan_line(jobs, states, len(cases), runs))
    if yes_spend != len(jobs):
        print(f"not started: pass --yes-spend {len(jobs)} to confirm exactly this many calls", file=sys.stderr)
        return EXIT_NOT_CONFIRMED
    for job in targets:
        if not layout.snapshot(job.case.id, job.condition).exists():
            write_text(layout.snapshot(job.case.id, job.condition), digests.for_condition(job.condition, job.case.clause))
    repeated = failed = 0
    for position, job in enumerate(jobs, start=1):
        prov = record_slot(layout, config, job, digests.for_condition(job.condition, job.case.clause), states[job.key])
        repeated += prov["attempt"] == MAX_ATTEMPTS
        failed += prov["status"] == FAILED
        print(f"[{position}/{len(jobs)}] {job.case.id}/{job.condition}-{job.run}: {prov['status']}"
              + (f" ({prov['failure']})" if prov["failure"] else "") + (" after a repeat" if prov["attempt"] == MAX_ATTEMPTS else ""),
              flush=True)
    print(f"recorded {len(jobs)} run(s): {repeated} needed a repeat, {failed} still failed after it (every attempt is kept); "
          "score them with --replay")
    return 1 if failed else 0


# ---- command line ---------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score or record the generalization check (see CRITERIA.md).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="score the committed recordings (the default; free)")
    mode.add_argument("--live", action="store_true", help="run the reviewer and record it; costs money")
    parser.add_argument("--yes-spend", type=int, metavar="N", help="with --live: the number of reviewer calls planned, to confirm them (the plan line says how many repeats may come on top)")
    parser.add_argument("--case", action="append", default=[], metavar="ID", help="only this case (repeatable); results are not written")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"runs per condition (default {DEFAULT_RUNS}, which CRITERIA.md assumes)")
    parser.add_argument("--skip-existing", action="store_true", help="with --live: leave runs that already have a recording alone")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)  # for the tests
    return parser


def run(args: argparse.Namespace) -> int:
    layout = Layout(args.repo_root.resolve())
    if args.runs < 1:
        raise HouseRulesError("--runs must be at least 1")
    if (args.yes_spend is not None or args.skip_existing) and not args.live:
        raise HouseRulesError("--yes-spend and --skip-existing only apply to --live")
    cases = load_cases(layout.cases_dir, args.case)
    config = load_repo_config(layout.root)
    if args.live:
        return run_live(layout, config, cases, build_digests(config, [case.clause for case in cases]), args.runs, args.yes_spend,
                        args.skip_existing)
    results = replay(layout, cases, read_text(config.digest_path), args.runs)
    print(render_text_table(results))
    if args.runs != DEFAULT_RUNS:
        print(f"note: CRITERIA.md assumes {DEFAULT_RUNS} runs per condition; this reading was not pre-registered")
    if args.case:
        print("results.json and results.md not written: --case scores only part of the check")
    elif not has_recordings(results):
        print("results.json and results.md not written: nothing is recorded yet (see --live)")
    else:
        write_results(layout, results)
        print(f"wrote {display_path(layout.results_json, layout.root)} and {display_path(layout.results_md, layout.root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except HouseRulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
