from pathlib import Path
from typing import Callable, Iterable


class SenderIndex:
    def __init__(self, path: Path, read_routes: Callable[[], Iterable[tuple[str, str]]]):
        self.path = path
        self.read_routes = read_routes

    def rebuild(self) -> dict[str, str]:
        routes = [(domain.strip().lower(), folder) for domain, folder in self.read_routes()]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as out:
            for domain, folder in routes:
                out.write(f"{domain}\t{folder}\n")
            out.write(f"end\t{len(routes)}\n")
        return dict(routes)

    def _load(self) -> dict[str, str] | None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        if not lines:
            return None
        tag, _, total = lines[-1].partition("\t")
        if tag != "end" or total != str(len(lines) - 1):
            return None
        pairs = [line.partition("\t") for line in lines[:-1]]
        if any(not sep for _, sep, _ in pairs):
            return None
        return {domain: folder for domain, _, folder in pairs}

    def folder_for(self, address: str) -> str:
        table = self._load()
        if table is None:
            table = self.rebuild()
        domain = address.rsplit("@", 1)[-1].strip().lower()
        return table.get(domain, "inbox")
