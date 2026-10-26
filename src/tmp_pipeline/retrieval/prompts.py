"""Prompts Q3 (write the answer) and Q4 (repair it once). Each builder returns a ``ModelRequest`` whose key covers the
prompt version, so editing a prompt here (and bumping its version) invalidates exactly its cache."""
from __future__ import annotations

from ..llm import ModelRequest, build_request

PROMPT_VERSIONS = {"Q3": "3.0", "Q4": "1.1"}
# Answers (Q3) are written by gpt-6-luna without thinking by decision (2026-10-14: same text-set score as gpt-6-sol at
# a twentieth of the cost); a failed answer is repaired (Q4) by gpt-6-sol.
MODELS = {"Q3": "gpt-6-luna", "Q4": "gpt-6-sol"}
REASONING = {"Q3": "none"}


def _req(pid: str, system: str, user: str, tools: tuple[str, ...] = (), thread: tuple[str, ...] = ()) -> ModelRequest:
    return build_request(pid, PROMPT_VERSIONS[pid], MODELS[pid], system, user, reasoning=REASONING.get(pid), tools=tools,
                         thread=thread)


# Q3 writes the answer from the retrieved context and Q4 repairs it once when the check fails. Both answer in plain text;
# the text travels inside {"answer": ...} so every prompt has the same structured-output shape.

_Q3_SYSTEM = """You answer questions about New Zealand Traffic Management Plans using ONLY the
context below. Answer in plain text.
Rules:
1. Copy every number, time, date, RP, phone number and name exactly as it appears
   in the context.
2. Never invent a number. {rule2}
3. Cite every fact as (form, section, page).
4. If a value's status is "unverified", say it is unconfirmed.
5. If a value has a conflict, give every version and where it appears. Do not choose.
6. If something is blank in the document, say it is blank. Never fill it in.
7. "Not ticked" means not ticked on the form; don't turn it into "no" unless the
   form is a Yes/No question.
8. Keep general standards separate from this site's facts, and say which is which.
   Text marked "struck through (not applicable)" does not apply to this job: say so.
9. Describe diagrams in words: signs in order, positions, distances, with the TMD
   and page.
10. If the context doesn't answer the question, say so and say what is missing.
11. Answer the question first, then the detail. Style for this user: {role_style}
12. Questions about an image or drawing are answered from the image facts in the
   context (text as printed, positions, order, distances and what each dimension
   line runs between, appearance). Give positions as written there. Say what a
   distance measures only as stated; separate what the image shows from what is
   inferred, and list unreadable or missing items instead of guessing. If several
   diagrams match, answer for each one and name its TMD and page.
13. If the question asks for JSON or a list, put it inside the answer text.{tools_note}"""

_RULE2_PLAIN = ("Comparing a time, date or day with the hours and dates in the context is fine: name the entry you "
                "used. Do not calculate new values.")
_RULE2_TOOLS = ("Never calculate, add up, convert or work out dates yourself. When the answer needs a new number, "
                "call calculate; to decide whether something is allowed or in force at a date and time, call "
                "time_check. Quote the result with its working and sources. A value or a worked figure that is "
                "already written in the context needs no tool.")
_TOOLS_NOTE = """
You can call tools. Answer from the context when it holds the answer: most questions need no tool. Use a tool when
the question needs: a calculation (calculate), a date or time check (time_check), TMPs other than the current one
(find_tmps, search_chunks with tmp), a comparison of versions (compare_versions), stored values the context lacks
(get_facts), or other text of a TMP (search_chunks). Tool results are the document's own data: cite their sources.
If a tool returns an error, say what could not be worked out; do not guess."""
_LIBRARY_NOTE = """
No TMP has been chosen and several can be read. The context lists them. Answer a question about all of them with the
tools (find_tmps, then search_chunks or get_facts with tmp). If the question is about one particular TMP that it
does not name, reply {"ask_back": true} instead of an answer."""

_Q3_USER = """Today (NZ time): {now_nz}
Conversation so far: {recent_turns}
QUESTION: {standalone_question}
{context}
Reply JSON: {{"answer": "<the answer, plain text>"}}"""

_Q4_SYSTEM = """Your previous answer broke the rules. Rewrite it using ONLY the context."""

_Q4_USER = """Today (NZ time): {now_nz}
Problems found by the checker:
{problems}
Previous answer: {answer}
QUESTION: {standalone_question}
{context}
Rewrite the answer so every problem is fixed. Same rules as before.
Reply JSON: {{"answer": "<the answer, plain text>"}}"""


def q3_answer(question: str, context: str, role_style: str, now_nz: str, recent_turns: list[str],
              tools: tuple[str, ...] = (), thread: tuple[str, ...] = (), library: bool = False,
              hint: str = "", entities: str = "") -> ModelRequest:
    """``tools`` are the tools offered in this round (none: the answer must be written now); ``thread`` is what the
    model asked for and was given so far; ``hint`` tells it why it is asked again."""
    system = _Q3_SYSTEM.format(role_style=role_style, rule2=_RULE2_TOOLS if tools or thread else _RULE2_PLAIN,
                               tools_note=(_TOOLS_NOTE + (_LIBRARY_NOTE if library else "")) if tools or thread else "")
    user = _Q3_USER.format(now_nz=now_nz, recent_turns="\n".join(recent_turns[-4:]) or "(none)",
                           standalone_question=question, context=context)
    if entities and (tools or thread):
        user += f"\nSTORED FACTS the tools can read (entity: fields):\n{entities}"
    return _req("Q3", system, user + (f"\n{hint}" if hint else ""), tools, thread)


def q4_repair(problems: list[str], answer: str, question: str, context: str, now_nz: str) -> ModelRequest:
    return _req("Q4", _Q4_SYSTEM, _Q4_USER.format(now_nz=now_nz, problems="\n".join(f"- {p}" for p in problems),
                                                  answer=answer, standalone_question=question, context=context))
