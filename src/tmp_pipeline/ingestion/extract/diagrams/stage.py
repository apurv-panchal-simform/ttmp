"""The pre-router diagram stage (plan section 2.6): read every diagram sheet and add its elements to the store.

Runs after zones and furniture and before the router, because it owns element creation (handlers never add elements).
It adds ``vector_symbol``, ``legend``, ``notes`` and ``region`` elements under the sheet, retypes the drawing frame the
Phase 1 table finder saw on each sheet to a ``region``, and returns the ``SheetStore`` the diagram handler reads.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from .... import logging_ as lg
from ....config import Config
from ....constants import MASTER_DPI
from ...geometry import area, centre_in
from ...models import Element, ElementStore
from ...vocab import ElementType, Flag, SymbolKind
from . import regions as rg
from .assoc import link_all
from .legend import read_legend
from .lineage import link_reuse
from .models import ImageInfo, Region, SheetModel, SheetStore, Symbol
from .overlay import OverlayPage, read_overlays
from .render import save_overlay
from .symbols import read_symbols
from .textlines import build_lines

_FRAME_SHARE = 0.6                  # a "table" this large is the drawing frame, not a table


def _coloured(s: Symbol) -> bool:
    return any(c != (0, 0, 0) for c in s.colours)


def _flat_title_block(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in raw.items():
        out[k] = " | ".join(v) if isinstance(v, list) else str(v)
    return out


def _read_sheet(ov: OverlayPage, cfg: Config) -> tuple[SheetModel, list[Region]]:
    d = cfg.diagrams
    size = (ov.width, ov.height)
    paths = [o for o in ov.objects if o.kind == "path"]
    lines = build_lines(ov.objects, d, cfg.quality.low_threshold, cfg.quality.min_words)
    legend, legend_box, _ = read_legend(ov.objects, lines, d)
    notes_box, notes = rg.find_notes(lines, paths, d)
    tb_box = rg.find_title_block(lines, paths, size, d)
    tb_raw = rg.read_title_block(tb_box, lines, paths, d) if tb_box else {}
    images, image_regions = rg.find_images(ov.of_kind("image"), size, tb_box, paths, d)
    figures = [rg.grow_figure_over_title(r, lines, paths) if r.kind == "figure" else r for r in image_regions]
    regions: list[Region] = [r for r in figures]
    if legend_box:
        regions.append(Region("legend", legend_box))
    if notes_box:
        regions.append(Region("notes", notes_box, text="\n".join(notes)))
    if tb_box:
        regions.append(Region("title_block", tb_box, text="\n".join(f"{k}: {v}" for k, v in _flat_title_block(tb_raw).items())))
    excluded = [r.bbox for r in regions if r.kind in ("legend", "notes", "title_block", "figure")]
    symbols = read_symbols(ov.objects, lines, legend, excluded, size, d)
    dropped = [s for s in symbols if s.kind == SymbolKind.LINE and s.meaning is None and not _coloured(s)]
    symbols = [s for s in symbols if s not in dropped]
    insets = [r.bbox for r in regions if r.kind == "inset"]
    for s in symbols:
        if any(centre_in(s.bbox, b) for b in insets):
            s.region = "inset"
    sheet = SheetModel(page=ov.page, vector=len(paths) >= cfg.zones.diagram_min_paths, legend=legend, notes=notes,
                       regions=regions, images=images, paths=len(paths))
    sheet.symbols = symbols
    sheet.callouts = [s.text for s in symbols if s.kind == SymbolKind.CALLOUT and not s.low_text_quality]
    sheet.low_quality_lines = [ln.text.strip() for ln in lines if ln.low_quality]
    sheet.title_block = _flat_title_block(tb_raw)
    project = tb_raw.get("project")
    no, rev = tb_raw.get("sheet"), tb_raw.get("rev")
    sheet.sheet = no if isinstance(no, str) else None
    sheet.rev = rev if isinstance(rev, str) else None
    if isinstance(project, list) and project:
        sheet.title = project[-1]
    for i, sym in enumerate(sheet.symbols):
        sym.id = f"tmp{i}"                            # replaced by the element id once the element exists
    sheet.roads = link_all(sheet.symbols, d)
    sheet.dropped_lines = len(dropped)
    return sheet, regions


def _element_for(store: ElementStore, page: int, size, etype: str, bbox, parent: str | None, text: str = "",
                 metadata: dict | None = None, flags: tuple[Flag, ...] = ()) -> Element:
    el = store.add(page, size, "diagram", etype, bbox, parent_id=parent, source="diagrams",
                   representations={"text": text} if text else {}, metadata=metadata or {})
    if flags:
        el.flags = sorted({*el.flags, *flags})
    return el


def _retype_frame(store: ElementStore, page: int, sheet_el: Element) -> None:
    page_area = store.pages[page].width_pt * store.pages[page].height_pt
    for t in store.of_type(ElementType.TABLE, page=page):
        if area(t.bbox_pt) >= _FRAME_SHARE * page_area:
            for child in [e for e in store.on_page(page) if e.parent_id == t.element_id]:
                child.parent_id = sheet_el.element_id
            store.retype(t, ElementType.REGION)
            t.metadata.update(kind="frame")
            t.parent_id = sheet_el.element_id


def tag_picture_hashes(store: ElementStore, pdf_path: str | Path, cfg: Config) -> int:
    """Give every informative-sized picture outside the diagram sheets a hash of its source pixels.

    A hash of a crop of the page render would match any two pictures on mostly white ground; the hash of the placed
    picture's own pixels does not, so the figure handler can reuse a caption only for the same picture.
    """
    pages: dict[int, list[Element]] = {}
    for im in store.of_type(ElementType.IMAGE):
        if store.pages[im.page].zone != "diagram" and im.metadata.get("area_share", 0) >= cfg.figures.decorative_max_area:
            pages.setdefault(im.page, []).append(im)
    if not pages:
        return 0
    tagged = 0
    for page, ov in read_overlays(pdf_path, sorted(pages)).items():
        for el in pages[page]:
            for obj in ov.of_kind("image"):
                if obj.phash and all(abs(a - b) < 2.0 for a, b in zip(el.bbox_pt, obj.bbox, strict=True)):
                    el.metadata["phash"] = obj.phash
                    tagged += 1
                    break
    return tagged


def read_diagrams(store: ElementStore, pdf_path: str | Path, cfg: Config, out_dir: str | Path | None = None,
                  render: bool = True) -> SheetStore:
    sheets = SheetStore()
    if not cfg.diagrams.enabled:
        return sheets
    tag_picture_hashes(store, pdf_path, cfg)
    sheet_els = {e.page: e for e in store.of_type(ElementType.DIAGRAM_SHEET)}
    if not sheet_els:
        return sheets
    t0 = time.perf_counter()
    overlays = read_overlays(pdf_path, sorted(sheet_els))
    for page in sorted(sheet_els):
        sheet, _ = _read_sheet(overlays[page], cfg)
        sheets.add(sheet)
    link_reuse({p: s.images for p, s in sheets.sheets.items()}, cfg.diagrams.near_dup_bits)
    for page, sheet in sheets.sheets.items():
        size = (store.pages[page].width_pt, store.pages[page].height_pt)
        sheet_el = sheet_els[page]
        sheet.sheet_element_id = sheet_el.element_id
        _retype_frame(store, page, sheet_el)
        sid = sheet_el.element_id
        sheet_el.metadata.update(sheet=sheet.sheet, title=sheet.title, rev=sheet.rev, vector=sheet.vector,
                                 symbols=len(sheet.symbols))
        for r in sheet.regions:
            el = _element_for(store, page, size, "region", r.bbox, sid, r.text, {"kind": r.kind, "label": r.label})
            r.element_id = el.element_id
        for im in sheet.images:
            _tag_image(store, page, im)
        if sheet.legend:
            box = next((r.bbox for r in sheet.regions if r.kind == "legend"), sheet.legend[0].bbox)
            _element_for(store, page, size, "legend", box, sid, "\n".join(f"{e.swatch}: {e.meaning}" for e in sheet.legend),
                         {"entries": [asdict(e) for e in sheet.legend]})
        if sheet.notes:
            box = next((r.bbox for r in sheet.regions if r.kind == "notes"), [0, 0, 0, 0])
            _element_for(store, page, size, "notes", box, sid, "\n".join(sheet.notes), {"notes": sheet.notes})
        mapping: dict[str, str] = {}
        for s in sheet.symbols:
            flags = tuple(f for f, on in ((Flag.NEEDS_CAPTION, s.needs_caption), (Flag.LOW_TEXT_QUALITY, s.low_text_quality)) if on)
            el = _element_for(store, page, size, "vector_symbol", s.bbox, sid, s.text,
                              {"kind": str(s.kind), "value": s.value, "unit": s.unit, "rp": s.rp, "road": s.road,
                               "region": s.region}, flags)
            mapping[s.id] = el.element_id
        _remap_ids(sheet, mapping)
        if render and out_dir is not None:
            path, _ = save_overlay(pdf_path, page, Path(out_dir) / "master")
            sheet.overlay = f"master/{path.name}"
            sheet.master = f"master/page{page:02d}_master.webp"
        lg.log_event("diagrams", "sheet", page=page, sheet=sheet.sheet, title=sheet.title, rev=sheet.rev,
                     symbols=len(sheet.symbols), legend=len(sheet.legend), notes=len(sheet.notes),
                     needs_caption=sum(s.needs_caption for s in sheet.symbols), vector=sheet.vector)
    lg.log_event("diagrams", "done", sheets=len(sheets), seconds=round(time.perf_counter() - t0, 2), dpi=MASTER_DPI)
    return sheets


def _tag_image(store: ElementStore, page: int, im: ImageInfo) -> None:
    """Link a placed picture to the Phase 1 image element it is (same box), so reuse and role travel with it."""
    for e in store.of_type(ElementType.IMAGE, page=page):
        if all(abs(a - b) < 2.0 for a, b in zip(e.bbox_pt, im.bbox, strict=True)):
            e.metadata.update(role=im.role, phash=im.phash, reused_on=im.reused_on)
            if im.reused_on and Flag.DUPLICATE not in e.flags and im.role not in ("aerial",):
                e.flags = sorted({*e.flags, Flag.DUPLICATE})
            im.element_id = e.element_id
            return


def _remap_ids(sheet: SheetModel, mapping: dict[str, str]) -> None:
    """Symbols point at each other by id (RP labels, plates); replace the provisional ids with the element ids."""
    for s in sheet.symbols:
        s.id = mapping.get(s.id, s.id)
        if s.rp_id:
            s.rp_id = mapping.get(s.rp_id, s.rp_id)
        if "plate" in s.extra:
            s.extra["plate"] = mapping.get(s.extra["plate"], s.extra["plate"])
