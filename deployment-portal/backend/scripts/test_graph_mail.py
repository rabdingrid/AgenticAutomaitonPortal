#!/usr/bin/env python3
"""Test Microsoft Graph mail setup without submitting a portal request.

Usage:
    cd deployment-portal/backend
    .venv/bin/python scripts/test_graph_mail.py
    .venv/bin/python scripts/test_graph_mail.py --to shukumar@nextsphere.com
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx
import graph_mail


def _token_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="Recipient email")
    args = parser.parse_args()

    c = graph_mail._cfg()
    print("=== Config (delegated / n8n-style) ===")
    print(f"  tenant: {'SET' if c['tenant'] else 'MISSING'}")
    print(f"  client_id: {'SET' if c['client_id'] else 'MISSING'}")
    print(f"  client_secret: {'SET' if c['client_secret'] else 'MISSING'}")
    print(f"  refresh_token: {'SET' if c['refresh_token'] else 'MISSING'}")
    print(f"  is_configured: {graph_mail.is_configured()}")
    if not graph_mail.is_configured():
        print("\nRun: .venv/bin/python scripts/graph_oauth_login.py")
        print("(one-time browser sign-in — same OAuth model as n8n Outlook)")
        return 1

    print("\n=== OAuth token (refresh_token grant) ===")
    try:
        tok = graph_mail._token()
        print("  Token: OK")
        claims = _token_claims(tok)
        scp = (claims.get("scp") or "").split()
        print(f"  Delegated scopes: {scp or '(none)'}")
        if not any(s in scp for s in ("Mail.Send", "Mail.ReadWrite")):
            print("  → Token lacks Mail.Send / Mail.ReadWrite.")
            print("    Re-run graph_oauth_login.py after granting delegated Mail.ReadWrite on the app.")
    except httpx.HTTPStatusError as e:
        print(f"  FAILED HTTP {e.response.status_code}: {e.response.text[:400]}")
        print("  → Refresh token expired or revoked? Re-run scripts/graph_oauth_login.py")
        return 1

    print("\n=== Signed-in user (/me) ===")
    r = httpx.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    print(f"  GET /me: {r.status_code}")
    if r.status_code == 200:
        u = r.json()
        print(f"  displayName: {u.get('displayName')}")
        print(f"  mail: {u.get('mail') or u.get('userPrincipalName')}")
    else:
        print(f"  {r.text[:400]}")

    to = args.to or (r.json().get("mail") if r.status_code == 200 else "") or c["sender"]
    if not to:
        print("\nPass --to recipient@company.com")
        return 1

    print(f"\n=== sendMail test (/me/sendMail) → {to} ===")
    try:
        graph_mail.send_mail(
            to,
            "[Portal Test] Graph mail works",
            "<p>If you received this, delegated Graph mail is working.</p>",
        )
        print("  SUCCESS (HTTP 202) — check recipient Inbox and your Sent folder")
        return 0
    except httpx.HTTPStatusError as e:
        print(f"  FAILED HTTP {e.response.status_code}")
        print(f"  {e.response.text[:800]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
