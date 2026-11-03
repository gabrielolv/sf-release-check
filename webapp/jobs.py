"""Background analysis runs with observable progress (stages, AI batches, cost, log)."""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from rc import ai_analysis, ai_api, org_inventory, prefilter, report
from rc.sfcli import SalesforceRest

from . import salesforce_oauth

STAGES = [("inventory", "Org inventory (read-only)"), ("prefilter", "Rule-based pre-assessment"),
          ("ai", "AI analysis of relevant changes"), ("summary", "AI summary for each audience"),
          ("report", "Report data and interactive report")]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class Job:
    def __init__(self, run_dir: Path, meta: dict):
        self.run_dir = run_dir
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.state = {
            "id": run_dir.name, "status": "queued", "created": meta["created"], "release": meta["release"],
            "source": meta["source"], "org": meta["org"], "options": meta["options"],
            "stages": [{"key": k, "label": l, "status": "pending", "detail": ""} for k, l in STAGES],
            "progress": {"done": 0, "total": 0, "features": 0}, "cost_usd": 0.0, "tokens": None, "log": [], "counts": None,
            "error": None, "started": None, "finished": None,
        }
        self._last_save = 0.0

    # ---- state helpers
    def log(self, msg: str) -> None:
        with self.lock:
            self.state["log"].append({"t": datetime.now().strftime("%H:%M:%S"), "msg": msg.strip()})
            self.state["log"] = self.state["log"][-400:]
        self.save()

    def stage(self, key: str, status: str, detail: str = "") -> None:
        with self.lock:
            for s in self.state["stages"]:
                if s["key"] == key:
                    s["status"] = status
                    if detail:
                        s["detail"] = detail
        self.save(force=True)

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        with self.lock:
            data = json.dumps(self.state, indent=1, default=str)
        (self.run_dir / "state.json").write_text(data, encoding="utf-8")

    def snapshot(self, log_tail: int = 120) -> dict:
        with self.lock:
            snap = json.loads(json.dumps(self.state, default=str))
        snap["log"] = snap["log"][-log_tail:]
        return snap

    # ---- pipeline
    def run(self, conn: dict, auth: dict | None) -> None:
        self.state.update(status="running", started=time.time())
        self.save(force=True)
        opts = self.state["options"]
        current = None
        try:
            org = {"instanceUrl": conn["instance_url"], "accessToken": conn["access_token"],
                   "apiVersion": conn["api_version"], "username": conn["username"],
                   "alias": self.state["org"]["alias"], "refresh": lambda: salesforce_oauth.refresh(conn)}

            current = "inventory"
            self.stage(current, "running")
            inv = org_inventory.collect(org, self.run_dir / "org", include_source=opts["include_source"],
                                        log=self.log)
            used = sum(c["present"] for c in inv["capabilities"])
            self.stage(current, "done", f"{used} capabilities in use, {len(inv.get('warnings', []))} queries skipped")
            self._check_cancel()

            current = "prefilter"
            self.stage(current, "running")
            pre = prefilter.run(self.run_dir / "release_features.json", self.run_dir / "org",
                                self.run_dir / "prefilter.json")
            s = pre["prelim_summary"]
            self.stage(current, "done", f"{pre['feature_count']} features: {s['High']} High, {s['Medium']} Medium, "
                                        f"{s['Low']} Low - {pre['ai_candidates']} shortlisted")
            self._check_cancel()

            if opts["no_ai"]:
                self.stage("ai", "skipped", "rule-based ratings only")
                self.stage("summary", "skipped")
            else:
                current = "ai"
                self.stage(current, "running")
                if auth["mode"] == "account":
                    # Claude account: official Claude Code CLI + agent, usage counts against the user's Claude plan
                    if conn.get("refresh_token"):
                        salesforce_oauth.refresh(conn)  # fresh token for the agent's read-only SOQL helper
                    env = {"CLAUDE_CODE_OAUTH_TOKEN": auth["secret"], "SF_INSTANCE_URL": conn["instance_url"],
                           "SF_ACCESS_TOKEN": conn["access_token"], "SF_API_VERSION": conn["api_version"]}
                    self.log("Running the analysis on Claude Code with your Claude account")
                    res = ai_analysis.analyse(self.run_dir, model=opts["model"], parallel=opts["parallel"],
                                              extra_env=env, on_event=self._on_ai_event, cancel=self.cancel)
                else:
                    res = ai_api.analyse_api(self.run_dir, auth["secret"], model=opts["model"],
                                             parallel=opts["parallel"], budget_usd=opts["budget_usd"],
                                             org_rest=SalesforceRest(org), on_event=self._on_ai_event,
                                             cancel=self.cancel)
                failed = len(res["failed_batches"])
                self.stage("ai", "done" if not failed else "warning",
                           f"{res['reviewed']} features reviewed" + (f", {failed} batch(es) not completed" if failed else ""))
                if self.cancel.is_set():
                    self.stage("summary", "skipped", "cancelled")
                else:
                    self.stage("summary", "done" if res["summary"] else "warning",
                               "" if res["summary"] else "summary unavailable - documents contain data tables only")
                with self.lock:
                    self.state["cost_usd"] = res["cost_usd"]
                    self.state["tokens"] = res.get("tokens")
            self._check_cancel()

            current = "report"
            self.stage(current, "running")
            report.render(self.run_dir)
            model = json.loads((self.run_dir / "report" / "report_data.json").read_text(encoding="utf-8"))
            with self.lock:
                self.state["counts"] = model["counts"]
                self.state["overall_risk"] = model["summary"].get("overall_risk")
                self.state["headline"] = model["summary"].get("headline")
            self.stage(current, "done")
            self.state["status"] = "done"
        except _CancelledRun:
            self.state["status"] = "cancelled"
            if current:
                self.stage(current, "skipped", "cancelled")
        except Exception as exc:
            self.state["status"] = "failed"
            self.state["error"] = str(exc)[:600]
            if current:
                self.stage(current, "failed", str(exc)[:200])
            self.log("ERROR: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
        finally:
            self.state["finished"] = time.time()
            self.save(force=True)

    def _check_cancel(self):
        if self.cancel.is_set():
            raise _CancelledRun()

    def _on_ai_event(self, e: dict) -> None:
        kind = e["type"]
        with self.lock:
            prog = self.state["progress"]
            if kind == "batches":
                prog.update(total=e["total"], features=e["features"], done=0)
            elif kind == "batch_done":
                prog["done"] = e["done"]
                self.state["cost_usd"] = e["cost"]
            elif kind == "cost":
                self.state["cost_usd"] = e["cost"]
            if e.get("tokens"):
                self.state["tokens"] = e["tokens"]
        if kind == "batches":
            self.log(f"Claude is reviewing {e['features']} features in {e['total']} batches")
            self.stage("ai", "running", f"0 / {e['total']} batches")
        elif kind == "batch_done":
            self.log(f"batch {e['batch']:02d} {'done' if e['ok'] else 'not completed'} "
                     f"({e['done']}/{e['total']}, est. ${e['cost']:.2f})")
            self.stage("ai", "running", f"{e['done']} / {e['total']} batches")
        elif kind == "tool":
            self.log(f"batch {e['batch']:02d} -> {e['tool']} {e['detail']}")
        elif kind == "synthesis_start":
            self.stage("summary", "running")
            self.log("Writing the executive, product, architecture and developer summaries")
        elif kind == "log":
            self.log(e["message"])


class _CancelledRun(Exception):
    pass


class JobManager:
    def __init__(self, data_dir: Path):
        self.runs_dir = data_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}

    def create(self, features_file: Path, source_name: str, conn: dict, auth: dict | None, options: dict) -> Job:
        release = json.loads(features_file.read_text(encoding="utf-8"))["release"]
        alias = re.sub(r"[^A-Za-z0-9-]+", "_", conn.get("org_name") or conn["username"].split("@")[-1])[:40]
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}_{alias}"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True)
        shutil.copyfile(features_file, run_dir / "release_features.json")
        meta = {"created": datetime.now().strftime("%Y-%m-%d %H:%M"), "release": release, "source": source_name,
                "org": {"alias": alias, "name": conn.get("org_name"), "username": conn["username"],
                        "instance_url": conn["instance_url"], "edition": conn.get("edition"),
                        "sandbox": conn.get("is_sandbox")},
                "options": options}
        (run_dir / "run.json").write_text(json.dumps({"release": release, "pdf": source_name, "target_org": alias,
                                                      "username": conn["username"],
                                                      "instance_url": conn["instance_url"], "started": run_id,
                                                      "ai": not options["no_ai"], "engine": options["engine"],
                                                      "options": options}, indent=1), encoding="utf-8")
        job = Job(run_dir, meta)
        job.save(force=True)
        self.jobs[run_id] = job
        threading.Thread(target=job.run, args=(dict(conn), auth), daemon=True, name=f"run-{run_id}").start()
        return job

    def get_dir(self, run_id: str) -> Path | None:
        if not RUN_ID_RE.match(run_id or ""):
            return None
        d = self.runs_dir / run_id
        return d if d.is_dir() else None

    def state(self, run_id: str) -> dict | None:
        if run_id in self.jobs:
            return self.jobs[run_id].snapshot()
        d = self.get_dir(run_id)
        if d and (d / "state.json").exists():
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            if st.get("status") in ("running", "queued"):
                st["status"] = "interrupted"  # the container restarted while it ran
            st["log"] = st.get("log", [])[-120:]
            return st
        return None

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            st = self.state(d.name) if d.is_dir() else None
            if st:
                out.append({k: st.get(k) for k in ("id", "status", "created", "release", "source", "org", "counts",
                                                   "cost_usd", "overall_risk")})
        return out

    def delete(self, run_id: str) -> bool:
        d = self.get_dir(run_id)
        job = self.jobs.get(run_id)
        if not d or (job and job.state["status"] == "running"):
            return False
        shutil.rmtree(d, ignore_errors=True)
        self.jobs.pop(run_id, None)
        return True
