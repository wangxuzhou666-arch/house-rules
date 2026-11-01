"""Behavioural ground truth for case rel-1b (run: python3.12 -I oracle.py).

Injects a failure in the middle of a replacement and checks whether usable state survives. The routes file is replaced
with a route that fails validation part-way; the sender index is rewritten with a write that fails part-way (disk full).
Exits 0 only if the routes file loses its earlier content for good and the sender index loses nothing that cannot be
rebuilt from its source.
"""
import contextlib
import errno
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
results = []

ROUTES = {
    1: [("billing.example", "Bills"), ("news.example", "News"), ("team.example", "Work")],
    2: [("billing.example", "Finance"), ("news.example", "Reading"), ("team.example", "Work")],
    3: [("billing.example", "Finance"), ("news.example", "Reading"), ("team.example", "Projects")],
}
SENDERS = {"billing.example": "billing@", "news.example": "digest@", "team.example": "lead@"}


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / "code" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(label, observed, expected):
    results.append(observed == expected)
    print(f"{'ok  ' if observed == expected else 'FAIL'} {label}: {observed} (expected {expected})")


def raises(call, error):
    try:
        call()
    except error:
        return True
    return False


def as_dicts(version):
    return [{"domain": domain, "folder": folder} for domain, folder in ROUTES[version]]


class FullDisk:
    def __init__(self, handle, writes):
        self.handle = handle
        self.left = writes

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self.handle.__exit__(*exc)

    def write(self, text):
        if self.left <= 0:
            raise OSError(errno.ENOSPC, "no space left on device")
        self.left -= 1
        return self.handle.write(text)


@contextlib.contextmanager
def failing_writes(after):
    original = Path.open

    def patched(self, mode="r", *args, **kwargs):
        handle = original(self, mode, *args, **kwargs)
        return FullDisk(handle, after) if "w" in mode else handle

    Path.open = patched
    try:
        yield
    finally:
        Path.open = original


def serves(lookup, version):
    return all(lookup(f"{SENDERS[domain]}{domain}") == folder for domain, folder in ROUTES[version])


def check_violation():
    routes = load("mail_routes")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "mail" / "routes.tsv"
        routes.apply_routes(path, as_dicts(1))
        fact("mail_routes: first apply serves its routes", serves(lambda a: routes.folder_for(path, a), 1), True)
        routes.apply_routes(path, as_dicts(2))
        fact("mail_routes: a clean re-apply replaces the routes", serves(lambda a: routes.folder_for(path, a), 2), True)
        failing = as_dicts(3)
        failing[1] = {"domain": "news", "folder": "Reading"}
        fact("mail_routes: an invalid route reaches the caller", raises(lambda: routes.apply_routes(path, failing), routes.RouteError), True)
        fact("mail_routes: earlier routes still served after the failed apply", serves(lambda a: routes.folder_for(path, a), 2), False)
        fact("mail_routes: the stored routes are still the complete earlier set after the failed apply",
             routes.load_routes(path) == dict(ROUTES[2]), False)
        fact("mail_routes: nothing else holds the earlier routes", sorted(p.name for p in path.parent.iterdir()), ["routes.tsv"])


def check_control():
    index_module = load("sender_index")
    state = {"version": 1, "down": False}

    def read_routes():
        if state["down"]:
            raise RuntimeError("source unavailable")
        return iter(ROUTES[state["version"]])

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "state" / "senders.idx"
        index = index_module.SenderIndex(path, read_routes)
        fact("sender_index: a missing index is built on demand and serves the current routes",
             serves(index.folder_for, 1), True)
        fact("sender_index: the built index is complete", path.read_text(encoding="utf-8").splitlines()[-1], "end\t3")
        state["version"] = 2
        index.rebuild()
        fact("sender_index: a clean rebuild replaces the entries", serves(index.folder_for, 2), True)

        before_outage = path.read_bytes()
        state["down"] = True
        fact("sender_index: lookups during a source outage are served from the index", serves(index.folder_for, 2), True)
        fact("sender_index: a rebuild whose source is unavailable reaches the caller", raises(index.rebuild, RuntimeError), True)
        fact("sender_index: a rebuild whose source is unavailable leaves the index file as it was", path.read_bytes() == before_outage, True)
        state["down"] = False

        state["version"] = 3
        with failing_writes(2):
            fact("sender_index: a write failing part-way reaches the caller", raises(index.rebuild, OSError), True)
        fact("sender_index: the failed rebuild left an incomplete index file",
             path.read_text(encoding="utf-8").splitlines()[-1].startswith("end"), False)
        fact("sender_index: the source of the entries is untouched", list(read_routes()) == ROUTES[3], True)
        fact("sender_index: every sender resolves with current content after the failed rebuild",
             serves(index.folder_for, 3), True)
        fact("sender_index: the next lookup rewrote a complete index", path.read_text(encoding="utf-8").splitlines()[-1], "end\t3")

        for label, blob in (("empty file", b""), ("binary content", b"\xff\xfe\x00\x81"),
                            ("entries without a closing line", b"billing.example\tOld\n"),
                            ("closing line with a wrong total", b"billing.example\tOld\nend\t7\n"),
                            ("line without a separator", b"billing.example\nend\t1\n")):
            path.write_bytes(blob)
            fact(f"sender_index: {label} is rebuilt from the source on the next lookup", serves(index.folder_for, 3), True)


check_violation()
check_control()
sys.exit(0 if all(results) else 1)
