"""What model-facing text may say about the tools a turn offers (#1939).

Every tool name the model reads is a tool THIS turn gives it. The given set is
the tool names the turn's model is offered: the names of
``StructuredOutputGenerator.build_da_tool_schemas()`` on a tool-loop turn, and
``NO_TOOLS`` on a tool-less one (the single-shot route, a DA provider that
cannot call tools, the ``ToolCallingUnsupportedError`` fallback, the #662
minimal retry). It is computed once per turn and threaded to every renderer as
a value; nothing reads it from ambient state.

Each template module renders its own tool sentences from the set, through the
predicates here: a sentence naming tool ``T`` renders only when ``T`` is
given, and otherwise its tool-less wording does, which is still a complete
instruction. ``tools_line`` states the set itself at the top of every
template: a belt to the per-sentence gates, not a substitute for them.

Cache note (#613): the set sits in the cached prefix of ``INVESTIGATION_BASE``.
It is constant per deployment and route, so the prefix stays byte-stable across
the turns that share one. Within a case it can change with the route: a
single-shot turn (nothing to search) is given none, a knowledge_query turn on
the same case runs the tool loop and is given the loop's set. That costs no
cache read: a knowledge_query turn already has a prefix of its own (its stage
instructions differ), and the single-shot path sends no ``cache_prompt``.
"""

from __future__ import annotations

#: The names of the tools a turn gives the model.
GivenTools = frozenset[str]

#: A tool-less turn's set.
NO_TOOLS: GivenTools = frozenset()

#: The two tools that read a case's evidence files, in the order prompts name
#: them.
EVIDENCE_SEARCH_TOOLS = ("search_file", "case_evidence_search")


def has(given: GivenTools, name: str) -> bool:
    """Whether ``name`` is given on this turn."""
    return name in given


def present(given: GivenTools, *names: str) -> list[str]:
    """``names`` that are given on this turn, in the order passed."""
    return [name for name in names if name in given]


def tools_line(given: GivenTools) -> str:
    """The one line every template states about this turn's tools.

    Sorted, so the same set renders the same bytes on every turn.
    """
    if not given:
        return (
            "No investigation tools are available on this turn; everything you "
            "can use is in this prompt."
        )
    return "Tools available on this turn: " + ", ".join(sorted(given)) + "."
