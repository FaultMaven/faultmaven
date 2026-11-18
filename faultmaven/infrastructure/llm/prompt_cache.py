"""The line that splits an investigation prompt into a cacheable prefix and a per-turn tail (#613).

Provider prompt caches match on a byte-identical PREFIX. A prompt whose first
bytes change every turn — case state, timestamps, the fence token — can never
be read back from the cache on the next turn, however much stable instruction
text follows. So the investigation prompt is laid out durable-first: the
standing instructions, then this boundary, then the rest.

What follows the boundary is not all per-turn. It is this turn's case — state,
the focus emphasis, evidence, hypotheses, conversation and the user's message —
then the closing rules (the immutable ``<security_constraints>`` and the
anti-padding closer, placed last so they are read last), and, on best-effort
structured-output providers, the schema instructions the engine appends after
the prompt. The wording therefore names only what it can promise: that this
turn's case follows.

Four users read ``CACHE_BOUNDARY``:

- ``faultmaven.core.investigation.prompts.templates.investigation`` renders it
  on its own line in ``INVESTIGATION_BASE``, as the last line of the durable
  prefix.
- ``faultmaven.infrastructure.llm.providers.anthropic`` splits the first user
  message of a ``cache_prompt`` request with :func:`split_at_cache_boundary`
  and places an explicit ``cache_control`` breakpoint at the end of the
  boundary line.
- ``faultmaven.infrastructure.llm.providers.openai_provider`` does the same
  with a ``prompt_cache_breakpoint`` on OpenAI GPT-5.6+ (Azure included),
  which matches the cache only at message ends and explicit breakpoints, so
  without one the prefix, sharing a message with the case, is never read
  back. Gemini and Fireworks cache prefixes with no marker.
- ``faultmaven.infrastructure.llm.router``, where content capture is on
  (#2616 K0), records an Opik span's prompt input from the boundary on, so the
  truncated telemetry payload shows this turn's case rather than the same
  static instructions on every call.

It lives in ``infrastructure`` rather than beside the template because the
provider and the router have to read it, and ``infrastructure`` must not
import ``core``.
"""

from __future__ import annotations

CACHE_BOUNDARY = (
    "=== CURRENT CASE (this turn's state, evidence and conversation follow) ==="
)


def split_at_cache_boundary(content: str) -> tuple[str, str] | None:
    """``content`` split after its FIRST boundary line, or None.

    The head ends with the boundary line, newline included; the two parts
    concatenate to ``content``, so the model reads the same prompt. The first
    occurrence is always the template's: everything above it is static
    instruction text, which the prefix structure guard keeps free of case data
    (``test_investigation_prefix_613.py``), so case content quoting the
    boundary lands in the tail and cannot move the split. None when there is
    no boundary or nothing but whitespace after it (a blank text part is
    refused by the APIs, and there would be nothing to cache apart).
    """
    if CACHE_BOUNDARY not in content:
        return None
    end = content.index(CACHE_BOUNDARY) + len(CACHE_BOUNDARY)
    if content.startswith("\n", end):
        end += 1
    head, tail = content[:end], content[end:]
    if not tail.strip():
        return None
    return head, tail
