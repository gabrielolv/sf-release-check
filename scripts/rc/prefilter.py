"""Deterministic first pass: score every release-note feature against the org inventory.

Produces a preliminary impact (High / Medium / Low / Not Applicable) with evidence, and the
shortlist of candidates the AI agent reviews in depth.

Usage:
    python -m rc.prefilter <release_features.json> <org_dir> <out.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CODE_TOKEN_RES = [
    re.compile(r"\b[A-Z][A-Za-z0-9]+\.[A-Z][A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?\b"),  # Namespace.Class(.member)
    re.compile(r"\blightning-[a-z][a-z-]+\b"),                                     # base components
    re.compile(r"\blightning/[a-zA-Z]+\b"),                                        # LWC modules
    re.compile(r"\b[a-z][A-Za-z0-9]+\(\)"),                                        # method()
    re.compile(r"(?<![\w@])@[A-Z][A-Za-z]+\b"),                                    # @Annotation
]
SOURCE_EXTS = {".cls", ".trigger", ".page", ".component", ".js", ".html", ".cmp", ".xml", ".app", ".evt", ".css"}


def _compile(patterns):
    return [re.compile(p, re.I) for p in patterns]


def _kw_re(keyword: str):
    # Acronyms / code words are case-sensitive; ordinary phrases are not.
    flags = 0 if (keyword.isupper() or any(c in keyword for c in ".(:/-@")) else re.I
    return re.compile(r"(?<![\w])" + re.escape(keyword) + r"(?![\w])", flags)


def load_source(org_dir: Path) -> dict[str, str]:
    root = org_dir / "org_source"
    if not root.exists():
        return {}
    return {str(p.relative_to(root)).replace("\\", "/"): p.read_text(encoding="utf-8", errors="replace")
            for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_EXTS}


def code_tokens(text: str, generic: set[str]) -> set[str]:
    return {tok for rx in CODE_TOKEN_RES for tok in rx.findall(text) if len(tok) > 4 and tok not in generic}


def relevance_raw(org_matches: dict) -> int:
    return max((m["score"] for m in org_matches.values()), default=0)


def org_edition(inv: dict) -> str:
    return (inv.get("organization", {}).get("OrganizationType") or "").replace(" Edition", "").strip()


def edition_excluded(where: str, edition: str, editions: list[str]) -> bool:
    if not where or not edition or "edition" not in where.lower():
        return False
    named = [e for e in editions if re.search(r"\b" + re.escape(e) + r"\b", where)]
    return bool(named) and not any(edition.lower().startswith(e.lower()) or e.lower() in edition.lower() for e in named)


def run(features_path: Path, org_dir: Path, out_path: Path) -> dict:
    release = json.loads(features_path.read_text(encoding="utf-8"))
    inv = json.loads((org_dir / "org_inventory.json").read_text(encoding="utf-8"))
    caps_cfg = {c["id"]: c for c in json.loads((CONFIG_DIR / "capabilities.json").read_text(encoding="utf-8"))["capabilities"]}
    rules = json.loads((CONFIG_DIR / "impact_rules.json").read_text(encoding="utf-8"))
    sev_high, sev_med, opt_in = (_compile(rules["severity"][k]) for k in ("high", "medium", "opt_in"))
    generic = set(rules["generic_code_tokens"])
    edition = org_edition(inv)
    present = {c["id"]: c for c in inv["capabilities"] if c["present"]}
    kw_cache = {cid: [(k, _kw_re(k)) for k in caps_cfg[cid].get("keywords", [])] for cid in caps_cfg}
    source = load_source(org_dir)

    results = []
    for f in release["features"]:
        if f.get("duplicate"):
            continue
        title, text, path = f["title"], f["text"], [f["area"], *f["path"]]
        reasons, matched = [], {}

        # --- relevance: which org capabilities does this feature touch?
        for cid, cfg in caps_cfg.items():
            score, why = 0, []
            if any(a and a in path for a in cfg.get("areas", [])):
                score += 3
                why.append("product area")
            title_hits = [k for k, rx in kw_cache[cid] if rx.search(title)]
            text_hits = [k for k, rx in kw_cache[cid] if k not in title_hits and rx.search(text)]
            score += 2 * min(len(title_hits), 2) + min(len(text_hits), 2)
            if title_hits or text_hits:
                why.append("keywords: " + ", ".join((title_hits + text_hits)[:4]))
            if score:
                matched[cid] = {"score": score, "why": why}
        org_matches = {cid: m for cid, m in matched.items() if cid in present}
        # Generic "always-on" capabilities only count when the feature is clearly about them.
        relevance = 0
        for cid, m in org_matches.items():
            weight = 0.5 if caps_cfg[cid]["detect"].get("always") and m["score"] < 3 else 1.0
            relevance = max(relevance, m["score"] * weight)
        foreign = [cid for cid, m in matched.items() if cid not in present and m["score"] >= 3]
        # A feature that belongs mainly to a product the org does not run (e.g. Tableau Next) is not applicable,
        # even if it mentions generic words such as "dashboard" or "Setup".
        top_foreign = max((matched[c]["score"] for c in foreign), default=0)
        dominated = top_foreign > relevance_raw(org_matches) and top_foreign >= 3

        # --- code evidence: does org source reference APIs named in the feature?
        hits = []
        for tok in sorted(code_tokens(text, generic)):
            files = [name for name, body in source.items() if tok in body]
            if files:
                hits.append({"token": tok, "files": files[:10], "file_count": len(files)})
        if hits:
            relevance += 3
            reasons.append(f"org code references {', '.join(h['token'] for h in hits[:3])}")

        # --- severity
        haystack = f"{title} {text}"
        high = [rx.pattern for rx in sev_high if rx.search(haystack)]
        med = [rx.pattern for rx in sev_med if rx.search(haystack)]
        optin = [rx.pattern for rx in opt_in if rx.search(haystack)]
        severity = "high" if high else ("medium" if med else "low")
        ru_group = None
        if f.get("is_release_update"):
            for label, level in rules["release_update_groups"].items():
                if any(p.startswith(label) for p in f["path"]):
                    ru_group, severity = next(p for p in f["path"] if p.startswith(label)), level
                    break
            else:
                severity = "high" if high else "medium"
        if optin and severity != "high" and not f.get("is_release_update"):
            severity = "low"
        if re.search(r"\((Pilot|Beta)\)", title) and severity != "high":
            severity = "low"

        # --- applicability
        excluded = edition_excluded(f.get("where", ""), edition, rules["editions"])
        if excluded:
            reasons.append(f"not available in {edition} Edition")
        if (not org_matches or dominated) and not hits:
            prelim = "Not Applicable"
            if foreign:
                reasons.append("product not in use: " + ", ".join(caps_cfg[c]["label"] for c in foreign[:3]))
        elif excluded:
            prelim = "Not Applicable"
        elif relevance >= 3 and (severity == "high" or (hits and severity == "medium")):
            prelim = "High"
        elif severity == "high" or (severity == "medium" and relevance >= 2):
            prelim = "Medium"
        elif relevance >= 1:
            prelim = "Low"
        else:
            prelim = "Not Applicable"
        if f.get("is_release_update") and org_matches and prelim in ("Low", "Not Applicable") and severity != "low":
            prelim = "Medium"

        # Release-update reminders on capabilities the org does not use stay N/A, but get a second look.
        candidate = prelim in ("High", "Medium") or bool(hits) or (prelim == "Low" and relevance >= 3) \
            or (f.get("is_release_update") and ru_group and not ru_group.startswith("Canceled"))
        results.append({
            "id": f["id"], "title": title, "area": f["area"], "path": f["path"], "page": f["page"],
            "pdf_page": f["pdf_page"], "where": f.get("where", ""), "when": f.get("when", ""),
            "how": f.get("how", ""), "is_release_update": f.get("is_release_update", False),
            "release_update_group": ru_group,
            "prelim_impact": prelim, "severity": severity, "relevance": round(relevance, 1),
            "capabilities": [{"id": cid, "label": caps_cfg[cid]["label"], "evidence": present[cid]["evidence"][:3],
                              "match": m["why"]} for cid, m in sorted(org_matches.items(), key=lambda x: -x[1]["score"])][:4],
            "code_hits": hits[:8],
            "severity_signals": (high + med)[:6],
            "opt_in_signals": optin[:3],
            "reasons": reasons,
            "ai_candidate": bool(candidate),
            "text": text,
        })

    order = {"High": 0, "Medium": 1, "Low": 2, "Not Applicable": 3}
    results.sort(key=lambda r: (order[r["prelim_impact"]], -r["relevance"], r["area"], r["title"]))
    summary = {k: sum(r["prelim_impact"] == k for r in results) for k in order}
    out = {"release": release["release"], "source": release["source"], "org_edition": edition,
           "feature_count": len(results), "prelim_summary": summary,
           "ai_candidates": sum(r["ai_candidate"] for r in results), "features": results}
    out_path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"  Prefilter: {summary} - {out['ai_candidates']} features shortlisted for AI review")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    run(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
