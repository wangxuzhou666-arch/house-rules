import sqlite3
from pathlib import Path


def event_key(account: str, provider: str, event_id: str) -> str:
    return f"{account}/{provider.lower()}/{event_id.strip()}"


def open_ledger(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "key TEXT PRIMARY KEY, kind TEXT NOT NULL, cents INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def record_charge(conn: sqlite3.Connection, account: str, provider: str, event_id: str, cents: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (key, kind, cents) VALUES (?, 'charge', ?)",
        (event_key(account, provider, event_id), cents),
    )
    conn.commit()
    return cur.rowcount == 1


def record_refund(conn: sqlite3.Connection, account: str, provider: str, charge_id: str) -> bool:
    cur = conn.execute(
        "UPDATE events SET refunded = 1 WHERE key = ? AND refunded = 0",
        (event_key(account, provider, charge_id),),
    )
    conn.commit()
    return cur.rowcount == 1


def net_cents(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(cents), 0) FROM events WHERE refunded = 0").fetchone()[0]
