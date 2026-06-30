"""
catalog.py — LOCAL FILE config loader (S3 stand-in for now).

Backed by JSON files in backend/config/. Auto-created with sensible
defaults on first run so a fresh clone works with zero manual setup.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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


# Service catalog is section-aware: each section has its own service lists per
# sub-type. Edit these names to update the dropdowns — keys/paths are derived.
_SECTION_SERVICE_NAMES: dict[str, dict[str, list[str]]] = {
    "build": {
        "microservice": [
            "Account", "Address", "Auditlog", "Dataintegration", "Communication",
            "Customercare", "CustomLinkGenerator", "Configuration", "Content",
            "ManageAccount", "Order", "Productcatalog", "TDMReports", "Payment",
            "Shipping", "Scheduler", "Tax", "BatchProcessing", "ReportTitan",
            "Customercarev2", "IdGenService", "Sftpsync", "Enrollment", "FLP360",
        ],
        "portal": [
            "Account", "Admin", "Customercare", "Corporate", "Customercarev2",
            "Ecommerce", "FLP360PWA", "Join", "Reports", "Shop",
        ],
        "utility": [
            "AccountCoreUtils", "FLP360Utils", "KafkaContract", "PortalUtils",
            "TitanUtils", "NextiveoUtility",
        ],
    },
    "yaml": {
        "microservice": [
            "Account", "Address", "Auditlog", "BatchProcessing", "Dataintegration",
            "Communication", "Customercare", "CustomLinkGeneratorService",
            "Configuration", "Content", "ManageAccount", "Order", "Productcatalog",
            "Reportingtitan", "Payment", "Shipping", "Scheduler", "Tax",
            "CustomerServiceV2", "TDMReports", "IdGenService", "Sftpsync",
            "Enrollment", "flp360",
        ],
        "portal": [
            "Admin", "Customercare", "Customercarev2", "Ecommerce", "FLP360PWA",
        ],
    },
    "db": {
        "microservice": [
            "Account", "Address", "Auditlog", "BatchProcessing", "Dataintegration",
            "Communication", "Customercare", "CustomLinkGenerator", "Configuration",
            "Content", "Order", "Product", "ReportTitan", "Payment", "Shipping",
            "Scheduler", "Tax", "Customercarev2", "IdGenService", "Sftpsync",
            "Enrollment", "flp360",
        ],
    },
    "phrases": {
        "portal": [
            "Account", "Admin", "Corporate", "Customercare", "CustomerCarePortalV2",
            "Titan-Ecommerce", "Join", "Reports", "Shop", "SSO", "flp360pwa", "CMS",
        ],
    },
}


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _mk_service(section: str, stype: str, name: str) -> dict[str, Any]:
    slug = _slug(name)
    return {
        "key": f"{section}:{stype}:{slug}",
        "label": name,
        "type": stype,
        "gitlab_project_path": f"titan/{stype}/{slug}",
    }


def load_section_services(section: str) -> dict[str, list[dict[str, Any]]]:
    """Returns {sub_type: [services]} for one section."""
    names = _SECTION_SERVICE_NAMES.get(section, {})
    return {stype: [_mk_service(section, stype, n) for n in lst] for stype, lst in names.items()}


def load_services(section: str | None = None, type: str | None = None) -> list[dict[str, Any]]:
    """Flat list, optionally filtered by section and/or type."""
    sections = [section] if section else list(_SECTION_SERVICE_NAMES.keys())
    out: list[dict[str, Any]] = []
    for sec in sections:
        for stype, items in load_section_services(sec).items():
            if type and stype != type:
                continue
            out.extend(items)
    return out


def get_service(key: str) -> dict[str, Any] | None:
    return next((s for s in load_services() if s["key"] == key), None)


def load_code_freeze() -> dict[str, Any]:
    """{"enabled": false, "updated_by": null, "updated_at": null}"""
    return _load_json("code_freeze.json", default={
        "enabled": False, "updated_by": None, "updated_at": None,
    })


def save_code_freeze(enabled: bool, updated_by: str) -> dict[str, Any]:
    data = {
        "enabled": enabled,
        "updated_by": updated_by,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    _save_json("code_freeze.json", data)
    return data
