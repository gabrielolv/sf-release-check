#!/usr/bin/env python3
"""Salesforce Release Impact Check.

Compares a Salesforce org with the release notes PDF in "Release Note/" and produces shareable technical
documentation (HTML, Excel, Markdown) rating every change High / Medium / Low / Not Applicable for this org.

    python release_check.py                         # interactive: log in / pick org, pick PDF, run everything
    python release_check.py --target-org MYSANDBOX     # use (or re-login to) a specific org alias
    python release_check.py --no-ai                 # rule-based ratings only (no Claude call)
    python release_check.py --engine api            # Claude API (ANTHROPIC_API_KEY) instead of Claude Code
    python release_check.py --resume output/<run>   # continue a run (e.g. after an AI batch failed)
    python release_check.py --report-only output/<run>

Steps: 1 login/select org -> 2 parse release notes -> 3 org inventory (read-only) -> 4 rule-based prefilter
       -> 5 Claude analysis (sf-release-impact-analyst agent) -> 6 render report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

NOTES_DIR = ROOT / "Release Note"
OUTPUT_DIR = ROOT / "output"


def banner(step: str) -> None:
    print(f"\n=== {step} " + "=" * max(0, 66 - len(step)))


def check_prereqs(need_ai: bool) -> None:
    missing = []
    for mod, pkg in (("pymupdf", "pymupdf"), ("openpyxl", "openpyxl")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        sys.exit(f"Missing Python packages: {' '.join(missing)}\n  Run: python -m pip install -r requirements.txt")
    from rc.sfcli import sf_exe
    sf_exe()
    if need_ai:
        from rc.ai_analysis import find_claude
        try:
            print(f"  Claude CLI: {find_claude()}")
        except FileNotFoundError as exc:
            sys.exit(f"{exc}\n  Or run with --no-ai for rule-based ratings only.")


def choose_pdf(arg: str | None, interactive: bool) -> Path:
    if arg:
        return Path(arg).resolve()
    pdfs = sorted(NOTES_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        sys.exit(f"No release notes PDF found in '{NOTES_DIR}'. Download it from "
                 "https://help.salesforce.com (Release Notes > PDF) and place it there.")
    if len(pdfs) == 1 or not interactive:
        return pdfs[0]
    print("\nRelease notes found:")
    for i, p in enumerate(pdfs, 1):
        print(f"  {i}) {p.name}")
    choice = input("Choose a file [1]: ").strip() or "1"
    return pdfs[int(choice) - 1]


def parse_cached(pdf: Path) -> Path:
    from rc import parse_release_notes
    st = pdf.stat()
    key = hashlib.sha1(f"{pdf.name}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:10]
    cache = OUTPUT_DIR / "_cache" / f"{pdf.stem}.{key}.features.json"
    if cache.exists():
        print(f"  Using cached parse: {cache.name}")
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result = parse_release_notes.parse(pdf)
    cache.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"  {result['release']}: {result['feature_count']} features from {result['page_count']} pages "
          f"({time.time() - t0:.0f}s)")
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-org", help="alias/username of an authenticated org (prompts to log in if expired)")
    ap.add_argument("--pdf", help="release notes PDF (default: newest in 'Release Note/')")
    ap.add_argument("--no-ai", action="store_true", help="skip the Claude analysis (rule-based ratings only)")
    ap.add_argument("--engine", choices=["claude-code", "api"], default="claude-code",
                    help="claude-code: headless Claude Code + agent (default); api: Claude API with ANTHROPIC_API_KEY")
    ap.add_argument("--budget", type=float, help="spending cap in USD (api engine only)")
    ap.add_argument("--model", help="Claude model for the analysis (default: your Claude Code default)")
    ap.add_argument("--parallel", type=int, default=3, help="concurrent Claude batches (default 3)")
    ap.add_argument("--skip-source", action="store_true", help="do not download unmanaged source code")
    ap.add_argument("--resume", type=Path, help="continue an existing run folder")
    ap.add_argument("--report-only", type=Path, help="only re-render the report for a run folder")
    args = ap.parse_args()
    interactive = sys.stdin.isatty()

    from rc import ai_analysis, org_inventory, prefilter, report
    from rc.sfcli import SfError, choose_org

    if args.report_only:
        banner("Rendering report")
        for kind, p in report.render(args.report_only.resolve()).items():
            print(f"  {kind.upper():5} {p}")
        return 0

    print("Salesforce Release Impact Check")
    if args.engine == "api" and not args.no_ai and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--engine api needs the ANTHROPIC_API_KEY environment variable.")
    check_prereqs(need_ai=not args.no_ai and args.engine == "claude-code")

    if args.resume:
        run_dir = args.resume.resolve()
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        print(f"  Resuming {run_dir.name} ({meta['release']} vs {meta['target_org']})")
    else:
        banner("1/6 Salesforce org")
        if not interactive and not args.target_org:
            sys.exit("Non-interactive session: pass --target-org <alias> (log in first with 'sf org login web').")
        try:
            org = choose_org(args.target_org)
        except SfError as exc:
            sys.exit(f"Could not connect to the org: {exc}")
        target = org.get("alias") or org["username"]
        print(f"  Using {target} -> {org['instanceUrl']}")

        banner("2/6 Release notes")
        pdf = choose_pdf(args.pdf, interactive)
        print(f"  {pdf.name}")
        features_cache = parse_cached(pdf)
        release = json.loads(features_cache.read_text(encoding="utf-8"))["release"]

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        run_dir = OUTPUT_DIR / f"{re.sub(r'[^A-Za-z0-9]+', '', release)}_{re.sub(r'[^A-Za-z0-9-]+', '_', target)}_{stamp}"
        run_dir.mkdir(parents=True)
        (run_dir / "release_features.json").write_bytes(features_cache.read_bytes())
        meta = {"release": release, "pdf": pdf.name, "target_org": target, "username": org["username"],
                "instance_url": org["instanceUrl"], "started": stamp, "ai": not args.no_ai}
        (run_dir / "run.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

        banner("3/6 Org inventory (read-only)")
        org_inventory.collect(target, run_dir / "org", include_source=not args.skip_source)

    banner("4/6 Rule-based pre-assessment")
    prefilter.run(run_dir / "release_features.json", run_dir / "org", run_dir / "prefilter.json")

    if not args.no_ai and meta.get("ai", True):
        banner("5/6 AI analysis (Claude)")
        if args.engine == "api":
            from rc import ai_api
            from rc.sfcli import SalesforceRest, display_org

            def show(e):
                if e["type"] == "batch_done":
                    print(f"    batch {e['batch']:02d}: {'ok' if e['ok'] else 'FAILED'} ({e['done']}/{e['total']}, "
                          f"est. ${e['cost']:.2f})")
                elif e["type"] == "log":
                    print(f"    {e['message']}")
            res = ai_api.analyse_api(run_dir, os.environ["ANTHROPIC_API_KEY"], model=args.model or ai_api.DEFAULT_MODEL,
                                     parallel=args.parallel, budget_usd=args.budget,
                                     org_rest=SalesforceRest(display_org(meta["target_org"])), on_event=show)
            print(f"  Estimated Claude API cost: ${res['cost_usd']:.2f}")
        else:
            res = ai_analysis.analyse(run_dir, model=args.model, parallel=args.parallel)
        print(f"  AI reviewed {res['reviewed']} features in {res['batches']} batches")
        if res["failed_batches"]:
            print(f"  ! {len(res['failed_batches'])} batch(es) failed; logs in {run_dir / 'ai' / 'logs'}")
            print(f"    Re-run with: python release_check.py --resume \"{run_dir}\"")
    else:
        banner("5/6 AI analysis skipped (--no-ai)")

    banner("6/6 Report")
    from rc import docs
    paths = report.render(run_dir)
    for kind, p in paths.items():
        print(f"  {kind.upper():5} {p}")
    for kind in ("technical", "business"):
        for fmt in ("docx", "pdf"):
            print(f"  {fmt.upper():5} {docs.generate(run_dir, kind, fmt)}")
    print("\nShare the Word/PDF documents (technical for developers and architects, business for managers and "
          "PM/PO), the HTML report and the Excel action list with the team.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
