"""Offline clean-install acceptance. Synthetic reviews; never invents human approval.

Run with --output-dir pointing to a new scratch directory. It retains the copied
distribution, fixture repositories and command results for inspection.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = ("ratchet", "bin", "schemas", "templates", "skills", ".claude-plugin", "docs", "samples", "scripts", "LICENSE", "README.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.absolute()
    if output.exists():
        parser.error("output directory must not exist; acceptance never overwrites previous evidence")
    output.mkdir(parents=True)
    tool = output / "distribution"
    tool.mkdir()
    for name in PAYLOAD:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, tool / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"))
        else:
            shutil.copy2(source, tool / name)
    repo = output / "own-style-repo"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="Synthetic acceptance", GIT_AUTHOR_EMAIL="acceptance@example.invalid",
               GIT_COMMITTER_NAME="Synthetic acceptance", GIT_COMMITTER_EMAIL="acceptance@example.invalid",
               PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
    commands = []

    def run(argv, cwd=repo, expected=0):
        proc = subprocess.run([str(x) for x in argv], cwd=cwd, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
        commands.append({"argv": [str(x) for x in argv], "cwd": str(cwd), "exit": proc.returncode,
                         "stdout": proc.stdout, "stderr": proc.stderr})
        (output / "commands.json").write_text(json.dumps(commands, indent=2))
        if proc.returncode != expected:
            raise RuntimeError(f"unexpected exit {proc.returncode} for {argv}: {proc.stderr}")
        return proc.stdout

    def ratchet(*argv):
        return [sys.executable, "-B", tool / "bin/ratchet", *argv]

    run(["git", "init", "-q", "-b", "main"])
    sample = tool / "samples/config-replacement"
    shutil.copy2(sample / "CODING-STYLE.md", repo / "MY-STYLE.md")
    shutil.copy2(sample / "safe_config.py", repo / "app.py")
    run(ratchet("init", "--dry-run", "--rules-file", "MY-STYLE.md"))
    assert not (repo / ".ratchet.json").exists()
    run(ratchet("init", "--yes", "--rules-file", "MY-STYLE.md"))
    run(ratchet("compile", "--check", "--strict"))
    assert "REL.1 | should-fix" in (repo / "CODING-STYLE.digest.md").read_text()
    assert "P0E.1" not in (repo / "CODING-STYLE.digest.md").read_text()
    run(["git", "add", "."])
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic baseline with own style"])
    shutil.copy2(sample / "unsafe_config.py", repo / "app.py")
    lines = (repo / "app.py").read_text().splitlines()
    review = [{"file": "app.py", "line": lines.index("    path.unlink(missing_ok=True)") + 1,
               "summary": "Synthetic review: deleting the old config before writing loses usable state.",
               "failure_scenario": "Writing fails after deletion, leaving no usable config.",
               "category": "rel-1", "severity": "should-fix"}]
    report = output / "synthetic-review.json"
    report.write_text(json.dumps(review))
    run(ratchet("review", "--from-file", report, "--run", "install-r1"))
    ledger_path = repo / ".style-findings.jsonl"
    before_replay = ledger_path.read_bytes()
    run(ratchet("review", "--from-file", report, "--run", "install-r1"))
    assert ledger_path.read_bytes() == before_replay
    context = json.loads(run(ratchet("decision-context")))
    finding_id = context["findings"][0]["finding"]["id"]
    assessment = {"v": 1, "context_id": context["context_id"], "assessments": [{
        "finding_ids": [finding_id], "verification": {"status": "supported", "basis": "Synthetic failure/control sample."},
        "action": "covered", "rule_ids": ["REL.1"], "reason": "The configured rule already covers loss on replacement.",
        "changes": []}]}
    assessment_file = output / "assessment.json"
    assessment_file.write_text(json.dumps(assessment))
    prepared = json.loads(run(ratchet("prepare-decisions", "--assessment", assessment_file)))
    batch_dir = repo / ".ratchet/proposals" / prepared["batch_id"]
    assert (batch_dir / "batch.json").exists() and not (batch_dir / "receipt.json").exists()
    # There was no human decision in this offline run, so never manufacture a receipt.
    shutil.copy2(sample / "safe_config.py", repo / "app.py")
    with (repo / "MY-STYLE.md").open("a") as handle:
        handle.write("\nSynthetic acceptance: changed source hash.\n")
    run(["git", "add", "app.py", "MY-STYLE.md"])
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "Must fail: stale staged digest"], expected=1)
    run(ratchet("compile"))
    run(["git", "add", "CODING-STYLE.digest.md"])
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm",
         f"Restore safe synthetic implementation\n\nFixes-Finding: {finding_id}"])
    counts = run(ratchet("count"))
    assert "1 total, 0 open" in counts
    run(ratchet("compile", "--check", "--strict"))
    bundled = output / "bundled-style-repo"
    bundled.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=bundled)
    run(ratchet("init", "--yes", "--template", "--no-hooks"), cwd=bundled)
    run(ratchet("compile", "--check", "--strict"), cwd=bundled)
    run([sys.executable, "-B", sample / "check_examples.py"])
    result = {"status": "passed", "kind": "offline_synthetic_acceptance", "human_decision": "not exercised",
              "checks": ["copied distribution runs independently", "own style and bundled template",
                         "hook probes", "review import and idempotent replay", "evidence and proposal preparation",
                         "staged stale digest rejected", "fixing commit closes finding", "failure/control samples"]}
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
