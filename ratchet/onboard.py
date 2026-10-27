"""`ratchet init`: wire a repo up in one idempotent pass.

Six steps, each reporting `add`, `update`, `ok` (already in place), `skip`, `manual` or `failed`: the config, the rules file,
the compiled digest, the CLAUDE.md import, two git hooks and the .gitignore entries. Nothing is overwritten:
files the tool owns are written only when absent, and edits to files the user owns (CLAUDE.md, .gitignore, hooks)
live in a marked block that a rerun finds and refreshes in place.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .blocks import HASH_BEGIN, HASH_END, HTML_BEGIN, HTML_END, split_block, upsert_block
from .config import CONFIG_NAME, Config, read_config_file
from .digest import Output, display_path, plan_outputs
from .errors import RatchetError
from .fsutil import is_executable, read_text, write_text
from .git import default_hooks_dir, hooks_dir, hooks_path_origin, uncommitted_paths
from .hooks import HOOK_NAMES, HookPlan, Pinned, plan_hook, probe_hook
from .outcomes import BATCH_NAME, PAGE_NAME
from .review import REVIEWS_SUBDIR
from .rules import load_rules
from .sources import DiskReader

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PACKAGE_ROOT / "templates" / "CODING-STYLE.template.md"
EXECUTABLE_PATH = PACKAGE_ROOT / "bin" / "ratchet"
TEMPLATE_TARGET = "CODING-STYLE.md"
CLAUDE_MD_HEADING = "## Code style digest (review-ratchet)"
COMMIT_MESSAGE = "chore: adopt review-ratchet"

_RULES_CANDIDATES = (TEMPLATE_TARGET, f"docs/{TEMPLATE_TARGET}")
STATUS_WORDS = {"add": ("added", "would add"), "update": ("updated", "would update"), "ok": ("already ok", "already ok"),
                "skip": ("skipped", "skipped"), "manual": ("MANUAL", "MANUAL"), "failed": ("FAILED", "FAILED")}


@dataclass(frozen=True)
class FileChange:
    path: Path
    old: str | None  # None: the file does not exist yet
    new: str


@dataclass(frozen=True)
class Step:
    name: str
    status: str  # a key of STATUS_WORDS
    detail: str  # may span lines (a MANUAL step shows what to paste)
    changes: tuple[FileChange, ...] = ()  # what a real run writes; --dry-run prints each as a diff
    notes: tuple[str, ...] = ()  # warnings shown under the step


@dataclass(frozen=True)
class InitOptions:
    dry_run: bool
    rules_file: str | None  # --rules-file
    use_template: bool  # --template
    install_hooks: bool  # false for --no-hooks
    edit_claude_md: bool  # false for --no-claude-md


@dataclass(frozen=True)
class InitResult:
    steps: list[Step]
    to_stage: list[str]  # repo-relative files a commit adopting the tool must contain; empty in a dry run


Ask = Callable[[str], str]  # prompt -> the answer line; None instead of an Ask means "do not ask, take the defaults"


# ---- step 1 + 2: config and rules file --------------------------------------------------------------------------

@dataclass(frozen=True)
class RulesChoice:
    rules_files: list[str]
    from_template: bool


def _bundled_template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RatchetError(f"the bundled rules template ({TEMPLATE_PATH}) is not in this copy of review-ratchet: a vendored copy made "
                           "before `ratchet vendor` included templates/ lacks it. Pass --rules-file PATH with your own rules file, "
                           "re-run `ratchet vendor`, or run init from a full checkout") from exc


def _detected_rules_file(root: Path) -> Path | None:
    return next((root / name for name in _RULES_CANDIDATES if (root / name).is_file()), None)


def _default_choice(root: Path) -> str:
    detected = _detected_rules_file(root)
    return "template" if detected is None else str(detected)


def _ask_rules_choice(root: Path, ask: Ask) -> str:
    default = _default_choice(root)
    shown = default if default == "template" else Path(default).relative_to(root).as_posix()
    answer = ask(f"Rules file: a path to your clause-labeled rules, or `template` for the bundled one [{shown}]: ").strip()
    return answer or default


def _rules_choice(root: Path, options: InitOptions, ask: Ask | None) -> RulesChoice:
    if options.rules_file is not None:
        choice = options.rules_file
    elif options.use_template:
        choice = "template"
    elif ask is not None:
        choice = _ask_rules_choice(root, ask)
    else:
        choice = _default_choice(root)
    if choice == "template":
        if not (root / TEMPLATE_TARGET).exists():
            _bundled_template()  # fail now, before the config step writes a rules_files entry that nothing can satisfy
        return RulesChoice([TEMPLATE_TARGET], from_template=True)
    path = Path(choice).expanduser().resolve()  # relative to the current directory, like every other path argument
    if not path.is_file():
        raise RatchetError(f"rules file not found: {choice}")
    if path.is_relative_to(root):
        return RulesChoice([path.relative_to(root).as_posix()], from_template=False)
    return RulesChoice([str(path)], from_template=False)


def _config_step(config: Config, options: InitOptions, ask: Ask | None) -> tuple[Step, RulesChoice]:
    path = config.root / CONFIG_NAME
    if config.rules_files:
        names = [display_path(rules_path, config.root) for rules_path in config.rules_files]
        return Step("config", "ok", f"{CONFIG_NAME} already lists rules_files {names}"), RulesChoice(names, from_template=False)
    raw = read_config_file(path) if path.exists() else {}
    choice = _rules_choice(config.root, options, ask)
    status = "update" if path.exists() else "add"
    new_text = json.dumps({**raw, "rules_files": choice.rules_files}, indent=2, ensure_ascii=False) + "\n"
    change = FileChange(path, read_text(path), new_text)
    if not options.dry_run:
        write_text(path, new_text)
    return Step("config", status, f"{CONFIG_NAME}: rules_files {choice.rules_files}", (change,)), choice


def _rules_step(config: Config, choice: RulesChoice, options: InitOptions) -> Step:
    target = config.root / choice.rules_files[0]
    if not choice.from_template:
        return Step("rules file", "ok", f"using {choice.rules_files[0]}")
    if target.exists():
        return Step("rules file", "ok", f"{choice.rules_files[0]} already exists; left untouched")
    if not options.dry_run:
        write_text(target, _bundled_template())
    return Step("rules file", "add", f"{choice.rules_files[0]} (copy of the bundled template; adjust the ADJUST comments)")


# ---- step 3: compile --------------------------------------------------------------------------------------------

class _PendingFileReader(DiskReader):
    """The working tree plus one file that a real run would have written by now (the template copy), so a dry run can
    render the digest it would produce."""

    def __init__(self, path: Path, content: bytes) -> None:
        self._path = path
        self._content = content

    def read(self, path: Path) -> bytes | None:
        found = super().read(path)
        return self._content if found is None and path == self._path else found


def _compile_detail(config: Config, stale: list[Output], digest_bytes: int) -> str:
    parts = [f"digest {display_path(output.path, config.root)}: {digest_bytes} bytes" if output.kind == "digest"
             else f"{display_path(output.path, config.root)} ({output.kind})" for output in stale]
    return ", ".join(parts)


def _compile_step(config: Config, choice: RulesChoice, options: InitOptions) -> tuple[Step, Config]:
    effective = dataclasses.replace(config, rules_files=tuple(config.root / name for name in choice.rules_files))
    reader = DiskReader()
    template_target = config.root / choice.rules_files[0]
    if options.dry_run and choice.from_template and not template_target.exists():  # the template is not copied yet
        reader = _PendingFileReader(template_target, _bundled_template().encode("utf-8"))
    rules = load_rules(effective, reader)
    outputs = plan_outputs(effective, rules, reader)
    stale = [output for output in outputs if output.is_stale]
    if not stale:
        return Step("compile", "ok", f"{display_path(effective.digest_path, effective.root)} is up to date"), effective
    if not options.dry_run:
        for output in stale:
            write_text(output.path, output.new)
    status = "add" if outputs[0].old is None else "update"
    changes = tuple(FileChange(output.path, output.old, output.new) for output in stale)
    return Step("compile", status, _compile_detail(effective, stale, len(outputs[0].new.encode("utf-8"))), changes), effective


# ---- step 4: CLAUDE.md ------------------------------------------------------------------------------------------

def _confirm(options: InitOptions, ask: Ask | None, question: str) -> bool:
    if ask is None:
        return True
    return ask(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def _claude_md_step(config: Config, options: InitOptions, ask: Ask | None) -> Step:
    if not options.edit_claude_md:
        return Step("CLAUDE.md", "skip", "--no-claude-md")
    path = config.root / "CLAUDE.md"
    import_line = f"@{display_path(config.digest_path, config.root)}"
    text = read_text(path)
    outside, has_block = split_block(text or "", HTML_BEGIN, HTML_END, "CLAUDE.md")
    if not has_block and any(line.strip() == import_line for line in outside):
        return Step("CLAUDE.md", "ok", f"{import_line} is already imported")
    new_text, status = upsert_block(text or "", HTML_BEGIN, HTML_END, [CLAUDE_MD_HEADING, import_line], "CLAUDE.md")
    if status == "ok":
        return Step("CLAUDE.md", "ok", f"{import_line} is already imported")
    if not _confirm(options, ask, f"Add `{import_line}` to CLAUDE.md?"):
        return Step("CLAUDE.md", "skip", "declined")
    if not options.dry_run:
        write_text(path, new_text)
    where = {"add": "in a new CLAUDE.md" if text is None else "appended at the end of CLAUDE.md", "update": "refreshed in place"}[status]
    return Step("CLAUDE.md", status, f"{import_line} under `{CLAUDE_MD_HEADING}`, {where} (Claude Code and /code-review read CLAUDE.md)",
                (FileChange(path, text, new_text),))


# ---- step 5: git hooks ------------------------------------------------------------------------------------------

def _is_portable(directory: Path, root: Path) -> bool:
    """A hooks directory inside the work tree is meant to be committed (`.git/hooks` is not tracked), so its block must hold
    no path of this machine."""
    return directory.is_relative_to(root) and not directory.is_relative_to(root / ".git")


def _hook_label(path: Path, root: Path) -> str:
    return display_path(path, root) if path.is_relative_to(root) else str(path)


def _shadowed_notes(root: Path, directory: Path) -> tuple[str, ...]:
    """core.hooksPath makes git ignore `.git/hooks`; say which executable hooks there silently stopped running."""
    default = default_hooks_dir(root)
    if directory == default or not default.is_dir():
        return ()
    names = sorted(entry.name for entry in default.iterdir()
                   if entry.is_file() and not entry.name.endswith(".sample") and is_executable(entry))
    if not names:
        return ()
    return (f"warning: core.hooksPath makes git use {_hook_label(directory, root)}, so these hooks in {_hook_label(default, root)} no "
            f"longer run: {', '.join(names)}. Move their logic into {_hook_label(directory, root)} or unset core.hooksPath",)


def _hook_detail(path: Path, config: Config, pinned: Pinned | None) -> str:
    detail = _hook_label(path, config.root)
    origin = hooks_path_origin(config.root)
    if origin is not None and not origin.is_relative_to(config.root / ".git"):
        detail += f" (core.hooksPath comes from {origin}, so every repo using that directory runs this hook; it skips repos without .ratchet.json)"
    if pinned is None:
        detail += " (portable: safe to commit; it finds ratchet through $RATCHET_BIN, `bin` in .ratchet.json, or PATH)"
        if EXECUTABLE_PATH.is_relative_to(config.root):
            detail += f'; this copy of ratchet is inside the repo, so set "bin": "{EXECUTABLE_PATH.relative_to(config.root).as_posix()}" in {CONFIG_NAME}'
        return detail
    if not EXECUTABLE_PATH.is_file():
        detail += f" WARNING: {EXECUTABLE_PATH} is missing"
    return detail + f" (runs with {pinned[0]})"


def _install_hook(config: Config, options: InitOptions, hook: str, path: Path, plan: HookPlan, pinned: Pinned | None,
                  notes: tuple[str, ...]) -> Step:
    name = f"hook {hook}"
    if plan.text is None:
        return Step(name, "manual", plan.note, notes=notes)
    changes = () if plan.status == "ok" else (FileChange(path, read_text(path), plan.text),)
    if changes and not options.dry_run:
        write_text(path, plan.text, make_executable=True)
    detail = _hook_detail(path, config, pinned)
    if options.dry_run:  # running the user's hook is not a read-only act, so a dry run only plans
        return Step(name, plan.status, detail, changes, notes)
    problem = probe_hook(path, hook, config.root)
    if problem is not None:
        return Step(name, "failed", problem, changes, notes)
    return Step(name, plan.status, f"{detail}; live check: the block ran", changes, notes)


def _hooks_steps(config: Config, directory: Path | None, options: InitOptions, ask: Ask | None) -> list[Step]:
    if directory is None:
        return [Step("git hooks", "skip", "--no-hooks")]
    pinned = None if _is_portable(directory, config.root) else (Path(sys.executable), EXECUTABLE_PATH)
    plans = {hook: plan_hook(directory / hook, hook, pinned) for hook in HOOK_NAMES}
    if any(plan.status != "ok" for plan in plans.values()) and not _confirm(
            options, ask, "Install git hooks (pre-commit: compile --check --staged; post-commit: infer)?"):
        return [Step("git hooks", "skip", "declined")]
    shadowed = _shadowed_notes(config.root, directory)
    return [_install_hook(config, options, hook, directory / hook, plan, pinned, shadowed if number == 0 else ())
            for number, (hook, plan) in enumerate(plans.items())]


# ---- step 6: .gitignore -----------------------------------------------------------------------------------------

def ignore_entries(config: Config) -> list[str]:
    runtime = [config.ledger_path, config.ledger_path.parent / PAGE_NAME, config.ledger_path.parent / BATCH_NAME]
    entries = [display_path(path, config.root) for path in runtime if path.is_relative_to(config.root)]
    return [*dict.fromkeys(entries), f"{REVIEWS_SUBDIR.as_posix()}/"]


def _gitignore_step(config: Config, options: InitOptions) -> Step:
    path = config.root / ".gitignore"
    text = read_text(path)
    present = {line.strip() for line in split_block(text or "", HASH_BEGIN, HASH_END, ".gitignore")[0]}
    wanted = [entry for entry in ignore_entries(config) if entry not in present and f"/{entry}" not in present]
    if not wanted:
        return Step(".gitignore", "ok", "runtime files are already ignored")
    new_text, status = upsert_block(text or "", HASH_BEGIN, HASH_END, wanted, ".gitignore")
    if status != "ok" and not options.dry_run:
        write_text(path, new_text)
    return Step(".gitignore", status, ", ".join(wanted), (FileChange(path, text, new_text),) if status != "ok" else ())


# ---- driver -----------------------------------------------------------------------------------------------------

def command_name() -> str:
    """How to type the tool in the next-steps lines: bare `ratchet` when that is this very executable on PATH."""
    found = shutil.which("ratchet")
    if found is not None and Path(found).resolve() == EXECUTABLE_PATH.resolve():
        return "ratchet"
    return shlex.quote(str(EXECUTABLE_PATH))


def next_steps(command: str) -> list[str]:
    return [
        f"next 1: {command} review --level high   (review the current changes; findings land in the ledger)",
        f"next 2: {command} count --by-rule        (see what is open, by clause)",
        f"next 3: {command} label-page             (label outcomes; a commit with a `Fixes-Finding: <id>` trailer closes a finding)",
    ]


def files_to_stage(config: Config, options: InitOptions, hook_files: list[Path]) -> list[str]:
    """Repo-relative paths of the files init manages that git sees as new or changed.

    CLAUDE.md, .gitignore and the hooks are shared with the user, so they count only when they carry init's marked block.
    """
    owned = [config.root / CONFIG_NAME, *config.rules_files, config.digest_path, *(target.path for target in config.inject_targets)]
    shared = [(config.root / "CLAUDE.md", HTML_BEGIN)] if options.edit_claude_md else []
    shared += [(config.root / ".gitignore", HASH_BEGIN), *((path, HASH_BEGIN) for path in hook_files)]
    present = [path for path in owned if path.is_file()] + [path for path, marker in shared if marker in (read_text(path) or "")]
    tracked_here = [Path(os.path.normpath(path)) for path in dict.fromkeys(present)]
    inside = [path for path in tracked_here if path.is_relative_to(config.root) and not path.is_relative_to(config.root / ".git")]
    return uncommitted_paths(config.root, [path.relative_to(config.root).as_posix() for path in inside])


def closing_lines(result: InitResult, options: InitOptions, command: str) -> list[str]:
    """What init prints last: the commit that adopts the tool, then either the next steps or, if nothing changed, one line."""
    commit: list[str] = []
    if result.to_stage:
        why = ("the pre-commit hook checks the staged copies, so until these are committed together every other commit is blocked"
               if options.install_hooks else "so the tool is configured for everyone")
        commit = [f"commit the files init created or changed, together ({why}):",
                  f'  git add {" ".join(shlex.quote(path) for path in result.to_stage)} && git commit -m "{COMMIT_MESSAGE}"']
    if all(step.status in ("ok", "skip") for step in result.steps):
        return ["review-ratchet is set up", *commit]
    return [*commit, *next_steps(command)]


def run_init(config: Config, options: InitOptions, ask: Ask | None, report: Callable[[Step], None]) -> InitResult:
    """Run the six steps in order, calling `report` after each. A step that cannot finish raises (earlier steps stay done and a
    rerun resumes); a hook that is installed but never runs is reported as `failed` and the remaining steps still run."""
    steps: list[Step] = []

    def finish(step: Step) -> None:
        steps.append(step)
        report(step)

    config_step, choice = _config_step(config, options, ask)
    finish(config_step)
    finish(_rules_step(config, choice, options))
    compile_step, effective = _compile_step(config, choice, options)
    finish(compile_step)
    finish(_claude_md_step(effective, options, ask))
    directory = hooks_dir(effective.root) if options.install_hooks else None
    for step in _hooks_steps(effective, directory, options, ask):
        finish(step)
    finish(_gitignore_step(effective, options))
    hook_files = [] if directory is None else [directory / hook for hook in HOOK_NAMES]
    return InitResult(steps, [] if options.dry_run else files_to_stage(effective, options, hook_files))
