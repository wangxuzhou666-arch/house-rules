"""A local, offline decision page. Findings are data, never HTML or executable instructions."""
from __future__ import annotations

import json
from pathlib import Path


def build_page(batch: dict) -> str:
    public = {key: batch[key] for key in ("id", "proposals", "evidence", "statistics")}
    payload = json.dumps(public, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = Path(__file__).with_name("proposal_page.html").read_text(encoding="utf-8")
    return template.replace("__BATCH_JSON__", payload)
