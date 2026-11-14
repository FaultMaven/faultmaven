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
Measured on the shipped pack through the real hybrid path (2026-11-14), bare
topics the pack covers scored 0.585–0.724 at rank 1, while topics it does not
cover scored 0.365–0.529: two of six cleared the push's 0.5 floor ("Oracle RAC
interconnect errors" → a JVM runbook at 0.529). So:

* below :data:`KB_QUESTION_CANDIDATE_FLOOR` nothing is shown and the reply says
  plainly that nothing was found — every negative under it was noise;
* at or above it, the closest runbooks are shown as CANDIDATES, and the reply
  names one as covering the topic only if its title or excerpt is about it.

A lookup that could not run (no knowledge service, a failed scope or search)
is reported as such, never as "nothing found": a failed or narrowed search
establishes nothing about what the knowledge base holds.

This module holds the lookup's types, limits and prompt block; the search
itself is ``milestone_engine/kb_question_lookup.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

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
    #: ``"question"`` (the user named the topic) or ``"problem_statement"``
    #: (the question named none, so the case's statement was searched).
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
    is a question.
    """
    raw = (intent_data or {}).get(KB_QUESTION_INTENT_KEY)
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    topic = raw.get("topic")
    if kind not in ("lookup", "listing") or not isinstance(topic, str):
        return None
    return {"kind": kind, "topic": topic.strip()}


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

_UNLABELLED_GUIDANCE = (
    "If general guidance would help, give it after that, and say plainly that "
    "it is general guidance, not a procedure from their knowledge base."
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
        "you otherwise would.",
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

    if lookup.query_source == "problem_statement":
        lines.append(
            "The question named no topic, so the engine searched with the case's "
            "problem statement:"
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
        lines.append(
            "Nothing in the knowledge base this user can access came close. Say "
            "so first, plainly — e.g. \"I couldn't find a runbook for that in "
            'the knowledge base you have access to." Never suggest that a '
            "runbook exists. " + _UNLABELLED_GUIDANCE
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
        "as the runbook's. If no candidate is about it, say first that you "
        "couldn't find a runbook for it in the knowledge base they have access "
        "to, then name the closest candidate as the nearest match and what it "
        "covers instead. " + _UNLABELLED_GUIDANCE
    )
    return "\n".join(lines) + "\n\n"
