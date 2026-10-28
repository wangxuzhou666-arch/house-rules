"""Single-writer transfer example: serialization finishes before replacing a file."""
import json
import os
import stat
import tempfile
from pathlib import Path


def store_settings(path: Path, values: dict) -> None:
    payload = json.dumps(values)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        prepared = Path(folder) / "settings.json"
        prepared.write_text(payload, encoding="utf-8")
        prepared.chmod(mode)
        os.replace(prepared, path)
