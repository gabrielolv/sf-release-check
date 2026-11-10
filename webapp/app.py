"""Salesforce Release Impact Check - local web app.

    uvicorn webapp.app:app --port 1717        (or: docker compose up)

Open http://localhost:1717 . Secrets (Salesforce tokens, AI API keys / tokens) live in this process's memory only and are
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

from rc import ai_api, ai_openai, docs, parse_release_notes  # noqa: E402
from rc.ai_analysis import check_account, find_claude  # noqa: E402

from . import salesforce_oauth as sfo  # noqa: E402
from .jobs import JobManager  # noqa: E402

DATA_DIR = Path(os.environ.get("RC_DATA_DIR", ROOT / "output" / "web")).resolve()
UPLOADS = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("RC_MAX_UPLOAD_MB", "200"))
ENV_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ENV_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
ENV_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
ENV_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
ENV_LLM_URL = os.environ.get("RC_LLM_BASE_URL", "")    # local / private OpenAI-compatible server
ENV_LLM_KEY = os.environ.get("RC_LLM_API_KEY", "")
ENV_LLM_MODEL = os.environ.get("RC_LLM_MODEL", "")
COOKIE = "rc_sid"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Salesforce Release Impact Check", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
jobs = JobManager(DATA_DIR)
SESSIONS: dict[str, dict] = {}   # sid -> {"sf": conn, "ai": {...}, "upload": {...}}
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


AI_LABELS = {"claude-account": "Claude account", "claude": "Claude API", "openai": "OpenAI", "gemini": "Gemini",
             "other": "Custom API", "local": "Local server"}
_ENV_MODELS: dict[str, dict] = {}   # models listed for env-configured OpenAI-compatible providers (cached on success)


def _env_openai_like(provider: str, key: str, base_url: str = "") -> dict:
    if provider not in _ENV_MODELS:
        try:
            base, models = ai_openai.connect(provider, key, base_url)
        except ai_openai.LLMError:
            return {"base_url": base_url or ai_openai.PROVIDERS[provider][1], "models": []}
        _ENV_MODELS[provider] = {"base_url": base, "models": models}
    return _ENV_MODELS[provider]


def ai_auth(sess: dict) -> dict | None:
    """The connected AI model: {'provider', 'mode': 'account'|'api'|'openai', 'secret', 'models', 'base_url',
    'model', 'source': 'session'|'environment'} or None. A page connection wins over environment variables."""
    ai = sess.get("ai")
    if ai:
        return {**ai, "source": "session"}
    claude_models = list(ai_api.MODELS)
    if ENV_OAUTH_TOKEN:
        return {"provider": "claude-account", "mode": "account", "secret": ENV_OAUTH_TOKEN, "models": claude_models,
                "source": "environment"}
    if ENV_API_KEY:
        return {"provider": "claude", "mode": "api", "secret": ENV_API_KEY, "models": claude_models,
                "source": "environment"}
    for provider, key in (("openai", ENV_OPENAI_KEY), ("gemini", ENV_GEMINI_KEY)):
        if key:
            return {"provider": provider, "mode": "openai", "secret": key, "source": "environment",
                    **_env_openai_like(provider, key)}
    if ENV_LLM_URL:
        info = _env_openai_like("local", ENV_LLM_KEY, ENV_LLM_URL)
        models = info["models"] or ([ENV_LLM_MODEL] if ENV_LLM_MODEL else [])
        return {"provider": "local", "mode": "openai", "secret": ENV_LLM_KEY, "source": "environment",
                "base_url": info["base_url"], "models": models, "model": ENV_LLM_MODEL}
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
    auth = ai_auth(sess)
    return {
        "salesforce": None if not sf else {k: sf.get(k) for k in ("username", "display_name", "org_name", "edition",
                                                                 "is_sandbox", "instance_url", "instance_name",
                                                                 "api_version", "env", "connected_at")},
        "ai": {"configured": bool(auth), "provider": auth and auth["provider"],
               "label": auth and AI_LABELS[auth["provider"]], "mode": auth and auth["mode"],
               "source": auth and auth["source"], "base_url": auth and auth.get("base_url"),
               "models": auth["models"] if auth else list(ai_api.MODELS),
               "default_model": (auth.get("model") or "") if auth and auth["mode"] == "openai"
               else ai_api.DEFAULT_MODEL,
               "account_available": claude_code_available()},
        "upload": sess.get("upload"),
        "oauth_client": "Salesforce CLI (PlatformCLI)" if sfo.CLIENT_ID == "PlatformCLI" else "custom External Client App",
    }


def _connect_openai_like(provider: str, key: str, base_url: str, model: str) -> dict:
    """Validate an OpenAI / Gemini / custom / local endpoint. A model that is given is checked for tool calling now;
    otherwise the model chosen in step 4 is checked when the analysis starts."""
    if provider in ("other", "local") and not base_url.strip():
        raise HTTPException(400, "Enter the base URL of the server, for example http://host.docker.internal:11434/v1")
    model = model.strip()
    try:
        base, models = ai_openai.connect(provider, key, base_url)
        if model:
            ai_openai.check_tool_calling(ai_openai.Client(base, key, 180), model)
            models = [model] + [m for m in models if m != model]
    except ai_openai.LLMError as exc:  # includes ModelRejected
        raise HTTPException(400, str(exc))
    if not models:
        raise HTTPException(400, "The server did not list its models - type the model name and connect again")
    return {"provider": provider, "mode": "openai", "secret": key, "base_url": base, "models": models, "model": model}


class KeyIn(BaseModel):
    provider: str = Field("claude", pattern="^(claude|openai|gemini|other)$")
    api_key: str = Field("", max_length=400)
    base_url: str = Field("", max_length=300)
    model: str = Field("", max_length=200)


@app.post("/api/ai/key")
def set_key(request: Request, body: KeyIn):
    sess = session(request)
    key = body.api_key.strip()
    if body.provider != "other" and len(key) < 8:
        raise HTTPException(400, "Paste the API key")
    if body.provider == "claude":
        try:
            models = ai_api.validate_key(key)
        except Exception as exc:
            raise HTTPException(400, f"The API key was rejected: {str(exc)[:200]}")
        sess["ai"] = {"provider": "claude", "mode": "api", "secret": key, "models": models}
    else:
        sess["ai"] = _connect_openai_like(body.provider, key, body.base_url, body.model)
    return {"ok": True}


class LocalIn(BaseModel):
    base_url: str = Field(min_length=8, max_length=300)
    api_key: str = Field("", max_length=400)
    model: str = Field("", max_length=200)


@app.post("/api/ai/local")
def set_local(request: Request, body: LocalIn):
    """A local (Ollama, LM Studio, vLLM, llama.cpp...) or private server with an OpenAI-compatible API."""
    session(request)["ai"] = _connect_openai_like("local", body.api_key.strip(), body.base_url, body.model)
    return {"ok": True}


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)


@app.post("/api/ai/account")
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
    sess["ai"] = {"provider": "claude-account", "mode": "account", "secret": token, "models": list(ai_api.MODELS)}
    return {"ok": True}


@app.delete("/api/ai")
def clear_ai(request: Request):
    session(request).pop("ai", None)
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
    budget_usd: float | None = Field(30.0, ge=1, le=1000)       # Claude API key
    token_cap_m: float | None = Field(15.0, ge=0.5, le=1000)    # OpenAI / Gemini / custom API: millions of tokens
    no_ai: bool = False


@app.post("/api/runs")
def start_run(request: Request, body: RunIn):
    sess = session(request)
    conn = require_sf(sess)
    upload = sess.get("upload")
    if not upload:
        raise HTTPException(400, "Upload the release notes first")
    auth = ai_auth(sess)
    if not body.no_ai and not auth:
        raise HTTPException(400, "Connect an AI model first - Claude account, API key or local server "
                                 "(or choose rule-based only)")
    options = {**body.model_dump(), "engine": "none" if body.no_ai else auth["mode"]}
    if not body.no_ai:
        options["provider"] = auth["provider"]
        options["provider_label"] = AI_LABELS[auth["provider"]]
        if auth["mode"] == "openai":
            body.model = options["model"] = body.model.strip()
            if not body.model:
                raise HTTPException(400, "Choose a model")
            try:  # reject models without tool calling before anything is downloaded or paid for
                ai_openai.check_tool_calling(ai_openai.Client(auth["base_url"], auth["secret"], 180), body.model)
            except ai_openai.LLMError as exc:
                raise HTTPException(400, str(exc))
            if auth["provider"] == "local":
                options["token_cap_m"] = None
        elif body.model not in ai_api.MODELS:
            raise HTTPException(400, "Unknown model")
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
