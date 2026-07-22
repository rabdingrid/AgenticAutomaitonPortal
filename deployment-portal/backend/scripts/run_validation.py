#!/usr/bin/env python3
"""
run_validation.py — drive the smart validator without a GitSpace webhook.

This is the "script for now" path: build a sample (or custom) deployment
request, run the full validation pipeline (GitSpace mock + Ollama AI), and
print the structured report plus the Markdown briefing a Dev Lead would read.

Usage:
    # from backend/ with the venv active:
    python scripts/run_validation.py                 # built-in sample request
    python scripts/run_validation.py --no-ai         # skip the AI calls (fast)
    python scripts/run_validation.py --json          # dump raw JSON report
    python scripts/run_validation.py --request my.json   # custom request payload

Scenario hooks (handled by the GitSpace mock):
    - a branch named like 'release/conflict-1' forces a merge conflict
    - a release path containing 'broken' forces a faulty artifact
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validators import validation  # noqa: E402

SAMPLE_REQUEST = {
    "environment": "INTEG",
    "jira_id": "TRB-16996",
    "sections": [
        {
            "section": "yaml",
            "release_branch": "R-2026-07-W1",
            "links": [
                {"sub_type": "microservice", "service_key": "yaml:microservice:account", "label": "Account"},
            ],
        },
        {
            "section": "db",
            "release_branch": "R-2026-07-W1",
            "links": [
                {"sub_type": "microservice", "service_key": "db:microservice:payment", "label": "Payment"},
            ],
        },
        {
            "section": "phrases",
            "release_branch": "R-2026-07-W1",
            "links": [
                {"sub_type": "portal", "service_key": "phrases:portal:customercareportalv2", "label": "CustomerCarePortalV2"},
            ],
        },
        {
            "section": "build",
            "branch_from": "develop",
            "branch_to": "R-2026-07-W1",
            "links": [
                {"sub_type": "microservice", "service_key": "build:microservice:reporttitan", "label": "ReportTitan"},
            ],
        },
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", help="Path to a JSON request payload")
    ap.add_argument("--no-ai", action="store_true", help="Skip Ollama AI calls")
    ap.add_argument("--json", action="store_true", help="Print raw JSON report")
    args = ap.parse_args()

    if args.request:
        with open(args.request, encoding="utf-8") as f:
            req = json.load(f)
    else:
        req = SAMPLE_REQUEST

    report = validation.run_validation(
        req["environment"], req.get("jira_id", ""), req["sections"], use_ai=not args.no_ai
    )

    if args.json:
        print(json.dumps(report, indent=2))
        return

    s = report["stats"]
    print("=" * 70)
    print(f"VALIDATION REPORT — {report['jira_id']} → {report['environment']}")
    print(f"Overall: {report['overall_status'].upper()}  "
          f"({s['passed']} pass / {s['warnings']} warn / {s['failed']} fail of {s['checked']})")
    print(f"AI: {'used (' + report['ai_model'] + ')' if report['ai_used'] else 'fallback (offline)'}")
    print("=" * 70)
    for g in report["sections"]:
        print(f"\n[{g['status'].upper()}] {g['title']}")
        for it in g["items"]:
            icon = {"pass": "OK ", "warn": "WARN", "fail": "FAIL"}[it["status"]]
            print(f"  {icon}  {it['label']}")
            for c in it["checks"]:
                print(f"        - {c['name']}: {c['detail']}")
            for u in it["urls"].values():
                if u:
                    print(f"        url: {u}")
    print("\n" + "-" * 70)
    print("BRIEFING FOR DEV LEAD:\n")
    print(report["summary_markdown"])


if __name__ == "__main__":
    main()
