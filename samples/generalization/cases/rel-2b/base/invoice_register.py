import hashlib
import sqlite3
from datetime import date
from pathlib import Path


def invoice_uid(order_ref: str) -> str:
    return hashlib.sha256(order_ref.strip().upper().encode("utf-8")).hexdigest()[:20]


def invoice_label(row: sqlite3.Row) -> str:
    return f"INV-{row['number']:06d}"


def open_register(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invoices ("
        "uid TEXT PRIMARY KEY, order_ref TEXT NOT NULL, number INTEGER NOT NULL, "
        "issued_on TEXT NOT NULL, cents INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def issue(conn: sqlite3.Connection, order_ref: str, issued_on: str, cents: int) -> str | None:
    uid = invoice_uid(order_ref)
    day = date.fromisoformat(issued_on).isoformat()
    number = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM invoices").fetchone()[0]
    cur = conn.execute(
        "INSERT OR IGNORE INTO invoices (uid, order_ref, number, issued_on, cents) VALUES (?, ?, ?, ?, ?)",
        (uid, order_ref.strip(), number, day, cents),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return invoice_label(conn.execute("SELECT * FROM invoices WHERE uid = ?", (uid,)).fetchone())


def mark_paid(conn: sqlite3.Connection, order_ref: str) -> bool:
    cur = conn.execute("UPDATE invoices SET paid = 1 WHERE uid = ? AND paid = 0", (invoice_uid(order_ref),))
    conn.commit()
    return cur.rowcount == 1


def outstanding(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT * FROM invoices WHERE paid = 0 ORDER BY number").fetchall()
    return [f"{invoice_label(row)} {row['cents'] / 100:.2f}" for row in rows]
