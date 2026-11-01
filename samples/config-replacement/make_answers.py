#!/usr/bin/env python3
"""Write the two JSON files of the decision step for the recorded review of this sample, with no model and no person.

In real use a model writes the assessment (does a finding warrant a rule, and which?) and a person answers the question that
follows. The workshop has neither, so this script plays both parts for the two findings of `recorded-review.txt`, and fills in
the ids that differ per repository (`context_id`, the finding ids, `batch_id`):

    house-rules decision-context > context.json
    python3 make_answers.py assessment context.json > assessment.json
    house-rules prepare-decisions --assessment assessment.json > prepared.json
    python3 make_answers.py answers prepared.json > answers.json
    house-rules apply-decisions --input answers.json

What it decides is fixed: the finding in heldout_unsafe.py becomes a new clause REL.1 (accepted), the permission-bit finding
in config_store.py becomes "keep" (deferred: one bug, not worth a rule). Edit ASSESSMENTS and ANSWERS in this file to play it
differently. It refuses any context other than the one this recorded review makes, because it would otherwise pin a verdict on
the wrong finding. Standard library only.
"""
import argparse
import json
import sys
from pathlib import Path

REL1 = (
    "- **[REL.1]** Preserve the existing usable configuration until its replacement is prepared and validated; "
    "a failed replacement must retain or restore usable state. (should-fix) "
    "<!-- digest: Preserve usable configuration until replacement is prepared and validated; "
    "retain or restore usable state on failure -->\n"
    "  Scope: ordinary local configuration files, one writer, no concurrent replacement."
)

# The model's part: what to say about each finding of the recorded review, by the file the finding names.
ASSESSMENTS = {
    "heldout_unsafe.py": {
        "action": "add",
        "basis": "Code inspection: open('w') truncates the file before json.dump runs.",
        "reason": "A reusable gap: no clause says to keep the old state until the replacement is ready.",
        "changes": [{"action": "add", "rule_id": "REL.1", "file": "CODING-STYLE.md", "after": REL1}],
    },
    "config_store.py": {
        "action": "keep",
        "basis": "Code inspection: os.replace swaps in a file with the default mode.",
        "reason": "A specific bug without a general rule worth the digest bytes.",
        "changes": [],
    },
}

# The person's part: what to answer for a group, by the action the model proposed for it.
ANSWERS = {
    "add": ("accept", "Yes, add REL.1 as written."),
    "keep": ("defer", "Not now; one bug is not a rule."),
}


class Refusal(Exception):
    """The input is not what the workshop step produces; the message says what to check."""


def read_json(path: str) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise Refusal(f"cannot read {path}: {error.strerror or error}") from error
    except ValueError as error:
        raise Refusal(f"{path} is not valid JSON ({error}); did the command that wrote it fail?") from error
    if not isinstance(document, dict):
        raise Refusal(f"{path} is not a JSON object")
    return document


def require(document: dict, key: str, kind: type, where: str):
    value = document.get(key)
    if not isinstance(value, kind) or not value:
        raise Refusal(f"{where} has no usable `{key}`")
    return value


def make_assessment(context: dict) -> dict:
    context_id = require(context, "context_id", str, "the context")
    entries = require(context, "findings", list, "the context")
    ids_by_file = {}
    for entry in entries:
        finding = entry.get("finding") if isinstance(entry, dict) else None
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str) or not isinstance(finding.get("file"), str):
            raise Refusal("a finding in the context lacks `id` or `file`")
        ids_by_file.setdefault(finding["file"], []).append(finding)
    if sorted(ids_by_file) != sorted(ASSESSMENTS) or any(len(found) != 1 for found in ids_by_file.values()):
        found = ", ".join(sorted(f"{item['file']}:{item['line']}" for found in ids_by_file.values() for item in found)) or "none"
        raise Refusal("this helper knows only the two findings of recorded-review.txt, one in heldout_unsafe.py and one in "
                      f"config_store.py; the context has: {found}")
    first = ids_by_file["heldout_unsafe.py"][0]
    if first.get("rule_id") != "unmapped":
        raise Refusal(f"the finding in heldout_unsafe.py is already labeled {first.get('rule_id')}, so there is no blind spot to "
                      "propose a rule for; start from the sample rules without REL.1 (workshop section 1)")
    assessments = []
    for name, said in ASSESSMENTS.items():
        assessments.append({
            "finding_ids": [ids_by_file[name][0]["id"]],
            "verification": {"status": "supported", "basis": said["basis"]},
            "action": said["action"],
            "rule_ids": [],  # for `add` the list must stay empty: it names clauses that already exist
            "reason": said["reason"],
            "changes": said["changes"],
        })
    return {"v": 1, "context_id": context_id, "assessments": assessments}


def make_answers(prepared: dict, confirmed_by: str) -> dict:
    if prepared.get("status") != "prepared":
        raise Refusal(f"the batch status is {prepared.get('status')!r}, not 'prepared'; use the output of prepare-decisions")
    batch_id = require(prepared, "batch_id", str, "the batch")
    groups = require(prepared, "groups", list, "the batch")
    answers = []
    for group in groups:
        action = group.get("assessment", {}).get("action") if isinstance(group, dict) else None
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or action not in ANSWERS:
            raise Refusal(f"a group in the batch has no answer to give (action {action!r}); this helper answers add and keep")
        decision, response = ANSWERS[action]
        answers.append({"id": group["id"], "decision": decision, "response": response})
    return {"v": 1, "batch_id": batch_id, "confirmed_by": confirmed_by, "answers": answers}


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="make_answers.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Both commands print JSON on stdout; redirect it into a file. Exit status 2 means the input was refused.")
    commands = parser.add_subparsers(dest="command", required=True)
    assessment = commands.add_parser("assessment", help="write the assessment (the model's part) from `decision-context` output")
    assessment.add_argument("context", help="a file holding the output of `house-rules decision-context`")
    answers = commands.add_parser("answers", help="write the answers (the person's part) from `prepare-decisions` output")
    answers.add_argument("prepared", help="a file holding the output of `house-rules prepare-decisions --assessment ...`")
    answers.add_argument("--confirmed-by", default="workshop learner",
                         help="the name recorded next to the decision in CODING-STYLE.changelog.md (default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    arguments = parse_arguments(argv)
    try:
        if arguments.command == "assessment":
            made = make_assessment(read_json(arguments.context))
        else:
            made = make_answers(read_json(arguments.prepared), arguments.confirmed_by)
    except Refusal as refusal:
        print(f"make_answers.py: error: {refusal}", file=sys.stderr)
        return 2
    json.dump(made, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
