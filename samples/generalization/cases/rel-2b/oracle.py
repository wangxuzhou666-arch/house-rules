"""Behavioural ground truth for case rel-2b (run: python3.12 -I oracle.py).

Each pair of files has an earlier version in base/ and the reviewed version in code/. A database is written with the earlier
version, then read and changed with the reviewed one. Exits 0 only if the payment ledger records its stored charges again
and cannot refund them, while the invoice register keeps the stored identity of every invoice and only displays it differently.
"""
import contextlib
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CHARGES = [("evt_1001", 1200), ("evt_1002", 800), ("evt_1003", 500)]
ORDERS = [("ORD-7001", "2026-03-02", 12000), ("ORD-7002", "2026-03-09", 8000), ("ORD-7003", "2026-04-01", 500)]
results = []


def load(folder, name):
    spec = importlib.util.spec_from_file_location(f"{folder}_{name}", HERE / folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(label, observed, expected):
    results.append(observed == expected)
    print(f"{'ok  ' if observed == expected else 'FAIL'} {label}: {observed} (expected {expected})")


def check_violation():
    before, after = load("base", "payment_events"), load("code", "payment_events")
    with tempfile.TemporaryDirectory() as folder:
        for name, module, args in (("base", before, ("stripe",)), ("code", after, ("acme", "stripe"))):
            with contextlib.closing(module.open_ledger(Path(folder) / f"fresh-{name}.db")) as conn:
                first = [module.record_charge(conn, *args, event, cents) for event, cents in CHARGES]
                second = [module.record_charge(conn, *args, event, cents) for event, cents in CHARGES]
                fact(f"payment_events ({name}): replaying the same charges into a fresh ledger records nothing the second time",
                     (first, second), ([True] * 3, [False] * 3))

        # a ledger written by the earlier version: three charges recorded, the second one refunded
        stored = Path(folder) / "stored.db"
        with contextlib.closing(before.open_ledger(stored)) as conn:
            for event, cents in CHARGES:
                before.record_charge(conn, "stripe", event, cents)
            before.record_refund(conn, "stripe", "evt_1002")
            fact("payment_events: the earlier version nets 1700 cents", before.net_cents(conn), 1700)
        refunding = Path(folder) / "refunding.db"
        shutil.copyfile(stored, refunding)
        with contextlib.closing(after.open_ledger(stored)) as conn:
            replay = [after.record_charge(conn, "acme", "stripe", event, cents) for event, cents in CHARGES]
            fact("payment_events: redelivered charges already in the old ledger are not recorded again", replay == [False] * 3, False)
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            fact("payment_events: the ledger still holds one row per charge", count == 3, False)
            fact("payment_events: the net amount is still 1700 cents after the redelivery", after.net_cents(conn) == 1700, False)
        with contextlib.closing(after.open_ledger(refunding)) as conn:
            fact("payment_events: a charge recorded by the earlier version can still be refunded",
                 after.record_refund(conn, "acme", "stripe", "evt_1001"), False)
            fact("payment_events: the net amount drops by the refund", after.net_cents(conn) == 500, False)


def check_control():
    before, after = load("base", "invoice_register"), load("code", "invoice_register")
    with tempfile.TemporaryDirectory() as folder:
        for name, module in (("base", before), ("code", after)):
            with contextlib.closing(module.open_register(Path(folder) / f"fresh-{name}.db")) as conn:
                first = [module.issue(conn, order, day, cents) is not None for order, day, cents in ORDERS]
                second = [module.issue(conn, order, day, cents) is not None for order, day, cents in ORDERS]
                fact(f"invoice_register ({name}): issuing the same orders twice into a fresh register adds nothing the second time",
                     (first, second), ([True] * 3, [False] * 3))

        # a register written by the earlier version: three invoices issued, the first one paid
        stored = Path(folder) / "stored.db"
        with contextlib.closing(before.open_register(stored)) as conn:
            for order, day, cents in ORDERS:
                before.issue(conn, order, day, cents)
            before.mark_paid(conn, "ORD-7001")
            fact("invoice_register: the earlier version lists the open invoices under its own labels",
                 before.outstanding(conn), ["INV-000002 80.00", "INV-000003 5.00"])
            identity = [tuple(row) for row in conn.execute("SELECT uid, order_ref, number FROM invoices ORDER BY number")]
        fact("invoice_register: both versions derive the same uid for every order",
             all(before.invoice_uid(order) == after.invoice_uid(order) for order, _, _ in ORDERS + [(" ord-7001 ", "", 0)]), True)
        with contextlib.closing(after.open_register(stored)) as conn:
            readded = [after.issue(conn, order, day, cents) for order, day, cents in ORDERS]
            fact("invoice_register: orders already in the old register are not issued again", readded, [None] * 3)
            fact("invoice_register: the register still holds one invoice per order",
                 conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 3)
            fact("invoice_register: an invoice stored by the earlier version can still be closed", after.mark_paid(conn, "ORD-7002"), True)
            fact("invoice_register: the invoice that was already paid stays closed", after.mark_paid(conn, "ORD-7001"), False)
            fact("invoice_register: stored uid, order and number of every invoice are unchanged",
                 [tuple(row) for row in conn.execute("SELECT uid, order_ref, number FROM invoices ORDER BY number")], identity)
            fact("invoice_register: old invoices are displayed in the new label style",
                 after.outstanding(conn), ["2026/0003 ORD-7003 5.00"])
            fact("invoice_register: a new order continues the numbering", after.issue(conn, "ORD-7004", "2026-05-11", 900), "2026/0004")


check_violation()
check_control()
sys.exit(0 if all(results) else 1)
