"""Single-writer sample: prepare a valid JSON object before replacing a regular file."""
import json
import os
import stat
import tempfile
from pathlib import Path


def parse_config(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return value


def replace_config(path: Path, text: str) -> None:
    parse_config(text)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        prepared = Path(folder) / "config.json"
        prepared.write_text(text, encoding="utf-8")
        prepared.chmod(mode)
        os.replace(prepared, path)


def read_config(path: Path) -> dict:
    return parse_config(path.read_text(encoding="utf-8"))
