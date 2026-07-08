"""
graph_mail.py — send email via Microsoft Graph (delegated OAuth, n8n-style).

Uses the same model as n8n Microsoft Outlook: you sign in once, store a refresh
token, and the portal sends mail as that user (delegated Mail.ReadWrite / Mail.Send).

Required env (when not set, notifications fall back to mock/log mode):
    MS_TENANT_ID
    MS_CLIENT_ID
    MS_CLIENT_SECRET
    MS_REFRESH_TOKEN   — from scripts/graph_oauth_login.py (one-time browser sign-in)

Optional:
    MS_SENDER_EMAIL    — display only; mail is sent via /me (the authorized user)

App registration: same delegated Mail.ReadWrite + offline_access as n8n.
No new Entra redirect URI if you use device-code login or paste MS_REFRESH_TOKEN from n8n.
"""

from __future__ import annotations

import os
import time
from typing import Any

from pathlib import Path

import httpx

_ENV_PATH = Path(__file__).resolve().parent / ".env"

_GRAPH = "https://graph.microsoft.com/v1.0"
_DELEGATED_SCOPES = "offline_access https://graph.microsoft.com/Mail.ReadWrite"

_token_cache: dict[str, Any] = {"access": "", "expires_at": 0.0}


def _cfg() -> dict[str, str]:
    """Read MS_* env at call time so .env updates apply without a full server restart."""
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH, override=True)
    except ImportError:
        pass
    return {
        "tenant": (os.getenv("MS_TENANT_ID") or "").strip(),
        "client_id": (os.getenv("MS_CLIENT_ID") or "").strip(),
        "client_secret": (os.getenv("MS_CLIENT_SECRET") or "").strip(),
        "refresh_token": (os.getenv("MS_REFRESH_TOKEN") or "").strip(),
        "sender": (os.getenv("MS_SENDER_EMAIL") or "").strip(),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["tenant"] and c["client_id"] and c["client_secret"] and c["refresh_token"])


def _token() -> str:
    """Access token via refresh_token grant (delegated / n8n-style)."""
    now = time.time()
    if _token_cache["access"] and now < float(_token_cache["expires_at"]) - 60:
        return str(_token_cache["access"])

    c = _cfg()
    if not c["refresh_token"]:
        raise RuntimeError(
            "MS_REFRESH_TOKEN is not set. Run: .venv/bin/python scripts/graph_oauth_login.py"
        )

    r = httpx.post(
        f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token",
        data={
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": c["refresh_token"],
            "scope": _DELEGATED_SCOPES,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["access"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return data["access_token"]


def exchange_auth_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    """Exchange authorization code for tokens (used by graph_oauth_login.py)."""
    c = _cfg()
    r = httpx.post(
        f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token",
        data={
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": _DELEGATED_SCOPES,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def authorization_url(*, redirect_uri: str, state: str = "portal") -> str:
    c = _cfg()
    from urllib.parse import urlencode

    params = urlencode({
        "client_id": c["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _DELEGATED_SCOPES,
        "state": state,
        "response_mode": "query",
    })
    return f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/authorize?{params}"


def send_mail(
    to: str,
    subject: str,
    html_body: str,
    *,
    cc: list[str] | None = None,
    text_body: str | None = None,
) -> None:
    """Send HTML email through Graph as the authorized user (/me/sendMail)."""
    if not is_configured():
        raise RuntimeError(
            "Microsoft Graph mail is not configured. Set MS_* vars and MS_REFRESH_TOKEN."
        )

    payload: dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }
    if cc:
        payload["message"]["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in cc
        ]

    tok = _token()
    r = httpx.post(
        f"{_GRAPH}/me/sendMail",
        headers={"Authorization": f"Bearer {tok}"},
        json=payload,
        timeout=45,
    )
    if r.status_code not in (202, 200):
        r.raise_for_status()
