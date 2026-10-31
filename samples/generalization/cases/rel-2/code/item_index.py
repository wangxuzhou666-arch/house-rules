import json
from pathlib import Path

KEY_VERSION = 2
FIELDS = {"key", "key_version", "title", "read"}


class IndexFileError(Exception):
    pass


def feed_host(feed_url: str) -> str:
    return feed_url.split("//", 1)[-1].split("/", 1)[0].lower()


def item_key(feed_url: str, guid: str) -> str:
    return f"{feed_host(feed_url)}|{guid.strip()}"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IndexFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) and FIELDS <= row.keys() for row in rows):
        raise IndexFileError(f"{path} does not hold a list of item rows")
    return rows


def save_rows(path: Path, rows: list[dict]) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    part.replace(path)


def import_items(path: Path, feed_url: str, items: list[tuple[str, str]]) -> list[str]:
    rows = load_rows(path)
    seen = {row["key"] for row in rows if row["key_version"] == KEY_VERSION}
    added = []
    for guid, title in items:
        key = item_key(feed_url, guid)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"key": key, "key_version": KEY_VERSION, "title": title, "read": False})
        added.append(key)
    save_rows(path, rows)
    return added


def mark_read(path: Path, feed_url: str, guid: str) -> bool:
    key = item_key(feed_url, guid)
    rows = load_rows(path)
    for row in rows:
        if row["key"] == key and row["key_version"] == KEY_VERSION:
            row["read"] = True
            save_rows(path, rows)
            return True
    return False


def unread_titles(path: Path) -> list[str]:
    return [row["title"] for row in load_rows(path) if not row["read"]]
