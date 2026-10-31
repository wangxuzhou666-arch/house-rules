"""Behavioural ground truth for case rel-3b (run: python3.12 -I oracle.py).

Same violation file as rel-3; the control also takes the fallback for a state file that cannot be decoded and for text the
JSON parser refuses with an error other than a syntax error. Leaves a bad state file (undecodable bytes, a parser limit, a
wrong shape, wrong field types, empty, truncated, ...) next to a log and runs the reader twice. Exits 0 only if the violation
fails on every run for each kind it does not guard, and the control treats every kind like a syntax error.
"""
import importlib.util
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.set_int_max_str_digits(4300)  # the parser's default limit, whatever the environment says
logging.disable(logging.CRITICAL)
HERE = Path(__file__).resolve().parent
LINES = ["l1", "l2", "l3"]
results = []

DEEP = b"[" * 100000 + b"]" * 100000
DIGITS = b"9" * 5000


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / "code" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(label, observed, expected):
    results.append(observed == expected)
    print(f"{'ok  ' if observed == expected else 'FAIL'} {label}: {observed} (expected {expected})")


def state_bytes(state, log_path):
    data = state.encode("utf-8") if isinstance(state, str) else state
    return data.replace(b"INO", str(log_path.stat().st_ino).encode("ascii"))


def recovers(reader, state):
    """True if the first run returns every line, repairs the state file, and the next run sees nothing new."""
    with tempfile.TemporaryDirectory() as folder:
        log_path, state_path = Path(folder) / "app.log", Path(folder) / "state.json"
        log_path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        state_path.write_bytes(state_bytes(state, log_path))
        try:
            first = reader(log_path, state_path)
            repaired = isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict)
            second = reader(log_path, state_path)
        except Exception:
            return False
        return first == LINES and repaired and second == []


def fails_every_run(reader, state):
    """True if two runs in a row raise and the state file is left exactly as it was."""
    with tempfile.TemporaryDirectory() as folder:
        log_path, state_path = Path(folder) / "app.log", Path(folder) / "state.json"
        log_path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        content = state_bytes(state, log_path)
        state_path.write_bytes(content)
        outcomes = []
        for _ in range(2):
            try:
                reader(log_path, state_path)
                outcomes.append(False)
            except Exception:
                outcomes.append(True)
        return outcomes == [True, True] and state_path.read_bytes() == content


def describe(state):
    text = state.decode("utf-8", errors="backslashreplace") if isinstance(state, bytes) else state
    return text if len(text) <= 40 else f"{text[:20]}...({len(text)} chars)"


def check(module_name, own_shapes, expect_unguarded_fail):
    """own_shapes: states valid as JSON text but wrong for this module's fields. expect_unguarded_fail: the module has no
    guard beyond the syntax error (the violation), so every kind but a syntax error must fail on every run."""
    module = load(module_name)
    reader = module.read_new_lines
    with tempfile.TemporaryDirectory() as folder:
        log_path, state_path = Path(folder) / "app.log", Path(folder) / "state.json"
        log_path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        first = reader(log_path, state_path)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("l4\n")
        fact(f"{module_name}: with no state file, a first run returns every line and a later run only the new one",
             (first, reader(log_path, state_path), reader(log_path, state_path)), (LINES, ["l4"], []))
    fact(f"{module_name}: state file that is not valid JSON takes the fallback and is repaired", recovers(reader, "{not json"), True)
    for state in own_shapes:
        if expect_unguarded_fail:
            fact(f"{module_name}: valid JSON {describe(state)} fails on every run and is never repaired", fails_every_run(reader, state), True)
        else:
            fact(f"{module_name}: valid JSON {describe(state)} takes the same fallback and is repaired", recovers(reader, state), True)
    for kind, state in SYNTAX_KINDS:
        fact(f"{module_name}: {kind} takes the fallback and is repaired", recovers(reader, state), True)
    for kind, state in SHARED_KINDS:
        if expect_unguarded_fail:
            fact(f"{module_name}: {kind} fails on every run and is never repaired", fails_every_run(reader, state), True)
        else:
            fact(f"{module_name}: {kind} takes the same fallback and is repaired", recovers(reader, state), True)


# Kinds the JSON parser reports as a syntax error: both files guard these.
SYNTAX_KINDS = [
    ("an empty state file", b""),
    ("a state file of only whitespace", b" \n\t\n"),
    ("a state file of NUL bytes", b"\x00\x00\x00\x00"),
    ("JSON cut off in the middle", b'{"inode": INO, "offset": 3, "lines": '),
    ("JSON behind a UTF-8 byte order mark", b'\xef\xbb\xbf{"inode": INO, "offset": 0, "lines": 0}'),
]

# Kinds that are not a syntax error: undecodable bytes, a parser limit, and a top-level value that is not an object.
SHARED_KINDS = [
    ("bytes that are not valid UTF-8", b"\xff\xfe\x00\x80"),
    ("a write cut in the middle of a multi-byte character", b'{"inode": INO, "offset": 0, "lines": 0, "name": "caf\xc3'),
    ("UTF-16 text", '{"inode": 1, "offset": 0, "lines": 0}'.encode("utf-16")),
    ("JSON nested 100000 levels deep", DEEP),
    ("a top-level integer of 5000 digits", DIGITS),
    ("valid JSON 7", b"7"),
]

check("access_tail", [
    '[]', 'null', '"text"', '{"offset": 3}', '{"inode": INO}', '{"inode": INO, "offset": "12"}',
    '{}', '{"inode": INO, "offset": null}', '{"inode": INO, "offset": 3.5}', '{"inode": INO, "offset": -5}',
    b'{"inode": INO, "offset": ' + DIGITS + b"}",
], True)
check("batch_log_reader", [
    '[]', 'null', '"text"', '{"offset": 3}', '{"lines": "3"}', '{"lines": -2}', '{"lines": true}',
    '{}', '{"lines": null}', '{"lines": 3.0}', '{"lines": [3]}', '{"lines": {"n": 3}}',
    b'{"lines": ' + DIGITS + b"}",
], False)

# A well-typed value that is out of range is not a bad file for either: both start again from the beginning.
for name in ("access_tail", "batch_log_reader"):
    reader = load(name).read_new_lines
    field = "offset" if name == "access_tail" else "lines"
    fact(f"{name}: valid JSON with {field} past the end of the log starts again from the beginning and is repaired",
         recovers(reader, '{"inode": INO, "offset": 99999999999999999999, "lines": 99999999999999999999}'), True)
sys.exit(0 if all(results) else 1)
