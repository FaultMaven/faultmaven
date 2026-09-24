"""Per-run reliability of the two engine↔model contracts.

Separate from ``tool_loop_metrics`` (context budget) and ``lifecycle_metrics``
(INV-XX invariants) for the same reason those are separate from each other:
different question. The question here is **how reliably does the configured
model hold up its side of the two structured contracts the investigation
engine depends on** — invoking tools well-formedly, and returning a response
the schema accepts. These are the reliability metrics a model A/B evaluation
reads (tool-call success rate, schema-validity rate); they are read-only and
never change what the engine does.

No provider/model labels on purpose: an evaluation run configures ONE
(provider, model) per role for the run's lifetime, so attribution comes from
run isolation (scrape before/after), and per-model labels would need
provider/model plumbed through the engine for no additional information.

Read as rates, never the numerator alone:

- ``faultmaven_tool_call_attempts_total``: every investigation-tool invocation
  the model emitted, by ``tool`` and ``outcome``. The *well-formed invocation
  rate* — the A/B "tool-call success" metric — is
  ``(ok + execution_error) / total``: an ``execution_error`` is a well-formed
  call whose TOOL failed (infrastructure noise, not model behavior), while
  ``invalid_args`` (arguments that don't parse as JSON) and ``unknown_tool``
  (a hallucinated name — bounded to ``unknown`` label the same way
  ``tool_loop_metrics`` bounds it) are the model failing the contract.
  The schema tool is deliberately NOT counted here — it is the response
  channel, measured by the schema counter below.

- ``faultmaven_schema_validation_total``: every structured response body the
  engine validated, by ``schema`` and ``outcome``, one increment per body.
  Every body goes through the degradation ladder
  (``_validate_with_degradation``), which is the one place that counts: it is
  reached from the tool-augmented path's schema-tool call, from
  ``_parse_text_as_schema``, and from the non-tool structured single-shot path
  (what a tool-incapable model, the ``ToolCallingUnsupportedError`` fallback, a
  FUNCTION_CALLING single shot and fm#1116's tool-less turn all run). That last
  path used to validate directly with ``model_validate_json`` and count
  separately; it has shared the ladder since fm#1116.

  Outcomes: ``clean`` (validated as emitted), ``repaired`` (validated first
  try, but only after an out-of-range confidence was rescaled from a percentage
  or a ``bool`` coerced — the model's meaning kept, nothing discarded; fm#1502),
  ``pruned`` (part of the body was discarded to keep the rest: an invalid list
  entry or optional sub-object quarantined, or an out-of-range confidence
  removed — dropped from an update-shaped record, or set aside for ingest to
  decide on a link), ``state_dropped`` (state_updates unrecoverable,
  conversational fallback), ``response_synthesized`` (required agent_response
  missing, placeholder filled, state_updates KEPT),
  ``response_synthesized_state_dropped`` (the placeholder validated only after
  dropping every state update as well), ``failed`` (unrecoverable, re-raised).

  ``repaired`` is its own outcome rather than ``clean`` because a repair
  happens INSIDE Pydantic: the ladder sees a first-try success, and folding it
  into ``clean`` would report a body whose confidences were all rewritten as
  one the model got right. ``repaired`` applies only when nothing was pruned
  or dropped; a body that needed both a repair and a prune is ``pruned``.

  The A/B "schema-validity" metric is ``clean / total``. Read state loss as
  ``(state_dropped + response_synthesized_state_dropped) / total`` — the
  synthesized-and-dropped rung is deliberately NOT folded into
  ``response_synthesized``: it loses everything that rung loses AND the turn's
  state, and counting the worse disposition as the lesser one is how a
  state-loss rate under-reports. The outcomes are not claimed to form a total
  order of severity; each names a specific loss, and a consumer sums the ones
  it cares about.

- ``faultmaven_schema_field_repairs_total``: every out-of-range confidence the
  engine acted on, by ``schema`` (the model class owning the field, e.g.
  ``HypothesisToAdd``), ``field`` and ``action`` — ``rescaled`` (a value in
  ``(1, 100]`` read as a percentage), ``coerced`` (a ``bool``), ``dropped``
  (removed from an update-shaped record, the stored value kept) or ``pruned``
  (the record carrying it removed). Field-level where the outcome above is
  body-level, so a repair and a drop are counted separately (fm#1502). Link
  confidences are counted at INGEST, the only point that knows whether the link
  is new; a body that never reaches ingest (a retried or failed generation)
  contributes nothing for them.
"""

from faultmaven.infrastructure.shims.metrics import Counter

# Pinned by tests for the same reason as SCHEMA_VALIDATION_OUTCOMES below.
# ``execution_error`` covers both a tool that returned success=False and a
# tool whose dispatch RAISED — the attempt is recorded either way, so the
# denominator does not shrink on the worst turns.
TOOL_CALL_OUTCOMES = ("ok", "execution_error", "invalid_args", "unknown_tool")

tool_call_attempts_total = Counter(
    "faultmaven_tool_call_attempts_total",
    "Investigation-tool invocations emitted by the model in the DA tool loop, "
    "labeled by ``tool`` and ``outcome`` (ok | execution_error | invalid_args "
    "| unknown_tool). Well-formed-invocation rate = (ok + execution_error) / "
    "total.",
    ["tool", "outcome"],
)

# The label vocabulary, pinned by tests: a call site that spells an outcome
# not in this tuple mints a new label silently, and the rates above are then
# computed over a population that quietly changed shape.
SCHEMA_VALIDATION_OUTCOMES = (
    "clean",
    "repaired",
    "pruned",
    "state_dropped",
    "response_synthesized",
    "response_synthesized_state_dropped",
    "failed",
)

schema_validation_total = Counter(
    "faultmaven_schema_validation_total",
    "Structured response bodies the engine validated (every body goes through "
    "the degradation ladder), labeled by ``schema`` and final ``outcome`` "
    "(clean | repaired | pruned | state_dropped | response_synthesized | "
    "response_synthesized_state_dropped | failed). Schema-validity rate = "
    "clean / total; state-loss rate = (state_dropped + "
    "response_synthesized_state_dropped) / total.",
    ["schema", "outcome"],
)

# Pinned by tests, for the same reason as the tuple above.
SCHEMA_FIELD_REPAIR_ACTIONS = ("rescaled", "coerced", "dropped", "pruned")

schema_field_repairs_total = Counter(
    "faultmaven_schema_field_repairs_total",
    "Out-of-range confidence values the engine acted on, labeled by ``schema`` "
    "(owning model class), ``field`` and ``action`` (rescaled | coerced | "
    "dropped | pruned). Link confidences are counted at ingest.",
    ["schema", "field", "action"],
)
