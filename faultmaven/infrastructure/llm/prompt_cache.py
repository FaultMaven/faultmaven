"""The line that splits an investigation prompt into a cacheable prefix and a per-turn tail (#613).

Provider prompt caches match on a byte-identical PREFIX. A prompt whose first
bytes change every turn — case state, timestamps, the fence token — can never
be read back from the cache on the next turn, however much stable instruction
text follows. So the investigation prompt is laid out durable-first: the
standing instructions, then this boundary, then everything that changes from
turn to turn.

Two users read ``CACHE_BOUNDARY``:

- ``faultmaven.core.investigation.prompts.templates.investigation`` renders it
  on its own line in ``INVESTIGATION_BASE``, as the last line of the durable
  prefix.
- ``faultmaven.infrastructure.llm.providers.anthropic`` looks for it in the
  first user message of a ``cache_prompt`` request and places an explicit
  ``cache_control`` breakpoint at the end of its line. Providers with automatic
  prefix caching (OpenAI, Gemini, Fireworks) need no marker; the stable prefix
  is enough for them.

It lives in ``infrastructure`` rather than beside the template because the
provider has to read it, and ``infrastructure`` must not import ``core``.

The wording is addressed to the model as well as to the code: it tells the
reader of the prompt that what follows is this turn's case data.
"""

CACHE_BOUNDARY = (
    "=== CURRENT CASE (everything below this line changes from turn to turn) ==="
)
