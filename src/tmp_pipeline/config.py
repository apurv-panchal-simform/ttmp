"""Template configuration (plan sections 1.5, 6.3): typed, validated, immutable.

``templates/default.toml`` is the base; a template file overrides keys (nested tables merge, lists replace).
Every section is a frozen dataclass, so a typo in a template, a wrong type or a missing key fails at load with
the dotted path of the problem instead of a ``KeyError`` deep in a stage. Regexes are compiled once here.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
import types
import typing
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT = Path(__file__).resolve().parent / "templates" / "default.toml"

RGB = tuple[float, float, float]


class ConfigError(ValueError):
    """The default or template config is malformed; the message names the offending key."""


def _rx(flags: int = 0):
    return field(metadata={"regex_flags": flags})


@dataclass(frozen=True)
class PageCfg:
    header_band_pt: float      # text above this distance from the top edge is header band
    footer_band_pt: float      # text within this distance of the bottom edge is footer band
    background_share: float    # a filled rect covering more than this share of the page is background, not a cell


@dataclass(frozen=True)
class IntakeCfg:
    min_text_chars: int        # fewer characters than this on a page = no usable text layer
    scanned_share: float       # scanned = more than this share of pages has no text layer


@dataclass(frozen=True)
class TextCfg:
    word_gap_em: float         # gap wider than this x font size starts a new word
    segment_gap_em: float      # gap wider than this x font size splits a line into segments
    segment_gap_min_pt: float
    line_join_em: float        # vertical gap (x font size) up to which segments form one block
    line_base_em: float        # fixed part of the allowed line-to-line gap (x font size)
    line_group_em: float       # glyphs whose baselines differ by less than this x font size share a line
    block_break_em: float      # a vertical jump larger than this x font size ends a block
    column_overlap: float      # share of the narrower segment two lines must overlap to be one column
    left_align_tol_pt: float
    row_tol_pt: float          # blocks whose tops are within this are read left to right
    heading_min_delta: float   # a bold line this many pt above body size is a level 1 heading; a bold line within it is level 2
    strike_band_low: float     # a strikethrough rule sits between these fractions of the line height...
    strike_band_high: float
    strike_min_cover: float    # ...and covers at least this share of the line's width
    strike_max_overhang_pt: float  # a longer rule than the text by more than this is a border, not a strikethrough


@dataclass(frozen=True)
class QualityCfg:
    low_threshold: float       # share of dictionary words below which a text block is flagged
    min_words: int             # blocks with fewer words are too short to judge, unless unmapped glyphs show


@dataclass(frozen=True)
class TablesCfg:
    thin_pt: float             # a rect thinner than this is a ruling line
    min_rule_len_pt: float
    min_fill_pt: float
    join_tol_pt: float
    edge_tol_pt: float
    min_parts: int
    min_cells: int
    header_fills: tuple[RGB, ...]   # fill colours that mark a header row (a template cue)
    header_fill_tol: float
    rule_cover: float          # a ruling segment closes a cell edge when it covers this share of the edge
    stitch_x_tol_pt: float     # column positions of a continued table match within this


@dataclass(frozen=True)
class TicksCfg:
    ticked: tuple[str, ...]
    unticked: tuple[str, ...]
    square_min_pt: float
    square_max_pt: float
    contain_pad_pt: float      # slack when testing that a mark lies inside a box
    square_aspect_tol_pt: float  # a vector square's width and height may differ by this much


@dataclass(frozen=True)
class CheckboxCfg:
    row_tol_pt: float          # vertical slack when deciding that a label shares a row with a box
    label_gap_pt: float        # the label of a label-then-box pair ends no further than this left of the box
    note_gap_pt: float         # free text counts as the box's note when it starts within this right of the box
    yes_labels: tuple[str, ...]
    no_labels: tuple[str, ...]


@dataclass(frozen=True)
class RiskCfg:
    colour_tol: float          # a cell colour is a key colour when its RGB distance is at most this
    column_tol_pt: float       # a score cell belongs to the impact column whose label is this close (x centre)
    row_tol_pt: float          # ...and to the likelihood row whose label centre is this close (y)
    description_match: float   # minimum text similarity to treat a register consequence as a key description


@dataclass(frozen=True)
class FurnitureCfg:
    min_page_share: float      # repeated on more than this share of pages = page furniture
    min_line_share: float      # a block is furniture when at least this share of its lines repeats
    form_name_regex: re.Pattern[str] = _rx(re.I)
    tmp_ref_regex: re.Pattern[str] = _rx()
    page_number_regex: re.Pattern[str] = _rx(re.I)


@dataclass(frozen=True)
class ZonesCfg:
    diagram_image_cover: float
    diagram_min_paths: int
    rating_tol: float
    rating_min_cells: int
    risk_page_table_share: float
    rating_colours: tuple[RGB, ...]
    form_kind: dict[str, str]  # footer form name (lower case, between "TMP" and "Form") -> zone
    header_band_regex: re.Pattern[str] = _rx(re.I)
    sheet_no_regex: re.Pattern[str] = _rx(re.I)
    generic_code_regex: re.Pattern[str] = _rx()


@dataclass(frozen=True)
class FiguresCfg:
    decorative_max_area: float  # smaller than this share of the page and no text inside = decorative
    informative_min_area: float  # larger than this share = informative; in between = unclear (needs_x1)
    repeat_iou: float          # same spot on most pages (IoU above this) = decorative
    repeat_min_pages: int      # a figure cannot "repeat" in a document shorter than this
    repeat_page_share: float   # a figure repeats when it sits in the same spot on more than this share of pages


@dataclass(frozen=True)
class Config:
    page: PageCfg
    intake: IntakeCfg
    text: TextCfg
    quality: QualityCfg
    tables: TablesCfg
    ticks: TicksCfg
    checkboxes: CheckboxCfg
    risk: RiskCfg
    furniture: FurnitureCfg
    zones: ZonesCfg
    figures: FiguresCfg
    template: str = "default"
    sha: str = ""              # hash of the merged raw config, stamped on every run and record

    def config_hash(self) -> str:
        return self.sha


# --- loading ---------------------------------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _coerce(value: Any, tp: Any, path: str, flags: int = 0) -> Any:
    origin = typing.get_origin(tp)
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{path}: expected a number, got {value!r}")
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected an integer, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string, got {value!r}")
        return value
    if tp == re.Pattern[str]:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a regex string, got {value!r}")
        try:
            return re.compile(value, flags)
        except re.error as e:
            raise ConfigError(f"{path}: invalid regex {value!r}: {e}") from e
    if origin is tuple:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list, got {value!r}")
        args = typing.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_coerce(v, args[0], f"{path}[{i}]") for i, v in enumerate(value))
        if len(args) != len(value):
            raise ConfigError(f"{path}: expected {len(args)} values, got {len(value)}")
        return tuple(_coerce(v, a, f"{path}[{i}]") for i, (v, a) in enumerate(zip(value, args, strict=True)))
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a table, got {value!r}")
        kt, vt = typing.get_args(tp)
        return {_coerce(k, kt, path): _coerce(v, vt, f"{path}.{k}") for k, v in value.items()}
    if origin in (types.UnionType, typing.Union):
        raise ConfigError(f"{path}: unsupported union type")
    raise ConfigError(f"{path}: unsupported type {tp!r}")


def _build(cls: type, data: Any, path: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a table")
    hints = typing.get_type_hints(cls)
    known = {f.name: f for f in fields(cls) if f.name not in ("template", "sha")}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)}")
    missing = sorted(set(known) - set(data))
    if missing:
        raise ConfigError(f"{path}: missing key(s) {', '.join(missing)}")
    kwargs = {}
    for name, f in known.items():
        sub = f"{path}.{name}"
        tp = hints[name]
        if isinstance(tp, type) and hasattr(tp, "__dataclass_fields__"):
            kwargs[name] = _build(tp, data[name], sub)
        else:
            kwargs[name] = _coerce(data[name], tp, sub, f.metadata.get("regex_flags", 0))
    return kwargs if cls is Config else cls(**kwargs)


def _validate(c: Config) -> None:
    def check(ok: bool, msg: str) -> None:
        if not ok:
            raise ConfigError(msg)
    for name in ("min_page_share", "min_line_share"):
        check(0 < getattr(c.furniture, name) <= 1, f"furniture.{name}: must be in (0, 1]")
    check(0 < c.page.background_share <= 1, "page.background_share: must be in (0, 1]")
    check(0 <= c.quality.low_threshold <= 1, "quality.low_threshold: must be in [0, 1]")
    check(0 <= c.intake.scanned_share <= 1, "intake.scanned_share: must be in [0, 1]")
    check(c.ticks.square_min_pt < c.ticks.square_max_pt, "ticks: square_min_pt must be below square_max_pt")
    check(c.figures.decorative_max_area <= c.figures.informative_min_area,
          "figures: decorative_max_area must not exceed informative_min_area")
    for i, rgb in enumerate(c.zones.rating_colours):
        check(all(0 <= v <= 1 for v in rgb), f"zones.rating_colours[{i}]: components must be in [0, 1]")
    for kind, zone in c.zones.form_kind.items():
        check(zone in ("forms", "risk", "narrative", "diagram", "generic_standard", "blank", "unclassified"),
              f"zones.form_kind.{kind}: unknown zone {zone!r}")


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"template file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


def load_config(template: str | Path | None = None) -> Config:
    raw = _read(DEFAULT)
    name = "default"
    if template is not None:
        raw = _merge(raw, _read(Path(template)))
        name = Path(template).stem
    parts = _build(Config, raw, "config")
    cfg = Config(**parts, template=name, sha=hashlib.sha256(
        json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()[:16])
    _validate(cfg)
    return cfg
