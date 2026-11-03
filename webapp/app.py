"""Salesforce Release Impact Check - local web app.

    uvicorn webapp.app:app --port 1717        (or: docker compose up)

Open http://localhost:1717 . Secrets (Salesforce tokens, Claude API key) live in this process's memory only and are
gone when it stops. Run data (inventory, source code, results, documents) is written to RC_DATA_DIR.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rc import ai_api, docs, parse_release_notes  # noqa: E402
from rc.ai_analysis import check_account, find_claude  # noqa: E402

from . import salesforce_oauth as sfo  # noqa: E402
from .jobs import JobManager  # noqa: E402

DATA_DIR = Path(os.environ.get("RC_DATA_DIR", ROOT / "output" / "web")).resolve()
UPLOADS = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("RC_MAX_UPLOAD_MB", "200"))
ENV_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ENV_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
COOKIE = "rc_sid"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Salesforce Release Impact Check", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
jobs = JobManager(DATA_DIR)
SESSIONS: dict[str, dict] = {}   # sid -> {"sf": conn, "claude": {...}, "upload": {...}}
PENDING: dict[str, dict] = {}    # oauth state -> pending login


def login_error(message: str) -> RedirectResponse:
    return RedirectResponse("/?login_error=" + urllib.parse.quote(message[:300]), status_code=303)


def session(request: Request) -> dict:
    sid = request.cookies.get(COOKIE)
    if not sid or sid not in SESSIONS:
        raise HTTPException(401, "Session expired - reload the page")
    return SESSIONS[sid]


def require_sf(sess: dict) -> dict:
    if not sess.get("sf"):
        raise HTTPException(403, "Log in to Salesforce first")
    return sess["sf"]


def claude_auth(sess: dict) -> dict | None:
    """{'mode': 'account'|'api', 'secret': ..., 'source': 'session'|'environment'} or None."""
    cl = sess.get("claude")
    if cl:
        return {"mode": cl["mode"], "secret": cl["secret"], "source": "session"}
    if ENV_OAUTH_TOKEN:
        return {"mode": "account", "secret": ENV_OAUTH_TOKEN, "source": "environment"}
    if ENV_API_KEY:
        return {"mode": "api", "secret": ENV_API_KEY, "source": "environment"}
    return None


def claude_code_available() -> bool:
    try:
        find_claude()
        return True
    except FileNotFoundError:
        return False


@app.middleware("http")
async def localhost_only_host(request: Request, call_next):
    # The OAuth callback is registered on "localhost"; keep the cookie on the same host name.
    host = request.headers.get("host", "")
    if host.startswith("127.0.0.1") and "localhost" in sfo.CALLBACK_URL:
        return RedirectResponse(str(request.url).replace("127.0.0.1", "localhost", 1))
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    resp = HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))
    sid = request.cookies.get(COOKIE)
    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {}
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
    return resp


# ------------------------------------------------------------------------------------------- Salesforce login
@app.get("/oauth/start")
def oauth_start(request: Request, env: str = "sandbox", domain: str = "", api: str = "latest"):
    sess = session(request)
    try:
        url, pending = sfo.start(env, domain, api)
    except sfo.OAuthError as exc:
        return login_error(str(exc))
    pending["sid"] = request.cookies.get(COOKIE)
    for st in [s for s, p in PENDING.items() if time.time() - p["created"] > 900]:
        PENDING.pop(st, None)
    PENDING[pending["state"]] = pending
    sess.pop("sf", None)
    return RedirectResponse(url, status_code=303)


@app.get("/OauthRedirect")
def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "", error_description: str = ""):
    pending = PENDING.pop(state, None)
    if error:
        return login_error(f"{error}: {error_description}")
    if not pending or pending["sid"] != request.cookies.get(COOKIE) or pending["sid"] not in SESSIONS:
        return login_error("Login session expired or opened in another browser - try again")
    try:
        SESSIONS[pending["sid"]]["sf"] = sfo.finish(pending, code)
    except Exception as exc:
        return login_error(str(exc))
    return RedirectResponse("/", status_code=303)


@app.post("/api/sf/logout")
def sf_logout(request: Request):
    sess = session(request)
    conn = sess.pop("sf", None)
    if conn:
        sfo.revoke(conn)
    return {"ok": True}


# ------------------------------------------------------------------------------------------- state
@app.get("/api/session")
def get_session(request: Request):
    sess = session(request)
    sf = sess.get("sf")
    cl = sess.get("claude") or {}
    auth = claude_auth(sess)
    return {
        "salesforce": None if not sf else {k: sf.get(k) for k in ("username", "display_name", "org_name", "edition",
                                                                 "is_sandbox", "instance_url", "instance_name",
                                                                 "api_version", "env", "connected_at")},
        "claude": {"configured": bool(auth), "mode": auth and auth["mode"], "source": auth and auth["source"],
                   "models": cl.get("models") or list(ai_api.MODELS), "default_model": ai_api.DEFAULT_MODEL,
                   "account_available": claude_code_available()},
        "upload": sess.get("upload"),
        "oauth_client": "Salesforce CLI (PlatformCLI)" if sfo.CLIENT_ID == "PlatformCLI" else "custom External Client App",
    }


class KeyIn(BaseModel):
    api_key: str = Field(min_length=20, max_length=300)


@app.post("/api/claude/key")
def set_key(request: Request, body: KeyIn):
    sess = session(request)
    try:
        models = ai_api.validate_key(body.api_key.strip())
    except Exception as exc:
        raise HTTPException(400, f"The API key was rejected: {str(exc)[:200]}")
    sess["claude"] = {"mode": "api", "secret": body.api_key.strip(), "models": models}
    return {"ok": True, "models": models}


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)


@app.post("/api/claude/account")
def set_account(request: Request, body: TokenIn):
    """Claude account (Pro / Max / Team / Enterprise) through the official Claude Code CLI: the user creates the
    token with `claude setup-token`; analyses then run on Claude Code and count against that plan."""
    sess = session(request)
    if not claude_code_available():
        raise HTTPException(400, "Claude Code is not installed in this environment - use the Docker image or install it")
    token = body.token.strip()
    try:
        check_account({"CLAUDE_CODE_OAUTH_TOKEN": token})
    except Exception as exc:
        raise HTTPException(400, f"The Claude account token was rejected: {str(exc)[:200]}")
    sess["claude"] = {"mode": "account", "secret": token, "models": list(ai_api.MODELS)}
    return {"ok": True}


@app.delete("/api/claude/key")
def clear_key(request: Request):
    session(request).pop("claude", None)
    return {"ok": True}


# ------------------------------------------------------------------------------------------- release notes
@app.post("/api/release-notes")
def upload_release_notes(request: Request, file: UploadFile = File(...)):
    sess = session(request)
    require_sf(sess)
    name = Path(file.filename or "release_notes").name
    ext = Path(name).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, "Upload the release notes as PDF or Word (.docx)")
    digest, size = hashlib.sha256(), 0
    tmp = UPLOADS / f"upload-{secrets.token_hex(6)}{ext}"
    with tmp.open("wb") as fh:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB << 20:
                fh.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, f"File larger than {MAX_UPLOAD_MB} MB")
            digest.update(chunk)
            fh.write(chunk)
    uid = digest.hexdigest()[:16]
    stored = UPLOADS / f"{uid}{ext}"
    tmp.replace(stored)
    features = UPLOADS / f"{uid}.features.json"
    if not features.exists():
        try:
            parsed = parse_release_notes.parse(stored)
        except Exception as exc:
            raise HTTPException(422, f"Could not read the release notes: {str(exc)[:300]}")
        if parsed["feature_count"] < 20:
            raise HTTPException(422, f"Only {parsed['feature_count']} features found - is this a Salesforce "
                                     "release notes document?")
        features.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
    parsed = json.loads(features.read_text(encoding="utf-8"))
    sess["upload"] = {"id": uid, "filename": name, "release": parsed["release"], "features": parsed["feature_count"],
                      "pages": parsed.get("page_count"), "format": ext[1:].upper()}
    return sess["upload"]


# ------------------------------------------------------------------------------------------- runs
class RunIn(BaseModel):
    model: str = ai_api.DEFAULT_MODEL
    include_source: bool = True
    parallel: int = Field(4, ge=1, le=8)
    budget_usd: float | None = Field(30.0, ge=1, le=1000)
    no_ai: bool = False


@app.post("/api/runs")
def start_run(request: Request, body: RunIn):
    sess = session(request)
    conn = require_sf(sess)
    upload = sess.get("upload")
    if not upload:
        raise HTTPException(400, "Upload the release notes first")
    auth = claude_auth(sess)
    if not body.no_ai and not auth:
        raise HTTPException(400, "Connect Claude first - account or API key (or choose rule-based only)")
    if body.model not in ai_api.MODELS:
        raise HTTPException(400, "Unknown model")
    options = {**body.model_dump(), "engine": "none" if body.no_ai else auth["mode"]}
    job = jobs.create(UPLOADS / f"{upload['id']}.features.json", upload["filename"], conn,
                      None if body.no_ai else auth, options)
    return {"id": job.state["id"]}


@app.get("/api/runs")
def list_runs(request: Request):
    session(request)
    return jobs.list()


@app.get("/api/runs/{run_id}")
def run_state(request: Request, run_id: str):
    session(request)
    st = jobs.state(run_id)
    if not st:
        raise HTTPException(404, "Run not found")
    d = jobs.get_dir(run_id)
    st["files"] = sorted(p.name for p in (d / "report").iterdir()) if d and (d / "report").is_dir() else []
    return st


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str):
    session(request)
    job = jobs.jobs.get(run_id)
    if not job:
        raise HTTPException(404, "Run is not active")
    job.cancel.set()
    job.log("Cancel requested - stopping after the current step")
    return {"ok": True}


@app.delete("/api/runs/{run_id}")
def delete_run(request: Request, run_id: str):
    session(request)
    if not jobs.delete(run_id):
        raise HTTPException(409, "Run not found or still running")
    return {"ok": True}


class DocIn(BaseModel):
    kind: str = Field(pattern="^(technical|business)$")
    format: str = Field(pattern="^(docx|pdf)$")


@app.post("/api/runs/{run_id}/documents")
def make_document(request: Request, run_id: str, body: DocIn):
    session(request)
    d = jobs.get_dir(run_id)
    st = jobs.state(run_id)
    if not d or not st or st["status"] not in ("done",) or not (d / "report" / "report_data.json").exists():
        raise HTTPException(409, "The analysis has not finished yet")
    path = docs.generate(d, body.kind, body.format)
    return {"file": path.name, "url": f"/api/runs/{run_id}/files/{path.name}"}


@app.get("/api/runs/{run_id}/files/{name}")
def download(request: Request, run_id: str, name: str):
    session(request)
    d = jobs.get_dir(run_id)
    if not d or "/" in name or "\\" in name:
        raise HTTPException(404)
    path = (d / "report" / name)
    if not path.is_file() or path.parent.resolve() != (d / "report").resolve():
        raise HTTPException(404)
    inline = name.endswith(".html") and request.query_params.get("view") == "1"
    return FileResponse(path, filename=None if inline else name)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
