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
  Two sites feed it and BOTH must, or the denominator silently excludes a
  whole class of turn: the degradation ladder (``_validate_with_degradation``,
  reached from the tool-augmented path and from ``_parse_text_as_schema``) and
  the non-tool structured single-shot path, which validates directly with
  ``model_validate_json`` and is what a tool-incapable model, the
  ``ToolCallingUnsupportedError`` fallback and a FUNCTION_CALLING single shot
  all run.

  Outcomes: ``clean`` (validated as-is), ``pruned`` (invalid sub-records
  quarantined), ``state_dropped`` (state_updates unrecoverable, conversational
  fallback), ``response_synthesized`` (required agent_response missing,
  placeholder filled, state_updates KEPT),
  ``response_synthesized_state_dropped`` (the placeholder validated only after
  dropping every state update as well), ``failed`` (unrecoverable, re-raised).

  The A/B "schema-validity" metric is ``clean / total``. Read state loss as
  ``(state_dropped + response_synthesized_state_dropped) / total`` — the
  synthesized-and-dropped rung is deliberately NOT folded into
  ``response_synthesized``: it loses everything that rung loses AND the turn's
  state, and counting the worse disposition as the lesser one is how a
  state-loss rate under-reports. The outcomes are not claimed to form a total
  order of severity; each names a specific loss, and a consumer sums the ones
  it cares about.
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
    "pruned",
    "state_dropped",
    "response_synthesized",
    "response_synthesized_state_dropped",
    "failed",
)

schema_validation_total = Counter(
    "faultmaven_schema_validation_total",
    "Structured response bodies the engine validated (degradation ladder and "
    "non-tool single-shot path), labeled by ``schema`` and final ``outcome`` "
    "(clean | pruned | state_dropped | response_synthesized | "
    "response_synthesized_state_dropped | failed). Schema-validity rate = "
    "clean / total; state-loss rate = (state_dropped + "
    "response_synthesized_state_dropped) / total.",
    ["schema", "outcome"],
)
