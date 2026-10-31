import json
from pathlib import Path

LABEL_STYLE = 2
FIELDS = {"uid", "feed", "number", "label_style", "title", "read"}


class ListFileError(Exception):
    pass


def feed_host(feed_url: str) -> str:
    return feed_url.split("//", 1)[-1].split("/", 1)[0].lower()


def make_uid(feed_url: str, guid: str) -> str:
    return f"{feed_host(feed_url)}|{guid.strip()}"


def label(row: dict) -> str:
    if row["label_style"] == 1:
        return f"{row['feed']}-{row['number']:04d}"
    return f"{row['feed']}#{row['number']}"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ListFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) and FIELDS <= row.keys() for row in rows):
        raise ListFileError(f"{path} does not hold a list of item rows")
    return rows


def save_rows(path: Path, rows: list[dict]) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    part.replace(path)


def add_items(path: Path, feed_url: str, items: list[tuple[str, str]]) -> list[str]:
    rows = load_rows(path)
    host = feed_host(feed_url)
    seen = {row["uid"] for row in rows}
    number = max((row["number"] for row in rows if row["feed"] == host), default=0)
    added = []
    for guid, title in items:
        uid = make_uid(feed_url, guid)
        if uid in seen:
            continue
        seen.add(uid)
        number += 1
        row = {"uid": uid, "feed": host, "number": number, "label_style": LABEL_STYLE, "title": title, "read": False}
        rows.append(row)
        added.append(label(row))
    save_rows(path, rows)
    return added


def mark_read(path: Path, feed_url: str, guid: str) -> bool:
    uid = make_uid(feed_url, guid)
    rows = load_rows(path)
    for row in rows:
        if row["uid"] == uid:
            row["read"] = True
            save_rows(path, rows)
            return True
    return False


def listing(path: Path) -> list[str]:
    return [f"{label(row)} {row['title']}" for row in load_rows(path) if not row["read"]]
