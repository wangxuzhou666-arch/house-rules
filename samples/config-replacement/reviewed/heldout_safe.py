"""Single-writer transfer example: serialization finishes before replacing a file."""
import json
import os
import tempfile
from pathlib import Path


def store_settings(path: Path, values: dict) -> None:
    payload = json.dumps(values)
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        prepared = Path(folder) / "settings.json"
        prepared.write_text(payload, encoding="utf-8")
        os.replace(prepared, path)
