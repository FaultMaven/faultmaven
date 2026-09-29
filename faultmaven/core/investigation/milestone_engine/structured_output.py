"""Parsing and repairing the LLM's structured (schema-tool-call) response: schema validation, degradation, sub-record pruning and the text/JSON fallback parsers."""

import difflib
import json
import logging
import re
from typing import Any, Optional

from faultmaven.core.investigation.confidence_repair import (
    CONFIDENCE_REPAIRS_CONTEXT_KEY,
    CONFIDENCE_UNREPAIRABLE,
    MEANING_PRESERVING_ACTIONS,
    ConfidenceAction,
    ConfidenceRepair,
)
from faultmaven.core.investigation.confidence_repair import (
    count as count_confidence_repair,
)
from faultmaven.core.investigation.reliability_metrics import (
    schema_validation_total,
)
from faultmaven.core.investigation.schemas import (
    BaseInteractionResponse,
)
from faultmaven.infrastructure.llm.json_response import (
    loads_llm_json,
)
from faultmaven.infrastructure.llm.providers import StopReason

from .response_synthesis import (
    synthesized_agent_response,
)

logger = logging.getLogger(__name__)


def _parse_schema_tool_call(
    tool_call: Any,
    schema_model: Any,
) -> BaseInteractionResponse:
    """Parse a schema tool call response into a Pydantic model.

    Applies the same JSON cleanup (nested parsing + enum fixing) as the
    single-shot path in _generate_structured_output.
    """
    args = tool_call.function.get("arguments", "{}")
    if isinstance(args, dict):
        content = json.dumps(args)
    else:
        content = args

    # Parse JSON (strict=False allows control chars in LLM-generated strings)
    content_obj = json.loads(content, strict=False)

    # Recursively parse nested JSON strings
    content_obj = _parse_nested_json(content_obj)

    # Coerce unresolvable state_updates to {} so Pydantic field defaults apply.
    # Covers two Fireworks/DeepSeek V3 failure modes:
    #   (a) null — LLM omitted the field entirely
    #   (b) string — JSON was truncated/malformed and _parse_nested_json
    #       could not repair it (e.g. closing "} cut off before XML tag)
    _su = content_obj.get("state_updates") if isinstance(content_obj, dict) else None
    if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
        content_obj["state_updates"] = {}

    # Fix hallucinated enum values
    schema_dict = schema_model.model_json_schema()
    content_obj = _fix_enum_violations(
        content_obj,
        schema_dict,
        root_defs=schema_dict.get("$defs"),
    )

    # Validate with Pydantic, degrading gracefully instead of 500ing on a
    # single malformed sub-record (parse-time cross-field validators).
    parsed = _validate_with_degradation(content_obj, schema_model)

    # Dropped-field detection: compare what the LLM emitted to what the
    # schema accepted. Any key the LLM put in the dict that isn't a
    # field on the schema gets silently dropped by Pydantic's default
    # extra="ignore". Log it so prompt-schema drift becomes observable.
    # Motivated by the prompt-instructs/schema-rejects bug class found
    # via behavioral eval — see ADR / docs.
    _log_dropped_fields(content_obj, parsed, schema_model)
    return parsed


def _record_schema_validation(schema_model, outcome: str) -> None:
    """One increment on ``schema_validation_total``.

    Shared by the degradation ladder and the non-tool structured
    single-shot path so both dispositions land in the same population —
    the A/B schema-validity rate is only meaningful over a denominator
    that includes every body the engine validated.
    """
    schema_validation_total.labels(schema=schema_model.__name__, outcome=outcome).inc()


def _synthesize_agent_response(parsed: Any, stop_reason: StopReason) -> Any:
    """Name an unusable ``agent_response`` by the response's stop reason.

    The engine owns response synthesis because it is the only layer that
    can see WHY the answer is unusable (#1442). Applied at the two places
    a parsed structured response leaves the LLM call with its envelope
    still in hand — the single-shot structured path and the tool loop's
    schema-tool call — rather than inside ``_validate_with_degradation``,
    which is shared with ``_parse_text_as_schema``: that recovery path
    must see the model's own blank answer to reject a prose-embedded
    example block, and a placeholder written first would pass its check.
    Moving synthesis up keeps the validator purely structural and passes
    nothing new down to it.

    Takes the stop reason rather than the response so the CALLER decides
    what it means: both callers pass :func:`schema_answer_stop_reason`,
    because at both a tool call is the answer rather than a handoff.

    Fires only when the answer is blank (missing answers were blanked by
    the validator). The result is a copy carrying
    ``_agent_response_synthesized``; *parsed* is returned unchanged when
    the answer is usable or the stop reason names no failure
    (``TOOL_CALLS``, which neither current caller passes).
    """
    answer = getattr(parsed, "agent_response", None)
    if not isinstance(answer, str) or answer.strip():
        return parsed
    text = synthesized_agent_response(stop_reason)
    if text is None:
        return parsed
    synthesized = parsed.model_copy(update={"agent_response": text})
    synthesized._agent_response_synthesized = True
    logger.warning(
        "agent_response_synthesized",
        extra={
            "schema": type(parsed).__name__,
            "stop_reason": stop_reason.value,
        },
    )
    return synthesized


def _validate_with_degradation(content_obj, schema_model):
    """Validate LLM structured output, degrading gracefully instead of 500ing.

    Parse-time cross-field validators (e.g. ``evidence_to_add.source_file_id``
    is required unless ``USER_DESCRIPTION``; ``evidence_need_updates`` state
    ``FULFILLED`` requires ``fulfilling_evidence_ids``) reject the WHOLE
    response object when a single sub-record is malformed — which 500s the
    turn before any milestone logic runs (the surgical strip can't help: that
    operates post-parse). This is the general never-500 backstop for that
    class (redesign §9 / the deferred "S4" item):

    1. Try to validate as-is.
    2. On failure, PRUNE the specific sub-records the ValidationError points
       at (keyed off the error ``loc`` paths — general, not per-invariant)
       and re-validate: the list entry for a ``loc`` with an index, or, for
       one without, the deepest OPTIONAL sub-object on its path (nulled —
       ``root_cause_conclusion``, ``knowledge_match``, ``milestones``; fm#1502).
       The bad sub-records are quarantined; everything else on the turn
       survives.
    3. If it still fails (an error on no prunable path), drop
       ``state_updates`` entirely and keep the conversational
       ``agent_response`` — the turn survives as a conversational reply
       rather than a 500.
    4. If even that fails, re-raise the original error (truly unrecoverable).

    An out-of-range confidence usually never reaches step 2: the schema's
    validators rescale a percentage, coerce a bool, or drop the field of an
    update-shaped record inside Pydantic (fm#1502). They report through the
    validation context, and ``_account_confidence`` turns the successful
    attempt's reports into the field-level counter, the body's ``repaired``
    outcome and the turn's ``validation_repairs``. Only an unrepairable
    value on an ADD-shaped record raises, and step 2 prunes that record.

    Upstream remains the real fix: provider-native constrained generation so
    the LLM cannot emit the invalid shape ([[project-llm-structured-output-strategy]]).
    This is the backstop, not a per-variant patch.
    """
    from pydantic import ValidationError

    def _record(outcome: str):
        _record_schema_validation(schema_model, outcome)

    def _validate(obj):
        # One attempt, with its OWN repair sink: validators report through
        # the validation context (fm#1502), and an attempt that fails must
        # report nothing — only the attempt whose result is returned counts.
        sink: list[ConfidenceRepair] = []
        parsed = schema_model.model_validate_json(
            json.dumps(obj), context={CONFIDENCE_REPAIRS_CONTEXT_KEY: sink}
        )
        return parsed, sink

    try:
        parsed, repairs = _validate(content_obj)
        # A repair happens INSIDE Pydantic, so this is a first-try success
        # either way — and a body whose confidences were rewritten is not
        # one the model got right. ``repaired`` only when every action kept
        # the model's meaning; a dropped field or a link value set aside for
        # ingest discarded something, which is what ``pruned`` counts.
        _record(
            "clean"
            if not repairs
            else (
                "repaired"
                if all(r.action in MEANING_PRESERVING_ACTIONS for r in repairs)
                else "pruned"
            )
        )
        return _account_confidence(parsed, repairs, None)
    except ValidationError as original_error:
        pruned, dropped, unhandled = _prune_invalid_sub_records(
            content_obj, original_error, schema_model
        )
        if dropped:
            try:
                parsed, repairs = _validate(pruned)
                logger.warning(
                    "structured_output_degraded: pruned invalid sub-record(s) "
                    f"{dropped} from {schema_model.__name__} and continued "
                    "(parse-time validator). Turn preserved.",
                    extra={"schema": schema_model.__name__, "pruned": dropped},
                )
                _record("pruned")
                return _account_confidence(parsed, repairs, original_error)
            except ValidationError:
                pass  # fall through to the conversational fallback

        # What the prune step removed stays removed below: the fallback
        # rungs build on the pruned body, so a record already quarantined
        # outside ``state_updates`` (an ``evidence_trail`` conclusion)
        # cannot come back and fail the rung that drops everything else.
        base = pruned if dropped else content_obj

        # Last resort: keep the response text, drop all structured updates.
        if isinstance(content_obj, dict) and content_obj.get("state_updates"):
            fallback = {**base, "state_updates": {}}
            try:
                parsed, repairs = _validate(fallback)
                # The prune path already logs its locs ("Turn preserved"); this
                # branch is reached only when an error the prune step could
                # not place remains (no list index, no optional sub-object on
                # its path) — log exactly those so each fallback is
                # self-diagnosing (was it correctly non-prunable, or a prune
                # gap?). Reference: S4 backstop observability.
                non_prunable = [
                    (list(e.get("loc", ())), e.get("msg", "")) for e in unhandled
                ]
                logger.warning(
                    "structured_output_degraded: dropped all state_updates from "
                    f"{schema_model.__name__} after an unrepairable validation "
                    f"error — conversational fallback (no 500). "
                    f"Non-prunable errors: {non_prunable}",
                    extra={
                        "schema": schema_model.__name__,
                        "non_prunable_errors": non_prunable,
                    },
                )
                _record("state_dropped")
                return _account_confidence(parsed, repairs, original_error)
            except ValidationError:
                pass

        # Rung: the model omitted the required user-facing agent_response
        # ITSELF (observed on gemini-3.5-flash resolution turns) — the rungs
        # above preserve agent_response and so cannot help. Fill it with ""
        # so a turn whose state_updates are otherwise valid survives
        # instead of 500ing. The conclusion stays the model's own (its
        # state_updates).
        # Fire when agent_response is MISSING or non-string (None, or a
        # malformed 0/[]/false the schema rejects) — i.e. not a usable reply.
        #
        # This rung is STRUCTURAL ONLY and writes no text (#1442). It used
        # to fill a fabricated "I've updated the investigation..." reply,
        # the same one for every cause, because this helper sees only the
        # parsed dict — never the provider's stop reason, which is what
        # says WHY there is no answer. Wording is now chosen one frame up
        # by ``_synthesize_agent_response``, which holds the response
        # envelope. That also REVERSES a decision this comment used to
        # record ("a model-provided "" is never overwritten"): an empty
        # answer is now named there, keyed on the stop reason, together
        # with the missing one this rung blanks — deliberately, because the
        # engine is the layer that can say why, and the service's blind
        # backstop is not where empty answers should be named.
        if isinstance(content_obj, dict) and not isinstance(
            content_obj.get("agent_response"), str
        ):
            placeholder = ""
            # Prefer keeping the model's state_updates; only DROP them as a
            # last resort — and say so, so a state-update loss is never logged
            # as a mere field-fill.
            for state_dropped, candidate in (
                (False, base),
                (True, {**base, "state_updates": {}}),
            ):
                try:
                    patched = {**candidate, "agent_response": placeholder}
                    parsed, repairs = _validate(patched)
                    logger.warning(
                        "structured_output_degraded: blanked missing "
                        f"agent_response on {schema_model.__name__} (model "
                        "omitted the required user-facing field)"
                        + (
                            " AND dropped all state_updates (unrepairable)"
                            if state_dropped
                            else ""
                        )
                        + " — turn preserved, no 500.",
                        extra={
                            "schema": schema_model.__name__,
                            "state_updates_dropped": state_dropped,
                        },
                    )
                    _record(
                        "response_synthesized_state_dropped"
                        if state_dropped
                        else "response_synthesized"
                    )
                    return _account_confidence(parsed, repairs, original_error)
                except ValidationError:
                    continue

        _record("failed")
        raise original_error


def _prune_invalid_sub_records(content_obj, error, schema_model=None):
    """Remove the sub-records a ValidationError flags.

    Returns ``(obj, pruned_paths, unhandled_errors)``.

    - A ``loc`` carrying a list index — ``('state_updates',
      'evidence_to_add', 0, 'source_file_id')`` or ``(..., 0)`` — prunes the
      entry at the DEEPEST index. General across any list field.
    - A ``loc`` with no index is placed on the deepest OPTIONAL sub-object
      along its path, read from ``schema_model``, and that sub-object is
      set to ``None`` — ``('state_updates', 'root_cause_conclusion',
      'likelihood')`` nulls ``root_cause_conclusion`` (fm#1502). Absence is
      what an optional sub-object means when the model has nothing to say,
      so this costs that sub-object and nothing else, where the next rung
      would drop every ``state_updates``. A required object (``state_updates``
      itself) or a non-object field (``outcome``) is never nulled: the error
      is returned as unhandled and falls through as before.

    Without ``schema_model`` only list entries are pruned.
    """
    import copy

    obj = copy.deepcopy(content_obj)
    to_remove: dict[tuple, set] = {}
    to_null: set[tuple] = set()
    unhandled: list[dict] = []
    for err in error.errors():
        loc = tuple(err.get("loc", ()))
        int_positions = [i for i, part in enumerate(loc) if isinstance(part, int)]
        if int_positions:
            last = int_positions[-1]
            list_path = loc[:last]
            to_remove.setdefault(list_path, set()).add(loc[last])
            continue
        prefix = (
            _optional_sub_record_prefix(schema_model, loc)
            if schema_model is not None
            else None
        )
        if prefix is None:
            unhandled.append(err)
        else:
            to_null.add(prefix)

    dropped: list[str] = []
    for list_path, indices in to_remove.items():
        node = obj
        ok = True
        for key in list_path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            for idx in sorted(indices, reverse=True):
                if 0 <= idx < len(node):
                    del node[idx]
                    path_str = ".".join(str(p) for p in list_path)
                    dropped.append(f"{path_str}[{idx}]")

    # Shortest first, so a sub-object inside one already nulled is skipped
    # (its parent is None by then) rather than reported twice.
    for path in sorted(to_null, key=len):
        parent = obj
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and parent.get(path[-1]) is not None:
            parent[path[-1]] = None
            dropped.append(".".join(str(p) for p in path))
    return obj, dropped, unhandled


def _optional_sub_record_prefix(schema_model, loc) -> Optional[tuple]:
    """The deepest prefix of ``loc`` naming an ``Optional[BaseModel]`` field.

    Walks the field annotations from ``schema_model`` down the string parts
    of ``loc``; stops at the first part that is not a model field or whose
    type is not a model. ``None`` when no optional sub-object lies on the
    path.

    Reads resolved annotations only: a quoted forward reference pydantic
    left unresolved would hide the sub-object it names, and the error would
    fall through to the drop-all rung. ``test_confidence_repair_1502``'s
    census fails if any field reachable from an engine schema carries one.
    """
    import types
    import typing

    from pydantic import BaseModel

    model = schema_model
    best: Optional[tuple] = None
    for depth, part in enumerate(loc):
        fields = getattr(model, "model_fields", None)
        if not isinstance(part, str) or not fields or part not in fields:
            break
        annotation = fields[part].annotation
        nullable = False
        if typing.get_origin(annotation) in (typing.Union, types.UnionType):
            args = typing.get_args(annotation)
            members = [a for a in args if a is not type(None)]
            nullable = len(members) < len(args)
            annotation = members[0] if len(members) == 1 else None
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            break
        if nullable:
            best = tuple(loc[: depth + 1])
        model = annotation
    return best


def _account_confidence(parsed, repairs, original_error):
    """Make the confidence actions behind ``parsed`` observable (fm#1502).

    ``repairs`` are what the successful attempt's validators reported;
    ``original_error``, when the ladder degraded, carries the unrepairable
    ADD-shaped values whose records the ladder pruned. Each action is
    counted on ``faultmaven_schema_field_repairs_total`` and kept on the
    response for the apply step to write onto the turn's
    ``validation_repairs``. A link value set aside for ingest is neither:
    ingest counts what it decides.
    """
    kept = [r for r in repairs if r.action is not ConfidenceAction.SET_ASIDE]
    if original_error is not None:
        for err in original_error.errors():
            if err.get("type") != CONFIDENCE_UNREPAIRABLE:
                continue
            ctx = err.get("ctx") or {}
            kept.append(
                ConfidenceRepair(
                    schema=str(ctx.get("schema", "?")),
                    field=str(ctx.get("field", "?")),
                    action=ConfidenceAction.PRUNED,
                    raw=err.get("input"),
                    where=".".join(str(p) for p in err.get("loc", ())),
                )
            )
    if not kept:
        return parsed
    for repair in kept:
        count_confidence_repair(repair)
    logger.warning(
        "structured_output_confidence_repaired",
        extra={
            "schema": type(parsed).__name__,
            "repairs": [repair.note() for repair in kept],
        },
    )
    if "_confidence_repairs" in getattr(type(parsed), "__private_attributes__", {}):
        parsed._confidence_repairs = kept
    return parsed


def _log_dropped_fields(
    raw: Any,
    parsed: Any,
    schema_model: Any,
) -> None:
    """Log when the LLM emitted top-level or state_updates fields that
    the schema doesn't accept (and thus silently dropped). One log line
    per dropped field — feed observability/quarterly review.

    TODO: walk depth limited to top-level + state_updates. Drops nested
    deeper (e.g., state_updates.hypotheses_to_add[].some_unknown_field)
    are invisible. Generalize to recursive descent if state schemas
    grow more nested or if the runtime signal misses real drift.
    """
    try:
        top_known = set(getattr(schema_model, "model_fields", {}).keys())
        if isinstance(raw, dict):
            top_dropped = [k for k in raw.keys() if k not in top_known]
            for k in top_dropped:
                logger.warning(
                    "structured_output_dropped_field",
                    extra={
                        "schema": schema_model.__name__,
                        "level": "top",
                        "field": k,
                    },
                )

            # Walk one level into state_updates (the most common drop site).
            state_updates = raw.get("state_updates")
            if isinstance(state_updates, dict):
                su_field = getattr(schema_model, "model_fields", {}).get(
                    "state_updates"
                )
                su_schema = getattr(su_field, "annotation", None) if su_field else None
                su_known = (
                    set(getattr(su_schema, "model_fields", {}).keys())
                    if su_schema
                    else set()
                )
                if su_known:
                    for k in state_updates.keys():
                        if k not in su_known:
                            logger.warning(
                                "structured_output_dropped_field",
                                extra={
                                    "schema": getattr(su_schema, "__name__", "?"),
                                    "level": "state_updates",
                                    "field": k,
                                },
                            )
    except Exception:
        # Logging must never break the response path.
        logger.debug("dropped-field detection failed", exc_info=True)


def _parse_text_as_schema(
    text: str,
    schema_model: Any,
) -> BaseInteractionResponse:
    """Parse free-form LLM text as a schema instance.

    Last-resort path used when a provider ignores tool_choice=required and
    emits the structured response inline as text (often wrapped in a
    ```json fence). Mirrors the markdown stripping + nested-JSON +
    enum-fix logic in _generate_structured_output's single-shot path.

    Raises ValueError if the parsed object is structurally valid but
    semantically empty (e.g., agent_response blank). This guards against
    false positives where prose happens to embed a JSON block that fits
    the schema but doesn't represent a real response — those should
    escalate to the non-tool fallback path, not be returned as-is.
    """
    content_obj = loads_llm_json(text)
    content_obj = _parse_nested_json(content_obj)
    _su = content_obj.get("state_updates") if isinstance(content_obj, dict) else None
    if isinstance(content_obj, dict) and (_su is None or isinstance(_su, str)):
        content_obj["state_updates"] = {}
    schema_dict = schema_model.model_json_schema()
    content_obj = _fix_enum_violations(
        content_obj,
        schema_dict,
        root_defs=schema_dict.get("$defs"),
    )
    parsed = _validate_with_degradation(content_obj, schema_model)
    _log_dropped_fields(content_obj, parsed, schema_model)

    # Semantic guard: agent_response is the user-facing payload of every
    # BaseInteractionResponse subclass. An empty value means the recovered
    # JSON was structurally valid but contained no actual response — most
    # likely we picked up an example block from the LLM's prose. Reject
    # it so the caller escalates to the non-tool fallback path instead of
    # surfacing an empty bubble to the user.
    agent_response = getattr(parsed, "agent_response", None)
    if not agent_response or not str(agent_response).strip():
        raise ValueError(
            "parsed schema has empty agent_response — likely a prose-embedded "
            "JSON example, not a real response"
        )
    return parsed


def _parse_nested_json(obj):
    """Recursively parse JSON strings in a dict/list structure."""
    if isinstance(obj, dict):
        return {k: _parse_nested_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_parse_nested_json(item) for item in obj]
    elif isinstance(obj, str):
        try:
            parsed = json.loads(obj)
            return _parse_nested_json(parsed)
        except (json.JSONDecodeError, TypeError):
            # Fireworks/DeepSeek V3 leaks XML tool-call format artifacts.
            # Apply two repair passes before giving up:
            #
            # Pass 1: strip trailing XML closing tags (e.g. </parameter></invoke>)
            # Pass 2: for JSON containers, truncate at the last valid terminator
            #         to handle stray closing braces/brackets (e.g. "[...]}")
            stripped_obj = obj.strip()

            # Pass 1 — XML closing tags
            stripped = re.sub(r"(\s*</\w+>)+\s*$", "", stripped_obj)
            if stripped != stripped_obj:
                try:
                    parsed = json.loads(stripped)
                    return _parse_nested_json(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Pass 2 — truncate at last valid JSON container terminator
            if stripped_obj:
                first_ch = stripped_obj[0]
                search_ch = "]" if first_ch == "[" else "}" if first_ch == "{" else None
                if search_ch:
                    last_pos = stripped_obj.rfind(search_ch)
                    if last_pos > 0:
                        candidate = stripped_obj[: last_pos + 1]
                        if candidate != stripped_obj:
                            try:
                                parsed = json.loads(candidate)
                                return _parse_nested_json(parsed)
                            except (json.JSONDecodeError, TypeError):
                                pass

            return obj
    else:
        return obj


def _fix_enum_violations(obj, schema_dict, root_defs=None):
    """Recursively fix enum violations in the response object."""

    if not isinstance(obj, dict):
        return obj

    properties = schema_dict.get("properties", {})
    if root_defs is None:
        root_defs = schema_dict.get("$defs", {})
    local_defs = schema_dict.get("$defs", {})
    all_defs = {**local_defs, **root_defs}

    fixed_obj = {}
    for key, value in obj.items():
        if key not in properties:
            fixed_obj[key] = value
            continue

        prop_schema = properties[key]

        if "enum" in prop_schema and isinstance(value, str):
            valid_values = prop_schema["enum"]
            if value not in valid_values:
                closest_match = difflib.get_close_matches(
                    value, valid_values, n=1, cutoff=0.6
                )
                if closest_match:
                    corrected = closest_match[0]
                    logger.warning(
                        f"Auto-correcting hallucinated enum value: "
                        f"'{value}' -> '{corrected}' for field '{key}'"
                    )
                    fixed_obj[key] = corrected
                else:
                    fallback = valid_values[0]
                    logger.warning(
                        f"No close match for hallucinated enum '{value}', "
                        f"using fallback '{fallback}' for field '{key}'"
                    )
                    fixed_obj[key] = fallback
            else:
                fixed_obj[key] = value

        elif isinstance(value, dict):
            nested_schema = None
            if "$ref" in prop_schema:
                ref_name = prop_schema["$ref"].split("/")[-1]
                nested_schema = all_defs.get(ref_name, {})
            elif "anyOf" in prop_schema:
                for option in prop_schema["anyOf"]:
                    if "$ref" in option:
                        ref_name = option["$ref"].split("/")[-1]
                        nested_schema = all_defs.get(ref_name, {})
                        break
                    elif option.get("type") != "null":
                        nested_schema = option
                        break
            elif "properties" in prop_schema:
                nested_schema = prop_schema

            if nested_schema:
                fixed_obj[key] = _fix_enum_violations(value, nested_schema, root_defs)
            else:
                fixed_obj[key] = value

        elif isinstance(value, list):
            fixed_list = []
            item_schema = None
            if "items" in prop_schema:
                if "$ref" in prop_schema["items"]:
                    ref_name = prop_schema["items"]["$ref"].split("/")[-1]
                    item_schema = all_defs.get(ref_name, {})
                else:
                    item_schema = prop_schema["items"]
            elif "anyOf" in prop_schema:
                for option in prop_schema["anyOf"]:
                    if option.get("type") == "array" and "items" in option:
                        if "$ref" in option["items"]:
                            ref_name = option["items"]["$ref"].split("/")[-1]
                            item_schema = all_defs.get(ref_name, {})
                        else:
                            item_schema = option["items"]
                        break

            for item in value:
                if isinstance(item, dict) and item_schema:
                    fixed_list.append(
                        _fix_enum_violations(item, item_schema, root_defs)
                    )
                else:
                    fixed_list.append(item)
            fixed_obj[key] = fixed_list

        else:
            fixed_obj[key] = value

    return fixed_obj
