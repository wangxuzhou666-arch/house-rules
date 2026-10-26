"""Mutation self-test for the compile feature: does the suite notice when the code is made wrong?

Run `python3 tests/mutation_check.py` (add `--full` to run the whole suite per mutant and list every test that went
red, which shows whether the test meant to catch a mutant did). Each mutant is one small edit of the package, applied one at a time to a
throwaway copy of the repo (the real tree is never touched). The suite must be green before the first mutant and after
the last one, and red for every mutant in between. A mutant the suite lets through means a test is missing or too weak.
Not named test_*.py, so `unittest discover` does not collect it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURE_RE = re.compile(r"^(?:FAIL|ERROR): (\S+) \((\S+)\)", re.MULTILINE)


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    old: str  # must occur exactly once in the file
    new: str


MUTANTS = (
    Mutant("table header matched case-sensitively", "ratchet/rules.py",
           "if [cell.lower() for cell in block[0][1]] != _SEVERITY_TABLE_HEADER:",
           "if block[0][1] != _SEVERITY_TABLE_HEADER:"),
    Mutant("override comment may be empty", "ratchet/rules.py",
           r'r"<!--\s*digest:\s*(\S.*?)\s*-->"', r'r"<!--\s*digest:\s*(.+?)\s*-->"'),
    Mutant("severity table separator row not required", "ratchet/rules.py",
           "if len(block) < 2 or not all(_SEPARATOR_CELL_RE.match(cell) for cell in block[1][1]):", "if False:"),
    Mutant("severity word not validated", "ratchet/rules.py",
           "            if severity not in SEVERITIES:\n                raise RatchetError(f\"{where}: severity", "            if False:\n                raise RatchetError(f\"{where}: severity"),
    Mutant("code fences not skipped", "ratchet/rules.py",
           "        elif not in_fence:\n            yield number, line", "        else:\n            yield number, line"),
    Mutant("conflicting severity rows accepted (first wins)", "ratchet/rules.py",
           "if family in mapping and mapping[family][0] != severity:", "if False:"),
    Mutant("layering: a later file may redefine a clause", "ratchet/rules.py",
           "            if clause_id in origin:\n", "            if False:\n"),
    Mutant("family matched by prefix, not exactly", "ratchet/rules.py",
           "return clause.tag or self.family_severity.get(family_of(rule_id))",
           "return clause.tag or next((s for f, s in self.family_severity.items() if rule_id.startswith(f)), None)"),
    Mutant("family table beats the clause's own tag", "ratchet/rules.py",
           "return clause.tag or self.family_severity.get(family_of(rule_id))",
           "return self.family_severity.get(family_of(rule_id)) or clause.tag"),
    Mutant("source hash truncated to 11 chars", "ratchet/rules.py",
           "hashlib.sha256(raw_bytes).hexdigest()[:12]", "hashlib.sha256(raw_bytes).hexdigest()[:11]"),
    Mutant("ingest severity fallback ignores the table", "ratchet/ingest.py",
           'severity = (rules.severity_of(rule_id) or "should-fix") if severity_defaulted else claimed_severity',
           'severity = ((rules.clauses[rule_id].tag if rule_id in rules.clauses else None) or "should-fix") '
           "if severity_defaulted else claimed_severity"),
    Mutant("add severity fallback ignores the table", "ratchet/ingest.py",
           'severity = item.get("severity", rules.severity_of(rule_id) or "should-fix")',
           'severity = item.get("severity", (rules.clauses[rule_id].tag if rule_id in rules.clauses else None) or "should-fix")'),
    Mutant("cap counts bytes, not characters", "ratchet/digest.py",
           "return text if len(text) <= max_chars else", 'return text if len(text.encode("utf-8")) <= max_chars else'),
    Mutant("cap drops the ellipsis", "ratchet/digest.py",
           "else text[:max_chars - 1].rstrip() + ELLIPSIS", "else text[:max_chars].rstrip()"),
    Mutant("override comment ignored", "ratchet/digest.py",
           "if clause.digest_override is not None:", "if False:"),
    Mutant("bold markers kept", "ratchet/digest.py",
           'text = clause.text.replace("**", "")', "text = clause.text"),
    Mutant("full stop cuts inside words and numbers", "ratchet/digest.py",
           r"\.(?=\s|$)|;(?=\s|$)", r"\.|;"),
    Mutant("bracket-aware cutting disabled", "ratchet/digest.py",
           "        elif depth == 0 and _SENTENCE_END_RE.match(text, index):",
           "        elif _SENTENCE_END_RE.match(text, index):"),
    Mutant("a stray closing bracket drives the depth negative", "ratchet/digest.py",
           "depth = max(depth - 1, 0)", "depth -= 1"),
    Mutant("full-width brackets not tracked", "ratchet/digest.py",
           r'_BRACKET_OPENERS = "([\uff08\u3010\u300c"', '_BRACKET_OPENERS = "(["'),
    Mutant("trailing colon trim disabled", "ratchet/digest.py",
           r'_TRAILING_JUNK_RE = re.compile(r"[\s:\uff1a]+$")', r'_TRAILING_JUNK_RE = re.compile(r"\s+$")'),
    Mutant("full-width colon not trimmed", "ratchet/digest.py",
           r'_TRAILING_JUNK_RE = re.compile(r"[\s:\uff1a]+$")', r'_TRAILING_JUNK_RE = re.compile(r"[\s:]+$")'),
    Mutant("header names only the first source", "ratchet/digest.py",
           "for source in rules.sources)", "for source in rules.sources[:1])"),
    Mutant("header shows absolute paths", "ratchet/digest.py",
           "return Path(os.path.relpath(path, root)).as_posix()", "return str(path)"),
    Mutant("inject ignores a missing end marker", "ratchet/digest.py",
           "end = _marker_index(text, END_MARKER, name)",
           "end = text.find(END_MARKER) if END_MARKER in text else len(text)"),
    Mutant("inject accepts duplicated markers", "ratchet/digest.py",
           "    if count > 1:\n", "    if False:\n"),
    Mutant("inject accepts the end marker before the begin marker", "ratchet/digest.py",
           "if end < begin + len(BEGIN_MARKER):", "if False:"),
    Mutant("a clause with no rule text is accepted", "ratchet/digest.py",
           "        if not rule:\n", "        if False:\n"),
    Mutant("a missing digest file counts as fresh", "ratchet/digest.py",
           "return self.old != self.new", "return self.old is not None and self.old != self.new"),
    Mutant("inject accepts a missing begin marker", "ratchet/digest.py",
           "begin = _marker_index(text, BEGIN_MARKER, name)",
           "begin = text.find(BEGIN_MARKER) if BEGIN_MARKER in text else 0"),
    Mutant("--check writes the stale outputs", "ratchet/cli.py",
           "        status = _compile_check(stale, len(outputs), config.root)",
           "        for output in stale:\n            _write_text(output.path, output.new)\n"
           "        status = _compile_check(stale, len(outputs), config.root)"),
    Mutant("--check exits 0 when outputs are stale", "ratchet/cli.py",
           '    print("run `ratchet compile` to regenerate", file=sys.stderr)\n    return 1',
           '    print("run `ratchet compile` to regenerate", file=sys.stderr)\n    return 0'),
    Mutant("--check ignores stale injected blocks", "ratchet/cli.py",
           "stale = [output for output in outputs if output.is_stale]",
           'stale = [output for output in outputs if output.is_stale and output.kind == "digest"]'),
    Mutant("--dry-run writes", "ratchet/cli.py",
           '            print(f"[dry-run] compile: would write {display_path(output.path, config.root)} ({output.kind})")',
           '            print(f"[dry-run] compile: would write {display_path(output.path, config.root)} ({output.kind})")\n'
           "            _write_text(output.path, output.new)"),
    Mutant("--strict ignored", "ratchet/cli.py",
           "(args.strict and over_budget)", "(False and over_budget)"),
    Mutant("size budget measured in characters", "ratchet/cli.py",
           'digest_bytes = len(outputs[0].new.encode("utf-8"))', "digest_bytes = len(outputs[0].new)"),
)


def run_suite(cwd: Path, full: bool) -> tuple[bool, str]:
    """(passed, failing tests). Without `full`, -f stops at the first failure, which keeps a caught mutant cheap."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # a same-size, same-second edit must not hit a stale .pyc
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", *([] if full else ["-f"])]
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode == 0:
        return True, ""
    failures = [dotted for _, dotted in FAILURE_RE.findall(proc.stderr)]
    return False, ", ".join(failures) if failures else proc.stderr.strip().splitlines()[-1]


def main(full: bool) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        broken = [m.name for m in MUTANTS if (copy / m.path).read_text(encoding="utf-8").count(m.old) != 1]
        if broken:
            print(f"these mutants no longer match exactly one place in their file; update them: {broken}")
            return 2
        green, detail = run_suite(copy, full)
        if not green:
            print(f"the unmutated suite is red ({detail}); fix that first")
            return 2

        survivors: list[str] = []
        for number, mutant in enumerate(MUTANTS, start=1):
            target = copy / mutant.path
            original = target.read_text(encoding="utf-8")
            target.write_text(original.replace(mutant.old, mutant.new), encoding="utf-8")
            try:
                passed, detail = run_suite(copy, full)
            finally:
                target.write_text(original, encoding="utf-8")
            print(f"{'SURVIVED' if passed else 'killed  '}  {number:2d}. {mutant.name}" + ("" if passed else f"  <- {detail}"))
            if passed:
                survivors.append(mutant.name)

        green_again, detail = run_suite(copy, full)
        if not green_again:
            print(f"the suite is red after restoring every mutant ({detail})")
            return 2
        print(f"{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} mutants killed; suite green again after restoring the original")
        return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(full="--full" in sys.argv[1:]))
