"""AI analysis through the Claude API (Anthropic Python SDK tool runner) - used by the web app / Docker image.

Same inputs, rules and outputs as the Claude Code engine (ai_analysis.py): the rules come from
.claude/skills/sf-release-impact/references/, results are merged by ai_analysis.merge_assessments().
The model can verify usage with read-only tools: SOQL against the org, grep/read of the downloaded source,
lookup/search of the parsed release notes. Results are returned as schema-validated JSON.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import anthropic
from anthropic import beta_tool

from .ai_analysis import IMPACTS, make_batches, merge_assessments, org_digest, write_merged
from .sfcli import SalesforceRest

REFERENCES = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "sf-release-impact" / "references"
MODELS = {  # USD per million tokens: input, output, cache write (5 min), cache read - used for the cost estimate
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
}
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}  # server-side refusal fallback
DEEP_SIZE, LIGHT_SIZE = 12, 35
MAX_TOKENS = 16000
MAX_SOQL_PER_BATCH = 25

_str_list = {"type": "array", "items": {"type": "string"}}
ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "impact": {"type": "string", "enum": list(IMPACTS)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "org_evidence": _str_list,
        "affected_components": _str_list,
        "actions": _str_list,
        "owner_roles": _str_list,
        "effort": {"type": "string", "enum": ["None", "S", "M", "L"]},
        "deadline": {"type": "string"},
        "audiences": {"type": "array", "items": {"type": "string",
                                                 "enum": ["Developers", "Architects", "PM/PO", "Managers"]}},
        "test_notes": {"type": "string"},
    },
    "required": ["id", "impact", "confidence", "summary", "rationale", "org_evidence", "affected_components",
                 "actions", "owner_roles", "effort", "deadline", "audiences", "test_notes"],
    "additionalProperties": False,
}
BATCH_SCHEMA = {"type": "object", "properties": {"assessments": {"type": "array", "items": ASSESSMENT_SCHEMA}},
                "required": ["assessments"], "additionalProperties": False}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SUMMARY_SCHEMA = _obj({
    "headline": {"type": "string"},
    "overall_risk": {"type": "string", "enum": ["High", "Medium", "Low"]},
    "executive_summary": {"type": "string"},
    "top_risks": {"type": "array", "items": _obj({
        "title": {"type": "string"}, "impact": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "feature_ids": _str_list, "why": {"type": "string"}, "mitigation": {"type": "string"},
        "owner": {"type": "string"}, "deadline": {"type": "string"}})},
    "key_dates": {"type": "array", "items": _obj({"date": {"type": "string"}, "event": {"type": "string"},
                                                  "source": {"type": "string"}})},
    "audiences": _obj({
        "managers": _obj({"summary": {"type": "string"}, "decisions_needed": _str_list}),
        "pm_po": _obj({"summary": {"type": "string"}, "user_facing_changes": _str_list,
                       "communication_needs": _str_list, "opportunities": _str_list}),
        "architects": _obj({"summary": {"type": "string"}, "considerations": _str_list}),
        "developers": _obj({"summary": {"type": "string"}, "tasks": {"type": "array", "items": _obj({
            "component": {"type": "string"}, "task": {"type": "string"}, "feature_ids": _str_list})}}),
    }),
    "plan": {"type": "array", "items": _obj({"phase": {"type": "string"}, "when": {"type": "string"},
                                             "activities": _str_list})},
    "assumptions_limitations": _str_list,
})


class Budget:
    """Thread-safe running cost estimate with an optional cap."""

    def __init__(self, model: str, cap_usd: float | None):
        self.prices = MODELS.get(model, MODELS[DEFAULT_MODEL])
        self.cap = cap_usd
        self.total = 0.0
        self.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self._lock = threading.Lock()

    def add(self, usage) -> float:
        p_in, p_out, p_cw, p_cr = self.prices
        n = {"input": usage.input_tokens or 0, "output": usage.output_tokens or 0,
             "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
             "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0}
        cost = (n["input"] * p_in + n["output"] * p_out + n["cache_write"] * p_cw + n["cache_read"] * p_cr) / 1_000_000
        with self._lock:
            self.total += cost
            for k, v in n.items():
                self.tokens[k] += v
            return self.total

    def exceeded(self) -> bool:
        return self.cap is not None and self.total >= self.cap


class Cancelled(RuntimeError):
    pass


def system_blocks(org_profile: str) -> list[dict]:
    """Stable system prompt (rules + rubric + audience guide + org profile), cached across all calls of a run."""
    parts = [(REFERENCES / name).read_text(encoding="utf-8")
             for name in ("analysis-rules.md", "impact-rubric.md", "audience-guide.md")]
    tools_note = ("## Tools in this session\n"
                  "- `soql_query`: read-only SOQL (Tooling API with tooling=true) against the org below.\n"
                  "- `grep_org_source` / `read_org_source`: the org's unmanaged Apex, Visualforce, LWC and Aura source.\n"
                  "- `get_release_feature` / `search_release_notes`: the full parsed release notes.\n"
                  "Use them when evidence would change a rating; skip them when the org profile already settles it.\n"
                  "Your final answer is the JSON object required by the response schema - nothing else.")
    return [{"type": "text", "text": "\n\n".join(parts + [tools_note])},
            {"type": "text", "text": org_profile, "cache_control": {"type": "ephemeral"}}]


def build_tools(run_dir: Path, rest: SalesforceRest | None, counter: dict):
    src_root = (run_dir / "org" / "org_source").resolve()
    release = json.loads((run_dir / "release_features.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in release["features"]}

    @beta_tool
    def soql_query(soql: str, tooling: bool = False) -> str:
        """Run a read-only SOQL query against the Salesforce org being assessed.

        Args:
            soql: One SELECT statement. Include a LIMIT; at most 200 rows are returned.
            tooling: True to use the Tooling API (ApexClass, Flow, CustomField, FlexiPage...).
        """
        q = soql.strip().rstrip(";")
        if not re.match(r"(?is)^select\s", q) or ";" in q:
            return "ERROR: only a single SELECT statement is allowed."
        if rest is None:
            return "ERROR: no org connection available in this run."
        counter["soql"] = counter.get("soql", 0) + 1
        if counter["soql"] > MAX_SOQL_PER_BATCH:
            return "ERROR: query limit for this batch reached - decide with the evidence you have."
        try:
            rows = rest.query(q, tooling=tooling, limit_records=200)[:200]
        except Exception as exc:  # SOQL errors go back to the model so it can fix the query
            return f"ERROR: {str(exc)[:500]}"
        return json.dumps({"rows": len(rows), "records": rows}, default=str)[:12000]

    @beta_tool
    def grep_org_source(pattern: str, max_results: int = 40) -> str:
        """Search the org's unmanaged source code (Apex, Visualforce, LWC, Aura) with a regular expression.

        Args:
            pattern: Python regular expression, matched case-insensitively per line.
            max_results: Maximum matching lines to return (default 40).
        """
        if not src_root.exists():
            return "No source code was downloaded for this run."
        try:
            rx = re.compile(pattern, re.I)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"
        hits = []
        for path in sorted(src_root.rglob("*")):
            if not path.is_file():
                continue
            for no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path.relative_to(src_root).as_posix()}:{no}: {line.strip()[:200]}")
                    if len(hits) >= max(1, min(max_results, 200)):
                        return "\n".join(hits)
        return "\n".join(hits) or "No matches."

    @beta_tool
    def read_org_source(path: str, start_line: int = 1, line_count: int = 200) -> str:
        """Read part of one source file found with grep_org_source.

        Args:
            path: File path relative to the source root, e.g. classes/LoginSvc.cls.
            start_line: First line to return (1-based).
            line_count: Number of lines to return (max 400).
        """
        target = (src_root / path).resolve()
        if src_root not in target.parents or not target.is_file():
            return "ERROR: file not found in the org source."
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        chunk = lines[start - 1:start - 1 + max(1, min(line_count, 400))]
        return "\n".join(f"{start + i}: {l}" for i, l in enumerate(chunk)) or "(empty range)"

    @beta_tool
    def get_release_feature(feature_id: str) -> str:
        """Return the full release-note text of one feature.

        Args:
            feature_id: Feature id such as F1a2b3c4d.
        """
        f = by_id.get(feature_id)
        return json.dumps(f, ensure_ascii=False)[:15000] if f else "ERROR: unknown feature id."

    @beta_tool
    def search_release_notes(query: str, max_results: int = 15) -> str:
        """Find release-note features whose title or text contains all the given words.

        Args:
            query: Words to look for, e.g. "username-password flow".
            max_results: Maximum features to list (default 15).
        """
        words = [w.lower() for w in query.split() if w]
        found = [f"{f['id']} | {f['area']} | {f['title']}" for f in release["features"]
                 if all(w in (f["title"] + " " + f["text"]).lower() for w in words)]
        return "\n".join(found[:max(1, min(max_results, 50))]) or "No matching features."

    return [soql_query, grep_org_source, read_org_source, get_release_feature, search_release_notes]


def _call(client, model: str, system: list, user: str, schema: dict, effort: str, tools: list,
          budget: Budget, cancel: threading.Event, on_tool: Callable[[str, str], None]) -> tuple[dict, str]:
    """One tool-runner conversation that must end in schema-valid JSON. Returns (data, stop_reason)."""
    extra = {}
    if model in FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    runner = client.beta.messages.tool_runner(
        model=model, max_tokens=MAX_TOKENS, system=system, tools=tools, max_iterations=20,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}], **extra)
    last = None
    for message in runner:
        last = message
        budget.add(message.usage)
        for block in message.content:
            if block.type == "tool_use":
                on_tool(block.name, json.dumps(block.input)[:160])
        if cancel.is_set():
            raise Cancelled("cancelled by user")
        if budget.exceeded():
            raise Cancelled(f"budget of ${budget.cap:.2f} reached")
    if last is None:
        raise RuntimeError("no response")
    if last.stop_reason == "refusal":
        raise RuntimeError("the model declined this batch (refusal)")
    text = "".join(b.text for b in last.content if b.type == "text")
    if last.stop_reason == "max_tokens" or not text.strip():
        return {}, "max_tokens"
    return json.loads(text), last.stop_reason


def analyse_api(run_dir: Path, api_key: str, model: str = DEFAULT_MODEL, parallel: int = 4,
                budget_usd: float | None = None, org_rest: SalesforceRest | None = None,
                on_event: Callable[[dict], None] = lambda e: None,
                cancel: threading.Event | None = None) -> dict:
    """Run batch assessments + synthesis via the Claude API. Emits progress events through `on_event`."""
    cancel = cancel or threading.Event()
    run_dir = run_dir.resolve()
    ai_dir = run_dir / "ai"
    for sub in ("batches", "results"):
        (ai_dir / sub).mkdir(parents=True, exist_ok=True)
    pre = json.loads((run_dir / "prefilter.json").read_text(encoding="utf-8"))
    inv = json.loads((run_dir / "org" / "org_inventory.json").read_text(encoding="utf-8"))
    profile = org_digest(inv)
    (ai_dir / "org_profile.md").write_text(profile, encoding="utf-8")
    system = system_blocks(profile)
    client = anthropic.Anthropic(api_key=api_key, max_retries=4)
    budget = Budget(model, budget_usd)
    batches = make_batches(pre, DEEP_SIZE, LIGHT_SIZE)
    total = len(batches)
    on_event({"type": "batches", "total": total, "features": sum(len(b["features"]) for b in batches)})

    def assess(n: int, b: dict, depth: int = 0) -> list[dict]:
        out = ai_dir / "results" / f"batch_{n:02d}.json"
        expected = {f["id"] for f in b["features"]}
        if depth == 0 and out.exists():  # resume
            rows = json.loads(out.read_text(encoding="utf-8"))["assessments"]
            if {r["id"] for r in rows} >= expected:
                return rows
        counter: dict = {}
        tools = build_tools(run_dir, org_rest, counter)
        user = (f"Batch {n:02d} ({b['mode']} mode) for release {pre['release']}. Assess every feature below.\n"
                f"```json\n{json.dumps(b['features'], ensure_ascii=False)}\n```")
        data, stop = _call(client, model, system, user, BATCH_SCHEMA, "high" if b["mode"] == "deep" else "low",
                           tools, budget, cancel,
                           lambda name, detail: on_event({"type": "tool", "batch": n, "tool": name, "detail": detail}))
        if stop == "max_tokens":
            if depth >= 2 or len(b["features"]) == 1:
                raise RuntimeError("response too long even after splitting the batch")
            half = len(b["features"]) // 2
            on_event({"type": "log", "message": f"batch {n:02d}: output too long - splitting into two halves"})
            rows = (assess(n, {**b, "features": b["features"][:half]}, depth + 1)
                    + assess(n, {**b, "features": b["features"][half:]}, depth + 1))
        else:
            rows = [r for r in data.get("assessments", []) if r.get("id") in expected]
        if depth == 0:
            out.write_text(json.dumps({"batch": n, "assessments": rows}, indent=1, ensure_ascii=False),
                           encoding="utf-8")
        return rows

    results, errors, done = {}, {}, 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {}
        for n, b in enumerate(batches, 1):
            futures[pool.submit(lambda n=n, b=b: (assess(n, b) if not (cancel.is_set() or budget.exceeded())
                                                  else None))] = n
        for fut in as_completed(futures):
            n = futures[fut]
            try:
                rows = fut.result()
                if rows is None:
                    errors[n] = "skipped (cancelled or budget reached)"
                else:
                    for r in rows:
                        results[r["id"]] = r
            except Cancelled as exc:
                errors[n] = str(exc)
            except Exception as exc:
                errors[n] = str(exc)[:300]
                on_event({"type": "log", "message": f"batch {n:02d} failed: {errors[n]}"})
            done += 1
            on_event({"type": "batch_done", "done": done, "total": total, "batch": n,
                      "ok": n not in errors, "cost": round(budget.total, 4),
                      "tokens": dict(budget.tokens)})

    merged = merge_assessments(pre, results)
    write_merged(ai_dir, pre["release"], merged)
    summary = {}
    if cancel.is_set():
        on_event({"type": "log", "message": "cancelled - summary skipped"})
    else:
        on_event({"type": "synthesis_start"})
        try:
            synth = json.loads((ai_dir / "synthesis_input.json").read_text(encoding="utf-8"))
            user = ("Write the release impact synthesis from these assessments.\n"
                    f"```json\n{json.dumps(synth, ensure_ascii=False)}\n```")
            relaxed = Budget(model, None)  # always allow the summary once the batches are paid for
            relaxed.total = budget.total
            relaxed.tokens = budget.tokens  # same dict, so the summary's tokens are counted too
            summary, stop = _call(client, model, system, user, SUMMARY_SCHEMA, "high", [], relaxed, cancel,
                                  lambda *_: None)
            budget.total = relaxed.total
            if stop == "max_tokens":
                raise RuntimeError("summary too long")
            (ai_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            on_event({"type": "log", "message": f"summary failed: {str(exc)[:300]}"})
            summary = {}
    on_event({"type": "cost", "cost": round(budget.total, 4), "tokens": dict(budget.tokens)})
    return {"batches": total, "failed_batches": errors, "reviewed": len(results), "summary": summary,
            "cost_usd": round(budget.total, 4), "tokens": dict(budget.tokens)}


def validate_key(api_key: str) -> list[str]:
    """Check the API key and return the ids of the supported models it can use."""
    client = anthropic.Anthropic(api_key=api_key, max_retries=1, timeout=20)
    available = {m.id for m in client.models.list()}
    return [m for m in MODELS if m in available] or list(MODELS)
