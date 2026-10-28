"""Intentionally faulty review sample. Do not use for real configuration storage."""
import json
from pathlib import Path


def replace_config(path: Path, text: str) -> None:
    path.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")


def read_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
