"""Schema catalog loader (plan section 3.1): entities, fields, section types, synonyms.

Names are compared after normalisation (lower case, punctuation collapsed to single spaces), so "Road name(s)",
"road name s" and "ROAD NAME(S)" are the same name. The loaded catalog is immutable and has a version that is
stamped on every record that used it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..errors import PipelineError

DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "templates" / "catalog.yaml"
FIELD_TYPES = {"text", "integer", "number", "percent", "date", "time", "rp", "speed", "distance", "duration", "bool",
               "list"}


class CatalogError(PipelineError):
    """The catalog file is malformed."""


def normalise(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%&]+", " ", name.lower()).split())


@dataclass(frozen=True)
class FieldDef:
    name: str
    owner: str                      # entity or section name
    type: str
    desc: str = ""
    unit: str | None = None
    critical: bool = False
    synonyms: tuple[str, ...] = ()
    pattern_kind: str | None = None  # numerics kind that can state this field directly (phone, duration, distance, ...)
    pattern_unit: str | None = None  # and the unit that kind must have


@dataclass(frozen=True)
class EntityDef:
    name: str
    description: str
    match_key: tuple[str, ...]
    fields: dict[str, FieldDef]
    one_per_document: bool = False   # every record of this entity describes the same thing (merged into one fact)


@dataclass(frozen=True)
class SignoffReq:
    role: str
    condition: str | None = None    # the requirement applies only when this document condition holds (tsl_requested)


@dataclass(frozen=True)
class SectionDef:
    name: str
    heading_synonyms: tuple[str, ...]
    fields: dict[str, FieldDef] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityMatch:
    entity: str
    mapped: dict[str, str]          # original name -> field name
    unmapped: list[str]
    score: float                    # share of the given names this entity explains


class Catalog:
    def __init__(self, version: str, entities: dict[str, EntityDef], sections: dict[str, SectionDef],
                 required_signoffs: tuple[SignoffReq, ...] = (), facts_entities: dict[str, EntityDef] | None = None,
                 record_kinds: dict[str, str] | None = None,
                 diagram_title_synonyms: dict[str, tuple[str, ...]] | None = None):
        self.version = version
        self.entities = entities            # what handlers map names to (synonyms drive code, descriptions prompts)
        self.sections = sections
        self.required_signoffs = required_signoffs
        # Phase 4: entities built by the field mapper only. They never take part in name matching or prompts,
        # so adding one cannot change what a handler reads.
        self.facts_entities = facts_entities or {}
        self.record_kinds = record_kinds or {}          # record kind -> fact entity, copied one to one
        self.diagram_title_synonyms = diagram_title_synonyms or {}   # canonical title -> other names for it
        self._field_index: dict[str, dict[str, str]] = {}
        for ename, ent in entities.items():
            idx: dict[str, str] = {}
            for fname, fd in ent.fields.items():
                for syn in (fname, *fd.synonyms):
                    key = normalise(syn)
                    if idx.setdefault(key, fname) != fname:
                        raise CatalogError(f"synonym {syn!r} of {ename}.{fname} already names {ename}.{idx[key]}")
            self._field_index[ename] = idx
        self._heading_index: dict[str, str] = {}
        for sname, sec in sections.items():
            for syn in sec.heading_synonyms:
                key = normalise(syn)
                if self._heading_index.setdefault(key, sname) != sname:
                    raise CatalogError(f"heading {syn!r} of section {sname} already names {self._heading_index[key]}")

    def field_for(self, entity: str, name: str) -> FieldDef | None:
        fname = self._field_index.get(entity, {}).get(normalise(name))
        return self.entities[entity].fields[fname] if fname else None

    def match_entity(self, names: list[str], candidates: tuple[str, ...] | None = None) -> EntityMatch | None:
        """The entity whose synonyms explain the most of ``names`` (None when nothing matches)."""
        best: EntityMatch | None = None
        for ename in candidates or tuple(self.entities):
            mapped: dict[str, str] = {}
            for n in names:
                f = self._field_index[ename].get(normalise(n))
                if f:
                    mapped[n] = f
            if not mapped:
                continue
            score = len(mapped) / len(names) if names else 0.0
            if best is None or (score, len(mapped)) > (best.score, len(best.mapped)):
                best = EntityMatch(ename, mapped, [n for n in names if n not in mapped], score)
        return best

    def section_type(self, heading: str) -> str | None:
        return self._heading_index.get(normalise(heading))

    def section_fields(self, section_type: str) -> dict[str, FieldDef]:
        sec = self.sections.get(section_type)
        return sec.fields if sec else {}

    def is_critical(self, owner: str, fname: str) -> bool:
        fd = (self.entities[owner].fields if owner in self.entities else self.section_fields(owner)).get(fname)
        return bool(fd and fd.critical)


def _fields(owner: str, raw: dict | None) -> dict[str, FieldDef]:
    out: dict[str, FieldDef] = {}
    for fname, spec in (raw or {}).items():
        if not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES:
            raise CatalogError(f"{owner}.{fname}: type must be one of {sorted(FIELD_TYPES)}")
        extra = set(spec) - {"type", "desc", "unit", "critical", "synonyms", "pattern"}
        if extra:
            raise CatalogError(f"{owner}.{fname}: unknown key(s) {sorted(extra)}")
        pattern = spec.get("pattern") or {}
        out[fname] = FieldDef(fname, owner, spec["type"], spec.get("desc", ""), spec.get("unit"),
                              bool(spec.get("critical", False)), tuple(spec.get("synonyms", ())),
                              pattern.get("kind"), pattern.get("unit"))
    return out


def _entities(raw: dict | None) -> dict[str, EntityDef]:
    out: dict[str, EntityDef] = {}
    for ename, ent in (raw or {}).items():
        extra = set(ent) - {"description", "match_key", "fields", "one_per_document"}
        if extra:
            raise CatalogError(f"{ename}: unknown key(s) {sorted(extra)}")
        fields_ = _fields(ename, ent.get("fields"))
        key = tuple(ent.get("match_key", ()))
        if any(k not in fields_ for k in key):
            raise CatalogError(f"{ename}.match_key: {list(key)} names a field the entity does not have")
        out[ename] = EntityDef(ename, ent.get("description", ""), key, fields_, bool(ent.get("one_per_document")))
    return out


def _signoffs(raw: list | None) -> tuple[SignoffReq, ...]:
    out = []
    for item in raw or ():
        if isinstance(item, str):
            out.append(SignoffReq(item))
        elif isinstance(item, dict) and len(item) == 1:
            (role, spec), = item.items()
            if not isinstance(spec, dict) or set(spec) - {"if"}:
                raise CatalogError(f"required_signoffs.{role}: expected {{if: <condition>}}")
            out.append(SignoffReq(role, spec.get("if")))
        else:
            raise CatalogError(f"required_signoffs: cannot read {item!r}")
    return tuple(out)


def load_catalog(path: str | Path | None = None) -> Catalog:
    p = Path(path) if path else DEFAULT_CATALOG
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"catalog file not found: {p}") from None
    except yaml.YAMLError as e:
        raise CatalogError(f"{p}: invalid YAML: {e}") from e
    if not isinstance(raw, dict) or "version" not in raw:
        raise CatalogError(f"{p}: expected a mapping with a version")
    entities = _entities(raw.get("entities"))
    facts_entities = _entities(raw.get("facts_entities"))
    clash = sorted(set(entities) & set(facts_entities))
    if clash:
        raise CatalogError(f"entities and facts_entities both define {clash}")
    sections: dict[str, SectionDef] = {}
    for sname, s in (raw.get("sections") or {}).items():
        raw_fields = s.get("fields") or []
        if isinstance(raw_fields, list):          # a plain list of names carries no type information
            raw_fields = {f: {"type": "text"} for f in raw_fields}
        sections[sname] = SectionDef(sname, tuple(s.get("heading_synonyms", ())), _fields(sname, raw_fields))
    synonyms = {str(k): tuple(v or ()) for k, v in (raw.get("diagram_title_synonyms") or {}).items()}
    return Catalog(str(raw["version"]), entities, sections, _signoffs(raw.get("required_signoffs")),
                   facts_entities, dict(raw.get("record_kinds") or {}), synonyms)
