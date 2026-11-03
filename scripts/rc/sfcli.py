"""Thin wrapper around the Salesforce CLI (sf) plus interactive org login/selection."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


class SfError(RuntimeError):
    pass


def sf_exe() -> str:
    exe = shutil.which("sf") or shutil.which("sf.cmd")
    if not exe:
        raise SfError("Salesforce CLI (sf) not found. Install it: https://developer.salesforce.com/tools/salesforcecli")
    return exe


def run_sf(*args: str, check: bool = True, interactive: bool = False) -> dict:
    """Run an sf command with --json and return the parsed payload."""
    cmd = [sf_exe(), *args]
    if interactive:
        # Login opens a browser; let the user see CLI messages, then read result as JSON.
        proc = subprocess.run(cmd + ["--json"], stdout=subprocess.PIPE, text=True, encoding="utf-8")
    else:
        proc = subprocess.run(cmd + ["--json"], capture_output=True, text=True, encoding="utf-8")
    out = (proc.stdout or "").lstrip("﻿")
    start = out.find("{")
    try:
        payload = json.loads(out[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        payload = {}
    if check and (proc.returncode != 0 or payload.get("status", 0) != 0):
        msg = payload.get("message") or (proc.stderr or out).strip()[:500]
        raise SfError(f"sf {' '.join(args)} failed: {msg}")
    return payload


def list_orgs() -> list[dict]:
    result = run_sf("org", "list", check=False).get("result", {})
    orgs, seen = [], set()
    for group in ("nonScratchOrgs", "sandboxes", "scratchOrgs", "devHubs", "other"):
        for org in result.get(group, []) or []:
            if org.get("username") in seen:
                continue
            seen.add(org.get("username"))
            orgs.append(org)
    return orgs


def display_org(target: str) -> dict:
    """Return org details including a fresh access token (raises if the session cannot be refreshed)."""
    res = run_sf("org", "display", "--target-org", target)["result"]
    if not res.get("accessToken") or "Unable" in str(res.get("connectedStatus", "")):
        raise SfError(f"Session for '{target}' is not valid: {res.get('connectedStatus')}")
    return res


def login_web(alias: str, instance_url: str) -> dict:
    print(f"\n  A browser window will open. Log in to Salesforce ({instance_url}) and click 'Allow'.")
    print("  Return here when the browser says authentication was successful...\n")
    res = run_sf("org", "login", "web", "--alias", alias, "--instance-url", instance_url,
                 interactive=True)["result"]
    print(f"  Logged in as {res.get('username')}")
    return res


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SfError("no interactive input available - run in a terminal, or log in first with "
                      "'sf org login web --alias <alias> --instance-url <login url>' and pass --target-org") from None
    return value or (default or "")


def _login_new() -> str:
    print("\nWhich type of org do you want to log in to?")
    print("  1) Sandbox            (https://test.salesforce.com)")
    print("  2) Production / DevEd (https://login.salesforce.com)")
    print("  3) My Domain URL      (e.g. https://acme--uat.sandbox.my.salesforce.com)")
    choice = _ask("Choose 1-3", "1")
    url = {"1": "https://test.salesforce.com", "2": "https://login.salesforce.com"}.get(choice)
    if not url:
        url = _ask("My Domain login URL").rstrip("/")
        if not url.startswith("https://"):
            url = "https://" + url
    alias = _ask("Alias for this org (letters, numbers, dashes)", "release-check-org")
    login_web(alias, url)
    return alias


def choose_org(preselected: str | None = None) -> dict:
    """Interactively pick an authenticated org or log in to a new one. Returns `sf org display` result."""
    if preselected:
        try:
            return display_org(preselected)
        except SfError as exc:
            print(f"\n! {exc}")
            known = next((o for o in list_orgs() if preselected in (o.get("alias"), o.get("username"))), {})
            if not sys.stdin.isatty():
                raise SfError(f"{exc}. Log in first: sf org login web --alias {preselected} "
                              f"--instance-url {known.get('loginUrl') or 'https://test.salesforce.com'}") from None
            if _ask("Log in again to this org now? (y/n)", "y").lower().startswith("y"):
                url = _ask("Login URL", (known.get("loginUrl") or "https://test.salesforce.com").rstrip("/"))
                login_web(preselected, url)
                return display_org(preselected)
            raise

    orgs = list_orgs()
    print("\nSalesforce orgs authenticated on this machine:")
    for i, org in enumerate(orgs, 1):
        status = org.get("connectedStatus") or org.get("status") or ""
        flag = ("OK" if status in ("Connected", "Active") else
                "expired - will ask to log in" if "expired" in status or "inactive" in status else "unreachable")
        sandbox = "sandbox" if org.get("isSandbox") else ("scratch" if org.get("isScratch") else "org")
        print(f"  {i}) {org.get('alias') or '-':<20} {org.get('username'):<50} {sandbox:<8} {flag}")
    print(f"  {len(orgs) + 1}) Log in to a different org (opens browser)")
    choice = _ask(f"Choose 1-{len(orgs) + 1}", str(len(orgs) + 1) if not orgs else "1")
    if choice.isdigit() and 1 <= int(choice) <= len(orgs):
        org = orgs[int(choice) - 1]
        target = org.get("alias") or org["username"]
        try:
            return display_org(target)
        except SfError as exc:
            print(f"\n! {exc}\n  The saved session expired - logging in again.")
            login_web(target, org.get("loginUrl") or org.get("instanceUrl") or "https://test.salesforce.com")
            return display_org(target)
    return display_org(_login_new())


class SalesforceRest:
    """Minimal REST/Tooling client using an OAuth access token (from the CLI or the web login).

    `org` keys: instanceUrl, accessToken, apiVersion and optionally `refresh` - a callable returning a new
    access token, used once when a request gets HTTP 401 (expired session during a long run).
    """

    def __init__(self, org: dict):
        self.base = org["instanceUrl"].rstrip("/")
        self.token = org["accessToken"]
        self.version = org.get("apiVersion") or "62.0"
        self._refresh = org.get("refresh")

    def get(self, path: str, _retry: bool = True) -> dict:
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and _retry and self._refresh:
                self.token = self._refresh()
                return self.get(path, _retry=False)
            body = exc.read().decode("utf-8", "replace")[:400]
            raise SfError(f"GET {path} -> HTTP {exc.code}: {body}") from None

    def query(self, soql: str, tooling: bool = False, limit_records: int | None = None) -> list[dict]:
        kind = "tooling/query" if tooling else "query"
        page = self.get(f"/services/data/v{self.version}/{kind}?q={urllib.parse.quote(soql)}")
        records = page.get("records", [])
        while not page.get("done", True) and (limit_records is None or len(records) < limit_records):
            page = self.get(page["nextRecordsUrl"])
            records.extend(page.get("records", []))
        for r in records:
            r.pop("attributes", None)
        return records

    def safe_query(self, soql: str, tooling: bool = False, warnings: list | None = None, **kw) -> list[dict]:
        try:
            return self.query(soql, tooling=tooling, **kw)
        except SfError as exc:
            if warnings is not None:
                warnings.append(str(exc)[:300])
            return []


if __name__ == "__main__":
    info = choose_org(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({k: info.get(k) for k in ("alias", "username", "instanceUrl", "apiVersion")}, indent=2))
