"""Assembling the API-facing TurnResponse from a completed turn: suggested actions, cause assurance, KB sources, and progress transparency."""

import logging
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Optional,
)

from faultmaven.core.investigation.turn_pipeline import submitted_name
from faultmaven.models.api import Source
from faultmaven.models.api_models import (
    AttachmentResult,
    ProgressTransparencyInfo,
    SuggestedActionResponse,
    TurnResponse,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _published_source_type,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_KB_SOURCES,
    Case,
    VerificationStatus,
)

logger = logging.getLogger(__name__)


def _build_turn_response(
    *,
    agent_response_text,
    case_id,
    clarification,
    payload,
    preprocess_results,
    raw_follow_ups,
    turn_meta,
    updated_case,
    uploaded_files_this_turn,
):
    """Assemble and return the TurnResponse (suggested actions, cause assurance, sources, progress transparency)."""
    suggested_actions = [
        SuggestedActionResponse(
            label=f["label"],
            type=f["action_type"],
            payload=f.get("payload"),
            body=f.get("body"),
            hints=f.get("hints"),
            intent=f.get("intent"),
        )
        for f in raw_follow_ups
    ]

    # Prepend classification-clarification suggestions (built at 3b)
    # when this turn's attachment hit classification_failed.
    # User-in-the-loop guidance takes priority over generic
    # follow-up suggestions from the engine.
    if clarification:
        suggested_actions = clarification + suggested_actions

    # Read-time assurance grade for narration-only clients (#572/INV-28):
    # present whenever the case has stated a root cause, recomputed from
    # the causal graph so a resolution turn (which never recomputes the
    # persisted progress field) still carries the true grade beside the
    # cause claim the LLM wrote into agent_response.
    turn_cause_assurance = None
    turn_cause_overclaim = None
    if updated_case.root_cause_conclusion is not None:
        from faultmaven.core.investigation.cause_assurance import (
            conclusion_overclaims,
            grade_cause_assurance,
        )

        _grade = grade_cause_assurance(updated_case)
        turn_cause_assurance = _grade.value
        turn_cause_overclaim = conclusion_overclaims(
            updated_case.root_cause_conclusion, _grade
        )

    # Which runbooks this turn's prompt carried (fm#1361), as the save
    # recorded them on the assistant row (``_record_turn_kb_sources``) — one
    # list, so the live response and history cannot disagree. Not
    # ``updated_case.kb_context``: both pre-fetch triggers fire during
    # response application, AFTER the answer was generated, so their hits
    # are first in front of the model on the next turn.
    turn_sources = [
        Source.model_validate(source)
        for source in turn_meta.get(MESSAGE_METADATA_KB_SOURCES) or []
    ]

    response = TurnResponse(
        agent_response=agent_response_text,
        turn_number=updated_case.current_turn,
        investigation_turn=updated_case.investigation_turn_count,
        milestones_completed=turn_meta.get("milestones_completed", []),
        case_state=updated_case.state,
        progress_made=turn_meta.get("progress_made", False),
        attachments_processed=[
            AttachmentResult(
                file_id=res.uploaded_file.file_id,
                # The chip the Copilot renders on the very turn
                # the user pasted — #666's most immediate surface.
                # ``file_id`` is the documented handle the frontend
                # references an attachment by, so this field is
                # display-only.
                #
                # ``submitted_name``, not ``uploaded_file.display_name``:
                # dedup matches on content_hash ALONE, so the row can
                # be one the user named differently on an earlier turn,
                # and naming the chip from it reports a filename they
                # never sent.
                filename=submitted_name(att.filename, res.uploaded_file),
                # Published as the 6-valued vocabulary (see the
                # field's description), so folded at the read
                # boundary: the row may hold either one (#583).
                source_type=_published_source_type(res.uploaded_file),
                file_size=res.uploaded_file.size_bytes,
                processing_status=("duplicate" if res.duplicate_of else "completed"),
                uploaded_at=datetime.now(timezone.utc).isoformat(),
                upload_source=res.uploaded_file.upload_source,
                duplicate_of=res.duplicate_of,
                duplicate_turn=res.duplicate_turn,
            )
            for att, res in zip(payload.attachments, preprocess_results)
        ],
        suggested_actions=suggested_actions,
        progress_transparency=_build_progress_transparency(turn_meta, updated_case),
        sources=turn_sources,
        cause_assurance=turn_cause_assurance,
        cause_overclaim=turn_cause_overclaim,
    )

    logger.info(
        f"Processed turn {response.turn_number} for case {case_id}, "
        f"status={response.case_state}, milestones={len(response.milestones_completed)}, "
        f"attachments={len(uploaded_files_this_turn)}, messages={updated_case.message_count}"
    )

    return response


def _build_progress_transparency(
    metadata: Dict[str, Any], case: "Case"
) -> Optional[ProgressTransparencyInfo]:
    """Build ProgressTransparencyInfo from turn metadata.

    ``verification_status`` carries the engine's persisted assessment for
    the turn (the grounding × progress join) so the frontend can surface the
    honest partial outcome — e.g. ``insufficient_evidence`` — alongside the
    stalled-milestone info.

    Emitted when transparent mode is active (stalled-milestone surfacing)
    **or** when the status is one of the honest-partial readings. The latter
    is decoupled from ``progress_transparent`` on purpose: a declared data
    wall reaches ``INSUFFICIENT_EVIDENCE`` *before* the time-stall thresholds
    that drive transparent mode, so gating the status on that flag would hide
    the very outcome the frontend needs to show. ``active`` still reflects
    transparent mode only.
    """
    verification_status = None
    cause_assurance = None
    if case.progress:
        if case.progress.verification_status:
            verification_status = case.progress.verification_status.value
        if case.progress.cause_assurance:
            cause_assurance = case.progress.cause_assurance.value

    transparent = bool(metadata.get("progress_transparent"))
    # Every engine-driven honest-partial reading is surfaced independently of
    # transparent mode, for the same reason: each can be reached on a turn
    # that never activates it, and gating on the flag would hide the outcome
    # the frontend exists to show. ``INSUFFICIENT_EVIDENCE`` via the declared
    # data wall (which fires before the time thresholds); ``TREATMENT_BLOCKED``
    # (#1136) because a case parked on an unapplied fix is conversational —
    # transparent mode counts investigative turns, so a fix-blocked stall can
    # sit in that cell for turns on end without ever tripping it;
    # ``RESTATEMENT_HELD`` (#1195) because it is carved OUT of
    # ``INSUFFICIENT_EVIDENCE`` — omitting it would silence, in this channel,
    # exactly the cases that channel used to (wrongly) report, which is the
    # suppression-without-replacement failure that fix exists to avoid.
    surface_honest_partial = verification_status in (
        VerificationStatus.INSUFFICIENT_EVIDENCE.value,
        VerificationStatus.TREATMENT_BLOCKED.value,
        VerificationStatus.RESTATEMENT_HELD.value,
    )
    if not transparent and not surface_honest_partial:
        return None

    return ProgressTransparencyInfo(
        active=transparent,
        pending_milestone=metadata.get("pending_milestone"),
        milestone_description=metadata.get("milestone_description"),
        repair_type=metadata.get("stagnation_type"),
        verification_status=verification_status,
        cause_assurance=cause_assurance,
    )
