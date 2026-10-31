import shutil
from pathlib import Path
from typing import Callable, Mapping


def publish(live_dir: Path, pages: Mapping[str, Callable[[], str]]) -> list[str]:
    if live_dir.exists():
        shutil.rmtree(live_dir)
    live_dir.mkdir(parents=True)
    names = []
    for name, render in pages.items():
        target = live_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(), encoding="utf-8")
        names.append(name)
    return names


def read_page(live_dir: Path, name: str) -> str:
    return (live_dir / name).read_text(encoding="utf-8")


def page_names(live_dir: Path) -> list[str]:
    if not live_dir.is_dir():
        return []
    return sorted(
        str(path.relative_to(live_dir)) for path in live_dir.rglob("*") if path.is_file()
    )
