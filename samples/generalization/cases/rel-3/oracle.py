"""Behavioural ground truth for case rel-3 (run: python3.12 -I oracle.py).

Leaves a state file in a wrong shape (valid JSON, wrong structure) next to a log
and runs the reader twice. Exits 0 only if the violation fails on every run while
handling a syntax error, and the control treats every wrong shape like a syntax error.
"""
import importlib.util
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
HERE = Path(__file__).resolve().parent
LINES = ["l1", "l2", "l3"]
results = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / "code" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(label, observed, expected):
    results.append(observed == expected)
    print(f"{'ok  ' if observed == expected else 'FAIL'} {label}: {observed} (expected {expected})")


def recovers(reader, state_text):
    """True if the first run returns every line, repairs the state file, and the next run sees nothing new."""
    with tempfile.TemporaryDirectory() as folder:
        log_path, state_path = Path(folder) / "app.log", Path(folder) / "state.json"
        log_path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        state_path.write_text(state_text.replace("INO", str(log_path.stat().st_ino)), encoding="utf-8")
        try:
            first = reader(log_path, state_path)
            repaired = isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict)
            second = reader(log_path, state_path)
        except Exception:
            return False
        return first == LINES and repaired and second == []


def fails_every_run(reader, state_text):
    """True if two runs in a row raise and the state file is left exactly as it was."""
    with tempfile.TemporaryDirectory() as folder:
        log_path, state_path = Path(folder) / "app.log", Path(folder) / "state.json"
        log_path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
        text = state_text.replace("INO", str(log_path.stat().st_ino))
        state_path.write_text(text, encoding="utf-8")
        outcomes = []
        for _ in range(2):
            try:
                reader(log_path, state_path)
                outcomes.append(False)
            except Exception:
                outcomes.append(True)
        return outcomes == [True, True] and state_path.read_text(encoding="utf-8") == text


def check(module_name, wrong_shapes, expect_wrong_shape_recovers):
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
    for text in wrong_shapes:
        if expect_wrong_shape_recovers:
            fact(f"{module_name}: valid JSON {text} takes the same fallback and is repaired", recovers(reader, text), True)
        else:
            fact(f"{module_name}: valid JSON {text} fails on every run and is never repaired", fails_every_run(reader, text), True)


check("access_tail", ['[]', 'null', '"text"', '{"offset": 3}', '{"inode": INO}', '{"inode": INO, "offset": "12"}'], False)
check("batch_log_reader", ['[]', 'null', '"text"', '{"offset": 3}', '{"lines": "3"}', '{"lines": -2}', '{"lines": true}'], True)
sys.exit(0 if all(results) else 1)
