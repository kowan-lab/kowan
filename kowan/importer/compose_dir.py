"""Compose folders: a directory of stacks, one sub-folder each (Dockge,
plain ``/opt/stacks``), picked in the browser and posted as files.

``group`` turns the posted relative paths into one candidate per folder
that holds a compose file, with its ``.env`` next to it.
"""

from __future__ import annotations

from typing import Dict, List

COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
MAX_FILE = 1 << 20


def group(files: Dict[str, str]) -> List[Dict[str, str]]:
    """``{relative path: text}`` -> ``[{name, folder, file, compose, env}]``,
    one per folder with a compose file (the first by COMPOSE_NAMES order)."""
    folders: Dict[str, Dict[str, str]] = {}
    for path, text in files.items():
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        if not parts or ".." in parts:
            continue
        folder, name = "/".join(parts[:-1]), parts[-1]
        folders.setdefault(folder, {})[name] = text
    out: List[Dict[str, str]] = []
    for folder, entries in sorted(folders.items()):
        found = next((n for n in COMPOSE_NAMES if n in entries), None)
        if found is None:
            continue
        name = folder.split("/")[-1] if folder else "stack"
        out.append({"name": name, "folder": folder, "file": found, "compose": entries[found], "env": entries.get(".env", "")})
    return out
