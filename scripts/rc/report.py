"""Render the shareable impact documentation: self-contained HTML, Excel workbook and Markdown.

Usage:
    python -m rc.report <run_dir>
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TEMPLATE = Path(__file__).resolve().parent / "report_template.html"
IMPACTS = ["High", "Medium", "Low", "Not Applicable"]
FILL = {"High": "F8D7DA", "Medium": "FFF3CD", "Low": "D1E7DD", "Not Applicable": "E9ECEF"}


def load_run(run_dir: Path) -> dict:
    ai = run_dir / "ai"
    if (ai / "assessments.json").exists():
        assessed = json.loads((ai / "assessments.json").read_text(encoding="utf-8"))
    else:  # --no-ai run: fall back to the deterministic ratings
        pre = json.loads((run_dir / "prefilter.json").read_text(encoding="utf-8"))
        assessed = {"release": pre["release"], "features": [
            {**{k: f[k] for k in ("id", "title", "area", "path", "page", "where", "when", "is_release_update",
                                  "release_update_group", "prelim_impact", "code_hits")},
             "impact": f["prelim_impact"], "ai_reviewed": False, "confidence": "low", "summary": "",
             "rationale": "; ".join(f["reasons"]), "org_evidence": [], "affected_components": [],
             "actions": [], "owner_roles": [], "effort": "", "deadline": "", "audiences": [], "test_notes": "",
             "capabilities": [c["label"] for c in f["capabilities"]]} for f in pre["features"]]}
    summary = json.loads((ai / "summary.json").read_text(encoding="utf-8")) if (ai / "summary.json").exists() else {}
    inv = json.loads((run_dir / "org" / "org_inventory.json").read_text(encoding="utf-8"))
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").exists() else {}
    return {"assessed": assessed, "summary": summary, "inventory": inv, "meta": meta}


def build_model(run: dict) -> dict:
    feats = run["assessed"]["features"]
    inv = run["inventory"]
    org = inv.get("organization", {})
    counts = Counter(f["impact"] for f in feats)
    by_area = defaultdict(Counter)
    for f in feats:
        by_area[f["area"]][f["impact"]] += 1
    areas = sorted(({"area": a, **{k: c.get(k, 0) for k in IMPACTS}} for a, c in by_area.items()),
                   key=lambda r: (-r["High"], -r["Medium"], -r["Low"], r["area"]))
    return {
        "release": run["assessed"]["release"],
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "org": {"name": org.get("Name"), "alias": inv.get("target_org"), "edition": org.get("OrganizationType"),
                "sandbox": org.get("IsSandbox"), "instance": org.get("InstanceName"), "url": inv.get("instance_url"),
                "api_version": inv.get("api_version"), "collected_at": inv.get("collected_at")},
        "source_pdf": run["meta"].get("pdf"),
        "counts": {k: counts.get(k, 0) for k in IMPACTS},
        "total": len(feats),
        "ai_reviewed": sum(1 for f in feats if f.get("ai_reviewed")),
        "areas": areas,
        "capabilities": [c for c in inv.get("capabilities", [])],
        "metadata": {k: v.get("total", 0) for k, v in inv.get("metadata", {}).items() if v.get("total")},
        "summary": run["summary"],
        "features": [{k: f.get(k) for k in ("id", "title", "area", "path", "page", "impact", "confidence", "summary",
                                            "rationale", "org_evidence", "affected_components", "actions",
                                            "owner_roles", "effort", "deadline", "audiences", "test_notes", "when",
                                            "is_release_update", "release_update_group", "ai_reviewed",
                                            "prelim_impact", "capabilities")} for f in feats],
    }


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")


def write_html(model: dict, path: Path) -> None:
    data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__REPORT_DATA__*/null", data)
    html = html.replace("__TITLE__", f"{model['release']} Impact - {model['org']['alias']}")
    path.write_text(html, encoding="utf-8")


def write_xlsx(model: dict, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = model["summary"]
    rows = [["Salesforce Release Impact Assessment"], [],
            ["Release", model["release"]], ["Org", f"{model['org']['name']} ({model['org']['alias']})"],
            ["Edition", model["org"]["edition"]], ["Instance", model["org"]["url"]],
            ["Generated", model["generated"]], ["Overall risk", s.get("overall_risk", "n/a")],
            ["Headline", s.get("headline", "")], []]
    rows += [[k, model["counts"][k]] for k in IMPACTS] + [["Total features", model["total"]],
                                                         ["AI-reviewed", model["ai_reviewed"]]]
    for r in rows:
        ws.append(r)
    ws["A1"].font = Font(size=14, bold=True)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100

    headers = ["Impact", "Area", "Feature", "Summary", "Why (this org)", "Actions", "Owners", "Effort", "Deadline",
               "Affected components", "Test notes", "Confidence", "Release update", "Page", "AI reviewed", "ID"]
    widths = [12, 20, 50, 50, 60, 60, 20, 8, 24, 40, 40, 11, 26, 7, 10, 12]

    def sheet(title, items):
        sh = wb.create_sheet(title)
        sh.append(headers)
        for f in items:
            sh.append([f["impact"], f["area"], f["title"], f.get("summary") or "", f.get("rationale") or "",
                       "\n".join(f.get("actions") or []), ", ".join(f.get("owner_roles") or []), f.get("effort") or "",
                       f.get("deadline") or "", "\n".join(f.get("affected_components") or []),
                       f.get("test_notes") or "", f.get("confidence") or "",
                       f.get("release_update_group") or ("Yes" if f.get("is_release_update") else ""),
                       f.get("page"), "Yes" if f.get("ai_reviewed") else "No", f["id"]])
        for i, w in enumerate(widths, 1):
            sh.column_dimensions[get_column_letter(i)].width = w
        for cell in sh[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="032D60")
        for row in sh.iter_rows(min_row=2):
            row[0].fill = PatternFill("solid", fgColor=FILL.get(row[0].value, "FFFFFF"))
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        sh.freeze_panes = "D2"
        sh.auto_filter.ref = sh.dimensions

    order = {k: i for i, k in enumerate(IMPACTS)}
    feats = sorted(model["features"], key=lambda f: (order[f["impact"]], f["area"], f["title"]))
    sheet("Action items", [f for f in feats if f["impact"] in ("High", "Medium")])
    sheet("All features", feats)

    ar = wb.create_sheet("By area")
    ar.append(["Area", *IMPACTS])
    for a in model["areas"]:
        ar.append([a["area"], *[a[k] for k in IMPACTS]])
    ar.column_dimensions["A"].width = 36
    for cell in ar[1]:
        cell.font = Font(bold=True)

    org = wb.create_sheet("Org profile")
    org.append(["Capability", "In use", "Evidence"])
    for c in model["capabilities"]:
        org.append([c["label"], "Yes" if c["present"] else "No", "; ".join(c["evidence"])])
    org.append([])
    org.append(["Metadata type", "Count"])
    for k, v in model["metadata"].items():
        org.append([k, v])
    org.column_dimensions["A"].width = 40
    org.column_dimensions["C"].width = 100
    wb.save(path)


def write_md(model: dict, path: Path) -> None:
    s, o, c = model["summary"], model["org"], model["counts"]
    aud = s.get("audiences", {})
    out = [f"# {model['release']} - Release Impact Assessment", "",
           f"**Org:** {o['name']} (`{o['alias']}`) - {o['edition']}{' sandbox' if o['sandbox'] else ''} - {o['url']}  ",
           f"**Generated:** {model['generated']} - **Features analysed:** {model['total']} "
           f"({model['ai_reviewed']} reviewed by AI)  ",
           f"**Overall risk:** {s.get('overall_risk', 'n/a')}", "",
           "| High | Medium | Low | Not Applicable |", "|---|---|---|---|",
           f"| {c['High']} | {c['Medium']} | {c['Low']} | {c['Not Applicable']} |", ""]
    if s.get("headline"):
        out += [f"> {s['headline']}", ""]
    if s.get("executive_summary"):
        out += ["## Executive summary (Managers)", "", s["executive_summary"], ""]
        out += [f"- **Decision:** {d}" for d in aud.get("managers", {}).get("decisions_needed", [])] + [""]
    if s.get("top_risks"):
        out += ["## Top risks", "", "| Risk | Impact | Owner | Deadline | Mitigation |", "|---|---|---|---|---|"]
        out += [f"| {r.get('title')} | {r.get('impact')} | {r.get('owner', '')} | {r.get('deadline', '')} | "
                f"{r.get('mitigation', '')} |" for r in s["top_risks"]] + [""]
    if s.get("key_dates"):
        out += ["## Key dates", ""] + [f"- **{d.get('date')}** - {d.get('event')}" for d in s["key_dates"]] + [""]
    sections = [("pm_po", "Product (PM / PO)", ["user_facing_changes", "communication_needs", "opportunities"]),
                ("architects", "Architecture", ["considerations"])]
    for key, title, lists in sections:
        if key in aud:
            out += [f"## {title}", "", aud[key].get("summary", ""), ""]
            for lst in lists:
                if aud[key].get(lst):
                    out += [f"**{lst.replace('_', ' ').capitalize()}**", ""] + [f"- {x}" for x in aud[key][lst]] + [""]
    if aud.get("developers"):
        out += ["## Developers", "", aud["developers"].get("summary", ""), "",
                "| Component | Task | Features |", "|---|---|---|"]
        out += [f"| {t.get('component')} | {t.get('task')} | {', '.join(t.get('feature_ids', []))} |"
                for t in aud["developers"].get("tasks", [])] + [""]
    if s.get("plan"):
        out += ["## Readiness plan", ""]
        for p in s["plan"]:
            out += [f"### {p.get('phase')} - {p.get('when', '')}"] + [f"- {a}" for a in p.get("activities", [])] + [""]
    for imp in ("High", "Medium"):
        items = [f for f in model["features"] if f["impact"] == imp]
        out += [f"## {imp} impact ({len(items)})", ""]
        for f in items:
            out += [f"### {f['title']}", f"*{f['area']} - release notes p. {f['page']} - id `{f['id']}`*", ""]
            if f.get("summary"):
                out += [f["summary"], ""]
            if f.get("rationale"):
                out += [f"**Why for this org:** {f['rationale']}", ""]
            if f.get("actions"):
                out += ["**Actions:**"] + [f"1. {a}" for a in f["actions"]] + [""]
            meta = [f"Owners: {', '.join(f['owner_roles'])}" if f.get("owner_roles") else "",
                    f"Effort: {f['effort']}" if f.get("effort") else "",
                    f"Deadline: {f['deadline']}" if f.get("deadline") else ""]
            if any(meta):
                out += [" - ".join(m for m in meta if m), ""]
    low = [f for f in model["features"] if f["impact"] == "Low"]
    out += [f"## Low impact ({len(low)})", "", "| Area | Feature | Note |", "|---|---|---|"]
    out += [f"| {f['area']} | {f['title']} | {(f.get('summary') or '').replace('|', '/')} |" for f in low] + [""]
    out += ["## Not applicable by area", "", "| Area | Count |", "|---|---|"]
    out += [f"| {a['area']} | {a['Not Applicable']} |" for a in model["areas"] if a["Not Applicable"]] + [""]
    if s.get("assumptions_limitations"):
        out += ["## Assumptions and limitations", ""] + [f"- {x}" for x in s["assumptions_limitations"]] + [""]
    path.write_text("\n".join(out), encoding="utf-8")


def render(run_dir: Path) -> dict[str, Path]:
    model = build_model(load_run(run_dir))
    out = run_dir / "report"
    out.mkdir(exist_ok=True)
    base = f"Release_Impact_{slug(model['release'])}_{slug(model['org']['alias'])}"
    paths = {"html": out / f"{base}.html", "xlsx": out / f"{base}.xlsx", "md": out / f"{base}.md"}
    (out / "report_data.json").write_text(json.dumps(model, indent=1, ensure_ascii=False), encoding="utf-8")
    write_html(model, paths["html"])
    write_xlsx(model, paths["xlsx"])
    write_md(model, paths["md"])
    return paths


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    for kind, p in render(Path(sys.argv[1])).items():
        print(f"  {kind}: {p}")
