"""Intentionally faulty transfer example: serialization can fail after truncation."""
import json
from pathlib import Path


def store_settings(path: Path, values: dict) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(values, stream)
