"""AI analysis: Claude (headless Claude Code + the sf-release-impact-analyst agent) reviews each shortlisted
feature against the org inventory, assigns the final Low / Medium / High impact and writes the documentation
content (rationale, actions, owners, audience summaries).

Usage:
    python -m rc.ai_analysis <run_dir> [--model MODEL] [--parallel N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT = "sf-release-impact-analyst"
IMPACTS = ("High", "Medium", "Low", "Not Applicable")
DEEP_BATCH, LIGHT_BATCH = 30, 70
DEEP_TEXT, LIGHT_TEXT = 3000, 500
TOOLS = ["Read", "Write", "Glob", "Grep",
         "Bash(sf data query:*)", "Bash(sf org list metadata:*)", "Bash(sf sobject describe:*)"]
SOQL_HELPER = 'python -m rc.soql'   # read-only SOQL without the sf CLI (Docker); needs SF_* env vars
SOQL_TOOL = "Bash(python -m rc.soql:*)"


def find_claude() -> str:
    """Locate the Claude Code CLI: $CLAUDE_CLI, PATH, or the binary bundled with the VS Code / Cursor extension."""
    if os.environ.get("CLAUDE_CLI") and Path(os.environ["CLAUDE_CLI"]).exists():
        return os.environ["CLAUDE_CLI"]
    for name in ("claude", "claude.exe", "claude.cmd"):
        if shutil.which(name):
            return shutil.which(name)
    home = Path.home()
    patterns = [str(home / d / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / exe)
                for d in (".vscode", ".vscode-insiders", ".cursor", ".windsurf") for exe in ("claude.exe", "claude")]
    found = sorted((p for pat in patterns for p in glob.glob(pat)), key=os.path.getmtime, reverse=True)
    if found:
        return found[0]
    raise FileNotFoundError("Claude Code CLI not found. Install it (https://claude.com/claude-code) "
                            "or set CLAUDE_CLI to the claude executable.")


def org_digest(inv: dict) -> str:
    """Compact Markdown profile of the org, given to every AI call."""
    o = inv.get("organization", {})
    md = inv.get("metadata", {})
    lines = [f"# Org profile: {o.get('Name', '?')} ({inv.get('target_org')})",
             f"- Edition: {o.get('OrganizationType')} | Sandbox: {o.get('IsSandbox')} | Instance: {o.get('InstanceName')} "
             f"| API v{inv.get('api_version')} | Collected: {inv.get('collected_at')}",
             "", "## Capabilities in use (with evidence)"]
    lines += [f"- **{c['label']}** ({c['strength']}): {'; '.join(c['evidence'][:4])}"
              for c in inv["capabilities"] if c["present"]]
    lines += ["", "## Capabilities NOT detected",
              ", ".join(c["label"] for c in inv["capabilities"] if not c["present"]), "", "## Metadata"]
    for key, s in md.items():
        if not s.get("total"):
            continue
        extra = ""
        if s.get("min_api_version"):
            extra += f", oldest API v{s['min_api_version']}"
        if s.get("by_process_type"):
            extra += f", types {s['by_process_type']}"
        if s.get("items"):
            extra += ", e.g. " + ", ".join(str(next(iter(i.values()))) for i in s["items"][:6])
        lines.append(f"- {key}: {s['total']} (unmanaged {s.get('unmanaged', '-')}){extra}")
    lines += ["", "## Installed packages"] + [f"- {p['namespace'] or '(no ns)'}: {p['name']} {p['version']}"
                                             for p in inv.get("packages", [])] or ["- none"]
    lines += ["", "## Licenses in use"] + [f"- {l['name']} ({l.get('kind', '?')}): {l['used']}/{l.get('total', '?')}"
                                           for l in inv.get("licenses", []) if l.get("used")]
    lines += ["", "## Logins last 30 days (type / sub-type / application / API type / API version: count)"]
    lines += [f"- {r.get('LoginType')} / {r.get('LoginSubType')} / {r.get('Application')} / {r.get('ApiType')} / "
              f"{r.get('ApiVersion')}: {r.get('count', r.get('expr0'))}" for r in inv.get("logins_30d", [])[:40]]
    top = sorted(inv.get("record_counts", {}).items(), key=lambda kv: -kv[1])[:40]
    lines += ["", "## Largest objects by record count", ", ".join(f"{k} ({v:,})" for k, v in top)]
    if inv.get("source_counts"):
        lines += ["", f"## Unmanaged source downloaded to org/org_source: {inv['source_counts']}"]
    return "\n".join(lines) + "\n"


def make_batches(pre: dict, deep_size: int = DEEP_BATCH, light_size: int = LIGHT_BATCH) -> list[dict]:
    cands = [f for f in pre["features"] if f["ai_candidate"]]
    deep = [f for f in cands if f["prelim_impact"] in ("High", "Medium") or f["code_hits"] or f["is_release_update"]]
    light = [f for f in cands if f not in deep]

    def slim(f, limit):
        keep = {k: f[k] for k in ("id", "title", "area", "path", "page", "where", "when", "how", "is_release_update",
                                  "release_update_group", "prelim_impact", "severity", "capabilities", "code_hits",
                                  "severity_signals", "reasons")}
        keep["text"] = f["text"][:limit] + (" ..." if len(f["text"]) > limit else "")
        return keep

    batches = []
    for mode, items, size, limit in (("deep", deep, deep_size, DEEP_TEXT), ("light", light, light_size, LIGHT_TEXT)):
        for i in range(0, len(items), size):
            batches.append({"mode": mode, "features": [slim(f, limit) for f in items[i:i + size]]})
    return batches


class Cancelled(RuntimeError):
    pass


def run_claude(prompt: str, cwd: Path, log: Path, model: str | None, timeout: int, add_dir: Path | None = None,
               extra_env: dict | None = None, cancel: threading.Event | None = None) -> int:
    tools = TOOLS + ([SOQL_TOOL] if extra_env and extra_env.get("SF_ACCESS_TOKEN") else [])
    cmd = [find_claude(), "-p", prompt, "--agent", AGENT, "--permission-mode", "acceptEdits",
           "--allowedTools", *tools, "--output-format", "json"]
    if add_dir:
        cmd += ["--add-dir", str(add_dir)]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, **(extra_env or {})}
    if "CLAUDE_CODE_OAUTH_TOKEN" in (extra_env or {}):
        env.pop("ANTHROPIC_API_KEY", None)  # an API key would override the Claude account
    if extra_env:
        env["PYTHONPATH"] = str(PROJECT_ROOT / "scripts") + os.pathsep + env.get("PYTHONPATH", "")
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, text=True, encoding="utf-8")
        deadline = time.time() + timeout
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled("cancelled by user")
            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd[:2], timeout)
            time.sleep(1)
    return proc.returncode


def claude_cost(log: Path) -> float:
    """Cost reported by Claude Code (with a Claude subscription it is the API-equivalent value, not a charge)."""
    try:
        return float(json.loads(log.read_text(encoding="utf-8")).get("total_cost_usd") or 0)
    except Exception:
        return 0.0


TOKEN_KEYS = ("input", "output", "cache_write", "cache_read")


def claude_tokens(log: Path) -> dict:
    """Token usage reported by Claude Code, summed over every model the run used."""
    out = dict.fromkeys(TOKEN_KEYS, 0)
    try:
        data = json.loads(log.read_text(encoding="utf-8"))
    except Exception:
        return out
    usage = list((data.get("modelUsage") or {}).values())
    if usage:
        for u in usage:
            out["input"] += u.get("inputTokens") or 0
            out["output"] += u.get("outputTokens") or 0
            out["cache_write"] += u.get("cacheCreationInputTokens") or 0
            out["cache_read"] += u.get("cacheReadInputTokens") or 0
    elif data.get("usage"):
        u = data["usage"]
        out.update(input=u.get("input_tokens") or 0, output=u.get("output_tokens") or 0,
                   cache_write=u.get("cache_creation_input_tokens") or 0,
                   cache_read=u.get("cache_read_input_tokens") or 0)
    return out


def check_account(extra_env: dict, timeout: int = 120) -> str:
    """Verify a Claude account token with a one-turn headless call. Returns the reply text, raises on failure."""
    env = {**os.environ, **extra_env}
    env.pop("ANTHROPIC_API_KEY", None)  # an API key would take precedence over the account token
    proc = subprocess.run([find_claude(), "-p", "Reply with the single word OK.", "--output-format", "json",
                           "--max-turns", "1"], capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=timeout, stdin=subprocess.DEVNULL, cwd=PROJECT_ROOT)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:300]) from None
    if data.get("is_error") or data.get("subtype") != "success":
        raise RuntimeError(str(data.get("result") or data.get("subtype"))[:300])
    return str(data.get("result", "")).strip()


def validate_batch(path: Path, expected: set[str]) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["assessments"] if isinstance(data, dict) else data
    good = []
    for r in rows:
        if r.get("id") in expected and r.get("impact") in IMPACTS:
            r.setdefault("actions", [])
            r.setdefault("owner_roles", [])
            good.append(r)
    if len(good) < 0.8 * len(expected):
        raise ValueError(f"only {len(good)}/{len(expected)} valid assessments")
    return good


def batch_prompt(run_rel: str, n: int, mode: str, soql_helper: bool = False) -> str:
    soql = (f"Read-only SOQL for evidence: `{SOQL_HELPER} \"SELECT ... LIMIT 50\"` (add `--tooling` for the "
            f"Tooling API); the sf CLI is not available.\n" if soql_helper else "")
    return (f"Release impact review, batch {n:02d} ({mode} mode).\n"
            f"Org profile: {run_rel}/ai/org_profile.md\n"
            f"Features to assess: {run_rel}/ai/batches/batch_{n:02d}.json\n"
            f"Full parsed release notes (look up related features if needed): {run_rel}/release_features.json\n"
            f"Org source code to grep for evidence: {run_rel}/org/org_source/\n"
            f"{soql}"
            f"Write your assessments to: {run_rel}/ai/results/batch_{n:02d}.json\n"
            f"Follow the 'Batch assessment' contract in your instructions exactly. Assess every feature id in the batch.")


def synthesis_input(release: str, merged: list[dict]) -> dict:
    """Compact view for the synthesis call: exact counts, full High/Medium items, Low titles, N/A counts by area."""
    counts = {k: sum(f["impact"] == k for f in merged) for k in IMPACTS}
    na_by_area: dict[str, int] = {}
    for f in merged:
        if f["impact"] == "Not Applicable":
            na_by_area[f["area"]] = na_by_area.get(f["area"], 0) + 1
    detail_keys = ("id", "title", "area", "impact", "confidence", "summary", "rationale", "affected_components",
                   "actions", "owner_roles", "effort", "deadline", "audiences", "when", "release_update_group")
    return {
        "release": release, "counts": counts, "total": len(merged),
        "ai_reviewed": sum(f["ai_reviewed"] for f in merged),
        "high_and_medium": [{k: f.get(k) for k in detail_keys} for f in merged if f["impact"] in ("High", "Medium")],
        "low": [{"id": f["id"], "title": f["title"], "area": f["area"], "summary": f.get("summary", "")}
                for f in merged if f["impact"] == "Low"],
        "not_applicable_by_area": dict(sorted(na_by_area.items(), key=lambda kv: -kv[1])),
    }


def synthesis_prompt(run_rel: str) -> str:
    return (f"Release impact synthesis.\n"
            f"Org profile: {run_rel}/ai/org_profile.md\n"
            f"Assessments (exact counts, High/Medium detail, Low titles): {run_rel}/ai/synthesis_input.json\n"
            f"Write the cross-audience summary to: {run_rel}/ai/summary.json\n"
            f"Follow the 'Synthesis' contract in your instructions exactly.")


def _print_event(e: dict) -> None:
    if e["type"] == "batches":
        print(f"  Claude is reviewing {e['features']} features in {e['total']} batches. "
              "This usually takes 10-30 minutes...")
    elif e["type"] == "batch_done":
        print(f"    batch {e['batch']:02d}: {'ok' if e['ok'] else 'FAILED - keeping preliminary ratings'}")
    elif e["type"] == "synthesis_start":
        print("  Claude is writing the executive / architecture / developer / product summary...")
    elif e["type"] == "log":
        print(f"    {e['message']}")


def analyse(run_dir: Path, model: str | None = None, parallel: int = 3, timeout: int = 1800,
            extra_env: dict | None = None, on_event=_print_event, cancel: threading.Event | None = None) -> dict:
    """Run the batches and the synthesis with headless Claude Code.

    extra_env: environment for the Claude Code processes - e.g. CLAUDE_CODE_OAUTH_TOKEN (Claude account) and
    SF_INSTANCE_URL / SF_ACCESS_TOKEN / SF_API_VERSION (enables the read-only `python -m rc.soql` helper).
    on_event: progress callback with the same event shapes as ai_api.analyse_api.
    """
    run_dir = run_dir.resolve()
    inside = run_dir.is_relative_to(PROJECT_ROOT)
    run_rel = os.path.relpath(run_dir, PROJECT_ROOT).replace("\\", "/") if inside else run_dir.as_posix()
    add_dir = None if inside else run_dir
    soql_helper = bool(extra_env and extra_env.get("SF_ACCESS_TOKEN"))
    ai_dir = run_dir / "ai"
    for sub in ("batches", "results", "logs"):
        (ai_dir / sub).mkdir(parents=True, exist_ok=True)
    pre = json.loads((run_dir / "prefilter.json").read_text(encoding="utf-8"))
    inv = json.loads((run_dir / "org" / "org_inventory.json").read_text(encoding="utf-8"))
    (ai_dir / "org_profile.md").write_text(org_digest(inv), encoding="utf-8")

    batches = make_batches(pre)
    for n, b in enumerate(batches, 1):
        (ai_dir / "batches" / f"batch_{n:02d}.json").write_text(
            json.dumps({"release": pre["release"], "mode": b["mode"], "features": b["features"]}, indent=1,
                       ensure_ascii=False), encoding="utf-8")
    on_event({"type": "batches", "total": len(batches), "features": sum(len(b["features"]) for b in batches)})
    cost = {"usd": 0.0}
    tokens = dict.fromkeys(TOKEN_KEYS, 0)
    cost_lock = threading.Lock()

    def claude(prompt: str, log: Path) -> int:
        rc = run_claude(prompt, PROJECT_ROOT, log, model, timeout, add_dir, extra_env, cancel)
        with cost_lock:
            cost["usd"] += claude_cost(log)
            for k, v in claude_tokens(log).items():
                tokens[k] += v
        return rc

    def work(n: int, b: dict):
        out = ai_dir / "results" / f"batch_{n:02d}.json"
        expected = {f["id"] for f in b["features"]}
        if out.exists():
            try:
                return n, validate_batch(out, expected), None  # resume a previous run
            except Exception:
                out.unlink()
        last_err = None
        for attempt in (1, 2):
            if cancel is not None and cancel.is_set():
                return n, [], "cancelled"
            log = ai_dir / "logs" / f"batch_{n:02d}_try{attempt}.log"
            try:
                rc = claude(batch_prompt(run_rel, n, b["mode"], soql_helper), log)
                if out.exists():
                    return n, validate_batch(out, expected), None
                last_err = f"no output file (exit {rc})"
            except Cancelled:
                return n, [], "cancelled"
            except Exception as exc:  # timeout, invalid JSON, too few rows
                last_err = str(exc)
                out.unlink(missing_ok=True)
            on_event({"type": "log", "message": f"batch {n:02d} attempt {attempt} failed: {last_err}"})
        return n, [], last_err

    results, errors, done = {}, {}, 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for fut in as_completed([pool.submit(work, n, b) for n, b in enumerate(batches, 1)]):
            n, rows, err = fut.result()
            for r in rows:
                results[r["id"]] = r
            if err:
                errors[n] = err
            done += 1
            on_event({"type": "batch_done", "done": done, "total": len(batches), "batch": n, "ok": not err,
                      "cost": round(cost["usd"], 4), "tokens": dict(tokens)})

    merged = merge_assessments(pre, results)
    write_merged(ai_dir, pre["release"], merged)

    summary = {}
    summary_path = ai_dir / "summary.json"
    summary_path.unlink(missing_ok=True)
    if cancel is not None and cancel.is_set():
        on_event({"type": "log", "message": "cancelled - summary skipped"})
    else:
        on_event({"type": "synthesis_start"})
        try:
            claude(synthesis_prompt(run_rel), ai_dir / "logs" / "synthesis.log")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            on_event({"type": "log", "message": f"summary failed ({exc}); the documents will contain data tables only"})
    on_event({"type": "cost", "cost": round(cost["usd"], 4), "tokens": dict(tokens)})
    return {"batches": len(batches), "failed_batches": errors, "reviewed": len(results), "summary": summary,
            "cost_usd": round(cost["usd"], 4), "tokens": dict(tokens)}


def merge_assessments(pre: dict, results: dict[str, dict]) -> list[dict]:
    """Combine AI assessments (by feature id) with the prefilter rows; unreviewed features keep their rule rating."""
    merged = []
    for f in pre["features"]:
        a = results.get(f["id"])
        merged.append({
            "id": f["id"], "title": f["title"], "area": f["area"], "path": f["path"], "page": f["page"],
            "where": f["where"], "when": f["when"], "is_release_update": f["is_release_update"],
            "release_update_group": f["release_update_group"], "prelim_impact": f["prelim_impact"],
            "impact": a["impact"] if a else f["prelim_impact"], "ai_reviewed": bool(a),
            "confidence": (a or {}).get("confidence", "low" if not a else "medium"),
            "summary": (a or {}).get("summary", ""), "rationale": (a or {}).get("rationale", "; ".join(f["reasons"])),
            "org_evidence": (a or {}).get("org_evidence", [c["label"] for c in f["capabilities"]]),
            "affected_components": (a or {}).get("affected_components", [h["token"] for h in f["code_hits"]]),
            "actions": (a or {}).get("actions", []), "owner_roles": (a or {}).get("owner_roles", []),
            "effort": (a or {}).get("effort", ""), "deadline": (a or {}).get("deadline", ""),
            "audiences": (a or {}).get("audiences", []), "test_notes": (a or {}).get("test_notes", ""),
            "code_hits": f["code_hits"], "capabilities": [c["label"] for c in f["capabilities"]],
        })
    return merged


def write_merged(ai_dir: Path, release: str, merged: list[dict]) -> None:
    (ai_dir / "assessments.json").write_text(json.dumps({"release": release, "features": merged}, indent=1,
                                                        ensure_ascii=False), encoding="utf-8")
    (ai_dir / "synthesis_input.json").write_text(json.dumps(synthesis_input(release, merged), indent=1,
                                                            ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--model")
    ap.add_argument("--parallel", type=int, default=3)
    args = ap.parse_args()
    res = analyse(args.run_dir, args.model, args.parallel)
    print(json.dumps({k: v for k, v in res.items() if k != "summary"}, indent=2))
    sys.exit(0)
