import secrets
from datetime import date


class TooManyRequests(Exception):
    pass


class CodeSender:
    def __init__(self, gateway, per_phone_limit: int = 3, today=date.today):
        self.gateway = gateway
        self.per_phone_limit = per_phone_limit
        self.today = today
        self.day = today()
        self.sent = {}

    def _used(self, phone: str) -> int:
        if self.today() != self.day:
            self.day = self.today()
            self.sent.clear()
        return self.sent.get(phone, 0)

    def send_code(self, phone: str) -> str:
        used = self._used(phone)
        if used >= self.per_phone_limit:
            raise TooManyRequests(f"{phone} already received {used} codes today")
        code = f"{secrets.randbelow(10**6):06d}"
        self.gateway.deliver(phone, f"Your verification code is {code}")
        self.sent[phone] = used + 1
        return code
