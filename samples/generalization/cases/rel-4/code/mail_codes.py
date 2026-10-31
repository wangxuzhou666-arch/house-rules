import secrets
import threading
from datetime import date


class TooManyRequests(Exception):
    pass


class CodeMailer:
    def __init__(self, transport, per_address_limit: int = 3, today=date.today):
        self.transport = transport
        self.per_address_limit = per_address_limit
        self.today = today
        self.day = today()
        self.sent = {}
        self.lock = threading.Lock()

    def _reserve(self, address: str) -> None:
        with self.lock:
            if self.today() != self.day:
                self.day = self.today()
                self.sent.clear()
            used = self.sent.get(address, 0)
            if used >= self.per_address_limit:
                raise TooManyRequests(f"{address} already received {used} codes today")
            self.sent[address] = used + 1

    def send_code(self, address: str) -> str:
        self._reserve(address)
        code = f"{secrets.randbelow(10**6):06d}"
        self.transport.send(address, "Your verification code", f"Your code is {code}")
        return code
