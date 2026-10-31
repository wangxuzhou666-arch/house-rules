"""Behavioural ground truth for case rel-1 (run: python3.12 -I oracle.py).

Injects a crash in the middle of a replacement and checks whether usable state
survives. Exits 0 only if the violation loses its state and the control does not.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
results = []


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


def crash():
    raise RuntimeError("injected crash")


def check_violation():
    site = load("site_publisher")
    with tempfile.TemporaryDirectory() as folder:
        live = Path(folder) / "live"
        first = {"index.html": lambda: "home v1", "about/team.html": lambda: "team v1"}
        site.publish(live, first)
        fact("site_publisher: first publish serves its pages",
             site.read_page(live, "index.html") == "home v1" and site.read_page(live, "about/team.html") == "team v1", True)
        second = {"index.html": lambda: "home v2", "about/team.html": lambda: "team v2"}
        site.publish(live, second)
        fact("site_publisher: a clean republish replaces the pages",
             site.read_page(live, "index.html") == "home v2" and site.read_page(live, "about/team.html") == "team v2", True)
        failing = {"index.html": lambda: "home v3", "about/team.html": crash}
        fact("site_publisher: injected render failure reaches the caller",
             raises(lambda: site.publish(live, failing), RuntimeError), True)
        try:
            served = site.read_page(live, "index.html") == "home v2" and site.read_page(live, "about/team.html") == "team v2"
        except OSError:
            served = False
        fact("site_publisher: earlier pages still served after the failed publish", served, False)
        fact("site_publisher: page list is still complete after the failed publish",
             site.page_names(live) == ["about/team.html", "index.html"], False)


def check_control():
    previews = load("preview_cache")
    state = {"version": 1, "fail_on": None}

    def render(name):
        if name == state["fail_on"]:
            crash()
        return f"{name}:v{state['version']}".encode()

    names = ["a", "b", "c"]
    with tempfile.TemporaryDirectory() as folder:
        cache = previews.PreviewCache(Path(folder) / "previews", render)
        cache.rebuild(names)
        fact("preview_cache: first build serves current previews", [cache.get(n) for n in names] == [b"a:v1", b"b:v1", b"c:v1"], True)
        state["version"] = 2
        cache.rebuild(names)
        fact("preview_cache: a clean rebuild replaces the previews", [cache.get(n) for n in names] == [b"a:v2", b"b:v2", b"c:v2"], True)
        state["version"], state["fail_on"] = 3, "b"
        fact("preview_cache: injected render failure reaches the caller",
             raises(lambda: cache.rebuild(names), RuntimeError), True)
        state["fail_on"] = None
        try:
            obtainable = [cache.get(n) for n in names] == [b"a:v3", b"b:v3", b"c:v3"]
        except OSError:
            obtainable = False
        fact("preview_cache: every preview is obtainable with current content after the failed rebuild", obtainable, True)


check_violation()
check_control()
sys.exit(0 if all(results) else 1)
