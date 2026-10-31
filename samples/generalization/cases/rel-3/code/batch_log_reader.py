import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_checkpoint(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("checkpoint %s is not valid JSON, reading from the start: %s", path, exc)
        return 0
    lines = data.get("lines") if isinstance(data, dict) else None
    if type(lines) is not int or lines < 0:
        log.warning("checkpoint %s has an unexpected shape, reading from the start", path)
        return 0
    return lines


def save_checkpoint(path: Path, lines: int) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps({"lines": lines}), encoding="utf-8")
    os.replace(part, path)


def read_new_lines(log_path: Path, checkpoint_path: Path) -> list[str]:
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        rows = handle.readlines()
    if rows and not rows[-1].endswith("\n"):
        rows.pop()
    done = load_checkpoint(checkpoint_path)
    if done > len(rows):
        done = 0
    save_checkpoint(checkpoint_path, len(rows))
    return [row.rstrip("\n") for row in rows[done:]]
