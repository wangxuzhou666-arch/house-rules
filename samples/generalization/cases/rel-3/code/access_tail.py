import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_position(path: Path) -> dict:
    fresh = {"inode": None, "offset": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fresh
    except json.JSONDecodeError as exc:
        log.warning("position file %s is not valid JSON, reading from the start: %s", path, exc)
        return fresh


def save_position(path: Path, inode: int, offset: int) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps({"inode": inode, "offset": offset}), encoding="utf-8")
    os.replace(part, path)


def read_new_lines(log_path: Path, position_path: Path) -> list[str]:
    position = load_position(position_path)
    info = log_path.stat()
    start = position["offset"] if position["inode"] == info.st_ino else 0
    if start > info.st_size:
        start = 0
    with log_path.open("rb") as handle:
        handle.seek(start)
        chunk = handle.read()
    complete = chunk[: chunk.rfind(b"\n") + 1]
    save_position(position_path, info.st_ino, start + len(complete))
    return complete.decode("utf-8", errors="replace").splitlines()
