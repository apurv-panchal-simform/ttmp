"""Shared test helpers (plain functions; fixtures live in conftest.py)."""
from tmp_pipeline.config import load_config
from tmp_pipeline.furniture import apply_furniture
from tmp_pipeline.layers.reader import read_layers
from tmp_pipeline.layers.to_elements import build_elements
from tmp_pipeline.zones import assign_zones

CFG = load_config()


def grid(c, x, y, cols, rows, cw=60, rh=20):
    """Draw a ruled grid of cols x rows cells with its lower-left corner at (x, y)."""
    c.setLineWidth(0.5)
    for i in range(rows + 1):
        c.line(x, y + i * rh, x + cols * cw, y + i * rh)
    for j in range(cols + 1):
        c.line(x + j * cw, y, x + j * cw, y + rows * rh)


def header_footer(c, w, h, form="1. TMP Planning Form", page=1):
    c.setFont("Helvetica", 8)
    c.drawString(60, h - 30, "TMP Reference KTS 26-1581")
    c.drawString(60, 25, f"New Zealand Guide to Temporary Traffic Management {form} Page {page}")


def prepare(pdf):
    """Run the Phase 1 stages that precede the router on a PDF and return the element store."""
    s = build_elements(read_layers(pdf), "sha256:t", CFG)
    assign_zones(s, CFG)
    apply_furniture(s, CFG)
    return s


# --- hand-built element stores (handler tests without PDFs or fonts) -------------------------------------------

def line(text, x0, y0, x1, y1, size=9.0, bold=False):
    return {"text": text, "size": size, "bold": bold, "bbox": [x0, y0, x1, y1], "struck": False, "words": []}


def add_block(store, lines, page=1, parent=None, zone="forms", **meta):
    """A text_block element whose lines are given; its box is their union, its text their joined text."""
    box = [min(ln["bbox"][0] for ln in lines), min(ln["bbox"][1] for ln in lines),
           max(ln["bbox"][2] for ln in lines), max(ln["bbox"][3] for ln in lines)]
    md = {"rotation": 0, "band": "body", "size": lines[0]["size"], "bold": lines[0]["bold"], **meta}
    return store.add(page, (842, 595), zone, "text_block", box, parent_id=parent, source="test",
                     representations={"text": "\n".join(ln["text"] for ln in lines), "lines": lines}, metadata=md)


def add_box(store, x0, y0, ticked, page=1, size=12, parent=None, zone="forms"):
    return store.add(page, (842, 595), zone, "checkbox", [x0, y0, x0 + size, y0 + size], parent_id=parent,
                     source="test", metadata={"state": "ticked" if ticked else "unticked", "method": "glyph"})


def store_with_page(page=1):
    from tmp_pipeline.models import ElementStore, PageInfo
    s = ElementStore("sha256:test")
    s.pages[page] = PageInfo(width_pt=842, height_pt=595)
    return s


def run_handler(store, handler, route_name):
    """Route a store with only ``handler`` real (stubs elsewhere) and return (records, router result)."""
    from tmp_pipeline.handlers.stubs import stub_registry
    from tmp_pipeline.records import RecordStore
    from tmp_pipeline.router import route
    reg = stub_registry()
    reg.replace(handler)
    records = RecordStore(store.doc_id)
    result = route(store, CFG, registry=reg, records=records)
    return records, result


def add_table(store, x0, y1, col_w, row_h, rows, *, page=1, zone="risk", header_rows=0, header_fill=(0.0, 0.31, 0.41),
              fills=None, no_rule_below=()):
    """A ruled table element plus one text block per non-empty cell. ``rows`` is a list of lists of str|None.
    ``no_rule_below`` lists (row, col) cells whose bottom rule is left out, so they merge with the cell below."""
    nr, nc = len(rows), len(col_w)
    xs = [x0]
    for w in col_w:
        xs.append(xs[-1] + w)
    ys = [y1 - i * row_h for i in range(nr + 1)]
    h_rules = []
    for i, y in enumerate(ys):
        for j in range(nc):
            if (i - 1, j) not in no_rule_below:
                h_rules.append([y, xs[j], xs[j + 1]])
    v_rules = [[x, ys[-1], ys[0]] for x in xs]
    fl = list(fills or [])
    for r in range(header_rows):
        for c in range(nc):
            fl.append({"bbox": [xs[c], ys[r + 1], xs[c + 1], ys[r]], "rgb": list(header_fill)})
    table = store.add(page, (842, 595), zone, "table", [xs[0], ys[-1], xs[-1], ys[0]], source="test",
                      metadata={"h_rules": h_rules, "v_rules": v_rules, "fills": fl, "n_xs": nc + 1, "n_ys": nr + 1,
                                "n_parts": 4, "n_fills": len(fl), "rating_cells": 0})
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if text:
                parts = text.split("\n")
                lns = [line(p, xs[c] + 3, ys[r] - 10 - 9 * k, xs[c + 1] - 3, ys[r] - 2 - 9 * k) for k, p in enumerate(parts)]
                add_block(store, lns, page=page, parent=table.element_id, zone=zone)
    return table, xs, ys


# --- synthetic diagram sheets ---------------------------------------------------------------------------------------

def noise_image(seed: int, size=(160, 100)):
    """A random picture: different seeds give far-apart perceptual hashes, the same seed the same hash."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype="uint8"))


def draw_text_along(c, x, y, text, angle_deg, size=7.0):
    """Draw ``text`` one glyph per text object along a baseline at ``angle_deg``, as the sample's sheets do."""
    import math
    ux, uy = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    c.setFont("Helvetica-Bold", size)
    for i, ch in enumerate(text):
        c.saveState()
        c.translate(x + ux * i * size * 0.62, y + uy * i * size * 0.62)
        c.rotate(angle_deg)
        c.drawString(0, 0, ch)
        c.restoreState()


def sheet_page(c, w, h, *, sheet="04", title="STOP GO", aerial_seed=1, extra=None, legend=True, notes=True, filler=20):
    """A one-page diagram sheet: aerial photo, legend, notes, a title block and whatever ``extra(c, w, h)`` draws."""
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(noise_image(aerial_seed)), 14, 96, w - 28, h - 110)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    for i in range(filler):                                  # a vector overlay: enough paths for the diagram zone rule
        c.line(100 + i * 10, 300, 105 + i * 10, 330)
    c.setFillColorRGB(1, 1, 1)
    c.rect(14, 10, w - 28, 87, stroke=1, fill=1)             # title block frame
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 6)
    c.drawString(20, 85, "PREPARED FOR CLIENT/CONTRACTOR:")
    c.line(700, 55, w - 20, 55)
    c.rect(700, 14, w - 714, 80, stroke=1, fill=0)
    c.drawString(705, 85, "TMP NUMBER:")
    c.drawString(705, 45, "SHEET NO:")
    c.drawString(765, 45, "REV NO:")
    c.drawString(708, 70, "KTS 26-1581")
    c.drawString(715, 28, sheet)
    c.drawString(775, 28, "00")
    c.rect(400, 14, 290, 80, stroke=1, fill=0)
    c.drawString(405, 85, "PROJECT DESCRIPTION:")
    c.drawString(450, 68, "WATER MAINTENANCE WORK")
    c.drawString(450, 50, "55 SUNRISE AVENUE, MURRAYS BAY")
    c.drawString(500, 32, title)
    if legend:
        c.setFillColorRGB(1, 1, 1)
        c.rect(14, h - 130, 100, 112, stroke=1, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(48, h - 30, "Legend")
        for k, (rgb, label) in enumerate([((0, 1, 0), "Work Area"), ((1, 1, 0), "Safety Zone")]):
            y = h - 50 - k * 14
            c.setFillColorRGB(*rgb)
            c.rect(18, y, 10, 10, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica", 7)
            c.drawString(32, y + 2, label)
    if notes:
        c.setFillColorRGB(0.86, 0.86, 0.86)
        c.rect(560, 102, 250, 100, stroke=1, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 8)
        c.drawString(566, 190, "NOTES")
        c.drawString(566, 172, "• 1M minimum lateral safety zone")
        c.drawString(566, 160, "• 10M Longitudinal safety zone and 30M taper length")
    if extra:
        extra(c, w, h)


def speed_disc(c, x, y, value, r=12):
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(0.75, 0.07, 0.12)
    c.setLineWidth(4)
    c.circle(x, y, r, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(x, y - 3, str(value))


def plate(c, x, y, text, rgb=(0.94, 0.49, 0.13), w=26, h=24):
    c.setFillColorRGB(*rgb)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(1)
    c.rect(x, y, w, h, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 4.5)
    c.drawCentredString(x + w / 2, y + h / 2 - 2, text)


def label(c, x, y, text, w=30, h=9):
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(1, 1, 1)
    c.rect(x, y, w, h, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 6)
    c.drawString(x + 2, y + 2.5, text)
