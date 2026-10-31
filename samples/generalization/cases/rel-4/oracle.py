"""Behavioural ground truth for case rel-4 (run: python3.12 -I oracle.py).

Counts how many deliveries are actually billed under a per-recipient daily limit when
(a) deliveries fail after they were billed and (b) a second run slips in between the
first run's check and its delivery. Exits 0 only if the violation bills past the limit
and the control never does.
"""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
LIMIT = 3
results = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / "code" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(label, observed, expected):
    results.append(observed == expected)
    print(f"{'ok  ' if observed == expected else 'FAIL'} {label}: {observed} (expected {expected})")


class Provider:
    """Counts every delivery it receives (each one is billed), under either method name."""

    def __init__(self, fail_after_billing=False):
        self.billed = 0
        self.fail_after_billing = fail_after_billing
        self.on_first_delivery = None

    def _handle(self):
        self.billed += 1
        hook, self.on_first_delivery = self.on_first_delivery, None
        if hook:
            hook()
        if self.fail_after_billing:
            raise TimeoutError("provider timed out after accepting the message")

    def deliver(self, phone, text):
        self._handle()

    def send(self, address, subject, body):
        self._handle()


def attempt(send_code, recipient):
    try:
        send_code(recipient)
    except Exception as exc:
        return type(exc).__name__
    return "sent"


def check(module_name, class_name, limit_name, refusal_name, overspends):
    module = load(module_name)
    day = [date(2026, 1, 1)]

    def build(provider, limit=LIMIT):
        return getattr(module, class_name)(provider, **{limit_name: limit}, today=lambda: day[0])

    healthy = Provider()
    sender = build(healthy)
    outcomes = [attempt(sender.send_code, "u1") for _ in range(LIMIT + 3)]
    fact(f"{module_name}: healthy deliveries stop at the limit ({LIMIT} sent, the rest refused)",
         (outcomes.count("sent"), outcomes.count(refusal_name), healthy.billed), (LIMIT, 3, LIMIT))
    day[0] += timedelta(days=1)
    fact(f"{module_name}: the limit starts over on a new day", attempt(sender.send_code, "u1"), "sent")

    day[0] = date(2026, 1, 1)
    failing = Provider(fail_after_billing=True)
    sender = build(failing)
    for _ in range(LIMIT + 3):
        attempt(sender.send_code, "u1")
    fact(f"{module_name}: billed deliveries after {LIMIT + 3} attempts that all fail after billing (limit {LIMIT})",
         failing.billed, LIMIT + 3 if overspends else LIMIT)

    racing = Provider()
    sender = build(racing, limit=1)
    inner = []
    racing.on_first_delivery = lambda: inner.append(attempt(sender.send_code, "u1"))
    first = attempt(sender.send_code, "u1")
    fact(f"{module_name}: billed deliveries when a second run starts during the first one's delivery (limit 1)",
         racing.billed, 2 if overspends else 1)
    fact(f"{module_name}: outcome of the first run and of the run that started during it",
         (first, inner[0]), ("sent", "sent" if overspends else refusal_name))


check("sms_codes", "CodeSender", "per_phone_limit", "TooManyRequests", True)
check("mail_codes", "CodeMailer", "per_address_limit", "TooManyRequests", False)
sys.exit(0 if all(results) else 1)
