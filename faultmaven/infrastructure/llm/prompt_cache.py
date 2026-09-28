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

Three users read ``CACHE_BOUNDARY``:

- ``faultmaven.core.investigation.prompts.templates.investigation`` renders it
  on its own line in ``INVESTIGATION_BASE``, as the last line of the durable
  prefix.
- ``faultmaven.infrastructure.llm.providers.anthropic`` looks for its first
  occurrence in the first user message of a ``cache_prompt`` request and
  places an explicit ``cache_control`` breakpoint at the end of its line.
  Providers with automatic prefix caching (OpenAI, Gemini, Fireworks) need no
  marker; the stable prefix is enough for them.
- ``faultmaven.infrastructure.llm.router`` records an Opik span's prompt input
  from the boundary on, so the truncated telemetry payload shows this turn's
  case rather than the same static instructions on every call.

It lives in ``infrastructure`` rather than beside the template because the
provider and the router have to read it, and ``infrastructure`` must not
import ``core``.
"""

CACHE_BOUNDARY = (
    "=== CURRENT CASE (this turn's state, evidence and conversation follow) ==="
)
