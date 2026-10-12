"""Given tool sets for prompt tests (#1939).

Prompt text names a tool only when the turn gives it, so a test asserting on a
rendered block has to say which set the turn was given. ``DEFAULT_GIVEN`` is
the registry's default six — what an ordinary tool-loop turn is given, pinned
against the real ``_create_investigation_tools`` by
``prompts/test_prompt_tools_are_given_tools_1934.py``. A test about a tool-less
turn passes ``NO_TOOLS``.
"""

from faultmaven.core.investigation.prompts.templates.tool_text import (
    NO_TOOLS,
    GivenTools,
)

DEFAULT_GIVEN: GivenTools = frozenset(
    {
        "search_file",
        "vectorize_file",
        "deep_analysis",
        "kb_qa",
        "case_evidence_search",
        "web_search",
    }
)

#: The default six plus the entity tools, given under FAULTMAVEN_ENTITY_REGISTRY
#: — the flag the entity-highlights block renders under.
ENTITY_GIVEN: GivenTools = DEFAULT_GIVEN | {"find_entity", "list_top_entities"}

__all__ = ["DEFAULT_GIVEN", "ENTITY_GIVEN", "NO_TOOLS"]
