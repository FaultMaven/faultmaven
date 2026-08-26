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

- ``faultmaven_schema_validation_total``: every structured response body that
  entered the validation ladder (``_validate_with_degradation`` — the
  tool-augmented path and the structured single-shot path that shares it), by
  ``schema`` and ``outcome``, one increment at the ladder's final disposition:
  ``clean`` (validated as-is), ``pruned`` (invalid sub-records quarantined),
  ``state_dropped`` (state_updates unrecoverable, conversational fallback),
  ``response_synthesized`` (required agent_response missing, placeholder
  filled), ``failed`` (unrecoverable, re-raised). The A/B "schema-validity"
  metric is ``clean / total``; everything below ``clean`` is state the model
  put at risk, in increasing order of loss.
"""

from faultmaven.infrastructure.shims.metrics import Counter

TOOL_CALL_OUTCOMES = ("ok", "execution_error", "invalid_args", "unknown_tool")

tool_call_attempts_total = Counter(
    "faultmaven_tool_call_attempts_total",
    "Investigation-tool invocations emitted by the model in the DA tool loop, "
    "labeled by ``tool`` and ``outcome`` (ok | execution_error | invalid_args "
    "| unknown_tool). Well-formed-invocation rate = (ok + execution_error) / "
    "total.",
    ["tool", "outcome"],
)

SCHEMA_VALIDATION_OUTCOMES = (
    "clean",
    "pruned",
    "state_dropped",
    "response_synthesized",
    "failed",
)

schema_validation_total = Counter(
    "faultmaven_schema_validation_total",
    "Structured response bodies through the engine's validation ladder, "
    "labeled by ``schema`` and final ``outcome`` (clean | pruned | "
    "state_dropped | response_synthesized | failed). Schema-validity rate = "
    "clean / total.",
    ["schema", "outcome"],
)
