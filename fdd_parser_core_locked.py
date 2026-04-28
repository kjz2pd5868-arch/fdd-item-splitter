#!/usr/bin/env python3
"""
Clean FDD splitter/parser.

Goal:
- Split FDD PDFs into FrontEnd, Items 1-21, and Franchise Agreement.
- Avoid hidden-text bleed by physically redacting content outside boundaries on boundary pages.
- Use a line-level parser with coordinates instead of relying on TOC page ranges alone.

Run:
    python3 -u fdd_splitter_v1_5_boundary_fixed_v3.py "FDD" "output" --batch --debug

Requires:
    python3 -m pip install pymupdf
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

try:
    import fitz  # PyMuPDF
except ImportError as exc:
    raise SystemExit("Install PyMuPDF first: python3 -m pip install pymupdf") from exc

ITEMS_TO_EXPORT = list(range(1, 22))
ITEMS_TO_DETECT = list(range(1, 24))
TOP_PAD = 3.0
BOTTOM_PAD = 3.0
MIN_PAGE_SLICE_HEIGHT = 18.0
HEADER_FOOTER_TOP = 42.0
HEADER_FOOTER_BOTTOM = 42.0

ITEM_TITLES = {
    1: ["FRANCHISOR", "PARENTS", "PREDECESSORS", "AFFILIATES"],
    2: ["BUSINESS EXPERIENCE"],
    3: ["LITIGATION"],
    4: ["BANKRUPTCY"],
    5: ["INITIAL FEES"],
    6: ["OTHER FEES"],
    7: ["ESTIMATED INITIAL INVESTMENT"],
    8: ["RESTRICTIONS ON SOURCES", "PRODUCTS AND SERVICES"],
    9: ["FRANCHISEE", "OBLIGATIONS"],
    10: ["FINANCING"],
    11: ["FRANCHISOR", "ASSISTANCE", "ADVERTISING", "COMPUTER", "TRAINING"],
    12: ["TERRITORY"],
    13: ["TRADEMARKS"],
    14: ["PATENTS", "COPYRIGHTS", "PROPRIETARY INFORMATION"],
    15: ["OBLIGATION TO PARTICIPATE", "ACTUAL OPERATION"],
    16: ["RESTRICTIONS ON WHAT", "MAY SELL"],
    17: ["RENEWAL", "TERMINATION", "TRANSFER"],
    18: ["PUBLIC FIGURES"],
    19: ["FINANCIAL PERFORMANCE"],
    20: ["OUTLETS", "FRANCHISEE INFORMATION"],
    21: ["FINANCIAL STATEMENTS"],
    22: ["CONTRACTS"],
    23: ["RECEIPTS"],
}

VALIDATION_ANCHORS = {
    1: ["franchisor"],
    2: ["business experience"],
    3: ["litigation"],
    4: ["bankruptcy"],
    5: ["initial franchise fee", "initial fees"],
    6: ["other fees", "gross sales"],
    7: ["estimated initial investment"],
    8: ["approved supplier", "restrictions on sources"],
    9: ["franchisee", "obligations"],
    10: ["financing"],
    11: ["training", "assistance"],
    12: ["territory"],
    13: ["trademark"],
    14: ["proprietary information", "copyright", "patent"],
    15: ["participate", "actual operation"],
    16: ["restrictions on what", "may sell"],
    17: ["renewal", "termination", "transfer"],
    18: ["public figures"],
    19: ["financial performance"],
    20: ["outlets", "franchisee information"],
    21: ["financial statements"],
}

NOISE_RE = re.compile(
    r"^(?:\d{1,4}\s*)?$|"
    r"^EAST\\|"
    r"^\d{1,4}\s*$|"
    r"^Page\s+\d+\s*$|"
    r"^\w[\w\- ]+\s+\d{4}\s*$",
    re.I,
)
ITEM_LINE_RE = re.compile(r"^\s*(?:U\s*)?ITEM\s+((?:1\d)|(?:2[0-3])|[1-9])\s*(?:U)?\b\s*[:\.]?\s*(.*)$", re.I)
TOC_RE = re.compile(r"table\s+of\s+contents", re.I)
DOT_LEADER_RE = re.compile(r"\.{3,}|_{3,}|\s\d{1,3}\s*$")
CONTRACT_START_RE = re.compile(r"\bFRANCHISE AGREEMENT\b", re.I)
ARTICLE_RE = re.compile(r"\bARTICLE\s+1\b|\bGRANT OF FRANCHISE\b", re.I)
BAD_CONTRACT_START_RE = re.compile(r"lease addendum|confidentiality agreement|state specific|receipt|financial statements", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


def norm(s: str) -> str:
    s = s.replace("\u00ad", "")
    s = s.replace("\u00a0", " ")
    s = s.replace("\uf0a7", " ").replace("\uf0b7", " ")
    s = s.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"[ \t]+", " ", s).strip()


def safe_name(name: str) -> str:
    name = re.sub(r"\.pdf$", "", name, flags=re.I).strip()
    name = re.sub(r"[\\/:*?\"<>|]", "", name)
    return re.sub(r"\s+", " ", name)


@dataclass(frozen=True)
class Line:
    page_idx: int       # zero based
    page_num: int       # one based
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass(frozen=True)
class Hit:
    item: int
    page_idx: int
    page_num: int
    y0: float
    y1: float
    x0: float
    text: str
    score: int
    context: str


@dataclass(frozen=True)
class Boundary:
    item: int
    start_page: int     # zero based
    start_y: float
    end_page: int       # zero based, inclusive
    end_y: Optional[float]  # on end_page, redact below this y. None means page bottom.


def get_text_string(page: fitz.Page, mode: str = "text") -> str:
    """Return PyMuPDF text output as a normalized string.

    PyMuPDF's get_text() is typed loosely by Pylance because different modes
    return different shapes. This helper is only for modes that should return
    text-like content.
    """
    raw: Any = page.get_text(mode)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return norm(raw)
    return norm(str(raw))


def get_text_dict(page: fitz.Page) -> Dict[str, Any]:
    """Return PyMuPDF dict text output with a stable type for Pylance."""
    raw: Any = page.get_text("dict")
    if isinstance(raw, dict):
        return cast(Dict[str, Any], raw)
    return {}


def as_dict(value: Any) -> Dict[str, Any]:
    return cast(Dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    return cast(List[Any], value) if isinstance(value, list) else []


def get_page_lines(doc: fitz.Document) -> Tuple[List[Line], List[str]]:
    all_lines: List[Line] = []
    page_texts: List[str] = []
    for pidx in range(doc.page_count):
        page = doc.load_page(pidx)
        page_text = get_text_string(page, "text")
        page_texts.append(page_text)

        text_dict = get_text_dict(page)
        for block_raw in as_list(text_dict.get("blocks")):
            block = as_dict(block_raw)
            if block.get("type") != 0:
                continue
            for line_raw in as_list(block.get("lines")):
                line_dict = as_dict(line_raw)
                spans = as_list(line_dict.get("spans"))
                span_texts: List[str] = []
                for span_raw in spans:
                    span = as_dict(span_raw)
                    span_text = norm(str(span.get("text", "")))
                    if span_text:
                        span_texts.append(span_text)
                text = norm(" ".join(span_texts))
                if not text:
                    continue
                bbox_raw = line_dict.get("bbox", (0.0, 0.0, 0.0, 0.0))
                if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) >= 4:
                    x0, y0, x1, y1 = (float(bbox_raw[0]), float(bbox_raw[1]), float(bbox_raw[2]), float(bbox_raw[3]))
                else:
                    x0, y0, x1, y1 = (0.0, 0.0, 0.0, 0.0)
                all_lines.append(Line(pidx, pidx + 1, x0, y0, x1, y1, text))
    all_lines.sort(key=lambda l: (l.page_idx, l.y0, l.x0))
    return all_lines, page_texts

def page_has_toc_noise(text: str) -> bool:
    low = text.lower()

    # Be careful: body pages often say "the table of contents of our Manual..."
    # (Item 11 does this immediately before Item 12 in some FDDs). Treat it as
    # TOC noise only when the page actually looks like a disclosure-document TOC.
    itemish = len(re.findall(r"(?im)^\s*item\s+\d{1,2}\b", text))
    dot_leader_lines = len(re.findall(r"(?m)(\.{3,}|_{3,}).*\d{1,4}\s*$", text))

    first_chunk = low[:1500]
    if TOC_RE.search(first_chunk) and (itemish >= 3 or dot_leader_lines >= 3):
        return True

    # Typical TOC pages contain lots of item lines with page numbers/dot leaders.
    # Body tables also repeat item references, so require dot leaders too.
    return itemish >= 5 and dot_leader_lines >= 3 and ("page" in first_chunk or "exhibit" in first_chunk or "attachments" in first_chunk)

def line_context(lines: List[Line], idx: int, window: int = 4) -> str:
    parts = []
    base = lines[idx]
    for j in range(idx, min(len(lines), idx + window + 1)):
        if lines[j].page_idx != base.page_idx:
            break
        parts.append(lines[j].text)
    return " | ".join(parts)


def expected_title_score(item: int, ctx: str) -> int:
    ctxu = ctx.upper()
    score = 0
    for key in ITEM_TITLES.get(item, []):
        if key.upper() in ctxu:
            score += 8
    return score


def is_probable_body_heading(line: Line, ctx: str, page_text: str) -> Tuple[bool, int, str]:
    m = ITEM_LINE_RE.match(line.text)
    if not m:
        return False, 0, "no_item_match"

    item = int(m.group(1))
    rest = norm(m.group(2))
    page_height_hint = 792.0

    # Reject obvious TOC lines and page references.
    if page_has_toc_noise(page_text):
        return False, 0, "toc_page"
    if DOT_LEADER_RE.search(line.text) and len(line.text) > 20:
        return False, 0, "dot_leader_or_trailing_page"
    if line.y0 < HEADER_FOOTER_TOP or line.y0 > (page_height_hint - HEADER_FOOTER_BOTTOM):
        # do not reject outright because pages vary; just penalize below
        pass

    score = 0
    reason = []

    # The most reliable FDD body heading is an isolated ITEM X line.
    if re.fullmatch(r"ITEM\s+\d{1,2}\s*[:\.]?", line.text.strip(), flags=re.I):
        score += 25
        reason.append("isolated_item_line")
    elif len(rest) <= 80 and rest.upper() == rest:
        score += 12
        reason.append("same_line_upper_title")
    elif any(rest.upper().startswith(key.upper()) for key in ITEM_TITLES.get(item, [])):
        score += 18
        reason.append("same_line_expected_title")
    else:
        score -= 12
        reason.append("long_or_nonheader_rest")

    ts = expected_title_score(item, ctx)
    score += ts
    if ts:
        reason.append("expected_title")

    # Some PDFs merge the item heading and the first sentence into one long line,
    # especially around short headings like "ITEM 12 TERRITORY". Do not require
    # the whole line/rest to be uppercase if the expected item title appears
    # immediately after ITEM X in the local context.
    early_ctx = ctx.upper()[:220]
    expected_nearby = any(key.upper() in early_ctx for key in ITEM_TITLES.get(item, []))
    if expected_nearby:
        score += 18
        reason.append("expected_title_near_heading")

    # Body headings tend to sit near the top or after a real paragraph break; allow mid-page same-page transitions.
    if 45 <= line.y0 <= 730:
        score += 4

    # Reject inline references unless title evidence rescues it.
    if len(line.text) > 95 and ts == 0:
        return False, score, "too_long_no_title"

    return score >= 14, score, "+".join(reason) or "accepted"


def page_text_has_real_item_heading(page_text: str, item: int) -> bool:
    """Page-level backup only. Conservative: table cells like Item 11 are not headings."""
    if page_has_toc_noise(page_text):
        return False

    lines = [norm(x) for x in page_text.splitlines() if norm(x)]
    for idx, txt in enumerate(lines):
        if text_line_has_real_item_heading(lines, idx, item):
            return True
    return False


def text_line_has_real_item_heading(lines: List[str], idx: int, item: int) -> bool:
    """Return True only when this exact text line is a real Item heading.

    Fix: obligation tables contain cells like "Item 11". Those are references,
    not section starts. A real heading needs the expected title nearby, e.g.
    ITEM 10 / FINANCING or ITEM 11 / FRANCHISOR'S ASSISTANCE.
    """
    if idx < 0 or idx >= len(lines):
        return False

    txt = norm(lines[idx])
    m = ITEM_LINE_RE.match(txt)
    if not m or int(m.group(1)) != item:
        return False

    if re.match(r"^\s*items\b", txt, flags=re.I):
        return False
    if DOT_LEADER_RE.search(txt) and len(txt) > 20:
        return False

    rest = norm(m.group(2))
    expected = [key.upper() for key in ITEM_TITLES.get(item, [])]
    same_or_next = " ".join(lines[idx: idx + 3]).upper()
    local_window = " ".join(lines[idx: idx + 6]).upper()

    if expected and any(key in same_or_next for key in expected):
        return True
    if expected and any(key in local_window for key in expected):
        return True
    if rest and len(rest) <= 100 and rest.upper() == rest:
        return True

    # Deliberately reject bare "ITEM 11" unless its title is nearby.
    return False


def line_has_real_item_heading(line: Line, lines: List[Line], line_index: int, page_text: str) -> bool:
    """Coordinate-aware heading validation for the exact visual line."""
    if page_has_toc_noise(page_text):
        return False

    m = ITEM_LINE_RE.match(line.text)
    if not m:
        return False
    item = int(m.group(1))

    nearby: List[str] = []
    for j in range(line_index, min(len(lines), line_index + 8)):
        if lines[j].page_idx != line.page_idx:
            break
        nearby.append(lines[j].text)

    return text_line_has_real_item_heading(nearby, 0, item) if nearby else False

def word_level_item_hits(doc: fitz.Document, page_texts: List[str]) -> List[Hit]:
    """Fallback detector for headings missed by PyMuPDF dict line extraction."""
    hits: List[Hit] = []

    for pidx in range(doc.page_count):
        page_text = page_texts[pidx]
        if page_has_toc_noise(page_text):
            continue

        page = doc.load_page(pidx)
        raw_words: Any = page.get_text("words")
        words = raw_words if isinstance(raw_words, list) else []

        rows: List[List[Any]] = []
        for w in sorted(words, key=lambda x: (float(x[1]), float(x[0])) if isinstance(x, (list, tuple)) and len(x) >= 5 else (0.0, 0.0)):
            if not isinstance(w, (list, tuple)) or len(w) < 5:
                continue
            y0 = float(w[1])
            if not rows:
                rows.append([w])
                continue
            prev_y = float(rows[-1][0][1])
            if abs(y0 - prev_y) <= 3.0:
                rows[-1].append(w)
            else:
                rows.append([w])

        for rindex, row in enumerate(rows):
            row_sorted = sorted(row, key=lambda x: float(x[0]))
            tokens = [norm(str(w[4])) for w in row_sorted if norm(str(w[4]))]
            if not tokens:
                continue

            row_text = norm(" ".join(tokens))
            m = ITEM_LINE_RE.match(row_text)

            if not m:
                for tindex, tok in enumerate(tokens[:-1]):
                    if tok.upper() == "ITEM" and re.fullmatch(r"([1-9]|1\d|2[0-3])", tokens[tindex + 1]):
                        item_num = int(tokens[tindex + 1])
                        rest = norm(" ".join(tokens[tindex + 2:]))
                        row_text = norm(f"ITEM {item_num} {rest}")
                        m = ITEM_LINE_RE.match(row_text)
                        break

            if not m:
                continue

            item = int(m.group(1))
            if item not in ITEMS_TO_DETECT:
                continue

            # Validate this exact word row, not merely the page.
            ctx_rows_for_validation = []
            for rr in rows[rindex: min(len(rows), rindex + 8)]:
                rr_sorted = sorted(rr, key=lambda x: float(x[0]))
                ctx_rows_for_validation.append(norm(" ".join(str(w[4]) for w in rr_sorted)))
            if not text_line_has_real_item_heading(ctx_rows_for_validation, 0, item):
                continue

            x0 = min(float(w[0]) for w in row_sorted)
            y0 = min(float(w[1]) for w in row_sorted)
            x1 = max(float(w[2]) for w in row_sorted)
            y1 = max(float(w[3]) for w in row_sorted)

            ctx_rows = []
            for rr in rows[rindex: min(len(rows), rindex + 10)]:
                rr_sorted = sorted(rr, key=lambda x: float(x[0]))
                ctx_rows.append(norm(" ".join(str(w[4]) for w in rr_sorted)))
            ctx = " | ".join(ctx_rows)

            ok, score, reason = is_probable_body_heading(Line(pidx, pidx + 1, x0, y0, x1, y1, row_text), ctx, page_text)
            if ok:
                hits.append(Hit(item, pidx, pidx + 1, y0, y1, x0, row_text, score + 6, ctx))

    return hits


def find_item_hits(doc: fitz.Document, lines: List[Line], page_texts: List[str]) -> List[Hit]:
    hits: List[Hit] = []
    for idx, line in enumerate(lines):
        m = ITEM_LINE_RE.match(line.text)
        if not m:
            continue
        item = int(m.group(1))
        if item not in ITEMS_TO_DETECT:
            continue
        page_text = page_texts[line.page_idx]

        # Critical boundary fix:
        # Obligation tables can contain standalone-looking cells like "Item 11".
        # Validate this exact visual line against the expected item title nearby.
        if not line_has_real_item_heading(line, lines, idx, page_text):
            continue

        ctx = line_context(lines, idx, 10)
        ok, score, reason = is_probable_body_heading(line, ctx, page_text)
        if ok:
            hits.append(Hit(item, line.page_idx, line.page_num, line.y0, line.y1, line.x0, line.text, score, ctx))

    hits.extend(word_level_item_hits(doc, page_texts))

    deduped: Dict[Tuple[int, int, int], Hit] = {}
    for h in hits:
        key = (h.item, h.page_idx, int(round(h.y0)))
        prev = deduped.get(key)
        if prev is None or h.score > prev.score:
            deduped[key] = h

    final_hits = list(deduped.values())
    final_hits.sort(key=lambda h: (h.page_idx, h.y0, -h.score))
    return final_hits


def pos_after(a_page: int, a_y: float, b_page: int, b_y: float, min_gap: float = 1.0) -> bool:
    if a_page > b_page:
        return True
    if a_page == b_page and a_y > b_y + min_gap:
        return True
    return False


def choose_heading_sequence(hits: List[Hit]) -> Dict[int, Hit]:
    """Choose a monotonic Item 1..23 sequence from all candidate hits."""
    chosen: Dict[int, Hit] = {}

    # Start at best Item 1 candidate. Usually first real body Item 1 after FDD front matter.
    item1s = [h for h in hits if h.item == 1]
    if not item1s:
        return chosen
    # Prefer earliest high-score hit, not TOC because TOC already filtered.
    current = sorted(item1s, key=lambda h: (h.page_idx, h.y0, -h.score))[0]
    chosen[1] = current

    for n in range(2, 24):
        candidates = [h for h in hits if h.item == n and pos_after(h.page_idx, h.y0, current.page_idx, current.y0, min_gap=4)]
        if not candidates:
            continue
        # Prefer the earliest plausible candidate after prior item. Score breaks ties.
        candidates.sort(key=lambda h: (h.page_idx, h.y0, -h.score))
        chosen[n] = candidates[0]
        current = candidates[0]
    return chosen


def parse_debug_items(raw: str) -> List[int]:
    """Parse --debug-items like '10,11,12,13' into a clean item number list."""
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if 1 <= n <= 23 and n not in out:
            out.append(n)
    return out or [10, 11, 12, 13]


def format_hit(h: Hit, chosen: Dict[int, Hit]) -> str:
    mark = "CHOSEN" if chosen.get(h.item) == h else "candidate"
    return (
        f"  ITEM {h.item:>2} | {mark:<9} | page {h.page_num:>4} "
        f"| y0={h.y0:>7.1f} | y1={h.y1:>7.1f} | score={h.score:>3} "
        f"| text={h.text!r}"
    )


def debug_item_detection(hits: List[Hit], chosen: Dict[int, Hit], debug_items: List[int]) -> None:
    """Print only the item-heading candidates we care about. No output files."""
    log("\n--- SURGICAL DEBUG: candidate heading hits ---")
    for item in debug_items:
        item_hits = [h for h in hits if h.item == item]
        log(f"\nITEM {item}: {len(item_hits)} candidate hit(s)")
        if not item_hits:
            log("  NO CANDIDATES FOUND")
            continue

        item_hits.sort(key=lambda h: (h.page_idx, h.y0, -h.score))
        for h in item_hits:
            log(format_hit(h, chosen))

        ch = chosen.get(item)
        if ch is None:
            log(f"  >>> ITEM {item} WAS NOT CHOSEN")
        else:
            log(f"  >>> CHOSEN ITEM {item}: page {ch.page_num}, y0={ch.y0:.1f}, score={ch.score}, text={ch.text!r}")


def debug_boundaries(boundaries: Dict[int, Boundary], chosen: Dict[int, Hit], debug_items: List[int]) -> None:
    """Print the actual export slices. This exposes item swallowing immediately."""
    log("\n--- SURGICAL DEBUG: export boundaries ---")
    for item in debug_items:
        b = boundaries.get(item)
        ch = chosen.get(item)
        if b is None:
            log(f"ITEM {item:>2}: MISSING boundary")
            continue

        start_text = ch.text if ch else ""
        end_y = "page bottom" if b.end_y is None else f"{b.end_y:.1f}"
        log(
            f"ITEM {item:>2}: start page {b.start_page + 1}, start_y={b.start_y:.1f} "
            f"-> end page {b.end_page + 1}, end_y={end_y} | start={start_text!r}"
        )

    if 11 in boundaries:
        b11 = boundaries[11]
        b12 = boundaries.get(12)
        if b12 is None:
            log("\n>>> WARNING: Item 12 boundary is missing. Item 11 will run until the next detected item.")
        elif b11.end_page != b12.start_page or (b11.end_y is not None and abs(b11.end_y - max(0.0, b12.start_y - (BOTTOM_PAD - TOP_PAD))) > 12):
            log("\n>>> CHECK: Item 11 end boundary does not line up cleanly with Item 12 start.")
        else:
            log("\n>>> GOOD: Item 11 appears to end at Item 12's start boundary.")


def debug_raw_page_text_around_item(page_texts: List[str], chosen: Dict[int, Hit], target_item: int = 12) -> None:
    """If Item 12 is missing, show nearby raw text pages so we can see what the PDF actually says."""
    if target_item in chosen:
        return

    # Use Item 11 and Item 13 as the local search window when available.
    hit11 = chosen.get(11)
    hit13 = chosen.get(13)
    start_page = hit11.page_idx if hit11 is not None else 0
    end_page = hit13.page_idx if hit13 is not None else min(len(page_texts) - 1, start_page + 8)

    start_page = max(0, start_page)
    end_page = min(len(page_texts) - 1, max(end_page, start_page))

    log(f"\n--- SURGICAL DEBUG: raw text search window for missing ITEM {target_item} ---")
    log(f"Searching pages {start_page + 1} through {end_page + 1} for literal ITEM {target_item} text.")

    pat = re.compile(rf"(?i)\bITEM\s+{target_item}\b")
    found_any = False
    for pidx in range(start_page, end_page + 1):
        text = page_texts[pidx]
        m = pat.search(text)
        if not m:
            continue
        found_any = True
        lo = max(0, m.start() - 350)
        hi = min(len(text), m.end() + 700)
        excerpt = text[lo:hi].replace("\n", " | ")
        log(f"\nPAGE {pidx + 1}: found literal ITEM {target_item}")
        log(f"  ...{excerpt}...")

    if not found_any:
        log(f"  No literal ITEM {target_item} found in that window. The PDF may split/encode the heading strangely.")



def build_boundaries(chosen: Dict[int, Hit], doc: fitz.Document) -> Dict[int, Boundary]:
    boundaries: Dict[int, Boundary] = {}
    for item in ITEMS_TO_EXPORT:
        if item not in chosen:
            continue
        start = chosen[item]
        next_hit = None
        for nxt in range(item + 1, 24):
            if nxt in chosen:
                next_hit = chosen[nxt]
                break
        if next_hit:
            end_page = next_hit.page_idx
            end_y = max(0.0, next_hit.y0 - BOTTOM_PAD)
        else:
            end_page = doc.page_count - 1
            end_y = None
        boundaries[item] = Boundary(item, start.page_idx, max(0.0, start.y0 - TOP_PAD), end_page, end_y)
    return boundaries


def rect_full_width(page: fitz.Page, y0: float, y1: float) -> Optional[fitz.Rect]:
    r = page.rect
    y0 = max(r.y0, min(r.y1, y0))
    y1 = max(r.y0, min(r.y1, y1))
    if y1 - y0 < MIN_PAGE_SLICE_HEIGHT:
        return None
    return fitz.Rect(r.x0, y0, r.x1, y1)


def redact_rect(page: fitz.Page, rect: Optional[fitz.Rect]) -> None:
    if rect is None:
        return
    page.add_redact_annot(rect, fill=(1, 1, 1))

    # PyMuPDF exposes this method dynamically in some type stubs, so avoid
    # direct attribute access to keep Pylance quiet while preserving behavior.
    apply_redactions = getattr(page, "apply_redactions", None)
    if callable(apply_redactions):
        redact_image_none = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
        apply_redactions(images=redact_image_none)


def crop_first_page_top_whitespace(page: fitz.Page, start_y: Optional[float]) -> None:
    """Crop away the blank redacted area above an item heading on the first page.

    This is safe because export_pdf_slice() already redacts text above start_y
    before cropping, so hidden text from the previous item is not preserved.
    """
    if start_y is None:
        return
    rect = page.rect
    crop_top = max(rect.y0, min(rect.y1 - MIN_PAGE_SLICE_HEIGHT, start_y))
    if crop_top <= rect.y0 + 2:
        return
    page.set_cropbox(fitz.Rect(rect.x0, crop_top, rect.x1, rect.y1))

def export_pdf_slice(src: fitz.Document, out_path: Path, start_page: int, start_y: Optional[float], end_page: int, end_y: Optional[float], crop_top: bool = False) -> None:
    out = fitz.open()
    out.insert_pdf(src, from_page=start_page, to_page=end_page)

    # Physically remove text above start boundary on first page.
    if start_y is not None and out.page_count:
        p = out.load_page(0)
        redact_rect(p, rect_full_width(p, 0.0, start_y))
        if crop_top:
            crop_first_page_top_whitespace(p, start_y)

    # Physically remove text below end boundary on last page.
    if end_y is not None and out.page_count:
        p = out.load_page(out.page_count - 1)
        redact_rect(p, rect_full_width(p, end_y, p.rect.y1))

    # Remove fully blank pages created by aggressive same-page redaction.
    keep = fitz.open()
    for i in range(out.page_count):
        text = get_text_string(out.load_page(i), "text")
        if text:
            keep.insert_pdf(out, from_page=i, to_page=i)
    if keep.page_count == 0:
        keep.insert_pdf(out, from_page=0, to_page=0)

    keep.save(out_path, garbage=4, deflate=True, clean=True)
    keep.close()
    out.close()


def text_between(lines: List[Line], start_page: int, start_y: Optional[float], end_page: int, end_y: Optional[float]) -> str:
    out: List[str] = []
    for ln in lines:
        if ln.page_idx < start_page or ln.page_idx > end_page:
            continue
        if ln.page_idx == start_page and start_y is not None and ln.y1 < start_y:
            continue
        if ln.page_idx == end_page and end_y is not None and ln.y0 >= end_y:
            continue
        if NOISE_RE.match(ln.text):
            continue
        out.append(ln.text)
    return "\n".join(out).strip() + "\n"


def find_contract_start(page_texts: List[str], item_hits: Dict[int, Hit]) -> Optional[int]:
    # Start after Item 21/22 area when possible.
    min_page = 0
    for n in (21, 22, 23):
        if n in item_hits:
            min_page = max(min_page, item_hits[n].page_idx)
    for i in range(min_page, len(page_texts)):
        block = "\n".join(page_texts[i:i + 6])
        if CONTRACT_START_RE.search(page_texts[i]) and ARTICLE_RE.search(block) and not BAD_CONTRACT_START_RE.search(page_texts[i][:1200]):
            return i
    # Fallback: first standalone franchise agreement after FDD items.
    for i in range(min_page, len(page_texts)):
        first = page_texts[i][:1500]
        if CONTRACT_START_RE.search(first) and not BAD_CONTRACT_START_RE.search(first):
            return i
    return None


def export_frontend(src: fitz.Document, out_path: Path, first_hit: Hit) -> None:
    if first_hit.page_idx == 0:
        export_pdf_slice(src, out_path, 0, None, 0, first_hit.y0 - BOTTOM_PAD)
    else:
        export_pdf_slice(src, out_path, 0, None, first_hit.page_idx, first_hit.y0 - BOTTOM_PAD)


def validate_text(item: int, text: str) -> Tuple[str, str]:
    low = text.lower()
    anchors = VALIDATION_ANCHORS.get(item, [])
    found = [a for a in anchors if a in low]
    missing = [a for a in anchors if a not in low]

    bleed = []
    for other in range(1, 24):
        if other == item:
            continue
        if re.search(rf"(?im)^\s*ITEM\s+{other}\b", text):
            bleed.append(str(other))

    status = "OK"
    notes = []
    if anchors and not found:
        status = "CHECK"
        notes.append("missing anchors: " + "; ".join(missing[:4]))
    if bleed:
        status = "CHECK"
        notes.append("contains other item heading(s): " + ",".join(bleed))
    if len(text.strip()) < 80:
        status = "CHECK"
        notes.append("very short text")
    return status, " | ".join(notes)


def write_csv(path: Path, rows: Iterable[Dict[str, object]], fieldnames: List[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def process_pdf(pdf_path: Path, output_root: Path, make_text: bool = False, debug: bool = False, debug_items: Optional[List[int]] = None) -> Dict[str, object]:
    log(f"\nProcessing: {pdf_path.name}")
    base = safe_name(pdf_path.name)
    out_dir = output_root / base
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    lines, page_texts = get_page_lines(doc)
    hits = find_item_hits(doc, lines, page_texts)
    chosen = choose_heading_sequence(hits)

    if debug:
        items_for_debug = debug_items or [10, 11, 12, 13]
        debug_item_detection(hits, chosen, items_for_debug)
        debug_raw_page_text_around_item(page_texts, chosen, target_item=12)

    if 1 not in chosen:
        raise RuntimeError("Could not locate real ITEM 1 heading.")

    manifest_rows: List[Dict[str, object]] = []
    validation_rows: List[Dict[str, object]] = []

    # FrontEnd
    frontend_path = out_dir / f"FrontEnd - {base}.pdf"
    export_frontend(doc, frontend_path, chosen[1])
    manifest_rows.append({"section": "FrontEnd", "start_page": 1, "end_page": chosen[1].page_num, "output": frontend_path.name})

    # Items
    boundaries = build_boundaries(chosen, doc)
    if debug:
        items_for_debug = debug_items or [10, 11, 12, 13]
        debug_boundaries(boundaries, chosen, items_for_debug)

    for item in ITEMS_TO_EXPORT:
        if item not in boundaries:
            manifest_rows.append({"section": f"Item {item}", "start_page": "", "end_page": "", "output": "MISSING"})
            validation_rows.append({"section": f"Item {item}", "status": "MISSING", "notes": "No heading found"})
            continue
        b = boundaries[item]
        item_pdf = out_dir / f"Item {item} - {base}.pdf"
        export_pdf_slice(doc, item_pdf, b.start_page, b.start_y, b.end_page, b.end_y, crop_top=True)
        manifest_rows.append({
            "section": f"Item {item}",
            "start_page": b.start_page + 1,
            "end_page": b.end_page + 1,
            "output": item_pdf.name,
        })

        txt = text_between(lines, b.start_page, b.start_y, b.end_page, b.end_y)
        status, notes = validate_text(item, txt)
        validation_rows.append({"section": f"Item {item}", "status": status, "notes": notes, "chars": len(txt)})

    # Contract / Franchise Agreement
    contract_start = find_contract_start(page_texts, chosen)
    if contract_start is not None:
        contract_pdf = out_dir / f"Contract - {base}.pdf"
        export_pdf_slice(doc, contract_pdf, contract_start, None, doc.page_count - 1, None)
        manifest_rows.append({"section": "Contract", "start_page": contract_start + 1, "end_page": doc.page_count, "output": contract_pdf.name})
        validation_rows.append({"section": "Contract", "status": "OK", "notes": "Franchise Agreement found", "chars": len("\n".join(page_texts[contract_start:]))})
    else:
        manifest_rows.append({"section": "Contract", "start_page": "", "end_page": "", "output": "MISSING"})
        validation_rows.append({"section": "Contract", "status": "CHECK", "notes": "No Franchise Agreement start found", "chars": 0})

    doc.close()

    status = "OK" if all(r.get("status") in ("OK", "") for r in validation_rows if str(r.get("section", "")).startswith("Contract")) else "CHECK"
    return {"source_pdf": pdf_path.name, "status": status, "output_dir": str(out_dir), "items_found": len([i for i in ITEMS_TO_EXPORT if i in chosen])}


def iter_pdfs(input_path: Path, batch: bool) -> List[Path]:
    if batch:
        return sorted(p for p in input_path.glob("*.pdf") if p.is_file())
    return [input_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean FDD parser/splitter using line-level ITEM heading detection.")
    parser.add_argument("input", help="Input PDF file, or folder when --batch is used")
    parser.add_argument("output", help="Output folder")
    parser.add_argument("--batch", action="store_true", help="Process all PDFs in input folder")
    parser.add_argument("--no-text", action="store_true", help="Compatibility flag; text files are not written in this version")
    parser.add_argument("--debug", action="store_true", help="Print surgical heading/boundary diagnostics without creating debug files")
    parser.add_argument("--debug-items", default="10,11,12,13", help="Comma-separated item numbers to inspect with --debug, default: 10,11,12,13")
    args = parser.parse_args()

    input_arg = str(args.input)
    output_arg = str(args.output)
    batch_arg = bool(args.batch)
    no_text_arg = bool(args.no_text)
    debug_arg = bool(args.debug)
    debug_items_arg = parse_debug_items(str(args.debug_items))

    input_path = Path(input_arg).expanduser().resolve()
    output_root = Path(output_arg).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    pdfs = iter_pdfs(input_path, batch_arg)
    if not pdfs:
        raise SystemExit("No PDFs found.")

    summary: List[Dict[str, object]] = []
    for pdf in pdfs:
        try:
            result = process_pdf(pdf, output_root, make_text=False, debug=debug_arg, debug_items=debug_items_arg)
            summary.append(result)
            log(f"  Done: {result['status']} | items_found={result['items_found']} | {result['output_dir']}")
        except Exception as exc:
            log(f"  ERROR: {pdf.name}: {exc}")
            traceback.print_exc()
            summary.append({"source_pdf": pdf.name, "status": "ERROR", "output_dir": "", "items_found": 0, "error": str(exc)})

    # Summary CSV
    fieldnames = sorted({k for row in summary for k in row.keys()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
