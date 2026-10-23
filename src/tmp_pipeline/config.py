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
class DescribeCfg:
    end_tol_pt: float          # a dimension line ends at an item when its edge (areas) or centre (signs) is this close
    end_max_d_pt: float        # ...and the item is no further than this from the line, across it
    garbled: re.Pattern[str]   # a character no correctly encoded sheet text has: the font encoding garbled the line
    garbled_match_min: float   # a model reading of garbled text keeps at least this share of its plain letters, in order
    skip_place: re.Pattern[str]    # a "place name" that is a web or email address
    read_tiles: bool           # X13 also reads the plan in tiles ([diagrams] tile_cols x tile_rows) for small detail


@dataclass(frozen=True)
class DiagramsCfg:
    enabled: bool
    touch_overlap: float       # two drawn objects belong to one symbol when this share of the smaller lies inside the other
    area_min_pt2: float        # a filled shape larger than this (pt squared) is an area, never part of a sign
    cone_run_gap_pt: float     # consecutive cones closer than this form one run
    fragment_max_pt2: float    # drawn pieces smaller than this (pt squared) that touch merge into one icon
    fragment_gap_pt: float     # ...when no further apart than this
    line_turn_deg: float       # a glyph continues a text line when its direction is within this of the line's
    line_max_gap_em: float     # ...and its origin is no further than this x font size from the last glyph
    tile_cols: int             # no-vector fallback: the plan is read in this many tile columns...
    tile_rows: int             # ...and rows
    tile_overlap_pt: float     # tiles overlap by this much so nothing is cut in half
    near_dup_bits: int         # pictures whose 64-bit hashes differ by at most this many bits are the same picture
    rp_assoc_max_pt: float     # an RP label belongs to the nearest sign if that sign is within this
    plate_assoc_max_pt: float  # a TEMPORARY plate claims the nearest speed disc within this
    title_block_max_share: float   # the title block frame covers less than this share of the page
    notes_bullet: re.Pattern[str]
    legend_title: re.Pattern[str]
    notes_title: re.Pattern[str]
    callout_min_words: int     # a framed text with at least this many words is an instruction callout
    patterns: dict[str, re.Pattern[str]]       # label shapes, matched on upper-case text without spaces
    sign_text: dict[str, re.Pattern[str]]      # sign kind -> text cue, first match wins
    title_labels: dict[str, re.Pattern[str]]   # title block field -> its printed label
    area_meanings: dict[str, str]              # legend meaning (lower case) -> area kind
    describe: DescribeCfg                      # image facts for image questions (diagrams/describe.py)


@dataclass(frozen=True)
class FactsCfg:
    tmd_mention: re.Pattern[str]            # a diagram reference; group 1 is its number
    title_stop: re.Pattern[str]             # a mention's title ends where this matches...
    title_max_words: int                                # ...or after this many words
    title_stopwords: tuple[str, ...]                    # words that never decide which sheet a title names
    stage_list_context: re.Pattern[str]     # a group label or heading path that holds a stage list
    name_abbreviations: dict[str, str]                  # word -> full word, applied before names are compared
    day_types: dict[str, re.Pattern[str]]               # canonical day type -> how it is written
    pointer_targets: dict[str, str]                     # "entity.field" that says "See Above" -> where it points
    tsl_row: re.Pattern[str]                # a table row whose headers match this states a speed limit
    tsl_requested: re.Pattern[str]          # authorisation text that requests a temporary speed limit
    order_reference_label: re.Pattern[str]  # the label of the client order / TMP reference pair
    contact_labels: re.Pattern[str]         # a label/value pair whose label matches this names a contact
    heading_max_gap_pt: float                           # text this close above a sign-off table names its block
    signoff_roles: dict[str, re.Pattern[str]]           # role -> heading cue, first match wins
    signoff_labels: dict[str, re.Pattern[str]]          # field -> printed label in the sign-off grid
    checked_by_role: str                                # a "Checked by" label row starts a block for this role
    flow_statements: dict[str, re.Pattern[str]]         # traffic mode -> sentence that claims it
    diagram_modes: dict[str, re.Pattern[str]]           # traffic mode -> sheet title that shows it
    incompatible_modes: tuple[str, ...]                 # "a:b": a statement of mode a contradicts a sheet of mode b
    start_date_fields: tuple[str, ...]                  # "entity.field" start dates that must all agree (rule 7)
    end_date_fields: tuple[str, ...]                    # "entity.field" end dates that must all agree (rule 7)


@dataclass(frozen=True)
class ChunksCfg:
    target_min: int                                     # tokens: chunks aim for target_min..target_max...
    target_max: int
    hard_max: int                                       # ...never exceed this...
    merge_below: int                                    # ...and siblings smaller than this are merged
    split_overlap_sentences: int                        # sentences repeated at the start of the next part of a split
    other_overlap: float                                # share of an other_section chunk repeated in the next one
    sign_schedule_max: int                              # signs per sheet_sign_schedule chunk
    breadcrumb_sep: str
    doc_context: str                                    # one-line document context; {activity} and {site}
    site_pattern: re.Pattern[str]                       # group 1 is the worksite address, in any record's text
    activity_context: re.Pattern[str]                   # the heading path that holds the activity description
    glossary: dict[str, str]                            # abbreviation -> expansion added to the embedding text
    parties: dict[str, re.Pattern[str]]                 # metadata party -> contact role or sign-off role that names it
    procedure_operations: dict[str, re.Pattern[str]]    # operation -> heading line in a procedure cell
    procedure_removal: re.Pattern[str]                  # the line that starts the removal steps
    procedure_table: re.Pattern[str]                    # a table whose text matches this holds procedures
    boilerplate_min_pages: int                          # the same text on this many pages is boilerplate
    default_access_groups: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalCfg:
    tmp_ref: re.Pattern[str]                            # a TMP reference; groups: company prefix, year, number
    time_mention: re.Pattern[str]                       # a time or day in a question (half of the time_check rule)
    follow_up: re.Pattern[str]                          # a message that leans on the previous turn (Q1 rewrites it)
    road_suffix: re.Pattern[str]                        # a word that ends a road name ("Avenue", "Rd")
    candidates: int                                     # keyword and vector candidates per search, before fusion
    rrf_k: int                                          # reciprocal rank fusion constant
    detail_boost: float                                 # rank bonus for chunks matching a TMD, road or RP in the question
    min_score_ratio: float                              # keyword candidates scoring below this share of the best are dropped
    max_linked_sheets: int                              # diagram overviews one search may add for the sheets its hits name
    context_budget: int                                 # tokens for the answer context; chunks are cut first
    sheet_whole: re.Pattern[str]                        # a question about a named sheet that needs all of it
    sheet_topics: dict[str, re.Pattern[str]]            # sheet chunk type -> question words that need all of it
    calendar: str                                       # school-term and holiday data file in tmp_pipeline/data ("" = none)
    timezone: str
    default_role: str
    rules: dict[str, re.Pattern[str]]                   # question type -> cue, in precedence order (plan 5.4)
    day_types: dict[str, re.Pattern[str]]               # day type in a question -> how it is asked
    day_type_facts: dict[str, str]                      # day type in a question -> canonical day type in the facts
    entities: dict[str, str]                            # get_facts entity -> stored entities ("a|b"; "*" = built in code)
    entity_cues: dict[str, re.Pattern[str]]             # words in a question -> the get_facts entity it asks about
    chunk_types: dict[str, tuple[str, ...]]             # recipe -> chunk types searched (empty = all)
    k: dict[str, int]                                   # recipe -> chunks kept
    calculations: dict[str, dict[str, str]]             # calculation -> when, entity, expression, result_unit, stated
    role_styles: dict[str, str]                         # user role -> Q3 answer style
    responses: dict[str, str]                           # fixed responses (plan 5.13)


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
    diagrams: DiagramsCfg
    facts: FactsCfg
    chunks: ChunksCfg
    retrieval: RetrievalCfg
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
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true or false, got {value!r}")
        return value
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
    d = c.diagrams
    check(0 < d.touch_overlap <= 1, "diagrams.touch_overlap: must be in (0, 1]")
    check(0 < d.title_block_max_share <= 1, "diagrams.title_block_max_share: must be in (0, 1]")
    check(0 <= d.near_dup_bits < 64, "diagrams.near_dup_bits: must be in [0, 64)")
    for key in ("rp_label", "dimension_label", "road_label", "house_label", "speed_value", "controller"):
        check(key in d.patterns, f"diagrams.patterns: missing {key!r}")
    k = c.chunks
    check(0 < k.merge_below <= k.target_min <= k.target_max <= k.hard_max,
          "chunks: need 0 < merge_below <= target_min <= target_max <= hard_max")
    check(0 <= k.other_overlap < 0.5, "chunks.other_overlap: must be in [0, 0.5)")
    check(k.sign_schedule_max > 0 and k.boilerplate_min_pages > 1, "chunks: sign_schedule_max > 0, boilerplate_min_pages > 1")
    check(k.site_pattern.groups >= 1, "chunks.site_pattern: needs a group for the address")
    r = c.retrieval
    check(r.tmp_ref.groups == 3, "retrieval.tmp_ref: needs groups for prefix, year and number")
    check(r.candidates > 0 and r.rrf_k > 0 and r.context_budget > 0, "retrieval: candidates, rrf_k, context_budget > 0")
    check(0.0 <= r.min_score_ratio < 1.0, "retrieval.min_score_ratio: must be in [0, 1)")
    check(r.max_linked_sheets >= 0, "retrieval.max_linked_sheets: must be >= 0")
    check("default" in r.k and all(v > 0 for v in r.k.values()), "retrieval.k: needs a positive 'default'")
    check(r.default_role in r.role_styles, "retrieval.default_role: must be a key of retrieval.role_styles")
    check(set(r.day_type_facts) <= set(r.day_types), "retrieval.day_type_facts: keys must be retrieval.day_types keys")
    check(set(r.day_type_facts.values()) <= set(c.facts.day_types),
          "retrieval.day_type_facts: values must be facts.day_types keys")
    check(set(r.entity_cues) <= set(r.entities), "retrieval.entity_cues: keys must be retrieval.entities keys")
    for name, calc in r.calculations.items():
        missing = {"when", "entity", "expression", "result_unit"} - set(calc)
        check(not missing, f"retrieval.calculations.{name}: missing {', '.join(sorted(missing))}")
        try:
            re.compile(calc.get("when", ""))
        except re.error as e:
            raise ConfigError(f"retrieval.calculations.{name}.when: bad regex: {e}") from None
    for key in ("ask_back", "no_match", "not_in_tmp", "out_of_scope", "no_access", "no_tmp"):
        check(key in r.responses, f"retrieval.responses: missing {key!r}")
    f = c.facts
    check(f.title_max_words > 0, "facts.title_max_words: must be positive")
    check(f.heading_max_gap_pt > 0, "facts.heading_max_gap_pt: must be positive")
    check(f.tmd_mention.groups >= 1, "facts.tmd_mention: needs a group for the sheet number")
    for key in ("signature", "date", "name", "checked_by"):
        check(key in f.signoff_labels, f"facts.signoff_labels: missing {key!r}")
    for pair in f.incompatible_modes:
        a, _, b = pair.partition(":")
        check(a in f.flow_statements and b in f.diagram_modes,
              f"facts.incompatible_modes: {pair!r} must be <flow_statements key>:<diagram_modes key>")
    for src, dst in f.pointer_targets.items():
        check(src.count(".") == 1 and dst.count(".") == 1,
              f"facts.pointer_targets.{src}: both sides must be entity.field")
    for name in (*f.start_date_fields, *f.end_date_fields):
        check(name.count(".") == 1, f"facts: date field {name!r} must be entity.field")
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
