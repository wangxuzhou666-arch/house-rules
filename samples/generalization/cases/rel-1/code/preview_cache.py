import shutil
from pathlib import Path
from typing import Callable


class PreviewCache:
    def __init__(self, root: Path, render: Callable[[str], bytes]):
        self.root = root
        self.render = render

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.png"

    def get(self, name: str) -> bytes:
        path = self._path(name)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")
            part.write_bytes(self.render(name))
            part.replace(path)
        return path.read_bytes()

    def rebuild(self, names: list[str]) -> int:
        if self.root.exists():
            shutil.rmtree(self.root)
        for name in names:
            self.get(name)
        return len(names)

    def cached_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.stem for path in self.root.glob("*.png"))
