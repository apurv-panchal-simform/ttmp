"""Phase 4: facts, checks and the review list (plan part 3).

Handler records stay as they are (``records.json``); this package joins them into one canonical TMP record
(``facts.json``): merged entities, the diagram register and every mention of it, pointers, sign-offs, approval,
conflicts, coverage and the review list (``review.json``).
"""
from .model import Facts
from .stage import map_fields, run_checks

__all__ = ["Facts", "map_fields", "run_checks"]
