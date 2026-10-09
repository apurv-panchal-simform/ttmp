"""Golden snapshot of the sample run. Refactors must not change zones, element counts, routes or flags;
a deliberate behaviour change regenerates tests/data/golden_sample_snapshot.json in the same commit."""
import json
from collections import Counter
from pathlib import Path

import pytest

from tmp_pipeline.run import run

GOLDEN = Path(__file__).parent / "data" / "golden_sample_snapshot.json"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory, request):
    res = run(request.getfixturevalue("sample_pdf"), tmp_path_factory.mktemp("golden"), render=False)
    s = res.store
    return {
        "elements": len(s.elements),
        "zones": {str(p): s.pages[p].zone for p in sorted(s.pages)},
        "types": dict(sorted(Counter(e.type for e in s.elements.values()).items())),
        "routes": dict(sorted(Counter(r["route"] for r in res.router.records).items())),
        "outcomes": dict(sorted(Counter(r["outcome"] for r in res.router.records).items())),
        "flags": dict(sorted(Counter(f for e in s.elements.values() for f in e.flags).items())),
        "records": dict(sorted(Counter(r.kind for r in res.records.records.values()).items())),
    }


@pytest.mark.parametrize("key", ["elements", "zones", "types", "routes", "outcomes", "flags", "records"])
def test_sample_matches_golden_snapshot(snapshot, key):
    expected = json.loads(GOLDEN.read_text())
    assert snapshot[key] == expected[key]
