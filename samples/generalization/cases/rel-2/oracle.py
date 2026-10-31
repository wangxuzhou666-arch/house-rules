"""Behavioural ground truth for case rel-2 (run: python3.12 -I oracle.py).

Each pair of files has an earlier version in base/ and the reviewed version in code/. A store is written with the earlier
version, then read and changed with the reviewed one. Exits 0 only if the item index re-adds the stored items and leaves
their old rows open, while the reading list keeps every stored item's identity.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
FEED = "https://News.Example.org/rss"
ITEMS = [("a1", "First"), ("a2", "Second"), ("a3", "Third")]
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
    before, after = load("base", "item_index"), load("code", "item_index")
    with tempfile.TemporaryDirectory() as folder:
        for name, module in (("base", before), ("code", after)):
            fresh = Path(folder) / f"fresh-{name}.json"
            module.import_items(fresh, FEED, ITEMS)
            fact(f"item_index ({name}): importing the same items twice into a fresh store adds nothing the second time",
                 module.import_items(fresh, FEED, ITEMS) == [], True)

        # a store written by the earlier version: two items imported, the first one read
        stored = Path(folder) / "stored.json"
        before.import_items(stored, FEED, ITEMS[:2])
        before.mark_read(stored, FEED, "a1")
        fact("item_index: the earlier version leaves 'Second' unread", before.unread_titles(stored), ["Second"])
        added = after.import_items(stored, FEED, ITEMS)
        fact("item_index: items already in the old store are not added again", len(added) == 1, False)
        titles = [row["title"] for row in after.load_rows(stored)]
        fact("item_index: only one row is stored for 'First'", titles.count("First") == 1, False)
        closed = after.mark_read(stored, FEED, "a2")
        fact("item_index: no unread 'Second' row remains after marking it read",
             closed and "Second" not in after.unread_titles(stored), False)


def check_control():
    before, after = load("base", "reading_list"), load("code", "reading_list")
    with tempfile.TemporaryDirectory() as folder:
        for name, module in (("base", before), ("code", after)):
            fresh = Path(folder) / f"fresh-{name}.json"
            module.add_items(fresh, FEED, ITEMS)
            fact(f"reading_list ({name}): importing the same items twice into a fresh store adds nothing the second time",
                 module.add_items(fresh, FEED, ITEMS) == [], True)

        # a store written by the earlier version, whose labels were drawn in style 1: two items imported, the first one read
        host = after.feed_host(FEED)
        stored = Path(folder) / "stored.json"
        before.add_items(stored, FEED, ITEMS[:2])
        before.mark_read(stored, FEED, "a1")
        fact("reading_list: the earlier version lists 'Second' in its own style", before.listing(stored), [f"{host}-0002 Second"])
        added = after.add_items(stored, FEED, ITEMS)
        fact("reading_list: items already in the old store are not added again", len(added) == 1, True)
        titles = [row["title"] for row in after.load_rows(stored)]
        fact("reading_list: only one row is stored for 'First'", titles.count("First") == 1, True)
        fact("reading_list: old rows keep their label style, new rows use the new one",
             after.listing(stored), [f"{host}-0002 Second", f"{host}#3 Third"])
        closed = after.mark_read(stored, FEED, "a2")
        fact("reading_list: no unread 'Second' row remains after marking it read",
             closed and after.listing(stored) == [f"{host}#3 Third"], True)


check_violation()
check_control()
sys.exit(0 if all(results) else 1)
