"""Salesforce OAuth 2.0 web-server flow with PKCE ("Log in with Salesforce", like Workbench).

Default client: the Salesforce CLI's global connected app `PlatformCLI`, which exists in every org and whose callback
is http://localhost:1717/OauthRedirect - that is why the app listens on port 1717. Orgs that block it can use their
own External Client App: set SF_CLIENT_ID (+ SF_CLIENT_SECRET if required) and SF_CALLBACK_URL.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get("SF_CLIENT_ID", "PlatformCLI")
CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
CALLBACK_URL = os.environ.get("SF_CALLBACK_URL", "http://localhost:1717/OauthRedirect")
SCOPES = os.environ.get("SF_SCOPES", "api refresh_token web")
ENVIRONMENTS = {"production": "https://login.salesforce.com", "sandbox": "https://test.salesforce.com"}


class OAuthError(RuntimeError):
    pass


def login_host(env: str, domain: str = "") -> str:
    if env in ENVIRONMENTS:
        return ENVIRONMENTS[env]
    host = domain.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    if not host:
        raise OAuthError("Enter your My Domain, e.g. acme--uat.sandbox.my.salesforce.com")
    if "." not in host:
        host += ".my.salesforce.com"
    if not host.endswith((".salesforce.com", ".force.com", ".salesforce.mil", ".cloudforce.com")):
        raise OAuthError("The custom domain must be a Salesforce domain (…my.salesforce.com)")
    return "https://" + host


def start(env: str, domain: str, api_version: str) -> tuple[str, dict]:
    """Return (authorize URL, pending-login record to keep until the callback)."""
    host = login_host(env, domain)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": CALLBACK_URL, "scope": SCOPES,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login"}
    pending = {"state": state, "verifier": verifier, "host": host, "env": env, "api": api_version,
               "created": time.time()}
    return f"{host}/services/oauth2/authorize?{urllib.parse.urlencode(params)}", pending


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8"))
            msg = f"{err.get('error')}: {err.get('error_description')}"
        except Exception:
            msg = f"HTTP {exc.code}"
        raise OAuthError(msg) from None


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def finish(pending: dict, code: str) -> dict:
    """Exchange the authorization code; return the session's Salesforce connection (tokens stay in memory)."""
    data = {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK_URL, "code_verifier": pending["verifier"]}
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    tok = _post(f"{pending['host']}/services/oauth2/token", data)
    instance, access = tok["instance_url"], tok["access_token"]
    ident = _get(tok["id"], access)
    api = pending["api"]
    if not api or api == "latest":
        versions = _get(f"{instance}/services/data/", access)
        api = max((v["version"] for v in versions), key=float)
    org = {}
    try:
        q = urllib.parse.quote("SELECT Name, OrganizationType, IsSandbox, InstanceName FROM Organization")
        rows = _get(f"{instance}/services/data/v{api}/query?q={q}", access).get("records", [])
        org = {k: v for k, v in (rows[0] if rows else {}).items() if k != "attributes"}
    except Exception:
        pass
    return {"host": pending["host"], "env": pending["env"], "instance_url": instance, "access_token": access,
            "refresh_token": tok.get("refresh_token"), "api_version": api,
            "username": ident.get("username"), "display_name": ident.get("display_name"),
            "user_id": ident.get("user_id"), "org_id": ident.get("organization_id"),
            "org_name": org.get("Name"), "edition": org.get("OrganizationType"),
            "is_sandbox": org.get("IsSandbox"), "instance_name": org.get("InstanceName"),
            "connected_at": time.strftime("%Y-%m-%d %H:%M")}


def refresh(conn: dict) -> str:
    if not conn.get("refresh_token"):
        raise OAuthError("session expired and no refresh token was issued - log in again")
    data = {"grant_type": "refresh_token", "refresh_token": conn["refresh_token"], "client_id": CLIENT_ID}
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    conn["access_token"] = _post(f"{conn['host']}/services/oauth2/token", data)["access_token"]
    return conn["access_token"]


def revoke(conn: dict) -> None:
    token = conn.get("refresh_token") or conn.get("access_token")
    if token:
        try:
            _post(f"{conn['host']}/services/oauth2/revoke", {"token": token})
        except Exception:
            pass
