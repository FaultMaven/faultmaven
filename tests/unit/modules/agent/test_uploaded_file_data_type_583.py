"""#583 — ``UploadedFile.data_type`` holds two vocabularies, read through one boundary.

The ruling: writers store the fine-grained ``DataType`` (lossless — intake and
both reclassification entry points already hold one), and rows written before
stay on the 6-valued ``EvidenceSourceType`` / ``UnifiedDataType`` string. No
data migration: every reader either accepts both vocabularies or goes through
``core.preprocessing.models.unified_data_type_of``, which does.

What is pinned here:

- the boundary itself, over the whole of both vocabularies, and the premise it
  rests on (the two are disjoint);
- that the boundary's fold and the writers' ``_DATA_TYPE_TO_SOURCE_TYPE`` agree,
  so a stored ``DataType`` reads back as the source type ``Evidence`` got;
- the writers, end to end, and the readers that fold for a published surface
  (``AttachmentResult.source_type``, the reclassification metric's label) on a
  row of each vocabulary;
- **State N**: the package-wide scan of every read of ``data_type``, each
  classified, shipped as a test so a new reader has to be classified too.
"""

from __future__ import annotations

import ast
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.preprocessing.models import (
    UnifiedDataType,
    to_unified_data_type,
    unified_data_type_of,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_RECLASSIFICATION_TOTAL,
)
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    _DATA_TYPE_TO_SOURCE_TYPE,
    InvestigationService,
    _published_source_type,
)
from faultmaven.modules.case.domain.models import EvidenceSourceType, UploadedFile

from .conftest import MockMilestoneEngine, RecordingCaseRepository, create_sample_case
from .test_file_reclassification_intent import (
    _package_modules,
    _reextraction_as,
    _ScopedVisitor,
)

# =============================================================================
# The boundary
# =============================================================================


class TestTheReadBoundary:
    def test_the_two_vocabularies_are_disjoint(self):
        """The premise of a two-way reader: no stored string means one thing
        read as a ``DataType`` and another read as the 6-valued type."""
        fine = {dt.value for dt in DataType}
        coarse = {t.value for t in UnifiedDataType} | {
            t.value for t in EvidenceSourceType
        }
        assert fine and coarse
        assert fine.isdisjoint(coarse), fine & coarse

    @pytest.mark.parametrize("dt", list(DataType), ids=lambda d: d.value)
    def test_every_data_type_reads_as_its_fold(self, dt):
        assert unified_data_type_of(dt.value) is to_unified_data_type(dt)

    @pytest.mark.parametrize("t", list(UnifiedDataType), ids=lambda t: t.value)
    def test_every_legacy_value_reads_as_itself(self, t):
        assert unified_data_type_of(t.value) is t

    def test_every_legacy_source_type_a_writer_produced_reads(self):
        """What pre-#583 rows actually hold: ``_infer_source_type(...).value``.
        ``user_description`` is an Evidence-only value no file writer emits."""
        written = set(_DATA_TYPE_TO_SOURCE_TYPE.values())
        for source_type in written:
            assert unified_data_type_of(source_type.value) is not None, source_type

    @pytest.mark.parametrize(
        "stored", [None, "", "unknown", "user_description", "logs.log"]
    )
    def test_absent_or_unrecognised_is_none_not_a_guess(self, stored):
        assert unified_data_type_of(stored) is None

    def test_case_and_whitespace_are_tolerated(self):
        assert unified_data_type_of(" Logs_And_Errors ") is UnifiedDataType.LOGS
        assert unified_data_type_of("LOGS") is UnifiedDataType.LOGS

    @pytest.mark.parametrize("dt", list(DataType), ids=lambda d: d.value)
    def test_the_boundary_agrees_with_the_writers_fold(self, dt):
        """A row stored as *dt* reads back as the source type its Evidence got.

        There are two 12→6 tables — ``_DETAILED_TO_UNIFIED`` behind the
        boundary and ``_DATA_TYPE_TO_SOURCE_TYPE`` behind Evidence — and a
        disagreement would make the file row and the claims citing it describe
        one file two ways, the shape #1470 closed."""
        assert (
            unified_data_type_of(dt.value).value == _DATA_TYPE_TO_SOURCE_TYPE[dt].value
        )


# =============================================================================
# Writers, and the readers that fold for a published surface
# =============================================================================


def _row(data_type):
    from datetime import UTC, datetime

    return UploadedFile(
        file_id="file_aaaaaaaaaaaa",
        filename="x.log",
        size_bytes=10,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        uploaded_by="user_owner",
        data_type=data_type,
    )


class TestPublishedSourceType:
    """``AttachmentResult.source_type`` is documented as the 6-valued type."""

    @pytest.mark.parametrize(
        "stored,published",
        [
            ("logs_and_errors", "logs"),
            ("structured_config", "configuration"),
            ("visual_evidence", "image"),
            ("logs", "logs"),
            ("configuration", "configuration"),
            (None, ""),
        ],
    )
    def test_either_vocabulary_publishes_the_six_valued_type(self, stored, published):
        assert _published_source_type(_row(stored)) == published


class TestIntakeWritesTheDataType:
    @pytest.mark.asyncio
    async def test_the_row_holds_the_data_type_and_the_chip_the_fold(self):
        from faultmaven.core.preprocessing.models import PreprocessingResult

        repo = RecordingCaseRepository()
        case = create_sample_case(user_id="user_owner")
        case.uploaded_files = []
        case.evidence = []
        repo._storage[case.case_id] = case

        preprocessing = AsyncMock()
        preprocessing.classify_and_extract = AsyncMock(
            return_value=PreprocessingResult(
                data_type=UnifiedDataType.CONFIGURATION,
                detailed_data_type=DataType.STRUCTURED_CONFIG,
                summary="a config",
                structural_index="key: value",
                content_size_bytes=10,
                content_type="text/plain",
                extraction_method="direct",
                compression_ratio=1.0,
                content_hash="c" * 64,
            )
        )
        storage = MagicMock()
        storage.store_file = AsyncMock(return_value={"storage_key": "k/cfg.yaml"})
        storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing,
            file_storage_service=storage,
        )
        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="look at this",
                attachments=[
                    Attachment(
                        content=b"key: value\n",
                        filename="cfg.yaml",
                        content_type="text/plain",
                    )
                ],
                intent=QueryIntent(type=IntentType.CONVERSATION),
            ),
        )

        saved = await repo.get(case.case_id)
        assert saved.uploaded_files[0].data_type == "structured_config"
        assert response.attachments_processed[0].source_type == "configuration"


class TestReclassificationMetricLabel:
    """``from_type`` is read off the file row; ``to_type`` is 6-valued. Both
    stored vocabularies must land on the same series."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["structured_config", "configuration"])
    async def test_from_type_is_folded(self, stored):
        repo = RecordingCaseRepository()
        case = create_sample_case(user_id="user_owner")
        row = _row(stored).model_copy(update={"storage_ref": "k/x.log"})
        case.uploaded_files = [row]
        case.evidence = []
        repo._storage[case.case_id] = case

        preprocessing = AsyncMock()
        preprocessing.reclassify_evidence = AsyncMock(
            side_effect=lambda **k: _reextraction_as(k["user_override"])
        )
        storage = MagicMock()
        storage.retrieve_file = AsyncMock(return_value=b"line1\nline2\n")

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing,
            file_storage_service=storage,
        )
        series = EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type="configuration", to_type="logs", trigger="clarification"
        )
        before = series._value.get()

        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="Application logs (x.log)",
                intent=QueryIntent(
                    type=IntentType.FILE_RECLASSIFICATION,
                    file_id=row.file_id,
                    data_type=DataType.LOGS_AND_ERRORS.value,
                ),
            ),
        )

        assert series._value.get() == before + 1
        saved = await repo.get(case.case_id)
        assert saved.uploaded_files[0].data_type == "logs_and_errors"


# =============================================================================
# State N — every read of ``data_type`` in the package, classified
# =============================================================================

#: The READ boundary. A reader that needs the 6-valued type calls it.
_BOUNDARY = "unified_data_type_of"

#: Constructors that parse a string as ONE vocabulary. A reader of the column
#: that calls one of these on it is the defect #583's review found in
#: ``vectorize_file_tool`` (``UnifiedDataType(stored)`` → TEXT on a miss).
_ONE_VOCABULARY_PARSERS = frozenset(
    {"UnifiedDataType", "EvidenceSourceType", "DataType", "DetailedDataType"}
)

_SVC = "modules/agent/domain/services/investigation_service.py"
_INGEST = "modules/case/domain/services/case_data_ingestion_service.py"

#: ``(module, scope, kind) -> category``. Categories:
#:
#: - ``boundary`` — reads ``UploadedFile.data_type`` and needs the 6-valued
#:   type; must call ``unified_data_type_of`` in the same function.
#: - ``agnostic`` — reads ``UploadedFile.data_type`` and uses it as an opaque
#:   string (a label shown to the model, an equality against its own earlier
#:   snapshot, a key nobody reads); must NOT parse it as one vocabulary.
#: - ``passthrough`` — a repository moving the string between row and model.
#: - ``other`` — a ``data_type`` that is not ``UploadedFile.data_type`` at all
#:   (a classifier result, an intent, a request body). Named with its reason.
_EXPECTED: dict[tuple[str, str, str], str] = {
    # --- UploadedFile.data_type, needs the 6-valued type -------------------
    (
        "modules/agent/tools/vectorize_file_tool.py",
        "VectorizeFileTool.execute_with_context",
        "attr",
    ): "boundary",
    (
        "modules/agent/tools/deep_analysis_tool.py",
        "DeepAnalysisTool.execute_with_context",
        "getattr",
    ): "boundary",
    # AttachmentResult.source_type, published as the 6-valued vocabulary.
    (_SVC, "_published_source_type", "attr"): "boundary",
    # ``previous_type`` → EVIDENCE_RECLASSIFICATION_TOTAL.from_type. The same
    # function also reads ``preprocessing_result.data_type`` (6-valued, the
    # ``to_type``) and parses ``intent.data_type`` via ``DataType(...)``.
    (_SVC, "InvestigationService._handle_file_reclassification", "attr"): "boundary",
    # --- UploadedFile.data_type, opaque string -----------------------------
    # The ``<uploaded_file data_type=…>`` attribute in the prompt.
    (
        "core/investigation/prompts/context_builder.py",
        "_render_orphan_file_block",
        "attr",
    ): "agnostic",
    # The referent check: equality against the value's own snapshot
    # (``OFFERED_DATA_TYPE_KEY``), so which vocabulary does not matter.
    (
        "core/investigation/suggestion_liveness.py",
        "file_data_types",
        "attr",
    ): "agnostic",
    # "(classified as …)" in the implicit query.
    (
        "core/investigation/turn_pipeline.py",
        "generate_implicit_query",
        "attr",
    ): "agnostic",
    # A row in the ``list_evidence_by_time`` tool result.
    (
        "modules/agent/tools/list_evidence_by_time_tool.py",
        "_format_unpromoted_files",
        "attr",
    ): "agnostic",
    # The engine attachment dict's ``data_type`` key. No engine code reads it
    # (``turn_uploads`` reads ``file_id`` / ``is_novel``).
    (_SVC, "_engine_attachment_metadata", "attr"): "agnostic",
    # --- repositories ------------------------------------------------------
    (
        "modules/case/infrastructure/sqlite_case_repository.py",
        "SQLiteCaseRepository._load_uploaded_files",
        "get",
    ): "passthrough",
    (
        "modules/case/infrastructure/sqlite_case_repository.py",
        "SQLiteCaseRepository._upsert_uploaded_files",
        "attr",
    ): "passthrough",
    (
        "modules/case/infrastructure/postgresql_hybrid_case_repository.py",
        "PostgreSQLHybridCaseRepository._upsert_uploaded_files",
        "attr",
    ): "passthrough",
    # --- not UploadedFile.data_type ----------------------------------------
    # Intent / request / tool-parameter ``data_type`` — a ``DataType`` the
    # caller names, parsed as one on purpose.
    (_SVC, "InvestigationService.process_turn", "attr"): "other",
    ("models/api_models.py", "QueryIntent.validate_intent_fields", "attr"): "other",
    ("modules/case/api/routes.py", "reclassify_evidence", "get"): "other",
    (
        "modules/agent/tools/reclassify_evidence_tool.py",
        "ReclassifyEvidenceTool.execute_with_context",
        "get",
    ): "other",
    # ``preprocessing_result.data_type`` (a ``UnifiedDataType``).
    (_SVC, "InvestigationService.reclassify_evidence", "attr"): "other",
    (
        "modules/preprocessing/preprocessing_service.py",
        "PreprocessingService.classify_and_extract",
        "attr",
    ): "other",
    # Classifier results / LLM routing kwargs / pattern-learner feedback.
    (
        "modules/preprocessing/classifier.py",
        "_opik_track_classifier.wrapper",
        "attr",
    ): "other",
    ("infrastructure/llm/router.py", "LLMRouter.generate", "get"): "other",
    (
        "core/processing/pattern_learner.py",
        "PatternLearner._analyze_feedback",
        "get",
    ): "other",
    # The legacy data-ingestion service's own in-memory classification
    # results; it never touches ``uploaded_files``.
    (_INGEST, "CaseDataIngestionService._calculate_confidence_score", "attr"): "other",
    (
        _INGEST,
        "CaseDataIngestionService._combine_processing_insights",
        "getattr",
    ): "other",
    (_INGEST, "CaseDataIngestionService._detect_anomalies", "attr"): "other",
    (_INGEST, "CaseDataIngestionService._execute_data_ingestion", "attr"): "other",
    (_INGEST, "CaseDataIngestionService._record_enhanced_operation", "attr"): "other",
    (_INGEST, "CaseDataIngestionService.get_processing_insights", "attr"): "other",
    (_INGEST, "CaseDataIngestionService.ingest_data_enhanced", "attr"): "other",
    (_INGEST, "CaseDataIngestionService.ingest_data_enhanced", "get"): "other",
    (_INGEST, "CaseDataIngestionService.learn_from_feedback", "attr"): "other",
}


def _is_data_type_key(node) -> bool:
    return isinstance(node, ast.Constant) and node.value == "data_type"


def _scan() -> tuple[dict[tuple[str, str, str], set[str]], set[str]]:
    """``(module, scope, kind) -> names called in that scope``, plus the files
    parsed. The called names are what the category checks read."""
    found: dict[tuple[str, str, str], set[str]] = {}
    calls: dict[tuple[str, str], set[str]] = {}
    modules = _package_modules(("data_type",))
    for rel, tree in modules:

        class _Walk(_ScopedVisitor):
            def _record(self, kind: str) -> None:
                found.setdefault((self.rel, self.scope, kind), set())

            def visit_Attribute(self, node):
                if node.attr == "data_type" and isinstance(node.ctx, ast.Load):
                    self._record("attr")
                self.generic_visit(node)

            def visit_Subscript(self, node):
                if _is_data_type_key(node.slice) and isinstance(node.ctx, ast.Load):
                    self._record("subscript")
                self.generic_visit(node)

            def visit_Call(self, node):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                calls.setdefault((self.rel, self.scope), set()).add(name)
                if name == "getattr" and len(node.args) >= 2:
                    if _is_data_type_key(node.args[1]):
                        self._record("getattr")
                elif name == "get" and node.args and _is_data_type_key(node.args[0]):
                    self._record("get")
                self.generic_visit(node)

        _Walk(rel).visit(tree)

    for key in found:
        found[key] = calls.get((key[0], key[1]), set())
    return found, {rel for rel, _ in modules}


def test_every_reader_of_uploaded_file_data_type_is_classified():
    """State N: every read of ``data_type`` in the package, and what it is.

    **N = 12 functions read ``UploadedFile.data_type``**: 4 need the 6-valued
    type and go through the boundary, 5 use it as an opaque string, 3 are
    repository pass-throughs. 17 more reads name a ``data_type`` that is not
    the column (intents, classifier results, a request body) and are listed
    with their reason. The scan found the two readers that parsed ONE
    vocabulary — ``vectorize_file_tool`` (found by review) and
    ``deep_analysis_tool``'s file-id branch (found by this scan) — and the
    two that published the column's raw value into a 6-valued surface.

    Matching is keyed on the attribute and the read's SHAPE, never on the
    receiver's name: ``x.data_type`` (load), ``getattr(x, "data_type", …)``,
    ``x.get("data_type", …)``, ``x["data_type"]``. The whole package is read.

    Then each category is CHECKED, not just listed, so a reader cannot pass by
    being filed under a convenient label:

    - ``boundary`` — the same function calls ``unified_data_type_of``;
    - ``agnostic`` / ``passthrough`` — the same function constructs none of
      ``UnifiedDataType`` / ``EvidenceSourceType`` / ``DataType``, i.e. it
      does not parse the column as one vocabulary.

    WHAT IT STILL MISSES, stated rather than implied:

    - SQL. The repositories also hydrate by column index (``row[15]``) and
      the PostgreSQL loader builds the row in ``json_build_object``; those
      move the string unchanged and are pass-throughs, but nothing here sees
      them. A ``WHERE data_type = 'logs'`` would be a reader this misses.
    - Whole-row serialisation (``model_dump()`` then iterating, ``**`` spread,
      ``vars()``) — none exists for ``UploadedFile`` today.
    - A one-vocabulary parse by LOOKUP rather than construction — a dict keyed
      on the 6-valued strings, which is exactly what ``deep_analysis_tool``
      did (``_TYPE_MAP.get(stored)``). The category check cannot tell that
      from any other ``.get``; filing a new reader as ``agnostic`` is the
      author's claim, and this list is where a reviewer reads it.
    - ``other`` is unchecked by construction: those reads are not the column.
    """
    found, parsed = _scan()

    assert {
        "modules/agent/tools/vectorize_file_tool.py",
        "modules/agent/tools/deep_analysis_tool.py",
        _SVC,
        "modules/case/infrastructure/sqlite_case_repository.py",
        "modules/case/infrastructure/postgresql_hybrid_case_repository.py",
    } <= parsed, f"the token filter excluded a module holding a known reader: {parsed}"

    assert set(found) == set(_EXPECTED), (
        "a read of data_type was added or removed — classify it in _EXPECTED. "
        f"new: {sorted(set(found) - set(_EXPECTED))}; "
        f"gone: {sorted(set(_EXPECTED) - set(found))}"
    )

    column_readers = [k for k, c in _EXPECTED.items() if c != "other"]
    assert len({(m, s) for m, s, _ in column_readers}) == 12

    for key, category in _EXPECTED.items():
        called = found[key]
        if category == "boundary":
            assert (
                _BOUNDARY in called
            ), f"{key} needs the 6-valued type but does not call {_BOUNDARY}"
        elif category in ("agnostic", "passthrough"):
            parses = called & _ONE_VOCABULARY_PARSERS
            assert not parses, (
                f"{key} is filed as {category} but parses with {sorted(parses)} — "
                f"a reader that needs the 6-valued type goes through {_BOUNDARY}"
            )
