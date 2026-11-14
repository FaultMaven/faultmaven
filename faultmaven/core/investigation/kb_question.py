"""A user's question about the knowledge base itself, looked up before generation.

Design: ``docs/architecture/investigation-engine/orchestration-capabilities.md``
§5.6. Knowledge reaches a turn three ways, and this is the one the USER starts:

* the push (``kb_prefetch``): runbooks the engine fetches at case transitions
  to help solve the problem; a miss is not worth mentioning;
* the pull (``kb_qa``): a lookup the model elects while it works; same;
* this lookup: the user asked what the knowledge base holds ("is there a
  runbook for X?", "what does our runbook on Y say?"). Here a miss IS the
  answer, so the reply has to report it.

The lookup runs in the engine before the prompt is built, so it needs no tool
and works on every route — the single-shot one included, which is given none.
It is not governed by ``KB_PREFETCH_ENABLED``: that flag switches the push, and
this is a read the user asked for.

Status is decided in two steps, because the score alone cannot decide "found".
Measured on the shipped pack through the real hybrid path (2026-11-14,
``tests/eval/kb_retrieval/question-lookups.json``, replayed through this lookup
by ``test_kb_question_floor_recording.py``): for 16 bare topics the pack covers,
the covering runbook's best chunk scored 0.585–0.724 (raw cosine); for 6 it
does not cover, the closest chunk scored 0.493–0.541, and every one of them
cleared the push's 0.5 floor ("SAP HANA backup failures" → a PostgreSQL
runbook at 0.541). So:

* below :data:`KB_QUESTION_CANDIDATE_FLOOR` nothing is shown and the reply says
  plainly that nothing was found — nothing under it was on topic;
* at or above it, the closest runbooks are shown as CANDIDATES, and the reply
  names one as covering the topic only if its title or excerpt is about it.

On the shipped pack the second step is the one that answers: all six off-topic
topics came back as candidates, so their "not found" is the reply's judgement,
and the first step speaks only for a knowledge base with nothing near the topic.
The floor is calibrated on bare topics; a query that is a description (the rest
of the message, or the problem statement) is reported more softly on a miss.

A lookup that could not run (no knowledge service, a failed scope or search)
is reported as such, never as "nothing found": a failed or narrowed search
establishes nothing about what the knowledge base holds.

This module holds the lookup's types, limits and prompt block; the search
itself is ``milestone_engine/kb_question_lookup.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

#: The ``intent_data`` key the investigation service writes the question under.
KB_QUESTION_INTENT_KEY = "kb_question"

#: The origin stamped on this lookup's entries — their ``trigger``, beside the
#: push's ``symptom`` / ``root_cause`` — and so on the turn's ``sources``.
KB_QUESTION_TRIGGER = "question"

#: Below this cosine nothing is a candidate (see the module docstring for the
#: measurement). Not the push's 0.5: that floor admits two of six off-topic
#: negatives, and above this one the reply judges, rather than the score.
KB_QUESTION_CANDIDATE_FLOOR = 0.45

KB_QUESTION_FETCH_LIMIT = 10
KB_QUESTION_MAX_ENTRIES = 3
KB_QUESTION_MAX_PER_RUNBOOK = 2
#: One section's text, bounded: the block is reserved in the prompt budget.
KB_QUESTION_EXCERPT_CHARS = 1200
KB_QUESTION_MAX_OTHER_TITLES = 4
#: A problem statement used as the query is cut here.
KB_QUESTION_QUERY_MAX_CHARS = 300


#: The user named the topic: a miss there is a plain "not found".
QUERY_FROM_QUESTION = "question"
#: A description searched as a whole — the rest of the message, or the case's
#: problem statement. The floor was calibrated on bare topics (see the module
#: docstring), so a miss on a sentence is reported as "no close match", not as
#: "there is no runbook".
QUERY_FROM_MESSAGE = "message"
QUERY_FROM_PROBLEM_STATEMENT = "problem_statement"


class KbLookupStatus(str, Enum):
    """What the lookup established."""

    #: Runbooks at or above the candidate floor; the reply judges whether any
    #: is actually about the topic.
    CANDIDATES = "candidates"
    #: The search ran and nothing reached the floor.
    NOTHING = "nothing"
    #: The search could not run. Establishes nothing about the knowledge base.
    UNAVAILABLE = "unavailable"
    #: The question named no topic and the case has no problem statement.
    NO_TOPIC = "no_topic"
    #: The user asked what runbooks there are; chat does not list them.
    LISTING = "listing"


@dataclass(frozen=True)
class KbLookup:
    """One turn's knowledge-base lookup. Not persisted."""

    status: KbLookupStatus
    #: What was searched for; empty when nothing was.
    query: str = ""
    #: Where the query came from: :data:`QUERY_FROM_QUESTION` (the user named
    #: the topic), :data:`QUERY_FROM_MESSAGE` (the rest of the message, which
    #: the question pointed back at) or :data:`QUERY_FROM_PROBLEM_STATEMENT`.
    query_source: str = ""
    #: Push-shaped entries (title, section, summary, score,
    #: parent_document_id, trigger), so the turn's ``sources`` read them the
    #: way they read the push.
    entries: tuple[dict, ...] = field(default_factory=tuple)
    #: Further runbook titles at or above the floor that were not shown.
    other_titles: tuple[str, ...] = field(default_factory=tuple)


def kb_question_of(intent_data: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The question the investigation service recorded, or ``None``.

    Defensive about shape: only ``{"kind": "lookup"|"listing", "topic": str}``
    is a question; ``from_context`` is read as a bool, absent meaning False.
    """
    raw = (intent_data or {}).get(KB_QUESTION_INTENT_KEY)
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    topic = raw.get("topic")
    if kind not in ("lookup", "listing") or not isinstance(topic, str):
        return None
    return {
        "kind": kind,
        "topic": topic.strip(),
        "from_context": raw.get("from_context") is True,
    }


def lookup_entries_only(entries: list) -> list:
    """The entries of a rendered-entries report that came from this lookup.

    A template without the push's ``{kb_results}`` slot still renders the
    lookup's block, so its report keeps these and drops the push's.
    """
    return [
        e
        for e in entries
        if isinstance(e, dict) and e.get("trigger") == KB_QUESTION_TRIGGER
    ]


#: How much of a section's text identifies it. The push carries a section's
#: snippet and this lookup its content, so the two forms of one section agree
#: on their opening, not on their length.
_SECTION_KEY_CHARS = 160


def section_key(document_id: Any, text: Optional[str]) -> tuple[str, str]:
    """One runbook section, whichever channel carried it: its document and the
    opening of its text, whitespace-normalised."""
    opening = " ".join((text or "").strip()[: _SECTION_KEY_CHARS * 2].split())
    # A snippet is the text's head plus an ellipsis, so a short section's
    # snippet is its whole text plus "...".
    opening = opening[:_SECTION_KEY_CHARS]
    for mark in ("...", "…"):
        opening = opening.removesuffix(mark)
    return (str(document_id or ""), opening.rstrip())


def without_lookup_duplicates(pushed: list, lookup: Optional["KbLookup"]) -> list:
    """The pushed entries ``lookup`` does not also carry.

    A section both channels found is cited once, as the lookup's: it answers
    what the user asked this turn.
    """
    if lookup is None or not lookup.entries:
        return pushed
    found = {
        section_key(e.get("parent_document_id"), e.get("summary"))
        for e in lookup.entries
    }
    return [
        e
        for e in pushed
        if section_key(e.get("parent_document_id"), e.get("summary")) not in found
    ]


def excerpt_of(text: Optional[str]) -> str:
    text = (text or "").strip()
    if len(text) <= KB_QUESTION_EXCERPT_CHARS:
        return text
    return text[:KB_QUESTION_EXCERPT_CHARS].rstrip() + " … [excerpt cut]"


#: The block's heading. The "don't mention the search" rules name it, so the
#: two cannot drift apart.
KB_LOOKUP_HEADING = "KNOWLEDGE BASE LOOKUP"

_DASHBOARD_POINTER = (
    "the full runbook is in the Knowledge Base page of the FaultMaven Dashboard"
)

# Guidance only where it belongs: a troubleshooting assistant offers an
# engineering procedure, not a recipe.
_UNLABELLED_GUIDANCE = (
    "If the topic is an engineering or operations one and general guidance "
    "would help, give it after that, and say plainly that it is general "
    "guidance, not a procedure from their knowledge base. Otherwise just say "
    "what the knowledge base holds."
)

# A search the model runs itself this turn can still find what this one missed.
_SAME_TURN_SEARCH = (
    "If a knowledge-base search you run yourself this turn does return a "
    "runbook about it, report that runbook instead."
)


def render_kb_lookup_block(lookup: Optional[KbLookup], fence: Any) -> str:
    """The prompt block for ``lookup``: renderer prose, then fenced data.

    ``fence`` is the assembly's ``PromptFence``: the query and every runbook's
    title and text are caller-controlled (a personal or team runbook is user
    text), so each goes through ``fence.element``. Empty when there is no
    lookup. Ends with a blank line so a template can place it directly before
    the next heading.
    """
    if lookup is None:
        return ""
    lines = [
        f"{KB_LOOKUP_HEADING} (the user asked about the knowledge base itself):",
        "This turn's message asks what the knowledge base holds, so your reply "
        "must report what this lookup found — including when it found nothing. "
        "The rules elsewhere in this prompt about not mentioning a knowledge-base "
        "search do not apply to this question. Answer the rest of the message as "
        "you otherwise would. (The question was recognised from its wording; if "
        "the message does not in fact ask what the knowledge base holds — it "
        "only mentions a runbook in passing — disregard this section.)",
    ]
    status = lookup.status
    if status is KbLookupStatus.LISTING:
        lines.append(
            "The user asked which runbooks exist. Chat cannot list the knowledge "
            "base: say so, point them to the Knowledge Base page of the "
            "FaultMaven Dashboard, and offer to look up a runbook on a specific "
            "topic. Never list runbook titles from memory or invent any."
        )
        return "\n".join(lines) + "\n\n"
    if status is KbLookupStatus.NO_TOPIC:
        lines.append(
            "The question names no topic, and the case has no problem statement "
            "to search with, so nothing was searched. Ask which topic or runbook "
            "they mean. Do not say whether a runbook exists."
        )
        return "\n".join(lines) + "\n\n"

    if lookup.query_source == QUERY_FROM_PROBLEM_STATEMENT:
        lines.append(
            "The question named no topic, so the engine searched with the case's "
            "problem statement:"
        )
    elif lookup.query_source == QUERY_FROM_MESSAGE:
        lines.append(
            "The question pointed back at the rest of the message, so the engine "
            "searched the runbooks this user can access with it:"
        )
    else:
        lines.append("The engine searched the runbooks this user can access for:")
    lines.append(fence.element("kb_lookup_query", lookup.query))

    if status is KbLookupStatus.UNAVAILABLE:
        lines.append(
            "The search could NOT run on this turn. Tell the user you could not "
            "check the knowledge base just now. Do NOT say no runbook exists — "
            "nothing was established either way. " + _UNLABELLED_GUIDANCE
        )
        return "\n".join(lines) + "\n\n"
    if status is KbLookupStatus.NOTHING:
        if lookup.query_source == QUERY_FROM_QUESTION:
            lines.append(
                "Nothing in the knowledge base this user can access came close. "
                "Say so first, plainly — e.g. \"I couldn't find a runbook for "
                'that in the knowledge base you have access to." Never suggest '
                "that a runbook exists. "
                + _SAME_TURN_SEARCH
                + " "
                + _UNLABELLED_GUIDANCE
            )
        else:
            # A description, not a named topic: the floor was set on bare
            # topics, so a miss here is weaker evidence.
            lines.append(
                "Nothing in the knowledge base this user can access came close to "
                "that description. Say first that you couldn't find a runbook "
                "matching it, and offer to look again if they name the runbook or "
                "topic they have in mind. Never suggest that a runbook exists. "
                + _SAME_TURN_SEARCH
                + " "
                + _UNLABELLED_GUIDANCE
            )
        return "\n".join(lines) + "\n\n"

    lines.append(
        "The closest runbooks it returned are below. Closest is not the same as "
        "about the topic: a runbook covers what was asked only if its title or "
        "excerpt is actually about it."
    )
    for n, entry in enumerate(lookup.entries, start=1):
        lines.append(f"CANDIDATE {n}:")
        lines.append(fence.element("kb_runbook_title", str(entry.get("title") or "")))
        lines.append(
            fence.element("kb_runbook_excerpt", str(entry.get("summary") or ""))
        )
    if lookup.other_titles:
        lines.append("Further runbooks the search returned, not shown above:")
        lines.append(fence.element("kb_runbook_titles", "\n".join(lookup.other_titles)))
    lines.append(
        "HOW TO ANSWER: If a candidate is about what was asked, name it by its "
        "title and answer from its excerpt; say these are excerpts and that "
        f"{_DASHBOARD_POINTER}. Do not present steps the excerpts do not contain "
        "as the runbook's. If no candidate is about it but a further runbook's "
        "title plainly is, name that runbook as the likely match, say its text "
        "was not retrieved here, and point them to the Dashboard. If no "
        "candidate is about it, say first that you couldn't find a runbook for "
        "it in the knowledge base they have access to, then name the closest "
        "candidate as the nearest match and what it covers instead. "
        + _SAME_TURN_SEARCH
        + " "
        + _UNLABELLED_GUIDANCE
    )
    return "\n".join(lines) + "\n\n"


#: A title in the fallback verdict is cut here: that prompt is the smallest.
_VERDICT_TITLE_CHARS = 120


def kb_lookup_verdict(
    lookup: Optional[KbLookup], element: Callable[[str, str], str]
) -> str:
    """The lookup in a few lines, for the minimal prompt a turn falls back to
    when the full one does not fit (its block is then dropped with the rest).

    Carries what the reply must not get wrong — found, nothing, or could not
    search — and the candidates' titles, never their text. Empty when there is
    no lookup. ``element(name, body)`` fences the titles, which are
    caller-controlled: the fallback passes its own, so its trust rule names
    the block.
    """
    if lookup is None:
        return ""
    status = lookup.status
    if status is KbLookupStatus.LISTING:
        verdict = (
            "The user asked which runbooks exist: chat cannot list them; point "
            "to the Knowledge Base page of the FaultMaven Dashboard."
        )
    elif status is KbLookupStatus.NO_TOPIC:
        verdict = "The user asked about a runbook without naming a topic: ask which."
    elif status is KbLookupStatus.UNAVAILABLE:
        verdict = (
            "The user asked about the knowledge base, but it could not be "
            "searched this turn: say so, and do not say no runbook exists."
        )
    elif status is KbLookupStatus.NOTHING:
        verdict = (
            "The user asked about the knowledge base, and a search found nothing "
            "close: say you couldn't find a matching runbook, and never suggest "
            "one exists."
        )
    else:
        titles = [
            str(e.get("title") or "")[:_VERDICT_TITLE_CHARS] for e in lookup.entries
        ]
        return (
            f"{KB_LOOKUP_HEADING}: the user asked about the knowledge base. The "
            "closest runbooks a search returned are titled as below; name one "
            "only if its title is about what was asked, and say the full runbook "
            f"is in the Knowledge Base page of the FaultMaven Dashboard.\n"
            + element("kb_runbook_titles", "\n".join(titles))
            + "\n\n"
        )
    return f"{KB_LOOKUP_HEADING}: {verdict}\n\n"
