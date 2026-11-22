"""AI analysis through any OpenAI-compatible Chat Completions endpoint: OpenAI, Google Gemini (its OpenAI-compatible
endpoint), Ollama, LM Studio, vLLM, llama.cpp or a private server.

Same inputs, rules, tools and outputs as the Claude engines: the batch orchestration is ai_api.run_analysis(), the
rules come from .claude/skills/sf-release-impact/references/ and the org tools (read-only SOQL, source search,
release-notes lookup) are the ones built by ai_api.build_tools().

The model must support tool calling (checked with check_tool_calling() before a run). The final answer is delivered
through a `submit_result` tool whose parameters are the JSON schema, which works on any tool-calling model and does
not depend on the server's structured-output support. A plain-JSON reply is accepted as a fallback.
Only the standard library (urllib) is used for HTTP, so there is no extra dependency.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .ai_api import (BATCH_SCHEMA, REFERENCES, SUMMARY_SCHEMA, Cancelled, build_tools, run_analysis)  # noqa: F401
from .sfcli import SalesforceRest

PROVIDERS = {  # provider -> (label, default base URL)
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "other": ("OpenAI-compatible API", ""),
    "local": ("Local / private server", ""),
}
SUBMIT = "submit_result"
MAX_ITERATIONS = 25
MAX_NUDGES = 2
NOT_CHAT = ("embed", "whisper", "tts", "dall-e", "moderation", "image", "audio", "realtime", "transcribe", "imagen",
            "veo", "aqa", "davinci", "babbage", "computer-use", "search")


class LLMError(RuntimeError):
    """A request to the model server failed (message is safe to show: it never contains the API key)."""


class ModelRejected(LLMError):
    """The model cannot be used for the analysis (for example, it has no tool calling)."""


def _error_text(status: int, body: str, reason: str = "") -> str:
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", data) if isinstance(data, dict) else data
        text = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        text = body
    return f"HTTP {status}: {(text or reason or '').strip()[:300]}"


def _unreachable_hint(base_url: str) -> str:
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return (" Inside Docker, 'localhost' is the container itself: use http://host.docker.internal:<port>/v1 "
                "to reach a server running on this computer.")
    return ""


class Client:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 900, cancel: threading.Event | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.cancel = cancel or threading.Event()

    def _once(self, method: str, path: str, body: dict | None) -> tuple[int, str, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers or {})

    def _request(self, method: str, path: str, body: dict | None = None, retries: int = 5) -> dict:
        last = "no response"
        for attempt in range(retries + 1):
            if self.cancel.is_set():
                raise Cancelled("cancelled by user")
            try:
                status, text, headers = self._once(method, path, body)
            except (socket.timeout, TimeoutError):
                raise LLMError("the model server did not answer in time (slow or overloaded model)") from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    raise LLMError("the model server did not answer in time (slow or overloaded model)") from None
                last = f"cannot connect to {self.base_url}: {exc.reason}"
                if attempt >= 1:  # a wrong address will not fix itself - fail fast
                    raise LLMError(last[:300] + _unreachable_hint(self.base_url)) from None
                self.cancel.wait(2)
                continue
            except OSError as exc:
                raise LLMError(f"request failed: {exc}"[:300]) from None
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                last = _error_text(status, text)
                try:
                    wait = float({k.lower(): v for k, v in headers.items()}.get("retry-after", ""))
                except ValueError:
                    wait = 2.0 * 2 ** attempt
                self.cancel.wait(min(wait, 60))
                continue
            if status >= 400:
                raise LLMError(_error_text(status, text))
            try:
                return json.loads(text)
            except ValueError:
                raise LLMError("the server did not return JSON - is the base URL an OpenAI-compatible /v1 "
                               "endpoint?") from None
        raise LLMError(last[:300])

    def list_models(self) -> list[str]:
        data = self._request("GET", "/models", retries=1)
        rows = data.get("data") if isinstance(data, dict) else data
        ids = []
        for row in rows or []:
            mid = (row.get("id") or row.get("name") or "") if isinstance(row, dict) else str(row)
            mid = mid.removeprefix("models/")
            if mid and not any(w in mid.lower() for w in NOT_CHAT):
                ids.append(mid)
        return sorted(set(ids))

    def chat(self, payload: dict) -> dict:
        return self._request("POST", "/chat/completions", payload)


def connect(provider: str, api_key: str, base_url: str = "") -> tuple[str, list[str]]:
    """Validate the credentials/endpoint. Returns (normalised base URL, chat model ids it lists - may be empty)."""
    if provider not in PROVIDERS:
        raise LLMError("unknown provider")
    base = (base_url.strip() or PROVIDERS[provider][1]).rstrip("/")
    base = re.sub(r"/chat/completions$", "", base)
    if not re.match(r"^https?://", base):
        raise LLMError("the base URL must start with http:// or https://")
    candidates = [base] if base.endswith(("/v1", "/openai", "/v1beta")) or provider in ("openai", "gemini") \
        else [base, base + "/v1"]
    last: LLMError | None = None
    for cand in candidates:
        try:
            return cand, Client(cand, api_key, timeout=30).list_models()
        except LLMError as exc:
            last = exc
            text = str(exc).lower()
            if "http 401" in text or "http 403" in text or "api key" in text:
                raise LLMError(f"the API key was rejected ({exc})") from None
            if "cannot connect" in text or "did not answer" in text:
                raise
    # The server answered but has no /models route (some private servers do not): accept it, the model is typed.
    if provider in ("local", "other") and last and ("HTTP 404" in str(last) or "HTTP 405" in str(last)):
        return candidates[0], []
    raise last or LLMError("could not connect")


def check_tool_calling(client: Client, model: str) -> None:
    """Raise ModelRejected unless the model answers a prompt by calling a tool."""
    spec = {"type": "function", "function": {
        "name": "echo", "description": "Return the given text.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
    try:
        resp = client.chat({
            "model": model, "tools": [spec],
            "messages": [{"role": "user", "content": "Call the echo function with text set to \"pong\". "
                                                     "Do not answer in plain text."}]})
    except LLMError as exc:
        text = str(exc)
        if any(w in text.lower() for w in ("tool", "function")):
            raise ModelRejected(f"'{model}' does not support tool calling ({text}). Choose a model with tool/function "
                                "calling.") from None
        raise ModelRejected(f"'{model}' could not be used: {text}") from None
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    calls = msg.get("tool_calls") or []
    if not any((c.get("function") or {}).get("name") == "echo" for c in calls):
        raise ModelRejected(f"'{model}' does not support tool calling: it answered in text instead of calling the "
                            "test function. The analysis needs a model with tool/function calling (for Ollama, pick "
                            "a model tagged 'tools').")


class TokenBudget:
    """Thread-safe token counter with an optional cap on new input + output tokens (cached reads are not counted)."""

    def __init__(self, cap_tokens: int | None):
        self.cap = cap_tokens
        self.total = 0.0  # USD is unknown for these providers
        self.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self._lock = threading.Lock()

    def add(self, usage: dict | None) -> None:
        u = usage or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        prompt = u.get("prompt_tokens") or 0
        with self._lock:
            self.tokens["input"] += max(0, prompt - cached)
            self.tokens["cache_read"] += cached
            self.tokens["output"] += u.get("completion_tokens") or 0

    def exceeded(self) -> bool:
        return self.cap is not None and self.tokens["input"] + self.tokens["output"] >= self.cap


def system_text(org_profile: str) -> str:
    rules = "\n\n".join((REFERENCES / name).read_text(encoding="utf-8")
                        for name in ("analysis-rules.md", "impact-rubric.md", "audience-guide.md"))
    tools_note = ("## Tools in this session\n"
                  "- `soql_query`: read-only SOQL (Tooling API with tooling=true) against the org below.\n"
                  "- `grep_org_source` / `read_org_source`: the org's unmanaged Apex, Visualforce, LWC and Aura source.\n"
                  "- `get_release_feature` / `search_release_notes`: the full parsed release notes.\n"
                  "Use them when evidence would change a rating; skip them when the org profile already settles it.\n"
                  f"When you are done, call `{SUBMIT}` exactly once with the complete result. Every field of the "
                  "schema is required. Do not write the answer as plain text.")
    return "\n\n".join([rules, tools_note, "## Org profile\n" + org_profile])


def _strip_schema(node):
    """Gemini's function-declaration schema rejects some JSON-Schema keywords; they are not needed here."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("additionalProperties", "title", "default"):
                continue  # schema keywords only - a property *named* title is handled below
            out[k] = ({name: _strip_schema(sub) for name, sub in v.items()}
                      if k == "properties" and isinstance(v, dict) else _strip_schema(v))
        return out
    if isinstance(node, list):
        return [_strip_schema(v) for v in node]
    return node


def _problems(schema: dict, data, path: str = "result") -> list[str]:
    """Minimal JSON-schema check (type, required, enum, items) whose messages are fed back to the model."""
    out: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            return [f"{path} must be an object"]
        for key in schema.get("required", []):
            if key not in data:
                out.append(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                out += _problems(sub, data[key], f"{path}.{key}")
    elif t == "array":
        if not isinstance(data, list):
            return [f"{path} must be an array"]
        for i, item in enumerate(data[:200]):
            out += _problems(schema.get("items", {}), item, f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            out.append(f"{path} must be a string")
        elif "enum" in schema and data not in schema["enum"]:
            out.append(f"{path} must be one of {schema['enum']}")
    return out[:6]


def _parse_json(text: str):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for cand in ([fenced.group(1)] if fenced else []) + [text]:
        try:
            return json.loads(cand)
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


def _call(client: Client, model: str, system: str, user: str, schema: dict, tools: list, budget: TokenBudget,
          cancel: threading.Event, on_tool: Callable[[str, str], None]) -> tuple[dict, str]:
    """One tool conversation that must end in a schema-valid result. Returns (data, stop_reason)."""
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                               "parameters": _strip_schema(t.input_schema)}} for t in tools]
    specs.append({"type": "function", "function": {
        "name": SUBMIT, "description": "Submit the final, complete result. Call it once, when the assessment is done.",
        "parameters": _strip_schema(schema)}})
    by_name = {t.name: t for t in tools}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    nudges = 0
    for _ in range(MAX_ITERATIONS):
        if cancel.is_set():
            raise Cancelled("cancelled by user")
        if budget.exceeded():
            raise Cancelled(f"token cap of {budget.cap:,} reached")
        resp = client.chat({"model": model, "messages": messages, "tools": specs})
        budget.add(resp.get("usage"))
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if choice.get("finish_reason") == "length":
            return {}, "max_tokens"
        calls = msg.get("tool_calls") or []
        if calls:
            # echo the assistant turn back (minus reasoning fields some servers refuse to receive)
            messages.append({k: v for k, v in msg.items() if v is not None and k not in ("reasoning", "reasoning_content")})
            for call in calls:
                fn = call.get("function") or {}
                name, raw = fn.get("name", ""), fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except ValueError:
                    args = None
                if args is None:
                    result = "ERROR: the arguments were not valid JSON."
                elif name == SUBMIT:
                    problems = _problems(schema, args)
                    if not problems:
                        return args, "end_turn"
                    result = "ERROR: fix these problems and call submit_result again: " + "; ".join(problems)
                elif name in by_name:
                    on_tool(name, raw[:160] if isinstance(raw, str) else json.dumps(raw)[:160])
                    try:
                        result = str(by_name[name].call(args))
                    except Exception as exc:
                        result = f"ERROR: {str(exc)[:400]}"
                else:
                    result = f"ERROR: unknown tool '{name}'."
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            continue
        # no tool call: accept a plain JSON answer, otherwise ask for submit_result
        data = _parse_json(msg.get("content") or "")
        if data is not None and not _problems(schema, data):
            return data, "end_turn"
        nudges += 1
        if nudges > MAX_NUDGES:
            raise LLMError("the model did not return a valid result (no usable submit_result call)")
        messages.append({"role": "assistant", "content": msg.get("content") or ""})
        messages.append({"role": "user", "content": f"Submit the final result now by calling `{SUBMIT}` with every "
                                                    "required field. Do not reply in plain text."})
    raise LLMError("the model kept calling tools without submitting a result")


def analyse_openai(run_dir: Path, base_url: str, api_key: str, model: str, parallel: int = 4,
                   token_cap: int | None = None, org_rest: SalesforceRest | None = None,
                   on_event: Callable[[dict], None] = lambda e: None,
                   cancel: threading.Event | None = None) -> dict:
    """Batch assessments + synthesis on an OpenAI-compatible model. Same events and result shape as analyse_api()."""
    cancel = cancel or threading.Event()
    client = Client(base_url, api_key, cancel=cancel)
    budget = TokenBudget(token_cap)
    return run_analysis(
        run_dir, model, parallel, budget, org_rest, on_event, cancel, system_text,
        lambda system, user, schema, effort, tools, on_tool:
            _call(client, model, system, user, schema, tools, budget, cancel, on_tool))
