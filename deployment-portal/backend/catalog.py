"""
catalog.py — LOCAL FILE config loader (S3 stand-in for now).

Backed by JSON files in backend/config/. Auto-created with sensible
defaults on first run so a fresh clone works with zero manual setup.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_DIR.mkdir(exist_ok=True)

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 2


def _load_json(key: str, default: Any) -> Any:
    cached = _CACHE.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    path = CONFIG_DIR / key
    if not path.exists():
        _save_json(key, default)
        data = default
    else:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

    _CACHE[key] = (time.time(), data)
    return data


def _save_json(key: str, data: Any) -> None:
    path = CONFIG_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)
    _CACHE.pop(key, None)


def load_environments() -> list[dict[str, Any]]:
    return _load_json("environments.json", default=[
        {"key": "DEV", "label": "Dev", "order": 1},
        {"key": "SUPPORT", "label": "Support", "order": 2},
        {"key": "INTEG", "label": "Integ", "order": 3},
        {"key": "UAT", "label": "UAT", "order": 4},
    ])


def save_environments(envs: list[dict[str, Any]]) -> None:
    _save_json("environments.json", envs)


def load_approvers() -> list[dict[str, Any]]:
    return _load_json("approvers.json", default=[
        {"key": "a-sharma", "name": "A. Sharma", "email": "a.sharma@company.com"},
        {"key": "r-patel", "name": "R. Patel", "email": "r.patel@company.com"},
        {"key": "d-kumar", "name": "D. Kumar", "email": "d.kumar@company.com"},
        {"key": "m-singh", "name": "M. Singh", "email": "m.singh@company.com"},
    ])


def save_approvers(approvers: list[dict[str, Any]]) -> None:
    _save_json("approvers.json", approvers)


def get_approver(key: str) -> dict[str, Any] | None:
    return next((a for a in load_approvers() if a["key"] == key), None)
