"""Step 2, classify the question (plan 5.4): rules first; Q1 when the rules find nothing, find several readings, or
the message is a follow-up. Without a model the rules' reading is used (or the previous turn's for a follow-up),
and a question no rule reads goes to the baseline recipe."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .. import logging_ as lg
from ..config import RetrievalCfg
from ..model import prompts
from ..model.client import ModelClient, ask
from ..model.validators import check_q1
from .details import code_details
from .resolve import Session

NEEDS_TMP = frozenset({"fact", "procedure", "diagram", "status", "calculation", "time_check", "summary",
                       "general_rule", "version_change"})


@dataclass
class Reading:
    question: str
    standalone: str
    types: list[str]                    # empty = baseline recipe
    confidence: str
    details: dict[str, str] = field(default_factory=dict)
    method: str = "rules"               # rules | Q1 | rules (no model) | follow-up (no model) | baseline
    follow_up: bool = False
    rule_types: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def baseline(self) -> bool:
        return not self.types


def rule_types(question: str, cfg: RetrievalCfg) -> list[str]:
    """Every type whose cue matches, in rule order; "fact" only when nothing else does. time_check also needs a time
    or day in the question."""
    out = []
    for t, rx in cfg.rules.items():
        if t == "fact" or not rx.search(question):
            continue
        if t == "time_check" and not cfg.time_mention.search(question):
            continue
        out.append(t)
    if not out and "fact" in cfg.rules and cfg.rules["fact"].search(question):
        out.append("fact")
    return out


def classify(question: str, session: Session | None, cfg: RetrievalCfg, model: ModelClient | None,
             now: dt.datetime) -> Reading:
    details = code_details(question, cfg, now.date())
    types = rule_types(question, cfg)
    follow_up = bool(session and session.turns and cfg.follow_up.search(question))
    if len(types) == 1 and not follow_up:
        return Reading(question, question, types, "high", details, "rules", follow_up, types)

    answer = ask(model, prompts.q1_classify(question, f"{now:%Y-%m-%d %A %H:%M}",
                                            session.tmp_ref if session else None, session.turns if session else []))
    q1 = check_q1(answer, question, prompts.QUESTION_TYPES, prompts.Q1_DETAILS)
    if q1 is not None:
        merged = {**q1.details, **details}            # code wins on what it reads exactly (times, dates, refs)
        if follow_up and session:
            merged = {**session.details, **merged}
        reading = Reading(question, q1.standalone_question, q1.types if q1.confidence != "low" else [],
                          q1.confidence, merged, "Q1", follow_up, types, q1.problems)
        lg.log_event("classify", "q1", types=q1.types, confidence=q1.confidence, problems=q1.problems)
        return reading

    if follow_up and session:
        merged = {**session.details, **details}
        use = types or list(session.types)
        return Reading(question, question, use, "medium" if use else "low", merged, "follow-up (no model)",
                       True, types)
    if types:
        return Reading(question, question, types, "medium", details, "rules (no model)", follow_up, types)
    return Reading(question, question, [], "low", details, "baseline", follow_up, types)
