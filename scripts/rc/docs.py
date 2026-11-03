"""Technical and non-technical release impact documents, exported as Word (.docx) and PDF.

Both documents are built once as a small block model, then rendered by two backends (python-docx, reportlab),
so Word and PDF always carry the same content.

Usage:
    python -m rc.docs <run_dir> <technical|business> <docx|pdf>
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from .report import build_model, load_run, slug

IMPACT_RGB = {"High": (0xB4, 0x23, 0x18), "Medium": (0xA1, 0x5C, 0x00), "Low": (0x1D, 0x7A, 0x43),
              "Not Applicable": (0x5C, 0x63, 0x70)}
IMPACT_FILL = {"High": "FDE8E7", "Medium": "FFF4DC", "Low": "E3F4EA", "Not Applicable": "EEF0F3"}
ACCENT = (0x03, 0x2D, 0x60)
KINDS = {"technical": "Technical Impact Assessment", "business": "Release Impact Summary"}


# ---------------------------------------------------------------------------------------------- document model
class Doc:
    """Ordered list of blocks: (kind, payload). Inline markup: **bold** and `code`."""

    def __init__(self, title: str, subtitle: str, meta: list[tuple[str, str]]):
        self.title, self.subtitle, self.meta, self.blocks = title, subtitle, meta, []

    def h1(self, t): self.blocks.append(("h1", t))
    def h2(self, t): self.blocks.append(("h2", t))
    def h3(self, t): self.blocks.append(("h3", t))
    def p(self, t): t and self.blocks.append(("p", t))
    def note(self, t): self.blocks.append(("note", t))
    def bullets(self, items): items and self.blocks.append(("bullets", [i for i in items if i]))
    def numbered(self, items): items and self.blocks.append(("numbered", [i for i in items if i]))
    def kv(self, pairs): self.blocks.append(("kv", [(k, v) for k, v in pairs if v]))
    def page_break(self): self.blocks.append(("break", None))

    def table(self, headers, rows, widths=None, impact_col=None):
        self.blocks.append(("table", {"headers": headers, "rows": rows, "widths": widths, "impact_col": impact_col}))


def _md_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}", text or "") if p.strip()]


# ---------------------------------------------------------------------------------------------- content builders
def _header(model: dict, kind: str) -> Doc:
    o = model["org"]
    return Doc(
        title=f"{model['release']} {KINDS[kind]}",
        subtitle=(f"{o['name']} ({o['alias']})" if o.get("name") else str(o["alias"])),
        meta=[("Org", f"{o.get('name') or ''} - {o.get('url') or ''}"),
              ("Edition", f"{o.get('edition') or ''}{' (sandbox)' if o.get('sandbox') else ''}"),
              ("Audience", "Developers, Architects" if kind == "technical" else "Managers, Product Managers, Product Owners"),
              ("Generated", model["generated"]),
              ("Release notes", model.get("source_pdf") or ""),
              ("Overall risk", model["summary"].get("overall_risk", "not assessed"))])


def _counts_table(doc: Doc, model: dict):
    c = model["counts"]
    doc.table(["Impact", "Features", "What it means"], [
        ["High", str(c["High"]), "Must be handled before the production upgrade"],
        ["Medium", str(c["Medium"]), "Test in the preview sandbox, communicate, plan"],
        ["Low", str(c["Low"]), "Optional / informational - no action required"],
        ["Not Applicable", str(c["Not Applicable"]), "Product, license or edition not used by this org"],
    ], widths=[1.2, 0.8, 4.0], impact_col=0)


def build_business(model: dict) -> Doc:
    s, aud = model["summary"], model["summary"].get("audiences", {})
    doc = _header(model, "business")
    doc.h1("1. Summary")
    if s.get("headline"):
        doc.note(s["headline"])
    for para in _md_paragraphs(s.get("executive_summary", "")):
        doc.p(para)
    _counts_table(doc, model)
    if aud.get("managers", {}).get("decisions_needed"):
        doc.h2("Decisions needed")
        doc.bullets(aud["managers"]["decisions_needed"])

    doc.h1("2. Top risks")
    if s.get("top_risks"):
        doc.table(["Risk", "Impact", "Owner", "Deadline", "Mitigation"],
                  [[f"**{r.get('title', '')}**\n{r.get('why', '')}", r.get("impact", ""), r.get("owner", ""),
                    r.get("deadline", ""), r.get("mitigation", "")] for r in s["top_risks"]],
                  widths=[2.4, 0.8, 1.1, 1.1, 2.2], impact_col=1)
    else:
        doc.p("No AI summary available for this run - see section 4 for the High impact items.")

    if s.get("key_dates"):
        doc.h1("3. Key dates")
        doc.table(["Date", "Event"], [[d.get("date", ""), d.get("event", "")] for d in s["key_dates"]],
                  widths=[1.4, 5.0])

    doc.h1("4. What must be done before the upgrade")
    high = [f for f in model["features"] if f["impact"] == "High"]
    if high:
        doc.table(["Change", "What it means for us", "Owner", "Deadline"],
                  [[f["title"], f.get("summary") or f.get("rationale") or "", ", ".join(f.get("owner_roles") or []),
                    f.get("deadline") or ""] for f in high], widths=[2.0, 3.0, 1.1, 1.3])
    else:
        doc.p("No High impact changes were found for this org.")
    effort = Counter(f.get("effort") or "n/a" for f in model["features"] if f["impact"] in ("High", "Medium"))
    owners = Counter(o for f in model["features"] if f["impact"] in ("High", "Medium") for o in f.get("owner_roles") or [])
    if effort:
        doc.h2("Effort overview (High + Medium items)")
        doc.table(["Effort", "Items"], [[k, str(v)] for k, v in sorted(effort.items())], widths=[2.0, 1.0])
    if owners:
        doc.table(["Owner role", "Items"], [[k, str(v)] for k, v in owners.most_common()], widths=[2.0, 1.0])

    pm = aud.get("pm_po", {})
    doc.h1("5. Impact on users and the product")
    doc.p(pm.get("summary", ""))
    for label, key in (("User-facing changes", "user_facing_changes"), ("Communication and training", "communication_needs"),
                       ("Opportunities for the backlog", "opportunities")):
        if pm.get(key):
            doc.h2(label)
            doc.bullets(pm[key])
    medium_users = [f for f in model["features"] if f["impact"] == "Medium" and "PM/PO" in (f.get("audiences") or [])]
    if medium_users:
        doc.h2("Medium impact changes users may notice")
        doc.table(["Change", "Summary"], [[f["title"], f.get("summary") or ""] for f in medium_users], widths=[2.4, 4.0])

    if s.get("plan"):
        doc.h1("6. Readiness plan")
        for ph in s["plan"]:
            doc.h3(f"{ph.get('phase', '')} - {ph.get('when', '')}")
            doc.bullets(ph.get("activities", []))

    doc.h1("Appendix - how to read this document")
    doc.table(["Level", "Meaning"], [
        ["High", "Breaking, mandatory or security change on something this org uses, with a hard date."],
        ["Medium", "Existing behavior changes or work is required, without outage risk if handled on time."],
        ["Low", "Optional or additive change in an area the org uses."],
        ["Not Applicable", "Product, license or edition not present in this org."]], widths=[1.3, 5.0], impact_col=0)
    doc.p(f"{model['total']} release-note features were analysed; {model['ai_reviewed']} were reviewed in depth by AI "
          "against the org's configuration and code. AI-generated content - verify High items before acting.")
    if s.get("assumptions_limitations"):
        doc.h2("Assumptions and limitations")
        doc.bullets(s["assumptions_limitations"])
    return doc


def _feature_detail(doc: Doc, f: dict, compact: bool = False):
    doc.h3(f["title"])
    doc.kv([("Impact", f["impact"]), ("Area", f["area"]), ("Reference", f"release notes p. {f['page']} - {f['id']}"),
            ("Confidence", f.get("confidence") or ""), ("Release update", f.get("release_update_group") or "")])
    doc.p(f.get("summary") or "")
    if f.get("rationale"):
        doc.p(f"**Why for this org:** {f['rationale']}")
    if f.get("affected_components"):
        doc.p("**Affected:** " + ", ".join(f"`{c}`" for c in f["affected_components"]))
    if f.get("actions"):
        doc.numbered(f["actions"])
    doc.kv([("Owners", ", ".join(f.get("owner_roles") or [])), ("Effort", f.get("effort") or ""),
            ("Deadline", f.get("deadline") or ""), ("Test notes", "" if compact else f.get("test_notes") or "")])


def build_technical(model: dict) -> Doc:
    s, aud = model["summary"], model["summary"].get("audiences", {})
    feats = model["features"]
    doc = _header(model, "technical")
    doc.h1("1. Overview")
    if s.get("headline"):
        doc.note(s["headline"])
    _counts_table(doc, model)
    doc.p(f"{model['total']} features analysed, {model['ai_reviewed']} reviewed in depth by AI. Ratings follow the "
          "impact rubric in the appendix. Feature ids (F...) and release-note page numbers trace every item back to "
          "its source.")

    doc.h1("2. Architecture considerations")
    arch = aud.get("architects", {})
    doc.p(arch.get("summary", ""))
    doc.bullets(arch.get("considerations", []))
    if s.get("top_risks"):
        doc.h2("Top risks")
        doc.table(["Risk", "Impact", "Mitigation", "Features"],
                  [[r.get("title", ""), r.get("impact", ""), r.get("mitigation", ""), ", ".join(r.get("feature_ids", []))]
                   for r in s["top_risks"]], widths=[2.0, 0.8, 3.0, 1.2], impact_col=1)

    doc.h1("3. Developer task list")
    dev = aud.get("developers", {})
    doc.p(dev.get("summary", ""))
    if dev.get("tasks"):
        doc.table(["Component", "Task", "Features"],
                  [[t.get("component", ""), t.get("task", ""), ", ".join(t.get("feature_ids", []))] for t in dev["tasks"]],
                  widths=[1.2, 4.2, 1.2])
    hits = [(f, h) for f in feats for h in (f.get("code_hits") or [])]
    if hits:
        doc.h2("Code references found in the org")
        doc.table(["Feature", "API / token", "Files"],
                  [[f["title"], f"`{h['token']}`", ", ".join(h["files"][:5]) + (" ..." if h["file_count"] > 5 else "")]
                   for f, h in hits[:80]], widths=[2.4, 1.6, 2.6])

    for n, imp in ((4, "High"), (5, "Medium")):
        items = [f for f in feats if f["impact"] == imp]
        doc.page_break()
        doc.h1(f"{n}. {imp} impact items ({len(items)})")
        if not items:
            doc.p(f"No {imp} impact items.")
        for f in items:
            _feature_detail(doc, f, compact=imp == "Medium")

    doc.page_break()
    rus = [f for f in feats if f.get("is_release_update") and f["impact"] != "Not Applicable"]
    doc.h1(f"6. Release updates ({len(rus)})")
    if rus:
        doc.table(["Release update", "Schedule", "Impact", "Action"],
                  [[f["title"], f.get("release_update_group") or "", f["impact"], (f.get("actions") or [""])[0]]
                   for f in rus], widths=[2.4, 1.5, 0.8, 2.3], impact_col=2)
    low = [f for f in feats if f["impact"] == "Low"]
    doc.h1(f"7. Low impact items ({len(low)})")
    if low:
        doc.table(["Area", "Feature", "Note"], [[f["area"], f["title"], f.get("summary") or ""] for f in low],
                  widths=[1.3, 2.6, 2.7])

    doc.h1("8. Org profile")
    doc.table(["Capability", "In use", "Evidence"],
              [[c["label"], "Yes" if c["present"] else "No", "; ".join(c["evidence"][:3])] for c in model["capabilities"]],
              widths=[2.0, 0.6, 4.0])
    doc.table(["Metadata", "Count"], [[k, f"{v:,}"] for k, v in model["metadata"].items()], widths=[3.0, 1.0])

    doc.h1("Appendix A - Not applicable by area")
    doc.table(["Area", "Features"], [[a["area"], str(a["Not Applicable"])] for a in model["areas"] if a["Not Applicable"]],
              widths=[3.0, 1.0])
    doc.h1("Appendix B - Method and impact rubric")
    doc.numbered(["Release notes parsed into features (changelogs, highlight recaps and availability matrices excluded).",
                  "Read-only org inventory: licenses, packages, record counts, metadata and API versions, login types, "
                  "unmanaged source code.",
                  "Rule-based pre-assessment of every feature (relevance x severity).",
                  "AI review of the shortlisted features with evidence checks (read-only SOQL, source search), "
                  "followed by the cross-audience synthesis."])
    doc.table(["Level", "Definition", "Expected action"], [
        ["High", "Breaking, mandatory or security change on something the org demonstrably uses, with a hard date",
         "Before the production upgrade"],
        ["Medium", "Behavior change or required work: default-on changes, future enforcement, deprecations, "
                   "API-version-bound changes", "Preview sandbox test, communicate, plan"],
        ["Low", "Optional or additive change in a used area", "None (opportunity)"],
        ["Not Applicable", "Product, license or edition not present", "None"]], widths=[1.1, 3.6, 1.9], impact_col=0)
    if s.get("assumptions_limitations"):
        doc.h2("Assumptions and limitations")
        doc.bullets(s["assumptions_limitations"])
    return doc


# ---------------------------------------------------------------------------------------------- Word renderer
def render_docx(doc: Doc, path: Path) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Inches

    d = Document()
    sec = d.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.8)
    base = d.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(10.5)
    for name, size in (("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11.5)):
        st = d.styles[name]
        st.font.name, st.font.size, st.font.color.rgb = "Calibri", Pt(size), RGBColor(*ACCENT)

    def add_inline(par, text, bold=False, color=None):
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text or ""):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                run = par.add_run(part[2:-2]); run.bold = True
            elif part.startswith("`") and part.endswith("`"):
                run = par.add_run(part[1:-1]); run.font.name = "Consolas"; run.font.size = Pt(9.5)
            else:
                run = par.add_run(part); run.bold = bold
            if color:
                run.font.color.rgb = RGBColor(*color)

    def shade(cell, hex_fill):
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_fill)
        tc_pr.append(shd)

    t = d.add_paragraph()
    add_inline(t, doc.title, bold=True, color=ACCENT)
    t.runs[0].font.size = Pt(22)
    st = d.add_paragraph()
    add_inline(st, doc.subtitle)
    st.runs[0].font.size = Pt(13)
    meta = d.add_table(rows=0, cols=2)
    for k, v in doc.meta:
        r = meta.add_row().cells
        add_inline(r[0].paragraphs[0], k, bold=True)
        add_inline(r[1].paragraphs[0], v)
    d.add_paragraph()

    for kind, payload in doc.blocks:
        if kind in ("h1", "h2", "h3"):
            d.add_heading(payload, level=int(kind[1]))
        elif kind == "p":
            add_inline(d.add_paragraph(), payload)
        elif kind == "note":
            par = d.add_paragraph()
            add_inline(par, payload, bold=True, color=ACCENT)
        elif kind in ("bullets", "numbered"):
            for item in payload:
                add_inline(d.add_paragraph(style="List Bullet" if kind == "bullets" else "List Number"), item)
        elif kind == "kv":
            par = d.add_paragraph()
            for i, (k, v) in enumerate(payload):
                add_inline(par, f"{'   ' if i else ''}**{k}:** ")
                color = IMPACT_RGB.get(v) if k == "Impact" else None
                add_inline(par, v, bold=bool(color), color=color)
        elif kind == "break":
            d.add_page_break()
        elif kind == "table":
            tb = d.add_table(rows=1, cols=len(payload["headers"]))
            tb.style = "Table Grid"
            tb.alignment = WD_TABLE_ALIGNMENT.CENTER
            for i, h in enumerate(payload["headers"]):
                cell = tb.rows[0].cells[i]
                add_inline(cell.paragraphs[0], h, bold=True, color=(0xFF, 0xFF, 0xFF))
                shade(cell, "%02X%02X%02X" % ACCENT)
            for row in payload["rows"]:
                cells = tb.add_row().cells
                for i, val in enumerate(row):
                    lines = str(val).split("\n")
                    add_inline(cells[i].paragraphs[0], lines[0])
                    for extra in lines[1:]:
                        add_inline(cells[i].add_paragraph(), extra)
                    if payload["impact_col"] == i and val in IMPACT_FILL:
                        shade(cells[i], IMPACT_FILL[val])
            if payload["widths"]:
                total = sum(payload["widths"])
                for row in tb.rows:
                    for i, w in enumerate(payload["widths"]):
                        row.cells[i].width = Inches(6.9 * w / total)
            for row in tb.rows:
                for cell in row.cells:
                    for par in cell.paragraphs:
                        for run in par.runs:
                            run.font.size = Pt(9)
            d.add_paragraph()
    footer = sec.footer.paragraphs[0]
    add_inline(footer, f"{doc.title} - {doc.subtitle} - AI-assisted assessment, verify before acting")
    footer.runs[0].font.size = Pt(8)
    d.save(path)


# ---------------------------------------------------------------------------------------------- PDF renderer
def _pdf_fonts() -> tuple[str, str, str]:
    """Register a Unicode TTF font when available (Docker: DejaVu; Windows: Arial); else built-in Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
                  ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/consola.ttf")]
    for reg, bold, mono in candidates:
        if all(Path(p).exists() for p in (reg, bold, mono)):
            pdfmetrics.registerFont(TTFont("RC", reg))
            pdfmetrics.registerFont(TTFont("RC-Bold", bold))
            pdfmetrics.registerFont(TTFont("RC-Mono", mono))
            pdfmetrics.registerFontFamily("RC", normal="RC", bold="RC-Bold", italic="RC", boldItalic="RC-Bold")
            return "RC", "RC-Bold", "RC-Mono"
    return "Helvetica", "Helvetica-Bold", "Courier"


def render_pdf(doc: Doc, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    font, bold, mono = _pdf_fonts()
    accent = colors.Color(*(c / 255 for c in ACCENT))
    st = {
        "title": ParagraphStyle("title", fontName=bold, fontSize=22, leading=27, textColor=accent, spaceAfter=6),
        "subtitle": ParagraphStyle("subtitle", fontName=font, fontSize=13, leading=17, spaceAfter=10),
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=15, leading=19, textColor=accent, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=16, textColor=accent, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=11, leading=14, spaceBefore=9, spaceAfter=3),
        "p": ParagraphStyle("p", fontName=font, fontSize=9.8, leading=13.5, spaceAfter=5),
        "note": ParagraphStyle("note", fontName=bold, fontSize=11, leading=15, textColor=accent, spaceAfter=8,
                               borderPadding=6, backColor=colors.HexColor("#EEF3FA")),
        "cell": ParagraphStyle("cell", fontName=font, fontSize=8.5, leading=11),
        "head": ParagraphStyle("head", fontName=bold, fontSize=8.5, leading=11, textColor=colors.white),
    }

    def inline(text: str) -> str:
        out = []
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", str(text or "")):
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                out.append(f"<b>{escape(part[2:-2])}</b>")
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append(f'<font face="{mono}" size="8.5">{escape(part[1:-1])}</font>')
            else:
                out.append(escape(part))
        return "".join(out).replace("\n", "<br/>")

    width = A4[0] - 3.6 * cm
    story = [Paragraph(inline(doc.title), st["title"]), Paragraph(inline(doc.subtitle), st["subtitle"])]
    meta = Table([[Paragraph(f"<b>{escape(k)}</b>", st["cell"]), Paragraph(inline(v), st["cell"])] for k, v in doc.meta],
                 colWidths=[3.2 * cm, width - 3.2 * cm])
    meta.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#D9DDE3")),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [meta, Spacer(1, 12)]

    for kind, payload in doc.blocks:
        if kind in ("h1", "h2", "h3", "p", "note"):
            story.append(Paragraph(inline(payload), st[kind]))
        elif kind in ("bullets", "numbered"):
            story.append(ListFlowable([ListItem(Paragraph(inline(i), st["p"]), leftIndent=12) for i in payload],
                                      bulletType="bullet" if kind == "bullets" else "1", start=None if kind == "bullets" else 1,
                                      leftIndent=14, bulletFontName=font, bulletFontSize=8))
        elif kind == "kv":
            parts = []
            for k, v in payload:
                val = inline(v)
                if k == "Impact" and v in IMPACT_RGB:
                    val = f'<font color="#%02X%02X%02X"><b>{val}</b></font>' % IMPACT_RGB[v]
                parts.append(f"<b>{escape(k)}:</b> {val}")
            story.append(Paragraph("&nbsp;&nbsp;&nbsp;".join(parts), st["p"]))
        elif kind == "break":
            story.append(PageBreak())
        elif kind == "table":
            ws = payload["widths"] or [1] * len(payload["headers"])
            col_w = [width * w / sum(ws) for w in ws]
            data = [[Paragraph(inline(h), st["head"]) for h in payload["headers"]]]
            data += [[Paragraph(inline(v), st["cell"]) for v in row] for row in payload["rows"]]
            tb = Table(data, colWidths=col_w, repeatRows=1)
            style = [("BACKGROUND", (0, 0), (-1, 0), accent), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C9CED6")),
                     ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
            if payload["impact_col"] is not None:
                for r, row in enumerate(payload["rows"], 1):
                    fill = IMPACT_FILL.get(row[payload["impact_col"]])
                    if fill:
                        style.append(("BACKGROUND", (payload["impact_col"], r), (payload["impact_col"], r),
                                      colors.HexColor("#" + fill)))
            tb.setStyle(TableStyle(style))
            story += [tb, Spacer(1, 8)]

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#5C6370"))
        canvas.drawString(1.8 * cm, 1.1 * cm, f"{doc.title} - {doc.subtitle} - AI-assisted assessment, verify before acting")
        canvas.drawRightString(A4[0] - 1.8 * cm, 1.1 * cm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm,
                      bottomMargin=1.8 * cm, title=doc.title, author="Salesforce Release Impact Check").build(
        story, onFirstPage=footer, onLaterPages=footer)


# ---------------------------------------------------------------------------------------------- entry point
def generate(run_dir: Path, kind: str, fmt: str) -> Path:
    if kind not in KINDS or fmt not in ("docx", "pdf"):
        raise ValueError("kind must be technical|business and fmt docx|pdf")
    model = build_model(load_run(run_dir))
    doc = build_technical(model) if kind == "technical" else build_business(model)
    out = run_dir / "report"
    out.mkdir(exist_ok=True)
    label = "Technical" if kind == "technical" else "Business"
    path = out / f"Release_Impact_{label}_{slug(model['release'])}_{slug(str(model['org']['alias']))}.{fmt}"
    (render_docx if fmt == "docx" else render_pdf)(doc, path)
    return path


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    print(generate(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
