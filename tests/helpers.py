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
