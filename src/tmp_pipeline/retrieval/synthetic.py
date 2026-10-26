"""Synthetic test documents made from one real run folder, for testing what needs several TMPs (the agent,
``find_tmps``, ``compare_versions``) while only one real TMP exists.

    tmp-pipeline-synthetic out/sha256-... out_synth/

writes three run folders under ``out_synth/``: the original (V1), a V2 of the same TMP with a few values changed,
and a different TMP on another road. The copies are made by text replacement in the run folder's files, so they
test retrieval and the agent, not extraction. Each copy has a ``SYNTHETIC`` file saying what was changed; keep
them out of ``out/`` so they never mix with real documents.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

from ..ingestion.facts.model import Facts
from .store import LocalStore, User

TEXT_FILES = ("facts.json", "chunks.jsonl", "elements.json", "records.json", "sheets.json", "review.json",
              "lineage.json", "report.md")


def _new_id(doc_id: str, name: str) -> str:
    return "sha256:" + hashlib.sha256(f"{doc_id}|{name}".encode()).hexdigest()


def make_variant(doc_dir: str | Path, out_root: str | Path, name: str, replace: dict[str, str] | None = None,
                 edits: dict[str, tuple[object, str]] | None = None, version: int | None = None) -> Path:
    """A copy of ``doc_dir`` under ``out_root`` with a new doc id. ``replace`` is text -> text in every file (longest
    first); ``edits`` is fact path -> (value, raw) in facts.json; ``version`` sets the TMP version."""
    src = Path(doc_dir)
    facts = Facts.load(src / "facts.json")
    old_id = facts.doc_id
    new_id = old_id if name == "original" else _new_id(old_id, name)
    dst = Path(out_root) / new_id.replace(":", "-")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    pairs = sorted({**(replace or {}), old_id: new_id}.items(), key=lambda kv: -len(kv[0]))
    for fname in TEXT_FILES:
        if not (src / fname).exists():
            continue
        text = (src / fname).read_text(encoding="utf-8")
        for a, b in pairs:
            text = text.replace(a, b)
        (dst / fname).write_text(text, encoding="utf-8")
    if (src / "master").is_dir():
        shutil.copytree(src / "master", dst / "master")
    if edits:
        data = json.loads((dst / "facts.json").read_text(encoding="utf-8"))
        by_id = {f["fact_id"]: f for f in data["facts"]}
        for path, (value, raw) in edits.items():
            fid, _, fld = path.partition(".")
            fv = by_id[fid]["fields"][fld]
            fv["value"], fv["raw"] = value, raw
        (dst / "facts.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    if version is not None:
        data = json.loads((dst / "elements.json").read_text(encoding="utf-8"))
        data["metadata"]["version"] = version
        (dst / "elements.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (dst / ".done").touch()
    (dst / "SYNTHETIC").write_text(json.dumps({"made_from": str(src), "variant": name, "replace": replace or {},
                                               "edits": {k: v[0] for k, v in (edits or {}).items()},
                                               "version": version}, indent=1, default=str), encoding="utf-8")
    return dst


def _bump_ref(ref: str, by: int) -> str:
    head, _, num = ref.rpartition("-")
    return f"{head}-{int(num) + by:0{len(num)}d}" if num.isdigit() else f"{ref}-X"


def make_set(doc_dir: str | Path, out_root: str | Path) -> list[Path]:
    """The original, a V2 (end date two weeks later, speed limit +10 km/h) and another TMP on another road."""
    src = Path(doc_dir)
    info = LocalStore(src).docs(User())[0]
    facts = Facts.load(src / "facts.json")
    out = [make_variant(src, out_root, "original", version=1)]

    edits: dict[str, tuple[object, str]] = {}
    replace: dict[str, str] = {}
    for f in facts.of("work_period"):
        fv = f.fields.get("end_date")
        if fv is not None and fv.value:
            end = dt.date.fromisoformat(str(fv.value)) + dt.timedelta(days=14)
            edits[f"{f.fact_id}.end_date"] = (end.isoformat(), f"{end.day} {end:%B %Y}")
            if fv.raw:
                replace[str(fv.raw)] = f"{end.day} {end:%B %Y}"
            replace[str(fv.value)] = end.isoformat()
            break
    for f in facts.of("speed_limit"):
        fv = f.fields.get("speed_kmh")
        if fv is not None and isinstance(fv.value, int):
            edits[f"{f.fact_id}.speed_kmh"] = (fv.value + 10, f"{fv.value + 10} km/h")
            replace[f"{fv.value} km/h"] = f"{fv.value + 10} km/h"
            break
    out.append(make_variant(src, out_root, "v2", replace, edits, version=2))

    ref = info.tmp_ref
    other = _bump_ref(ref, 121)
    swap = {ref: other, ref.replace(" ", "-"): other.replace(" ", "-")}
    swap.update({a: _bump_ref(a, 121) for a in info.aliases})
    if info.roads:
        swap[info.roads[0]] = "Beach Road"
        short = info.roads[0].replace("Avenue", "Ave").replace("Street", "St").replace("Road", "Rd")
        swap[short] = "Beach Road"
    if info.site_address:
        swap[info.site_address] = "12 Beach Road, Browns Bay"
    if info.suburb:
        swap[info.suburb] = "Browns Bay"
    out.append(make_variant(src, out_root, "other_tmp", swap, version=1))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp-pipeline-synthetic", description=__doc__.split("\n\n")[0])
    ap.add_argument("doc_dir", help="a finished run folder (out/sha256-...)")
    ap.add_argument("out_root", help="where to write the synthetic set (keep it apart from out/)")
    a = ap.parse_args(argv)
    if Path(a.out_root).resolve() == Path(a.doc_dir).resolve().parent:
        print("error: write the synthetic set to its own folder, not next to the real document", file=sys.stderr)
        return 2
    for p in make_set(a.doc_dir, a.out_root):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
