from pathlib import Path


class RouteError(ValueError):
    pass


def render_route(route: dict) -> str:
    domain = str(route.get("domain", "")).strip().lower()
    folder = str(route.get("folder", "")).strip()
    if "." not in domain or "@" in domain or " " in domain:
        raise RouteError(f"invalid domain {domain!r}")
    if not folder or "\t" in folder or "\n" in folder:
        raise RouteError(f"invalid folder for {domain}")
    return f"{domain}\t{folder}\n"


def apply_routes(routes_path: Path, routes: list[dict]) -> int:
    routes_path.parent.mkdir(parents=True, exist_ok=True)
    with routes_path.open("w", encoding="utf-8") as out:
        for route in routes:
            out.write(render_route(route))
    return len(routes)


def load_routes(routes_path: Path) -> dict[str, str]:
    if not routes_path.exists():
        return {}
    table = {}
    for line in routes_path.read_text(encoding="utf-8").splitlines():
        domain, _, folder = line.partition("\t")
        if domain and folder:
            table[domain] = folder
    return table


def folder_for(routes_path: Path, address: str) -> str:
    domain = address.rsplit("@", 1)[-1].strip().lower()
    return load_routes(routes_path).get(domain, "inbox")
