"""The one seam every reclassification path crosses.

Owns the mechanics shared by the clarification-intent handler and the
``reclassify_evidence`` escape hatch: folding a re-extraction's classification
verdict and coverage window onto the backing ``UploadedFile``, deciding
whether an ``Evidence`` row's coverage was inherited from the file (and so
must move with it), and reconciling both of a case's collections — file row,
every Evidence row backed by it, and the retired clarification — to one
file's reclassification (#1470/#1471).
"""

import copy
import logging
from typing import Any, Dict, List, NamedTuple, Optional

from faultmaven.core.investigation.coverage_trust import CALLER_DECLARED_COVERAGE_SOURCE
from faultmaven.core.investigation.milestone_engine.milestone_inference import (
    _evidence_coverage,
)
from faultmaven.core.investigation.suggestion_liveness import (
    drop_clarifications_for_file,
)
from faultmaven.core.investigation.turn_uploads import report_turn_uploads
from faultmaven.core.preprocessing.models import unified_data_type_of
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_RECLASSIFICATION_TOTAL,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service.attachments import (
    _binary_placeholder,
    _is_binary_content,
)
from faultmaven.modules.agent.domain.services.investigation_service.clarification import (
    _CLARIFICATION_FRIENDLY_NAMES,
    _upload_subject,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _infer_source_type,
)
from faultmaven.modules.case.contracts import (
    Case,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceSourceType,
    UploadedFile,
)

logger = logging.getLogger(__name__)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)


def _classification_block(preprocessing_result) -> Optional[Dict[str, Any]]:
    """The re-extraction's ``classification`` verdict, if it published one.

    Lifted from ``extraction_metadata["evidence_metadata"]["classification"]``
    — the same block ``reclassify_evidence`` already reads for the addressed
    row, read once here because it is a FILE-level fact that applies to every
    row behind the file (see :func:`_reclassified_collections`).

    Returns ``None`` when the result carries no such block, which is the
    signal to leave every row's existing metadata alone rather than to write
    an empty verdict over a real one.
    """
    pp_metadata = getattr(preprocessing_result, "extraction_metadata", None)
    if not isinstance(pp_metadata, dict):
        return None
    evidence_metadata = pp_metadata.get("evidence_metadata")
    if not isinstance(evidence_metadata, dict):
        return None
    classification = evidence_metadata.get("classification")
    return classification if isinstance(classification, dict) else None


def _refreshed_coverage(
    file_meta: "UploadedFile",
    preprocessing_result,
) -> Dict[str, Any]:
    """The coverage fields a re-extraction supersedes, as a patch (#1471).

    Separate from the caller only so the ``update=`` dict there stays a
    literal: the fm#918 scan that states how many places write
    ``UploadedFile.data_type`` matches ``model_copy(update={...})`` by
    literal key, and declares a patch dict built in a variable as a shape it
    cannot see. Building this part here and spreading it keeps that guard
    pointed at the writer it is watching.

    Three fields move together or not at all. ``coverage_source`` is not
    decoration on the span — it is what ``coverage_trust`` reads to decide
    whether the instant may be STATED at all — so a window refreshed under
    the previous extractor's provenance label would be a worse row than the
    stale window it replaced.
    """
    start_ts = getattr(preprocessing_result, "coverage_start_ts", None)
    end_ts = getattr(preprocessing_result, "coverage_end_ts", None)
    if start_ts is not None or end_ts is not None:
        # Parsed content wins, and the span carries its own provenance. Both
        # ends are taken as the extractor reported them, half-open span
        # included: that is what intake does with the same object, and a
        # normalisation applied on only one of the two writers would make a
        # reclassified row differ from a freshly ingested one.
        return {
            "coverage_start_ts": start_ts,
            "coverage_end_ts": end_ts,
            "coverage_source": getattr(preprocessing_result, "coverage_source", None),
        }
    if file_meta.coverage_source == CALLER_DECLARED_COVERAGE_SOURCE:
        # Not extractor output, so a re-extraction cannot refute it: the
        # instant is a forwarding client's statement about when it SAW the
        # content. Kept. Intake's own precedence is the same one — parsed
        # content wins, ``observed_at`` is the fallback — and clearing here
        # would delete the only temporal signal an alert notification has.
        return {}
    # The new extractor found nothing and what is on the row came from the
    # OLD one. Clearing is the honest read: every consumer treats a present
    # span as fact, so a window nothing supports any more is worse than none.
    return {
        "coverage_start_ts": None,
        "coverage_end_ts": None,
        "coverage_source": None,
    }


def _file_row_with_reclassification(
    file_meta: "UploadedFile",
    preprocessing_result,
) -> "UploadedFile":
    """UploadedFile row updated with re-extracted preprocessing artifacts.

    Post-010 routing: data_type / summary / structural_index describe the
    FILE and live on ``uploaded_files``; Evidence rows carry only the
    LLM-authored claim. Reached through :func:`_reclassified_collections`,
    which is what both reclassification paths call.

    **The coverage window moves with the extraction that produced it**
    (#1471). Reclassification re-runs extraction under a different
    ``DataType``, so a different extractor parses a different timestamp
    range; leaving the window put described the file by the extractor it had
    just replaced — a ``structured_config`` → ``logs_and_errors`` file kept
    ``coverage_start_ts = None`` and contributed nothing to timeline
    assembly, and a ``logs_and_errors`` → ``code`` file kept a log window it
    cannot have. ``coverage_source`` moves with the span rather than being
    left behind: it is what ``coverage_trust`` reads to decide whether the
    instant may be STATED, so a refreshed window under a stale provenance
    label would be a worse row than the stale window was.

    The one value a re-extraction may not overwrite is a ``caller_declared``
    span. That instant was never read out of the content — it is the
    forwarding client's statement about when it SAW the content, seeded at
    intake precisely because the content parsed to nothing. Re-parsing the
    same bytes under a different data type cannot refute it, so it is kept
    when the new extraction yields no window of its own. This mirrors
    intake's own precedence, where parsed content always wins and
    ``observed_at`` is the fallback — applied here in the same order.
    """
    return file_meta.model_copy(
        update={
            # The fine-grained ``DataType``, not the 6-valued projection
            # (#583): lossless, and what the reclassification was keyed on.
            # Readers go through ``unified_data_type_of``, which accepts both
            # vocabularies, so rows written before #583 need no migration.
            "data_type": preprocessing_result.detailed_data_type.value,
            "summary": preprocessing_result.summary,
            "structural_index": preprocessing_result.structural_index,
            **_refreshed_coverage(file_meta, preprocessing_result),
        },
        deep=True,
    )


def _span_was_inherited_from(evidence, file_row) -> bool:
    """True when *evidence*'s stored coverage is a restatement of *file_row*'s.

    ``milestone_engine._evidence_coverage`` gives a new row the FILE's span
    when the file's is a single instant, provenance included. Such a row is
    not making a claim of its own — it is repeating the file's — so when the
    file's span moves, it must move with it. A row that parsed its own span
    from its extract IS making a claim of its own, and reclassifying the file
    does not touch the extract that claim rests on.

    The test is equality of the whole triple against the file row as it stood
    before the reclassification, plus the point-span condition that is the
    only shape ever inherited. A row that independently parsed exactly the
    file's instant is indistinguishable from one that inherited it, and is
    treated as inherited: the two agreed about the same instant, and no
    cheaper discriminator exists that does not re-parse — which is the thing
    that must not happen here (see the caller).
    """
    start = getattr(evidence, "coverage_start_ts", None)
    if start is None or start != getattr(evidence, "coverage_end_ts", None):
        return False
    return (
        start,
        getattr(evidence, "coverage_end_ts", None),
        getattr(evidence, "coverage_source", None),
    ) == (
        getattr(file_row, "coverage_start_ts", None),
        getattr(file_row, "coverage_end_ts", None),
        getattr(file_row, "coverage_source", None),
    )


def _reclassified_collections(
    case: "Case",
    file_id: str,
    preprocessing_result,
    new_source_type: EvidenceSourceType,
) -> "tuple[list[UploadedFile], list[Evidence], Optional[list[dict[str, Any]]]]":
    """Both of a case's collections re-aligned to one file's reclassification.

    The single seam every reclassification crosses. Returns a THREE-tuple,
    ``(uploaded_files, evidence, last_suggestions)`` — the file row rebuilt
    from the re-extraction, **every** Evidence row backed by that file
    re-aligned, and the stored suggestions with the question this answers
    retired. Neither input collection is mutated.

    Hoisted because the two paths had re-aligned Evidence differently
    (#1470). The turn seam (``_handle_file_reclassification``) looped every
    row with the same ``source_file_id``; the out-of-band path
    (``reclassify_evidence``) addressed exactly one. So a file backing two
    Evidence rows — the ordinary case once the LLM has anchored two claims on
    one upload — came out of ``PATCH /evidence/{id}/classification``
    described by two contradictory source types, with nothing to reconcile
    them. One file has one classification; how many claims cite it is not a
    property of the classification. Making that structural is what stops the
    two paths agreeing only for as long as both remember to.

    Claim content is untouched: an Evidence row's LLM-authored ``summary``
    and ``extract`` are what it ASSERTS, and reclassifying the file it was
    read from does not rewrite the assertion. What IS re-aligned is
    everything a row holds that is a restatement of a FILE-level fact, and
    the file-level facts are what just changed — ``source_type``, the
    coverage window a row INHERITED from the file row, and the classifier's
    ``classification`` verdict. Each is justified at its own line below.

    Returns a third value, the ``last_suggestions`` list with the
    clarification this reclassification ANSWERS retired, so that retirement
    cannot be got right by one caller and forgotten by another.
    """
    new_files_list = list(case.uploaded_files or [])
    file_index = next(
        (i for i, uf in enumerate(new_files_list) if uf.file_id == file_id),
        None,
    )
    if file_index is None:
        # REFUSED, not partially applied. Both callers resolve the row before
        # they get here, so this is unreachable today — but it is unreachable
        # inside the one function designated as the single seam, and the
        # failure it would otherwise produce is precisely the one the seam
        # exists to prevent: the Evidence loop below would re-align every row
        # to a classification that no file row records, manufacturing the
        # contradiction rather than reconciling it.
        raise NotFoundError("UploadedFile", file_id)
    new_files_list[file_index] = _file_row_with_reclassification(
        new_files_list[file_index], preprocessing_result
    )

    # The rows are re-derived against the case as it will be AFTER the file
    # row moves, because an Evidence row's own coverage was INHERITED from
    # that row (see below). ``previous_file_row`` is what it looked like
    # BEFORE, which is how a row that inherited is told apart from one that
    # parsed its own span.
    previous_file_row = case.uploaded_files[file_index]
    reclassified_view = case.model_copy(update={"uploaded_files": new_files_list})
    new_classification = _classification_block(preprocessing_result)

    new_evidence_list = list(case.evidence or [])
    for i, ev in enumerate(new_evidence_list):
        if ev.source_file_id != file_id:
            continue
        update: Dict[str, Any] = {"source_type": new_source_type}

        # #1471, the Evidence half. ``milestone_engine._evidence_coverage``
        # copies the FILE's span AND its provenance onto a row at creation
        # when the file span is a single instant, so a row born that way
        # asserts an instant the reclassified file no longer supports —
        # ``symptom_currency`` reads ``ev.coverage_end_ts`` with
        # ``is_vouched(ev.coverage_source)`` for staleness. The same argument
        # that clears the file window applies
        # verbatim: a window nothing supports any more is worse than none.
        #
        # ONLY a row that INHERITED its span from the file follows the file.
        # A row that parsed its own span from its extract keeps it: that span
        # is more authoritative than the file's, and reclassification does not
        # change the row's extract.
        #
        # Which is which is decided by comparing the stored triple against the
        # file row as it stood BEFORE this reclassification, rather than by
        # re-parsing the extract. Re-parsing would be the obvious way and it is
        # wrong: ``extract_time_range_ts`` INVENTS the year for
        # ``syslog_bsd_noyear`` from ``datetime.now()``, so the same bytes
        # parse to a different instant on a different day. Measured — a row
        # written on 2026-12-10 from ``Dec 15 03:00:00`` stores 2025-12-15, and
        # re-parsing it on 2026-12-20 yields 2026-12-15: a 365-day jump on an
        # operation about data TYPE, applied to every sibling at once, on a
        # field ``symptom_currency`` reads as fact. Nothing here may move an
        # instant the user did not ask to move.
        #
        # The new value comes from ``_evidence_coverage`` with NO extract, so
        # it is that function's file rule (resolution order 2) exactly, taken
        # from the function that owns it rather than restated here — and with
        # no parse, so it cannot depend on the clock either.
        if _span_was_inherited_from(ev, previous_file_row):
            start_ts, end_ts, coverage_source = _evidence_coverage(
                reclassified_view, ev.source_file_id, None
            )
            update["coverage_start_ts"] = start_ts
            update["coverage_end_ts"] = end_ts
            update["coverage_source"] = coverage_source

        # The classifier's verdict is a FILE-level fact, and it changed for
        # every row behind the file — not just the addressed one. Left stale,
        # a sibling keeps the intake confidence and ``_confidence_marker``
        # still renders ``confidence="low"`` with "treat the file extract as
        # tentative" for a row the user has just corrected, which the prompt
        # then acts on by re-asking. ``extractor`` is NOT fanned out: its
        # ``attempts`` trail records what was asked of THAT row, and stamping
        # a request nobody made onto a neighbour would make the trail lie.
        if new_classification is not None:
            merged = dict(ev.metadata or {})
            # ‼ DEEP-COPIED PER ROW. ``model_copy(update=..., deep=True)`` does
            # NOT deep-copy the update VALUES — pydantic v2 applies them with
            # ``copied.__dict__.update(update)`` AFTER the deepcopy — so
            # handing the same dict to every row makes them share one mutable
            # object, and that object is a live reference into
            # ``preprocessing_result``. Measured before this copy: mutating one
            # sibling's confidence changed every other sibling's AND the
            # PreprocessingResult's. The ``deep=True`` below reads as a
            # guarantee it does not provide.
            merged["classification"] = copy.deepcopy(new_classification)
            update["metadata"] = merged

        new_evidence_list[i] = ev.model_copy(update=update, deep=True)

    # The question this reclassification ANSWERS is retired here too, so a
    # caller cannot get the collections right and the retirement wrong. The
    # turn seam reaches the same end state a second way — it hands
    # ``metadata["file_reclassified"]["file_id"]`` back to ``process_turn``,
    # which passes it as ``resolved_file_id`` to
    # ``_carry_forward_unresolved_clarifications``. Measured: that filter's
    # predicate (``is_clarification_entry(entry) and entry_file_id(entry) !=
    # resolved_file_id``) is the exact complement of the one here, so the two
    # agree and running both is idempotent. Doing it here as well is what
    # makes it true for a THIRD caller that never touches the turn loop —
    # which is the same argument the collections were hoisted on.
    retired_suggestions = drop_clarifications_for_file(case.last_suggestions, file_id)

    return new_files_list, new_evidence_list, retired_suggestions


async def _handle_file_reclassification(
    file_storage_service,
    preprocessing_service,
    case: "Case",
    file_id: Optional[str],
    data_type_value: Optional[str],
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve a classification_failed upload by reclassifying its file.

    Engine-owned resolution for the classification-clarification
    suggestions (see ``_build_classification_clarification``):
    the user's click/typed choice arrives as a ``file_reclassification``
    intent carrying the UploadedFile ID and the target DataType. The
    handler re-runs preprocessing under ``user_override`` and updates the
    file's artifacts — mechanically, with no LLM call, so the choice can
    never be misread as an analysis request.

    Post-010: preprocessing artifacts (data_type, summary,
    structural_index) land on the UploadedFile. Any Evidence rows already
    backed by this file get their ``source_type`` re-aligned; usually
    there are none at clarification time (Evidence is born later, during
    INVESTIGATING).

    Returns:
        Result dict with deterministic agent response and updated case.

    Raises:
        ValidationException: terminal case, missing/unknown intent
            fields, or the file has no stored raw bytes to re-extract
            (→ 422).
        NotFoundError: file_id not in this case, or the stored blob is
            gone from storage (→ 404).
        ServiceException: storage/preprocessing service unavailable
            (→ 500).
    """
    # Terminal guard. The other SERVICE intents inherit terminal
    # protection by delegating to engine.process_turn (which
    # short-circuits terminal cases to Q&A); this handler never reaches
    # the engine, so it must refuse mutation itself — a stale
    # clarification button or a direct POST must not rewrite a closed
    # case's files/evidence.
    _refuse_if_terminal(case, "files")
    if not file_id or not data_type_value:
        raise ValidationException(
            "file_id and data_type required for file_reclassification intent",
            {"field": "file_id" if not file_id else "data_type"},
        )
    try:
        data_type = DataType(data_type_value)
    except ValueError:
        valid = ", ".join(t.value for t in DataType)
        raise ValidationException(
            f"Unknown data_type '{data_type_value}'. Valid: {valid}",
            {"field": "data_type"},
        )

    logger.info(
        f"Processing file reclassification: {file_id} → {data_type.value} "
        f"for case {case.case_id}"
    )

    file_index = next(
        (i for i, uf in enumerate(case.uploaded_files or []) if uf.file_id == file_id),
        None,
    )
    if file_index is None:
        raise NotFoundError("UploadedFile", file_id)
    file_meta = case.uploaded_files[file_index]

    if not file_meta.has_stored_content:
        raise ValidationException(
            f"Uploaded file {file_id} has no stored raw content — "
            "reclassification requires re-running the extractor over "
            "the original bytes.",
            {"field": "file_id"},
        )
    # NotFoundError from storage (blob missing) passes through process_turn
    # unwrapped → 404, never a 5xx on a clicked suggestion.
    preprocessing_result, new_source_type = await _reextract_under_override(
        file_storage_service, preprocessing_service, file_meta, data_type
    )
    # Folded for the metric label, whose ``to_type`` is the 6-valued
    # ``preprocessing_result.data_type``: the row may hold either
    # vocabulary (#583), and a label mixing the two would split one
    # transition across series.
    previous = unified_data_type_of(file_meta.data_type)
    previous_type = previous.value if previous else "unknown"

    # One seam (#1470): the file row, EVERY Evidence row backed by it,
    # and the retirement of the question this answers. Claim content —
    # the LLM-authored summary/extract — stays untouched.
    (
        new_files_list,
        new_evidence_list,
        retired_suggestions,
    ) = _reclassified_collections(case, file_id, preprocessing_result, new_source_type)

    # Shallow copy with the replaced collections. ``messages`` gets a
    # fresh list because process_turn appends the agent message to the
    # returned case; every other field is only ever reassigned, never
    # mutated in place, so sharing by reference is safe — and skips
    # deep-copying the whole case (messages, hypotheses, causal graph)
    # on a mechanical click path.
    updated_case = case.model_copy(
        update={
            "uploaded_files": new_files_list,
            "evidence": new_evidence_list,
            # Retired at the seam as well as by the ``resolved_file_id``
            # round-trip below; the two predicates are complements, so
            # this is idempotent (see the seam).
            "last_suggestions": retired_suggestions,
            "messages": list(case.messages),
        }
    )

    EVIDENCE_RECLASSIFICATION_TOTAL.labels(
        from_type=str(previous_type),
        to_type=preprocessing_result.data_type.value,
        trigger="clarification",
    ).inc()

    subject = _upload_subject(file_meta)
    friendly = _CLARIFICATION_FRIENDLY_NAMES.get(data_type.value, {}).get(
        "long"
    ) or data_type.value.replace("_", " ")
    agent_response = f"Got it — I've recorded {subject} as {friendly}."
    if preprocessing_result.summary:
        agent_response += f"\n\n{preprocessing_result.summary}"

    return {
        "agent_response": agent_response,
        "suggested_follow_ups": [
            {
                "label": "Analyze it now",
                "action_type": "DECIDE",
                # The identifier, not ``subject``: this payload is
                # replayed as a standalone turn, where "the text you
                # pasted" has no antecedent. The sentence above it is in
                # conversation and keeps the prose form.
                "payload": f'Analyze "{file_meta.display_name}".',
                "body": "Run the analysis with the corrected classification.",
            }
        ],
        "case_updated": updated_case,
        "metadata": {
            "progress_made": False,
            "milestones_completed": [],
            "file_reclassified": {
                "file_id": file_id,
                "from_type": str(previous_type),
                "to_type": new_source_type.value,
            },
            **report_turn_uploads(case.case_id, case.current_turn, attachments),
        },
    }


def _refuse_if_terminal(case: "Case", what: str) -> None:
    """The one terminal refusal both reclassification seams raise (422).

    *what* names the thing being reclassified: ``files`` for the clarification
    handler, ``evidence`` for the evidence reclassifier.
    """
    if case.is_terminal:
        raise ValidationException(
            f"Cannot reclassify {what} on a closed case — the "
            "investigation is terminal; only questions about the case "
            "are accepted.",
            {"case_state": case.state.value},
        )


class AppliedReclassification(NamedTuple):
    """What ``apply_reclassification`` did: the addressed row, and the
    previous and new type the caller labels its counter with."""

    evidence: Evidence
    previous_type: str
    new_type: str


async def _reextract_under_override(
    file_storage_service,
    preprocessing_service,
    file_meta: "UploadedFile",
    data_type: DataType,
    previous_metadata: Optional[Dict[str, Any]] = None,
):
    """Retrieve the stored raw bytes behind *file_meta* and re-run
    preprocessing under ``user_override=data_type``.

    Shared mechanics of both reclassification paths — the PATCH /
    agent-tool ``reclassify_evidence`` and the clarification-intent
    ``_handle_file_reclassification``. Callers own target lookup,
    authorization, terminal/conflict policy, persistence, and
    response shape.

    Returns:
        ``(preprocessing_result, new_source_type)`` where the source
        type is inferred from the result's fine-grained
        ``detailed_data_type`` (the coarse UnifiedDataType in
        ``data_type`` never matches the source-type map's keys).

    Raises:
        ServiceException: storage/preprocessing service unavailable.
        NotFoundError: stored blob missing from storage.
    """
    if not file_storage_service:
        raise ServiceException("File storage service unavailable; cannot re-extract")
    if not preprocessing_service:
        raise ServiceException("Preprocessing service unavailable; cannot reclassify")

    # Fetch raw bytes + decode. Storage returns bytes; extractors
    # operate on strings (UTF-8 is the convention per the upload path).
    # Skip the destructive decode for binary content (see
    # _is_binary_content).
    raw_bytes = await file_storage_service.retrieve_file(file_meta.storage_ref)
    filename = file_meta.filename or "the uploaded file"
    if _is_binary_content(filename, file_meta.content_type, raw_bytes):
        content = _binary_placeholder(filename, file_meta.content_type, len(raw_bytes))
        logger.info(
            "binary content: skipping UTF-8 decode on reclassify",
            extra={"filename": filename, "size_bytes": len(raw_bytes)},
        )
    else:
        content = raw_bytes.decode("utf-8", errors="replace")

    preprocessing_result = await preprocessing_service.reclassify_evidence(
        content=content,
        filename=filename,
        user_override=data_type,
        previous_metadata=previous_metadata,
    )
    return preprocessing_result, _infer_source_type(
        preprocessing_result.detailed_data_type
    )


class EvidenceReclassifier:
    """Reclassify the file behind an existing evidence row (#1941).

    The port both callers share, as ONE instance the container builds: the
    ``PATCH .../classification`` route (``app.state.evidence_reclassifier``)
    and the ``reclassify_evidence`` agent tool. It needs only the case
    repository, file storage and preprocessing, which the infrastructure layer
    registers before any tool is built, and no engine or LLM. That is what
    lets the tool be constructed complete: it used to hold the whole
    investigation service, which is built after the milestone engine, which is
    built from the tool registry, so the tool was constructed with ``None`` and
    every call answered "not wired".

    WRITE MODEL (#1465). The write mode is chosen by which method a caller
    calls. ``reclassify_out_of_band`` (the PATCH endpoint, which runs outside
    any turn) owns its write: load, guard, apply to a deep copy, save the
    copy. ``reclassify_in_turn`` (the ``reclassify_evidence`` agent tool,
    called from inside the engine's tool loop) applies the change to the
    aggregate the turn is holding and does not save. A turn ends with ONE
    aggregate save of the case object it has been holding since it started;
    that save has no idea a row was written underneath it, so a write made on
    a separately loaded copy was overwritten by it: the file row, the Evidence
    row and the fm#918 clarification drop alike. The model was told the
    reclassification succeeded and the case ended the turn as though it never
    happened.

    The defect is in the ORDERING, not in the values, so the fix is where the
    write lands rather than what it contains: the turn's existing end-of-turn
    save is then the write — one save per turn, which is the write model the
    engine already has. The rejected alternative is making the end-of-turn
    save merge rather than replace. It is worse on the same argument: the
    aggregate save is what makes a turn atomic, and a merging save would have
    to decide, field by field, whether a difference is a concurrent write to
    keep or a deliberate revert to apply — a question neither side of the
    merge carries the information to answer. It would also make every other
    in-turn write ambiguous to pay for one caller.

    What this does NOT close is the concurrency reach #1465 also names: a
    ``PATCH`` landing from another request while a turn is mid-flight is still
    a lost update, because the per-case lock lives in
    ``MilestoneEngine.process_turn`` and nothing outside a turn takes it. That
    needs a lock rather than a parameter and is a separate change.
    """

    def __init__(self, case_repository, file_storage_service, preprocessing_service):
        self.repository = case_repository
        self.file_storage_service = file_storage_service
        self.preprocessing_service = preprocessing_service

    @staticmethod
    def _require_reclassifiable(
        case: "Case", evidence_id: str, user_id: str, *, reads: bool
    ) -> int:
        """The one guard both entry points run in front of
        ``apply_reclassification``, in this order: the driver gate (403), the
        evidence lookup (404), then the terminal refusal (422). Returns the
        addressed evidence's index.

        The case's effective DRIVER only (ADR-020 D2), and one who still READS
        it: ``reads`` is that read fact, which each entry point supplies. The
        creator always reads their case, and inside a turn
        (``reclassify_in_turn``) the turn route resolved the case through the
        read allowlist; an assigned driver on the PATCH route
        (``reclassify_out_of_band``) reads it only if the route says so
        (``caller_reads_case``). Without that last arm the PATCH route — which
        has no read check of its own ahead of this 403 — would admit a driver
        who had lost every share before a release reached the row.
        """
        if case.effective_driver_id != user_id or not reads:
            raise AuthorizationError(
                f"User {user_id} not authorized for case {case.case_id}"
            )

        evidence_index: Optional[int] = None
        for i, ev in enumerate(case.evidence or []):
            if ev.evidence_id == evidence_id:
                evidence_index = i
                break
        if evidence_index is None:
            raise NotFoundError("Evidence", evidence_id)

        # AFTER the evidence lookup, so a closed case answers 404 for an
        # evidence id that does not exist exactly as an open one does. Ahead
        # of the lookup it would answer 422 there, and the case's state would
        # decide what a missing row is called.
        #
        # The same terminal refusal ``_handle_file_reclassification`` makes,
        # on both entry points. The terminal short-circuit does not keep a
        # closed case away from ``reclassify_in_turn`` by itself: it
        # short-circuits INTO a tool loop, which hands its tools a
        # ``ToolContext`` carrying ``in_memory_case=case`` and returns that
        # same object as ``case_updated`` for ``process_turn`` to save. Since
        # #1964 that loop offers only ``TERMINAL_QA_TOOLS`` and runs no tool it
        # did not offer, and ``reclassify_evidence`` is not among them, so this
        # refusal is the second line: for any path that still reaches the
        # reclassifier with a terminal case.
        #
        # The in-turn write model (#1465, the class docstring) is why that
        # second line is needed. The tool applies its change to the turn's own
        # aggregate and the turn's save persists it, so without this refusal a
        # case whose own handler documents "no state mutations" would be
        # persistently mutable. While the tool wrote a separately loaded copy
        # instead, the terminal turn's aggregate save overwrote that copy, and
        # the #1465 lost update was what kept closed cases unchanged.
        _refuse_if_terminal(case, "evidence")
        return evidence_index

    async def apply_reclassification(
        self, case: "Case", evidence_index: int, data_type: DataType
    ) -> AppliedReclassification:
        """Apply the reclassification IN PLACE on *case* and persist nothing.

        The write mode belongs to the caller: ``reclassify_in_turn`` passes
        the turn's own aggregate and leaves the save to the turn,
        ``reclassify_out_of_band`` passes a deep copy of the case it loaded
        and saves that. This method has no policy and no counter of its own.
        The guard (``_require_reclassifiable``) has already run.

        Raises:
            ConflictError: evidence has no backing file (409,
                ``conflict_reason="no_backing_file"``).
            ServiceException / NotFoundError: from the re-extraction.
        """
        evidence = case.evidence[evidence_index]
        evidence_id = evidence.evidence_id
        file_meta = case.find_uploaded_file(evidence.source_file_id)
        if file_meta is None or not file_meta.has_stored_content:
            raise ConflictError(
                f"Evidence {evidence_id} has no stored raw file — "
                "reclassification requires re-running the extractor "
                "over the original content, which is not available for "
                "evidence that was created without file storage.",
                resource_type="evidence",
                resource_id=evidence_id,
                conflict_reason="no_backing_file",
            )
        # A stored file (checked above) implies file_meta is present.
        preprocessing_result, new_source_type = await _reextract_under_override(
            self.file_storage_service,
            self.preprocessing_service,
            file_meta,
            data_type,
            previous_metadata=evidence.metadata,
        )

        # Lift the updated evidence_metadata block from the result.
        pp_metadata = preprocessing_result.extraction_metadata
        new_evidence_metadata: Optional[Dict[str, Any]] = None
        if isinstance(pp_metadata, dict):
            candidate = pp_metadata.get("evidence_metadata")
            if isinstance(candidate, dict):
                new_evidence_metadata = candidate

        previous_type = evidence.source_type.value
        new_type = preprocessing_result.data_type.value

        # Post-010 routing: preprocessing artifacts (data_type, summary,
        # structural_index) describe the FILE and land on
        # ``uploaded_files``. Evidence carries the LLM's claim — we only
        # re-align ``source_type`` so the agent sees consistent data on
        # the next turn. The LLM-authored ``summary`` and ``extract``
        # fields on Evidence are left untouched: they are claim content,
        # not preprocessing output.
        #
        # Through the shared seam (#1470) so EVERY Evidence row backed by
        # this file is re-aligned, not just the addressed one. The file has
        # one classification; how many claims cite it is not a property of
        # the classification, and leaving the siblings behind described one
        # file with two contradictory source types.
        (
            new_files_list,
            new_evidence_list,
            dropped_suggestions,
        ) = _reclassified_collections(
            case, evidence.source_file_id, preprocessing_result, new_source_type
        )

        # The ADDRESSED row additionally takes the re-extraction's
        # ``evidence_metadata`` block — the classification confidence and the
        # extractor-attempt trail for the request that was made about THIS
        # row. Deliberately not fanned out to the siblings: the trail records
        # what was asked of this evidence, and stamping a request nobody made
        # onto a neighbouring row would make the observability trail lie.
        #
        # CONDITIONAL, because this REPLACES the metadata the seam merged
        # rather than adding to it. Unconditional, a result carrying no
        # ``evidence_metadata`` set the addressed row's metadata to ``None``
        # while its siblings kept the merged verdict — one file, two answers,
        # which is the shape #1470 exists to close. Latent today because
        # ``_build_result`` always writes the block; latent in the one row the
        # caller actually asked about.
        #
        # Deep-copied for the reason the seam deep-copies: this block IS
        # ``preprocessing_result.extraction_metadata["evidence_metadata"]``,
        # and ``model_copy(deep=True)`` does not copy ``update`` values — so
        # without this the persisted row holds a live reference into the
        # preprocessing result.
        if new_evidence_metadata is not None:
            updated_evidence = new_evidence_list[evidence_index].model_copy(
                update={"metadata": copy.deepcopy(new_evidence_metadata)},
                deep=True,
            )
            new_evidence_list[evidence_index] = updated_evidence
        else:
            updated_evidence = new_evidence_list[evidence_index]

        # fm#918 exposure 1: this path writes ``UploadedFile.data_type``
        # without going through the turn seam, so nothing else rewrites
        # ``last_suggestions`` and the file's clarification choices stay
        # armed for the TYPED arm — the resolver reads this list, and
        # this is what empties it. A DECIDE **click** is not covered
        # and is not meant to be: a click carries its intent on the
        # request and never consults this list
        # (``suggestion_is_live``), so a card the client still shows
        # can still be clicked and still reaches the same end state.
        # That is consent rather than inference, which is the whole
        # distinction INV-26 rests on — but it does mean "exposure 1
        # is closed" is true of typing and not of clicking. Raised on
        # fm#918 rather than decided here.
        #
        # Answering the question here is exact; the referent
        # check in ``suggestion_is_live`` compares the stored value, and
        # on a row written before #583 that was the 12→6 projection,
        # which cannot see a reclassification WITHIN a source type
        # (logs_and_errors → command_output, both ``logs``) — how a
        # typed "Application logs (x.log)" on the next turn overwrote
        # the answer the user had just given here.
        #
        # True of BOTH write modes: an in-turn write is the turn's own save
        # (#1465), an out-of-band one is saved by its caller. The drop itself
        # comes back from the seam with the collections.
        #
        # Three whole-field assignments, no in-place list mutation: the lists
        # above are new lists built from the case's own, so this leaves any
        # list the caller may still be holding alone. In place on the object
        # given, never a ``model_copy`` here: in a turn that object is the one
        # the turn holds and saves at its end, so a copy would be the bug
        # (#1465). The out-of-band caller makes its own copy before calling.
        case.uploaded_files = new_files_list
        case.evidence = new_evidence_list
        case.last_suggestions = dropped_suggestions

        return AppliedReclassification(
            evidence=updated_evidence,
            previous_type=str(previous_type or "unknown"),
            new_type=new_type,
        )

    # The pre-split name on both entry points keeps trace's function_name series whole.
    @trace("evidence_reclassifier_reclassify_evidence")
    async def reclassify_out_of_band(
        self,
        case_id: str,
        evidence_id: str,
        user_id: str,
        data_type: DataType,
        *,
        caller_reads_case: bool = False,
    ) -> Evidence:
        """Reclassify from OUTSIDE any turn (the PATCH endpoint): load, guard,
        apply to a deep copy, save the copy.

        No turn owns this write, so this method does. See
        ``apply_reclassification`` for what is applied and the class docstring
        for the write model.

        Args:
            case_id: Case owning the evidence.
            evidence_id: Evidence being reclassified.
            user_id: User making the request (authorisation check).
            data_type: Target data type (DataType enum value).
            caller_reads_case: The caller resolved this case through the
                read allowlist. An assigned driver is admitted only with it;
                the creator needs no such proof.

        Returns:
            The ADDRESSED Evidence row, with the re-aligned ``source_type``
            and the re-extraction's ``metadata``. Its siblings on the same
            file are re-aligned too but not returned.

        Raises:
            NotFoundError: case or evidence not found (404).
            AuthorizationError: user is not the case's driver (403).
            ValidationException: the case is TERMINAL (422). Raised AFTER the
                evidence lookup, so a missing evidence id is still a 404
                whatever state the case is in.
            ConflictError: evidence has no backing file (409,
                ``conflict_reason="no_backing_file"``).
            ServiceException: any other failure (500).
        """
        case = await self.repository.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)
        evidence_index = self._require_reclassifiable(
            case,
            evidence_id,
            user_id,
            reads=caller_reads_case or case.user_id == user_id,
        )
        # On a deep copy, never on the object ``get`` returned: a repository
        # may hand back its stored object by reference (the in-memory one
        # does), which a turn may be holding, and a save that fails must leave
        # the store as it was. The copy is what is saved.
        working = case.model_copy(deep=True)
        applied = await self.apply_reclassification(working, evidence_index, data_type)
        await self.repository.save(working)

        # ``api`` counts a COMMITTED reclassification: the save has run.
        self._count_and_log(case_id, evidence_id, applied, trigger="api")
        return applied.evidence

    # The pre-split name on both entry points keeps trace's function_name series whole.
    @trace("evidence_reclassifier_reclassify_evidence")
    async def reclassify_in_turn(
        self,
        case_id: str,
        case: "Case",
        evidence_id: str,
        user_id: str,
        data_type: DataType,
    ) -> Evidence:
        """Reclassify from INSIDE a turn, on the aggregate the turn is holding.

        *case* is that aggregate. The change lands on it in place and nothing
        is persisted here: the turn's own end-of-turn save is the write
        (#1465). Raises the same errors as ``reclassify_out_of_band``, plus a
        ``ValidationException`` when *case* is not the addressed case.
        """
        if case.case_id != case_id:
            # A turn may only write its OWN case. Reaching another one
            # through the in-flight handle would write it without its
            # lock and then persist it on the wrong turn's save.
            raise ValidationException(
                "in_flight_case does not belong to the addressed case",
                {
                    "case_id": case_id,
                    "in_flight_case_id": case.case_id,
                },
            )
        # In a turn the route resolved the case through the read allowlist.
        evidence_index = self._require_reclassifiable(
            case, evidence_id, user_id, reads=True
        )
        applied = await self.apply_reclassification(case, evidence_index, data_type)

        # ``agent_tool`` counts an APPLIED reclassification: the write is on
        # the turn's aggregate and the turn's own save commits it, so a turn
        # that dies after this line leaves a count with no row behind it.
        self._count_and_log(case_id, evidence_id, applied, trigger="agent_tool")
        return applied.evidence

    @staticmethod
    def _count_and_log(
        case_id: str,
        evidence_id: str,
        applied: AppliedReclassification,
        *,
        trigger: str,
    ) -> None:
        # WHAT THIS COUNTS, per trigger, because the answer is not the same
        # for all three and reading it wrong misreads the graph.
        #
        # ``api`` counts a COMMITTED reclassification; ``agent_tool`` counts
        # an APPLIED one. That is deliberate rather than overlooked, and it is
        # what makes the labels comparable: ``clarification`` (the turn-seam
        # sibling) has always counted applications for exactly the same
        # reason — it hands its case back for ``process_turn`` to save and
        # increments before that happens. Aligning ``agent_tool`` with the
        # other TURN-BORNE trigger, rather than with the out-of-band one it no
        # longer resembles, keeps "reclassifications this deployment
        # performed" a sum worth taking. A strictly committed counter needs
        # the count to move to whoever owns persistence; noted on the PR as
        # follow-up.
        EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type=applied.previous_type,
            to_type=applied.new_type,
            trigger=trigger,
        ).inc()

        logger.info(
            "Reclassified evidence %s in case %s: %s -> %s (trigger=%s)",
            evidence_id,
            case_id,
            applied.previous_type,
            applied.new_type,
            trigger,
        )
