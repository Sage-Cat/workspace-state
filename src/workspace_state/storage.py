from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .util import atomic_json, data_home

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_name(name: str) -> str:
    if not SAFE_NAME.fullmatch(name):
        raise ValueError("snapshot names may contain letters, numbers, dot, underscore, and dash")
    return name


def path_for(name: str) -> Path:
    return data_home() / "snapshots" / (validate_name(name) + ".json")


def save(snapshot: dict[str, Any]) -> Path:
    path = path_for(str(snapshot["name"]))
    atomic_json(path, snapshot)
    return path


def load(name: str) -> dict[str, Any]:
    import json

    path = path_for(name)
    if not path.exists():
        raise FileNotFoundError(f"snapshot not found: {name}")
    return json.loads(path.read_text())


def list_all(include_archived: bool = False) -> list[dict[str, Any]]:
    import json

    result = []
    for path in sorted((data_home() / "snapshots").glob("*.json")):
        try:
            value = json.loads(path.read_text())
            if include_archived or not value.get("archived", False):
                result.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)
