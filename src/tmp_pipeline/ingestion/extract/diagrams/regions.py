"""Regions of a diagram sheet, in page points (plan section 2.6 and 2.7): legend, notes, title block, insets, figures.

Anchors (the words "NOTES", "PREPARED FOR") come from the template config. A region is the smallest stroked or filled
box around its anchor text, so a changed layout moves the box with the text rather than failing.
"""
from __future__ import annotations

from ....config import DiagramsCfg
from ...geometry import area, centre_in, contains, union
from .models import ImageInfo, Region
from .overlay import OverlayObject
from .textlines import TextLine


def _boxes_around(point_box: list[float], paths: list[OverlayObject], min_width: float = 0.0) -> list[OverlayObject]:
    return [p for p in paths if p.kind == "path" and centre_in(point_box, p.bbox) and p.width >= min_width and p.height > 3]


def smallest_box_around(line: TextLine, paths: list[OverlayObject]) -> list[float] | None:
    boxes = _boxes_around(line.bbox, paths, line.bbox[2] - line.bbox[0])
    boxes = [p for p in boxes if p.height >= (line.bbox[3] - line.bbox[1]) * 1.5]
    return min(boxes, key=lambda p: area(p.bbox)).bbox if boxes else None


def _horizontal(ln: TextLine) -> bool:
    return min(ln.angle % 360.0, 360.0 - ln.angle % 360.0) <= 5.0


def find_notes(lines: list[TextLine], paths: list[OverlayObject], cfg: DiagramsCfg) -> tuple[list[float] | None, list[str]]:
    title = next((ln for ln in lines if cfg.notes_title.match(ln.text)), None)
    if title is None:
        return None, []
    box = smallest_box_around(title, paths) or [title.bbox[0], title.bbox[1] - 150, title.bbox[2] + 200, title.bbox[3]]
    inside = sorted((ln for ln in lines if ln is not title and centre_in(ln.bbox, box) and _horizontal(ln)),
                    key=lambda ln: (-ln.centre[1], ln.bbox[0]))
    notes: list[str] = []
    for ln in inside:
        text = ln.text.strip()
        if cfg.notes_bullet.match(text):
            notes.append(cfg.notes_bullet.sub("", text, count=1).strip())
        elif notes:                           # a wrapped bullet continues the one before
            notes[-1] += " " + text
        else:
            notes.append(text)
    return box, notes


def find_title_block(lines: list[TextLine], paths: list[OverlayObject], page_size: tuple[float, float],
                     cfg: DiagramsCfg) -> list[float] | None:
    anchor = next((ln for ln in lines if cfg.title_labels["prepared_for"].search(ln.key)), None)
    if anchor is None:
        return None
    limit = cfg.title_block_max_share * page_size[0] * page_size[1]
    frames = [p for p in _boxes_around(anchor.bbox, paths) if p.width >= 0.5 * page_size[0] and area(p.bbox) <= limit]
    return max(frames, key=lambda p: area(p.bbox)).bbox if frames else None


def read_title_block(box: list[float], lines: list[TextLine], paths: list[OverlayObject],
                     cfg: DiagramsCfg) -> dict[str, object]:
    """Printed label -> value, by cell where a label has its cell to itself and by the nearest line below otherwise."""
    inside = [ln for ln in lines if centre_in(ln.bbox, box)]
    labelled: dict[str, TextLine] = {}
    for ln in inside:
        for field_name, rx in cfg.title_labels.items():
            if rx.search(ln.key) and field_name not in labelled:
                labelled[field_name] = ln
    label_ids = {id(ln) for ln in labelled.values()}
    cells = [p.bbox for p in paths if p.kind == "path" and contains(box, p.bbox, 0.5) and area(p.bbox) < area(box) * 0.5
             and p.height > 20]
    out: dict[str, object] = {}
    for name, label in labelled.items():
        cell = min((c for c in cells if centre_in(label.bbox, c)), key=area, default=None)
        sharing = [o for o in labelled.values() if cell is not None and centre_in(o.bbox, cell)]
        if cell is not None and len(sharing) == 1:
            vals = sorted((ln for ln in inside if id(ln) not in label_ids and centre_in(ln.bbox, cell)
                           and ln.centre[1] < label.bbox[1]), key=lambda ln: -ln.centre[1])
        else:
            below = [ln for ln in inside if id(ln) not in label_ids and ln.centre[1] < label.bbox[1]
                     and min(ln.bbox[2], label.bbox[2]) - max(ln.bbox[0], label.bbox[0]) > 0
                     and (cell is None or centre_in(ln.bbox, cell))]
            vals = sorted(below, key=lambda ln: -ln.centre[1])[:1]
        if vals:
            out[name] = [v.text.strip() for v in vals] if name == "project" else vals[0].text.strip()
    return out


def find_images(overlay_images: list[OverlayObject], page_size: tuple[float, float], title_block: list[float] | None,
                paths: list[OverlayObject], cfg: DiagramsCfg) -> tuple[list[ImageInfo], list[Region]]:
    """Classify each placed picture: the aerial under the drawing, insets, figures and repeated banners and logos."""
    page_area = page_size[0] * page_size[1]
    infos: list[ImageInfo] = []
    regions: list[Region] = []
    for im in overlay_images:
        ratio = area(im.bbox) / page_area
        if title_block is not None and centre_in(im.bbox, title_block):
            role = "decorative"
        elif ratio >= 0.5:
            role = "aerial"
        elif im.level > 0:
            role = "inset"
        else:
            role = "figure"
        infos.append(ImageInfo(im.index, im.bbox, im.px, im.phash, role, im.level, []))      # reuse is linked later (lineage)
        if role == "inset":
            frame = [p.bbox for p in paths if p.kind == "path" and p.stroke is not None and contains(p.bbox, im.bbox, 3.0)
                     and area(p.bbox) < 3 * area(im.bbox)]
            regions.append(Region("inset", min(frame, key=area) if frame else im.bbox))
        elif role == "figure":
            regions.append(Region("figure", im.bbox))
    return infos, regions


def grow_figure_over_title(region: Region, lines: list[TextLine], paths: list[OverlayObject], reach_pt: float = 30.0) -> Region:
    """A figure's drawn title sits just above the picture: the crop grows upward to include it (plan section 2.7)."""
    top = region.bbox[3]
    titles = [ln for ln in lines if region.bbox[0] - 5 <= ln.centre[0] <= region.bbox[2] + 5
              and top - 2 <= ln.bbox[1] <= top + reach_pt]
    if not titles:
        return region
    box = union([region.bbox, *(ln.bbox for ln in titles)])
    for p in paths:
        if p.kind == "path" and any(centre_in(ln.bbox, p.bbox) for ln in titles) and area(p.bbox) < 8000:
            box = union([box, p.bbox])
    text = " ".join(ln.text.strip() for ln in sorted(titles, key=lambda ln: -ln.centre[1]))
    return Region(region.kind, box, text, region.element_id, text)

