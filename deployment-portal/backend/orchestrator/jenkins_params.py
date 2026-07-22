"""
jenkins_params.py — Build Jenkins job parameter dicts for Titan build jobs (INTEG POC).

Supports Titan-Microservices, Titan-Portals, YML_Automation_V3,
DB-Script-Automation-Liquibase, and Json_Automation_V2.
Environment and Branch are fixed to integ for build jobs. MergeID defaults blank
for build-only runs; RELEASE_TAG from DevOps.

YAML service name mapping lives in config/jenkins_yaml_service_map.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "jenkins_jobs.json"
_YAML_SERVICE_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "jenkins_yaml_service_map.json"
_DB_SERVICE_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "jenkins_db_service_map.json"
_PHRASES_PORTAL_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "jenkins_phrases_portal_map.json"

_BUILD_AGENT_KEYS = {
    "microservice": "titan_microservices",
    "portal": "titan_portals",
    "yaml": "yml_automation_v3",
    "db": "db_script_automation_liquibase",
    "phrases": "json_automation_v2",
}


def _load_config() -> dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_yaml_service_map() -> dict[str, dict[str, str]]:
    if not _YAML_SERVICE_MAP_PATH.exists():
        return {"microservice": {}, "portal": {}}
    with _YAML_SERVICE_MAP_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "microservice": {k: v for k, v in (data.get("microservice") or {}).items() if not k.startswith("_")},
        "portal": {k: v for k, v in (data.get("portal") or {}).items() if not k.startswith("_")},
    }


def get_job_config(jenkins_key: str) -> dict[str, Any]:
    return _load_config()[jenkins_key]


def get_microservices_config() -> dict[str, Any]:
    return get_job_config("titan_microservices")


def get_portals_config() -> dict[str, Any]:
    return get_job_config("titan_portals")


def _load_label_map(path: Path, bucket: str) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in (data.get(bucket) or {}).items() if not k.startswith("_")}


def get_yml_automation_config() -> dict[str, Any]:
    return get_job_config("yml_automation_v3")


def get_db_liquibase_config() -> dict[str, Any]:
    return get_job_config("db_script_automation_liquibase")


def get_json_automation_config() -> dict[str, Any]:
    return get_job_config("json_automation_v2")


def map_yaml_service(service_label: str, sub_type: str = "microservice") -> str:
    """Map portal catalog label → Jenkins YML_Automation_V3 SERVICE param.

    Edit config/jenkins_yaml_service_map.json when Jenkins enum names differ.
    """
    label = (service_label or "").strip()
    if not label:
        return label
    maps = _load_yaml_service_map()
    bucket = maps.get(sub_type) or maps.get("microservice") or {}
    return bucket.get(label, label)


def map_db_microservice(service_label: str, sub_type: str = "microservice") -> str:
    """Map portal catalog label → Jenkins DB-Script-Automation-Liquibase MICROSERVICE param."""
    label = (service_label or "").strip()
    if not label:
        return label
    bucket = _load_label_map(_DB_SERVICE_MAP_PATH, sub_type) or _load_label_map(_DB_SERVICE_MAP_PATH, "microservice")
    return bucket.get(label, label)


def map_phrases_portal(portal_label: str, sub_type: str = "portal") -> str:
    """Map portal catalog label → Jenkins Json_Automation_V2 Portals param."""
    label = (portal_label or "").strip()
    if not label:
        return label
    bucket = _load_label_map(_PHRASES_PORTAL_MAP_PATH, sub_type) or _load_label_map(_PHRASES_PORTAL_MAP_PATH, "portal")
    return bucket.get(label, label)


def phrases_update_for_sub_type(sub_type: str) -> str:
    """Map form sub_type → Jenkins Json_Automation_V2 Update param."""
    return {
        "phrases": "Json",
        "schemaforms": "SchemaForms",
        "newschemaforms": "NewSchemaForms",
    }.get((sub_type or "phrases").lower(), "Json")


def json_action_for_phrases_filename(filename: str) -> str:
    """Phrases.json → Add; Phrases-remove.json → Delete."""
    low = (filename or "").lower()
    if "remove" in low or low == "phrases-remove.json":
        return "Delete"
    return "Add"


def sort_phrases_json_deployments(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Delete always runs before Add when both files are on the release branch."""
    deletes = [f for f in files if (f.get("action") or "").lower() == "delete"]
    adds = [f for f in files if (f.get("action") or "").lower() != "delete"]
    return deletes + adds


def yaml_action_for_filename(filename: str) -> str:
    """parameters.yml → Update; parameters-remove.yml → Remove."""
    low = (filename or "").lower()
    if "remove" in low or low == "parameters-remove.yml":
        return "Remove"
    return "Update"


def build_titan_microservices_params(
    service_label: str,
    release_tag: str = "",
    merge_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, str]:
    """Parameter map for Titan-Microservices (INTEG only for now).

    Defaults mirror the Jenkins job's own defaults: blank MergeID, empty
    Utilities, IsYMLUpdate=no. RELEASE_TAG comes from DevOps at approval.
    """
    cfg = get_microservices_config()
    defaults = cfg["defaults"]
    if merge_id is None:
        merge_id = cfg.get("merge_id_default", "")
    if user_id is None:
        user_id = cfg.get("user_id_default", "")

    return {
        "MergeID": merge_id,
        "Service": service_label,
        "ReleaseType": defaults["ReleaseType"],
        "Environment": cfg["environment"],
        "Branch": cfg["branch"],
        "IsReverseMerge": defaults["IsReverseMerge"],
        "Utilities": defaults["Utilities"],
        "IsYMLUpdate": defaults["IsYMLUpdate"],
        "FeatureRelease": defaults["FeatureRelease"],
        "RELEASE_TAG": release_tag or "",
        "USER_ID": user_id or "",
    }


def build_titan_portals_params(
    portal_label: str,
    release_tag: str = "",
    merge_id: str | None = None,
) -> dict[str, str]:
    """Parameter map for Titan-Portals (INTEG only for now).

    Build-only: blank MergeID, blank IsTaskDefUpdate / TaskDefParam / JSON.
    Portal name maps to the Jenkins ``Portal`` parameter (e.g. Account).
    """
    cfg = get_portals_config()
    defaults = cfg["defaults"]
    if merge_id is None:
        merge_id = cfg.get("merge_id_default", "")

    return {
        "MergeID": merge_id,
        "Portal": portal_label,
        "JSON": defaults.get("JSON", ""),
        "ReleaseType": defaults["ReleaseType"],
        "Environment": cfg["environment"],
        "Branch": cfg["branch"],
        "IsReverseMerge": defaults["IsReverseMerge"],
        "FeatureRelease": defaults["FeatureRelease"],
        "IsTaskDefUpdate": defaults.get("IsTaskDefUpdate", ""),
        "TaskDefParam": defaults.get("TaskDefParam", ""),
        "RELEASE_TAG": release_tag or "",
    }


def build_yml_automation_params(
    *,
    service_label: str,
    sub_type: str = "microservice",
    environment: str = "INTEG",
    release_branch: str = "",
    release_tag: str = "",
    action: str = "Update",
    restart: str | None = None,
) -> dict[str, str]:
    """Parameter map for YML_Automation_V3."""
    cfg = get_yml_automation_config()
    defaults = cfg.get("defaults") or {}
    type_map = cfg.get("type_of_service") or {}
    type_of = type_map.get(sub_type) or type_map.get("microservice") or "Microservices"
    jenkins_service = map_yaml_service(service_label, sub_type)
    env = (environment or "INTEG").strip().upper()
    act = action if action in ("Update", "Remove") else defaults.get("ACTION", "Update")
    restart_val = (restart if restart is not None else defaults.get("RESTART", "NO")).strip().upper()
    if restart_val not in ("YES", "NO"):
        restart_val = "NO"
    return {
        "TYPE_OF_SERVICE": type_of,
        "SERVICE": jenkins_service,
        "ENVIRONMENT": env,
        "ACTION": act,
        "Branch": (release_branch or release_tag or "").strip(),
        "RELEASE_TAG": (release_tag or "").strip(),
        "RESTART": restart_val,
    }


def build_db_liquibase_params(
    *,
    service_label: str,
    sub_type: str = "microservice",
    environment: str = "INTEG",
    release_branch: str = "",
    release_tag: str = "",
    action: str = "Update",
) -> dict[str, str]:
    """Parameter map for DB-Script-Automation-Liquibase."""
    cfg = get_db_liquibase_config()
    defaults = cfg.get("defaults") or {}
    microservice = map_db_microservice(service_label, sub_type)
    env = (environment or "INTEG").strip().upper()
    act = action if action in ("Update", "Rollback") else defaults.get("ACTION", "Update")
    branch = (release_branch or defaults.get("Branch") or "").strip()
    return {
        "MICROSERVICE": microservice,
        "ENVIRONMENT": env,
        "ACTION": act,
        "Branch": branch,
        "RELEASE_TAG": (release_tag or "").strip(),
    }


def build_json_automation_params(
    *,
    portal_label: str,
    environment: str = "INTEG",
    phrases_branch: str = "",
    release_version: str = "",
    sub_type: str = "phrases",
    update: str | None = None,
    action: str | None = None,
    json_action: str | None = None,
) -> dict[str, str]:
    """Parameter map for Json_Automation_V2 (Json / SchemaForms / NewSchemaForms deploy)."""
    cfg = get_json_automation_config()
    portal = map_phrases_portal(portal_label)
    env = (environment or cfg.get("environment") or "integ").strip().lower()
    update_val = update or phrases_update_for_sub_type(sub_type)
    params: dict[str, str] = {
        "Portals": portal,
        "Environment": env,
        "PhrasesBranch": (phrases_branch or "").strip(),
        "Update": update_val,
        "Release_Version": (release_version or "").strip(),
    }
    if update_val == "Json":
        act = action or json_action or "Add"
        if act in ("Add", "Delete"):
            params["Action"] = act
    return params


def microservices_job_path() -> str:
    return get_microservices_config()["job_path"]


def portals_job_path() -> str:
    return get_portals_config()["job_path"]


def yml_automation_job_path() -> str:
    return get_yml_automation_config()["job_path"]


def db_liquibase_job_path() -> str:
    return get_db_liquibase_config()["job_path"]


def json_automation_job_path() -> str:
    return get_json_automation_config()["job_path"]


def job_path_for_agent(agent_type: str) -> str:
    """Resolve Jenkins job path for an orchestrator agent type."""
    key = _BUILD_AGENT_KEYS.get(agent_type)
    if key:
        return get_job_config(key)["job_path"]
    raise KeyError(f"No Jenkins job configured for agent type '{agent_type}'")


def build_params_for_agent(
    agent_type: str,
    service_label: str,
    release_tag: str = "",
    merge_id: str | None = None,
    *,
    sub_type: str = "microservice",
    environment: str = "INTEG",
    release_branch: str = "",
    yaml_action: str = "Update",
    db_action: str = "Update",
    json_action: str = "Add",
) -> dict[str, str]:
    """Build the Jenkins parameter map for an orchestrator agent."""
    if agent_type == "yaml":
        return build_yml_automation_params(
            service_label=service_label,
            sub_type=sub_type,
            environment=environment,
            release_branch=release_branch,
            release_tag=release_tag,
            action=yaml_action,
        )
    if agent_type == "db":
        return build_db_liquibase_params(
            service_label=service_label,
            sub_type=sub_type,
            environment=environment,
            release_branch=release_branch,
            release_tag=release_tag,
            action=db_action,
        )
    if agent_type == "phrases":
        return build_json_automation_params(
            portal_label=service_label,
            environment=environment,
            phrases_branch=release_branch,
            release_version=release_tag,
            sub_type=sub_type,
            action=json_action,
        )
    if agent_type == "portal":
        return build_titan_portals_params(
            portal_label=service_label,
            release_tag=release_tag,
            merge_id=merge_id,
        )
    return build_titan_microservices_params(
        service_label=service_label,
        release_tag=release_tag,
        merge_id=merge_id,
    )
