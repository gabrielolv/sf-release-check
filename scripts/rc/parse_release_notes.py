"""Parse a Salesforce Release Notes PDF into a structured list of features.

Salesforce publishes the release notes as a PrinceXML PDF with a consistent layout:
  * headings use the "SalesforceSans-Book" font, larger size = higher level
  * every page footer is "Salesforce Release Notes / <Product Area> / <page>" (9.5pt)
  * release updates in the summary section are bold 10pt lines ending in "(Release Update)"
  * feature bodies carry "Where:", "When:", "Why:" and "How:" paragraphs

Usage:
    python -m rc.parse_release_notes <pdf> <out.json>
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

FOOTER_SIZE = 9.5
MIN_HEADING_SIZE = 10.5
# Sections that are changelogs, availability matrices, TOC or legal boilerplate.
SKIP_AREAS = {
    "Release Note Changes",
    "How and When Do Features Become Available?",
    "Previous Release Notes",
    "Legal Documentation",
    "Other Salesforce Products and Services",
}
FIELD_PREFIXES = ("Where:", "When:", "Why:", "How:", "Note", "Important", "Warning", "Tip", "Example")
FIELD_RE = re.compile(r"\b(Where|When|Why|How):\s*(.*?)(?=\s(?:Where|When|Why|How):\s|\sSee Also\s|$)", re.S)
CHAR_FIXES = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
              " ": " ", "�": "'", "​": ""}


def clean(text: str) -> str:
    for bad, good in CHAR_FIXES.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip()


LINK_COLOR = 4870396  # blue bold link-titles: "highlights" recaps of features documented elsewhere
NOISE_TITLE_RE = re.compile(r"Release Note Changes|^(January|February|March|April|May|June|July|August|September|"
                            r"October|November|December) (\d{4}|'\d\d)$|^See Also$|Highlights$", re.I)


def page_lines(page):
    """Yield (size, is_book, is_bold, color, text) for each visual line on the page."""
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            first = spans[0]
            yield (round(first["size"], 1), "Book" in first["font"], bool(first["flags"] & 16), first["color"],
                   "".join(s["text"] for s in line["spans"]))


def detect_release(doc) -> str:
    for _, title, _ in doc.get_toc()[:5]:
        m = re.search(r"(Spring|Summer|Winter)\s+['’]?(\d{2})", title)
        if m:
            return f"{m.group(1)} '{m.group(2)}"
    return "Unknown release"


def parse(path: str | Path) -> dict:
    """Parse release notes from a PDF (Salesforce's published layout) or a Word document."""
    if Path(path).suffix.lower() == ".docx":
        return parse_docx(path)
    return parse_pdf(path)


# Product areas used when a Word document has no page footers to tell us the area.
KNOWN_AREAS = ["Salesforce Overall", "Release Updates", "Agentforce and Generative AI", "AIforce", "Analytics",
               "Automation", "Apromore", "Commerce", "Customization", "Data 360", "Deployment", "Development",
               "Experience Cloud", "Field Service", "Hyperforce", "Industries", "Marketing", "Mobile", "MuleSoft",
               "Partner Cloud", "Platform", "Public Sector", "Revenue Management", "Sales", "Salesforce CMS",
               "Salesforce Suites", "Security, Identity, and Privacy", "Service", "Slack Integrations"]


def _features_from_nodes(nodes: list[dict]) -> list[dict]:
    features, seen = [], set()
    for node in nodes:
        body = clean(" ".join(node["body"]))
        if len(body) < 40 or node.get("overview") or NOISE_TITLE_RE.search(node["title"]) \
                or any(NOISE_TITLE_RE.search(p) for p in node["path"]):
            continue  # section heading, highlights recap or changelog
        fields = {k.lower(): clean(v) for k, v in FIELD_RE.findall(body)}
        key = (node["title"], body[:200])
        fid = "F" + hashlib.sha1("|".join([node["area"], *node["path"], node["title"]]).encode()).hexdigest()[:8]
        features.append({
            "id": fid, "title": node["title"], "area": node["area"], "path": node["path"],
            "page": node["page"], "pdf_page": node["pdf_page"],
            "where": fields.get("where", ""), "when": fields.get("when", ""),
            "why": fields.get("why", ""), "how": fields.get("how", ""), "text": body,
            "is_release_update": "(Release Update)" in node["title"] or node["area"] == "Release Updates",
            "duplicate": key in seen,
        })
        seen.add(key)
    return features


def parse_docx(docx_path: str | Path) -> dict:
    """Word release notes: headings come from 'Heading N' / 'Title' styles (or short bold paragraphs)."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(docx_path))
    nodes: list[dict] = []
    stack: list[tuple[int, dict]] = []
    release = "Unknown release"
    para_no = 0

    def heading_level(p) -> int | None:
        name = (p.style.name or "") if p.style is not None else ""
        if name == "Title":
            return 1
        m = re.match(r"Heading\s*(\d)", name)
        if m:
            return int(m.group(1))
        text = p.text.strip()
        runs = [r for r in p.runs if r.text.strip()]
        if runs and all(r.bold for r in runs) and 3 < len(text) < 160 and not text.endswith((".", ":")) \
                and not text.startswith(FIELD_PREFIXES):
            return 6  # unstyled bold line -> lowest-level heading
        return None

    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            rows = [" | ".join(clean(c.text) for c in row.cells)
                    for row in Table(block, doc).rows]
            if stack:
                stack[-1][1]["body"].append(" ; ".join(rows))
            continue
        if tag != "p":
            continue
        p = Paragraph(block, doc)
        text = p.text.strip()
        if not text:
            continue
        para_no += 1
        if release == "Unknown release":
            m = re.search(r"(Spring|Summer|Winter)\s+['’]?(\d{2})", text)
            if m:
                release = f"{m.group(1)} '{m.group(2)}"
        level = heading_level(p)
        if level is None:
            if stack:
                stack[-1][1]["body"].append(text)
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        path = [n["title"] for _, n in stack]
        area = next((t for t in [*path, clean(text)] if t in KNOWN_AREAS), path[0] if path else clean(text))
        node = {"title": clean(text), "area": area, "path": path, "pdf_page": None, "page": f"¶{para_no}",
                "body": []}
        stack.append((level, node))
        nodes.append(node)
    features = _features_from_nodes(nodes)
    return {"release": release, "source": Path(docx_path).name, "page_count": None,
            "feature_count": len(features), "features": features}


def parse_pdf(pdf_path: str | Path) -> dict:
    doc = pymupdf.open(str(pdf_path))
    release = detect_release(doc)
    nodes: list[dict] = []
    stack: list[tuple[float, dict]] = []  # (heading size, node)
    current: dict | None = None
    bold_buffer: list[str] = []
    last_was_heading = False

    def open_node(title: str, size: float, area: str, page_no: int, printed: str):
        nonlocal current
        while stack and stack[-1][0] <= size:
            stack.pop()
        node = {
            "title": clean(title),
            "area": area,
            "path": [n["title"] for _, n in stack],
            "pdf_page": page_no,
            "page": printed,
            "body": [],
        }
        stack.append((size, node))
        nodes.append(node)
        current = node

    def flush_bold(area, page_no, printed, rel_size):
        """Bold 10pt runs ending in '(Release Update)' are release-update headings."""
        nonlocal bold_buffer
        if not bold_buffer:
            return
        joined = clean(" ".join(bold_buffer))
        if joined.endswith("(Release Update)") and not joined.startswith(FIELD_PREFIXES):
            open_node(joined, rel_size, area, page_no, printed)
        elif current is not None:
            current["body"].append(joined)
        bold_buffer = []

    for page in doc:
        lines = list(page_lines(page))
        footer = [t.strip() for s, _, _, _, t in lines if s == FOOTER_SIZE]
        area = footer[1] if len(footer) >= 2 else ""
        printed = footer[2] if len(footer) >= 3 else ""
        if not area or area in SKIP_AREAS or area.isdigit() or "CONTENTS" in [t.strip() for *_, t in lines]:
            current = None
            stack.clear()
            continue
        page_no = page.number + 1
        for size, is_book, is_bold, color, text in lines:
            if size == FOOTER_SIZE or size == 15.0:  # 15pt = embedded changelog bullets
                continue
            if is_bold and color == LINK_COLOR and current is not None and text.strip() != "See Also":
                current["overview"] = True
            if is_book and size >= MIN_HEADING_SIZE:
                flush_bold(area, page_no, printed, 1.0)
                if last_was_heading and current is not None and stack and stack[-1][0] == size and not current["body"]:
                    current["title"] = clean(current["title"] + " " + text)  # wrapped heading
                else:
                    open_node(text, size, area, page_no, printed)
                last_was_heading = True
                continue
            last_was_heading = False
            if is_bold and size <= 10.0 and not text.strip().startswith(FIELD_PREFIXES):
                bold_buffer.append(text)
                continue
            flush_bold(area, page_no, printed, 1.0)
            if current is not None:
                current["body"].append(text)
        flush_bold(area, page_no, printed, 1.0)

    features = _features_from_nodes(nodes)
    return {"release": release, "source": Path(pdf_path).name, "page_count": doc.page_count,
            "feature_count": len(features), "features": features}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    result = parse(argv[1])
    Path(argv[2]).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{result['release']}: {result['feature_count']} features parsed from {result['page_count']} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
