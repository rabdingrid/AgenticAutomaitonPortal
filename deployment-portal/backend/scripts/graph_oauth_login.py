#!/usr/bin/env python3
"""
graph_oauth_login.py — one-time Microsoft sign-in (n8n-style delegated OAuth).

Enterprise tenants often block device-code flow. Use --n8n (recommended if n8n
Outlook already works — same redirect URI, no new Entra setup).

Usage:
    cd deployment-portal/backend
    .venv/bin/python scripts/graph_oauth_login.py --n8n

Writes MS_REFRESH_TOKEN to backend/.env.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

import httpx
import graph_mail

N8N_REDIRECT_URI = os.getenv(
    "MS_OAUTH_REDIRECT_URI",
    "http://localhost:5678/rest/oauth2-credential/callback",
)
N8N_PORT = 5678


def _upsert_env(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    # Quote for bash `source .env` — refresh tokens contain ~ and other shell metacharacters.
    safe = value.replace("\\", "\\\\").replace('"', '\\"')
    line = f'{key}="{safe}"'
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def _save_refresh(refresh: str) -> None:
    _upsert_env("MS_REFRESH_TOKEN", refresh)
    print("\nSaved MS_REFRESH_TOKEN to", ENV_PATH)
    print("Restart ./run.sh then: .venv/bin/python scripts/test_graph_mail.py --to you@company.com")


def _device_code_flow(c: dict[str, str]) -> dict | None:
    print("=== Device code sign-in ===\n")
    print("Note: many orgs block this (Conditional Access). If browser says")
    print("'You don't have access to this', use: graph_oauth_login.py --n8n\n")
    try:
        r = httpx.post(
            f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/devicecode",
            data={"client_id": c["client_id"], "scope": graph_mail._DELEGATED_SCOPES},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"Device code start failed ({e.response.status_code}): {e.response.text[:400]}")
        return None

    d = r.json()
    print(d.get("message", ""))
    print(f"\nCode: {d.get('user_code', '?')}\n")

    interval = int(d.get("interval", 5))
    deadline = time.time() + int(d.get("expires_in", 900))
    device_code = d["device_code"]

    while time.time() < deadline:
        time.sleep(interval)
        tr = httpx.post(
            f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "device_code": device_code,
            },
            timeout=30,
        )
        if tr.status_code == 200:
            return tr.json()
        err = tr.json().get("error", "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 2
            continue
        if err == "expired_token":
            print("Device code expired (sign-in blocked or too slow). Use --n8n instead.")
        else:
            print(f"Device code token error: {tr.text[:500]}")
        return None

    print("Device code timed out.")
    return None


class _N8nHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if not self.path.startswith("/rest/oauth2-credential/callback"):
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(urlparse(self.path).query)
        if "code" in qs:
            _N8nHandler.code = qs["code"][0]
            body = b"<html><body><h2>Signed in</h2><p>Close this tab and return to the terminal.</p></body></html>"
            self.send_response(200)
        else:
            _N8nHandler.error = qs.get("error_description", qs.get("error", ["unknown"]))[0]
            body = f"<html><body><h2>Failed</h2><p>{_N8nHandler.error}</p></body></html>".encode()
            self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


def _n8n_redirect_flow(c: dict[str, str]) -> dict | None:
    """Same OAuth flow as n8n Outlook — reuses existing Entra redirect URI."""
    _N8nHandler.code = None
    _N8nHandler.error = None

    print("=== n8n-style browser sign-in (recommended) ===")
    print(f"Redirect URI: {N8N_REDIRECT_URI}")
    print()
    print("1. Stop n8n (it uses port 5678): quit Docker / stop n8n process")
    print("2. Press Enter here when port 5678 is free...")
    try:
        input()
    except EOFError:
        pass

    url = graph_mail.authorization_url(redirect_uri=N8N_REDIRECT_URI)
    server = HTTPServer(("127.0.0.1", N8N_PORT), _N8nHandler)
    server.timeout = 1

    def _serve() -> None:
        deadline = time.time() + 180
        while time.time() < deadline and not _N8nHandler.code and not _N8nHandler.error:
            server.handle_request()
        server.server_close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    print("Opening browser — sign in as rabdin@nextsphere.com (same as n8n)...")
    webbrowser.open(url)
    thread.join(timeout=185)

    if _N8nHandler.error:
        print(f"OAuth error: {_N8nHandler.error}")
        return None
    if not _N8nHandler.code:
        print("No callback received. Is n8n still on port 5678? Try: lsof -i :5678")
        return None

    try:
        return graph_mail.exchange_auth_code(_N8nHandler.code, redirect_uri=N8N_REDIRECT_URI)
    except httpx.HTTPStatusError as e:
        print(f"Token exchange failed: {e.response.text[:500]}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Get MS_REFRESH_TOKEN for portal email")
    parser.add_argument(
        "--n8n",
        action="store_true",
        help="Use n8n redirect URI (use this if device code is blocked by your org)",
    )
    args = parser.parse_args()

    c = graph_mail._cfg()
    missing = [k for k in ("tenant", "client_id", "client_secret") if not c[k]]
    if missing:
        print("Missing in .env:", ", ".join(f"MS_{k.upper()}" for k in missing))
        return 1

    if args.n8n:
        tokens = _n8n_redirect_flow(c)
    else:
        tokens = _device_code_flow(c)
        if not tokens:
            print("\nTrying n8n redirect fallback...\n")
            tokens = _n8n_redirect_flow(c)

    if not tokens:
        print("\nIf device code showed 'You don't have access', your admin blocks it.")
        print("Run: .venv/bin/python scripts/graph_oauth_login.py --n8n")
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("No refresh_token returned. Ensure delegated Mail.ReadWrite + offline_access on the app.")
        return 1

    _save_refresh(refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
