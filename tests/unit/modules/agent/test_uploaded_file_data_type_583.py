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
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.preprocessing.models import (
    UnifiedDataType,
    to_unified_data_type,
    unified_data_type_of,
)
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _DATA_TYPE_TO_SOURCE_TYPE,
    _published_source_type,
)
from faultmaven.modules.case.domain.models.evidence import (
    EvidenceSourceType,
    UploadedFile,
)

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
    stored vocabularies must land on the same series.

    Asserted on the labels the code PASSES, with the metric patched, not on
    the counter's value: under ``PROMETHEUS_ENABLED=false`` (the Standalone
    CI job) the metric is a no-op with no ``_value`` to read.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["structured_config", "configuration"])
    async def test_from_type_is_folded(self, stored, monkeypatch):
        from faultmaven.modules.agent.domain.services.investigation_service import (
            reclassification as investigation_service,
        )

        metric = MagicMock()
        monkeypatch.setattr(
            investigation_service, "EVIDENCE_RECLASSIFICATION_TOTAL", metric
        )
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

        metric.labels.assert_called_once_with(
            from_type="configuration", to_type="logs", trigger="clarification"
        )
        metric.labels.return_value.inc.assert_called_once_with()
        saved = await repo.get(case.case_id)
        assert saved.uploaded_files[0].data_type == "logs_and_errors"


# =============================================================================
# State N — every read of ``data_type`` in the package, classified PER SITE
# =============================================================================

#: The READ boundary. A reader that needs the 6-valued type calls it.
_BOUNDARY = "unified_data_type_of"

#: Constructors that parse a string as ONE vocabulary. Handing the column to
#: one is the defect #583's review found in ``vectorize_file_tool``
#: (``UnifiedDataType(stored)`` → TEXT on a miss).
_ONE_VOCABULARY_PARSERS = frozenset(
    {"UnifiedDataType", "EvidenceSourceType", "DataType", "DetailedDataType"}
)

#: A string literal naming the column in SQL: the repositories' ``SELECT`` /
#: ``INSERT … ON CONFLICT`` / ``json_build_object`` texts. Docstrings excluded.
_SQL = re.compile(r"\b(select|insert|update)\b|json_build_object", re.IGNORECASE)

#: Every token a matcher below keys on. ``UploadedFile`` is its own token:
#: ``UploadedFile(**f)`` names no ``data_type`` at all.
_TOKENS = ("data_type", "UploadedFile")


class _Site:
    """One read of ``data_type``: where, of what, and the node itself."""

    def __init__(self, rel, scope, kind, receiver, node, func):
        self.key = (rel, scope, kind, receiver)
        self.node = node
        self.func = func


def _receiver(node) -> str:
    return ast.unparse(node)


def _is_data_type_key(node) -> bool:
    return isinstance(node, ast.Constant) and node.value == "data_type"


def _scan(
    modules: tuple[tuple[str, ast.Module], ...] | None = None,
) -> tuple[list[_Site], set[str]]:
    """Every read SITE of ``data_type`` in *modules*, plus the files parsed.

    *modules* is ``(path, parsed module)`` pairs and defaults to the
    package's candidate files; the probe-table test hands in synthetic ones,
    so they go through this same walker.

    A site is keyed on ``(module, scope, shape, receiver)`` — the receiver is
    the source text of the object read from (``res.uploaded_file``,
    ``intent``, ``row[15]``) — and sites with the same key are COUNTED, so a
    second read of the same thing in the same function changes the census.
    The first version keyed on ``(function, shape)`` alone, so one entry
    filed as ``other`` for ``intent.data_type`` hid a read of the column
    added beside it; reverting this PR's own chip fix stayed green.
    """
    sites: list[_Site] = []
    if modules is None:
        modules = _package_modules(_TOKENS)
    for rel, tree in modules:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._fm_parent = parent  # type: ignore[attr-defined]

        class _Walk(_ScopedVisitor):
            def __init__(self, rel):
                super().__init__(rel)
                self.funcs = []

            def _named(self, node):
                is_func = not isinstance(node, ast.ClassDef)
                if is_func:
                    self.funcs.append(node)
                super()._named(node)
                if is_func:
                    self.funcs.pop()

            visit_FunctionDef = _named
            visit_AsyncFunctionDef = _named
            visit_ClassDef = _named

            def _add(self, kind, receiver, node):
                func = self.funcs[-1] if self.funcs else None
                sites.append(_Site(self.rel, self.scope, kind, receiver, node, func))

            def visit_Attribute(self, node):
                if node.attr == "data_type" and isinstance(node.ctx, ast.Load):
                    self._add("attr", _receiver(node.value), node)
                self.generic_visit(node)

            def visit_Subscript(self, node):
                if _is_data_type_key(node.slice) and isinstance(node.ctx, ast.Load):
                    self._add("subscript", _receiver(node.value), node)
                self.generic_visit(node)

            def visit_Dict(self, node):
                # ``{"data_type": row[13]}`` — a positional row read, the
                # repositories' hydration shape.
                for k, v in zip(node.keys, node.values):
                    if (
                        _is_data_type_key(k)
                        and isinstance(v, ast.Subscript)
                        and isinstance(v.slice, ast.Constant)
                        and isinstance(v.slice.value, int)
                    ):
                        self._add("row_index", _receiver(v), v)
                self.generic_visit(node)

            def visit_Constant(self, node):
                if (
                    isinstance(node.value, str)
                    and "data_type" in node.value
                    and _SQL.search(node.value)
                    and not isinstance(getattr(node, "_fm_parent", None), ast.Expr)
                ):
                    self._add("sql", "<sql>", node)
                self.generic_visit(node)

            def visit_Call(self, node):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "getattr" and len(node.args) >= 2:
                    if _is_data_type_key(node.args[1]):
                        self._add("getattr", _receiver(node.args[0]), node)
                elif name == "get" and node.args and _is_data_type_key(node.args[0]):
                    self._add("get", _receiver(fn.value), node)
                elif name != "setattr" and any(_is_data_type_key(a) for a in node.args):
                    # A helper taking the attribute NAME — the
                    # ``_get_data_attribute(x, "data_type")`` shape.
                    self._add("name_arg", f"{name}({_receiver(node.args[0])})", node)
                elif name == "UploadedFile":
                    for kw in node.keywords:
                        if kw.arg is None:
                            self._add("construct_spread", _receiver(kw.value), node)
                        elif kw.arg == "data_type":
                            self._add("construct_kw", _receiver(kw.value), kw.value)
                self.generic_visit(node)

        _Walk(rel).visit(tree)
    return sites, {rel for rel, _ in modules}


def _uses(site: _Site) -> list[ast.AST]:
    """The expressions the read's VALUE reaches in its function: the read
    itself, plus every later use of a local name it is bound to (one hop).

    A name is bound by ``name = <expr containing the read>``, ``name: T =
    <expr>``, ``(name := <expr>)``, or one element of ``a, b = x, y``."""
    reached: list[ast.AST] = [site.node]
    if site.func is None:
        return reached
    names: list[str] = []
    node = site.node
    while hasattr(node, "_fm_parent") and not isinstance(node, ast.stmt):
        parent = node._fm_parent
        if isinstance(parent, ast.NamedExpr) and parent.value is node:
            names.append(parent.target.id)
        node = parent
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(node.value, (ast.Tuple, ast.List))
            and len(target.elts) == len(node.value.elts)
        ):
            for t, v in zip(target.elts, node.value.elts):
                if isinstance(t, ast.Name) and any(n is site.node for n in ast.walk(v)):
                    names.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.value is not None:
            names.append(node.target.id)
    reached += [
        n
        for n in ast.walk(site.func)
        if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load)
    ]
    return reached


def _is_str_literal(node) -> bool:
    """A string constant, or a tuple/list/set made only of them — what a
    comparison against ONE vocabulary looks like (``== "logs"``, ``in (…)``)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_str_literal(e) for e in node.elts)
    return False


#: Methods that return the string transformed but still in its vocabulary, so
#: a parse after one still reads the column as one vocabulary.
_NORMALISERS = frozenset(
    {"lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title"}
)


def _is_enum_member(node) -> bool:
    """``UnifiedDataType.LOGS``, ``EvidenceSourceType.LOGS.value``, or a
    tuple/list/set made only of them: a comparison against ONE vocabulary."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_enum_member(e) for e in node.elts)
    while isinstance(node, ast.Attribute):
        node = node.value
        if isinstance(node, ast.Name) and node.id in _ONE_VOCABULARY_PARSERS:
            return True
    return False


def _is_named_constant(node) -> bool:
    """``_LOG_TYPES`` / ``mod.LOG_TYPES``: a module constant, whose members
    the comparison cannot see — read as a vocabulary by convention."""
    name = getattr(node, "id", None) or getattr(node, "attr", None)
    return bool(name) and name.lstrip("_").isupper()


def _consumer(expr: ast.AST) -> str | None:
    """What *expr* is handed to, when that is a parse.

    Walks up through what keeps the value in its vocabulary — ``x or d``,
    ``a if c else b``, ``(n := x)``, ``str(x)`` and a normalising method
    (``x.lower()``, ``.strip()`` …) — then names the consumer: the callee of a
    positional or keyword argument, ``"get"`` for ``.get(x)``, ``"[]"`` for
    ``MAP[x]``, ``"==literal"`` / ``"==enum"`` / ``"in_named_set"`` for a
    comparison against string literals, enum members or a module constant,
    and ``"startswith"`` for ``x.startswith("…")`` / ``endswith``."""
    node = expr
    while True:
        parent = getattr(node, "_fm_parent", None)
        if isinstance(parent, (ast.BoolOp, ast.IfExp, ast.NamedExpr)):
            node = parent
        elif (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "str"
            and len(parent.args) == 1
            and parent.args[0] is node
        ):
            node = parent
        elif (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and isinstance(getattr(parent, "_fm_parent", None), ast.Call)
            and parent._fm_parent.func is parent
        ):
            call = parent._fm_parent
            if parent.attr in _NORMALISERS:
                node = call
            elif parent.attr in ("startswith", "endswith") and any(
                _is_str_literal(a) for a in call.args
            ):
                return "startswith"
            else:
                return None
        else:
            break
    if isinstance(parent, ast.keyword):
        # ``Parser(value=x)``: a keyword argument's parent is the ``keyword``
        # node, not the call, so step through it to the call.
        parent = getattr(parent, "_fm_parent", None)
    if isinstance(parent, ast.Call) and (
        node in parent.args or any(kw.value is node for kw in parent.keywords)
    ):
        fn = parent.func
        return getattr(fn, "id", None) or getattr(fn, "attr", None)
    if isinstance(parent, ast.Subscript) and parent.slice is node:
        return "[]"
    if isinstance(parent, ast.Compare):
        others = [
            side for side in [parent.left, *parent.comparators] if side is not node
        ]
        if any(_is_str_literal(side) for side in others):
            return "==literal"
        if any(_is_enum_member(side) for side in others):
            return "==enum"
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops) and any(
            _is_named_constant(side) for side in parent.comparators
        ):
            return "in_named_set"
    return None


#: What a site handed to one of these has done: parsed the column as ONE
#: vocabulary. ``get`` and ``[]`` catch a lookup keyed on the value —
#: ``MAP.get(x)``, ``MAP[x]``, ``UnifiedDataType[x]`` — and, because
#: ``_consumer`` walks through ``str()`` and the normalising methods (#1646),
#: the form ``deep_analysis_tool`` used before #583 as well:
#: ``_TYPE_MAP.get(str(ft).lower(), UnifiedDataType.TEXT)``.
_PARSES = _ONE_VOCABULARY_PARSERS | {
    "get",
    "[]",
    "==literal",
    "==enum",
    "in_named_set",
    "startswith",
}


def _consumers(site: _Site) -> set[str]:
    """Everything the site's value is handed to: the ``_consumer`` of each of
    its ``_uses``."""
    return {_consumer(use) for use in _uses(site)} - {None}


def _one_vocabulary_parses(site: _Site) -> set[str]:
    """The per-site rule: which of the site's consumers read its value as ONE
    vocabulary. Empty is what an ``opaque`` or ``passthrough`` site must be;
    the census test and the probe-table test both call this."""
    return _consumers(site) & _PARSES


_SVC = "modules/agent/domain/services/investigation_service/service.py"
_ATTACHMENTS = "modules/agent/domain/services/investigation_service/attachments.py"
_RECLASSIFICATION = (
    "modules/agent/domain/services/investigation_service/reclassification.py"
)
_TURN_BOOKKEEPING = (
    "modules/agent/domain/services/investigation_service/turn_bookkeeping.py"
)
_INGEST = "modules/case/domain/services/case_data_ingestion_service.py"
#: #1707 split SQLite's repository into a package: ``find_uploaded_file_by_content_hash``
#: stayed on the owner in ``repository.py``; ``_load_*`` moved to module functions
#: in ``loading.py``; ``_upsert_*`` and ``_row_to_case`` moved to ``saving.py`` /
#: ``rows.py`` respectively (they read no instance state but ``db``).
_SQLITE = "modules/case/infrastructure/sqlite_case_repository/repository.py"
_SQLITE_LOADING = "modules/case/infrastructure/sqlite_case_repository/loading.py"
_SQLITE_SAVING = "modules/case/infrastructure/sqlite_case_repository/saving.py"
_SQLITE_ROWS = "modules/case/infrastructure/sqlite_case_repository/rows.py"
_PG = "modules/case/infrastructure/postgresql_hybrid_case_repository/repository.py"
_PG_LOADING = "modules/case/infrastructure/postgresql_hybrid_case_repository/loading.py"
_PG_SAVING = "modules/case/infrastructure/postgresql_hybrid_case_repository/saving.py"

#: ``(module, scope, shape, receiver) -> (category, count)``. Categories:
#:
#: - ``boundary`` — a read of ``UploadedFile.data_type`` whose value reaches
#:   ``unified_data_type_of``;
#: - ``opaque`` — a read of the column used as an uninterpreted string (a
#:   label, an equality against its own earlier snapshot, a key nobody
#:   reads); its value must reach no parse;
#: - ``passthrough`` — a repository moving the string between row and model;
#:   same rule as ``opaque``;
#: - ``other`` — not ``UploadedFile.data_type`` at all. Named with a reason,
#:   and unchecked by construction — which is why the RECEIVER is in the key:
#:   a read of ``res.uploaded_file`` beside an ``other`` read of ``intent``
#:   is a different site, not the same entry.
_CB = "core/investigation/prompts/context_builder/evidence.py"
_EXPECTED: dict[tuple[str, str, str, str], tuple[str, int]] = {
    # --- UploadedFile.data_type, needs the 6-valued type (4 functions) -----
    # Both reads sit in ``data_type_str = (… if … else …)``, which is parsed.
    (
        "modules/agent/tools/vectorize_file_tool.py",
        "VectorizeFileTool.execute_with_context",
        "attr",
        "file_meta",
    ): ("boundary", 2),
    (
        "modules/agent/tools/deep_analysis_tool.py",
        "DeepAnalysisTool.execute_with_context",
        "getattr",
        "file_meta",
    ): ("boundary", 1),
    # AttachmentResult.source_type, published as the 6-valued vocabulary.
    (_TURN_BOOKKEEPING, "_published_source_type", "attr", "uploaded_file"): (
        "boundary",
        1,
    ),
    # ``previous_type`` → EVIDENCE_RECLASSIFICATION_TOTAL.from_type.
    (_RECLASSIFICATION, "_handle_file_reclassification", "attr", "file_meta"): (
        "boundary",
        1,
    ),
    # --- UploadedFile.data_type, opaque string (5 functions) ---------------
    # The ``<uploaded_file data_type=…>`` attribute in the prompt.
    (_CB, "_render_orphan_file_block", "attr", "uf"): ("opaque", 1),
    # The referent check: equality against the value's own snapshot
    # (``OFFERED_DATA_TYPE_KEY``), so which vocabulary does not matter.
    ("core/investigation/suggestion_liveness.py", "file_data_types", "attr", "uf"): (
        "opaque",
        1,
    ),
    # "(classified as …)" in the implicit query.
    ("core/investigation/turn_pipeline.py", "generate_implicit_query", "attr", "uf"): (
        "opaque",
        1,
    ),
    # A row in the ``list_evidence_by_time`` tool result.
    (
        "modules/agent/tools/list_evidence_by_time_tool.py",
        "_format_unpromoted_files",
        "attr",
        "uf",
    ): ("opaque", 1),
    # The engine attachment dict's ``data_type`` key. No engine code reads it
    # (``turn_uploads`` reads ``file_id`` / ``is_novel``).
    (_ATTACHMENTS, "_engine_attachment_metadata", "attr", "uf"): ("opaque", 1),
    # --- repositories: the string between row and model (9 functions) ------
    (_SQLITE_LOADING, "_load_uploaded_files", "sql", "<sql>"): (
        "passthrough",
        1,
    ),
    (_SQLITE_LOADING, "_load_uploaded_files", "get", "row_dict"): (
        "passthrough",
        1,
    ),
    (_SQLITE_LOADING, "_load_uploaded_files_bulk", "sql", "<sql>"): (
        "passthrough",
        1,
    ),
    (
        _SQLITE_LOADING,
        "_load_uploaded_files_bulk",
        "row_index",
        "row[13]",
    ): ("passthrough", 1),
    (
        _SQLITE,
        "SQLiteCaseRepository.find_uploaded_file_by_content_hash",
        "sql",
        "<sql>",
    ): ("passthrough", 1),
    (
        _SQLITE,
        "SQLiteCaseRepository.find_uploaded_file_by_content_hash",
        "construct_kw",
        "row[15]",
    ): ("passthrough", 1),
    (_SQLITE_SAVING, "_upsert_uploaded_files", "sql", "<sql>"): (
        "passthrough",
        1,
    ),
    (_SQLITE_SAVING, "_upsert_uploaded_files", "attr", "file"): (
        "passthrough",
        1,
    ),
    (_SQLITE_ROWS, "_row_to_case", "construct_spread", "f"): (
        "passthrough",
        1,
    ),
    # ``get``'s query builds each row with ``json_build_object(…'data_type',
    # f.data_type…)``; ``_row_to_case`` spreads it into ``UploadedFile(**f)``.
    (_PG, "PostgreSQLHybridCaseRepository.get", "sql", "<sql>"): ("passthrough", 1),
    (
        _PG,
        "PostgreSQLHybridCaseRepository.find_uploaded_file_by_content_hash",
        "sql",
        "<sql>",
    ): ("passthrough", 1),
    (
        _PG,
        "PostgreSQLHybridCaseRepository.find_uploaded_file_by_content_hash",
        "construct_kw",
        "row[15]",
    ): ("passthrough", 1),
    (_PG_SAVING, "_upsert_uploaded_files", "sql", "<sql>"): (
        "passthrough",
        2,
    ),
    (_PG_SAVING, "_upsert_uploaded_files", "attr", "file"): (
        "passthrough",
        1,
    ),
    (_PG_LOADING, "_row_to_case", "construct_spread", "f"): (
        "passthrough",
        1,
    ),
    # --- not UploadedFile.data_type ----------------------------------------
    # Intent / request / tool-parameter ``data_type`` — a ``DataType`` the
    # caller names, parsed as one on purpose. #1707 wave 3: this read now
    # lives in the dispatch phase extracted from ``process_turn``.
    (_SVC, "InvestigationService._dispatch_turn", "attr", "intent"): ("other", 1),
    ("models/api_models.py", "QueryIntent.validate_intent_fields", "attr", "self"): (
        "other",
        1,
    ),
    (
        "modules/case/api/routes/conversation.py",
        "reclassify_evidence",
        "get",
        "body",
    ): ("other", 1),
    (
        "modules/agent/tools/reclassify_evidence_tool.py",
        "ReclassifyEvidenceTool.execute_with_context",
        "get",
        "params",
    ): ("other", 1),
    # ``preprocessing_result.data_type`` / a classifier result.
    (
        _RECLASSIFICATION,
        "_handle_file_reclassification",
        "attr",
        "preprocessing_result",
    ): ("other", 1),
    (
        _SVC,
        "InvestigationService.reclassify_evidence",
        "attr",
        "preprocessing_result",
    ): ("other", 1),
    (
        "modules/preprocessing/preprocessing_service.py",
        "PreprocessingService.classify_and_extract",
        "attr",
        "classification",
    ): ("other", 1),
    (
        "modules/preprocessing/classifier.py",
        "_opik_track_classifier.wrapper",
        "attr",
        "result",
    ): ("other", 1),
    # LLM routing kwargs; pattern-learner feedback dicts.
    ("infrastructure/llm/router.py", "LLMRouter.generate", "get", "kwargs"): (
        "other",
        1,
    ),
    (
        "core/processing/pattern_learner.py",
        "PatternLearner._analyze_feedback",
        "get",
        "actual_result",
    ): ("other", 1),
    (
        "core/processing/pattern_learner.py",
        "PatternLearner._analyze_feedback",
        "get",
        "predicted_result",
    ): ("other", 1),
    # The prompt attribute's NAME (``_attr("data_type", …)``), not a read —
    # the orphan-file block's value is the ``uf`` site above; the evidence
    # block's is ``ev.source_type``.
    (_CB, "_render_orphan_file_block", "name_arg", "_attr('data_type')"): ("other", 1),
    (_CB, "_render_evidence_block", "name_arg", "_attr('data_type')"): ("other", 1),
    # Prompt prose that happens to contain "update"/"select" and the word —
    # one site in each submodule that now holds it (blocks.py:
    # _EVIDENCE_GROUNDING_BLOCK; investigation.py: INVESTIGATION_BASE).
    (
        "core/investigation/prompts/templates/blocks.py",
        "<module>",
        "sql",
        "<sql>",
    ): ("other", 1),
    (
        "core/investigation/prompts/templates/investigation.py",
        "<module>",
        "sql",
        "<sql>",
    ): ("other", 1),
    # The legacy data-ingestion service's own in-memory classification
    # results; it never touches ``uploaded_files``.
    (_INGEST, "CaseDataIngestionService._calculate_confidence_score", "attr", "data"): (
        "other",
        1,
    ),
    (
        _INGEST,
        "CaseDataIngestionService._combine_processing_insights",
        "getattr",
        "classification_result",
    ): ("other", 1),
    (_INGEST, "CaseDataIngestionService._detect_anomalies", "attr", "data"): (
        "other",
        2,
    ),
    (
        _INGEST,
        "CaseDataIngestionService._execute_data_ingestion",
        "attr",
        "classification_result",
    ): ("other", 2),
    (
        _INGEST,
        "CaseDataIngestionService._record_enhanced_operation",
        "attr",
        "result",
    ): ("other", 1),
    (
        _INGEST,
        "CaseDataIngestionService.get_processing_insights",
        "attr",
        "entry['result']",
    ): ("other", 1),
    (_INGEST, "CaseDataIngestionService.get_processing_insights", "attr", "result"): (
        "other",
        1,
    ),
    (
        _INGEST,
        "CaseDataIngestionService.ingest_data_enhanced",
        "attr",
        "classification_result",
    ): ("other", 3),
    (
        _INGEST,
        "CaseDataIngestionService.ingest_data_enhanced",
        "get",
        "regular_result",
    ): ("other", 1),
    (
        _INGEST,
        "CaseDataIngestionService.learn_from_feedback",
        "attr",
        "original_processing['result']",
    ): ("other", 1),
    (
        _INGEST,
        "CaseDataIngestionService._execute_data_analysis",
        "name_arg",
        "_get_data_attribute(data)",
    ): ("other", 2),
    (
        _INGEST,
        "CaseDataIngestionService._execute_data_deletion",
        "name_arg",
        "_get_data_attribute(data)",
    ): ("other", 1),
    (
        _INGEST,
        "CaseDataIngestionService._generate_recommendations",
        "name_arg",
        "_get_data_attribute(data)",
    ): ("other", 1),
}

#: Functions that read ``UploadedFile.data_type``: 4 boundary + 5 opaque +
#: 9 repository pass-through. Stated here so a change to it is a decision.
_N_COLUMN_READERS = 18


def test_every_reader_of_uploaded_file_data_type_is_classified():
    """State N: every read SITE of ``data_type`` in the package, and what it is.

    **N = 18 functions read ``UploadedFile.data_type``**: 4 need the 6-valued
    type and go through the boundary, 5 use it as an opaque string, 9 are
    repository pass-throughs (SQL text, positional row reads, keyword and
    ``**`` construction of ``UploadedFile``). Every other ``data_type`` read
    in the package is listed as ``other`` with its reason. Two readers parsed
    ONE vocabulary before #583's fix — ``vectorize_file_tool`` (found by
    review) and ``deep_analysis_tool``'s file-id branch (found by this scan).

    **Shapes matched** — keyed on the read's shape and the object read from,
    never on a variable's spelling:

    - ``x.data_type`` (load), ``getattr(x, "data_type", …)``,
      ``x.get("data_type", …)``, ``x["data_type"]``;
    - ``helper(x, "data_type", …)`` — any call taking the attribute NAME
      (``_get_data_attribute``), except ``setattr``;
    - ``{"data_type": row[13]}`` — a positional row read into a dict;
    - ``UploadedFile(…, data_type=…)`` and ``UploadedFile(**x)``;
    - a non-docstring string literal naming ``data_type`` in SQL
      (``SELECT`` / ``INSERT`` / ``UPDATE`` / ``json_build_object``).

    And a PARSE of a read's value — what reads it as ONE vocabulary (#1646):

    - handing it to ``UnifiedDataType`` / ``EvidenceSourceType`` /
      ``DataType`` / ``DetailedDataType``, as a positional or a keyword
      argument (``UnifiedDataType(value=x)``);
    - a lookup keyed on it: ``MAP.get(x)``, ``MAP[x]``
      (``UnifiedDataType[x.upper()]`` included);
    - a comparison, with the value on either side, against string literals
      (``== "logs"``, ``in ("logs", …)``), against enum members or a
      tuple/list/set of them (``== UnifiedDataType.LOGS``,
      ``== EvidenceSourceType.LOGS.value``), or ``in`` / ``not in`` a module
      constant (``_LOG_TYPES``, ``mod.LOG_TYPES``) whose members the
      comparison cannot see;
    - ``x.startswith("…")`` / ``x.endswith("…")`` with a literal argument.

    **Checked per SITE, not per function.** Each site's value is followed to
    what consumes it — the read itself, and every use of a local name it is
    bound to (one hop: ``name = …``, ``name: T = …``, ``(name := …)``, or its
    own element of ``a, b = x, y``) — through what keeps the value in its
    vocabulary: ``x or default``, ``a if c else b``, ``(n := x)``,
    ``str(x)`` and the normalising methods (``lower``, ``upper``, ``strip``,
    ``lstrip``, ``rstrip``, ``casefold``, ``title``). Any other method call
    ends the walk, and is not a parse (``x.replace("_", " ")``):

    - ``boundary``: the value reaches ``unified_data_type_of``;
    - ``opaque`` / ``passthrough``: it reaches no PARSE above;
    - ``other``: unchecked by construction. That is why the object read from
      is in the key: a read of ``res.uploaded_file`` added beside an
      ``other`` read of ``intent`` is a new site, and fails the census.

    **WHAT IT STILL MISSES**, stated rather than implied:

    - a constant KEY: ``entry.get(OFFERED_DATA_TYPE_KEY)`` (suggestion_liveness)
      names no ``"data_type"`` literal — it reads a suggestion entry, not the
      column, but a column read spelled through a constant would escape too;
    - a helper whose attribute name arrives in a variable or keyword;
    - dataflow beyond the one hop above, or through a function call (a
      parse inside a helper the value is passed to);
    - a parse in a CALLER of a function that returns the value in a
      container. 3 opaque sites do: ``suggestion_liveness.file_data_types``,
      ``_format_unpromoted_files`` and ``_engine_attachment_metadata``;
    - a one-vocabulary predicate in SQL text (``WHERE data_type IN ('logs',
      …)``). 0 today: the two SQL texts with ``data_type`` before ``=`` are
      the upserts' ``SET data_type = COALESCE(…)``;
    - membership in a set held in a lowercase local name (``x in
      seen_types``), which reads the same as a snapshot. 0 on a column read
      today;
    - a transforming method other than the normalisers (``x.replace(…)``,
      ``x.split(…)``): the walk stops there. 0 on a column read today;
    - ``match`` statements. The package has none today;
    - whole-row serialisation. It exists, and moves the value verbatim:
      ``Case.model_validate(case.model_dump())`` in the SQLite save,
      ``CheckpointService.create_checkpoint``'s ``case.model_dump()`` snapshot
      (a snapshot taken before #583 carries the 6-valued string, one taken
      after carries the ``DataType``; nothing restores a ``Case`` from a
      snapshot today, and if something did, the value would re-enter the
      column in a vocabulary every reader already accepts), and any
      ``model_dump`` / ``vars`` / ``**`` spread other than into
      ``UploadedFile`` itself;
    - SQL that names the column without one of the markers above, and a
      positional row read not built into a dict or ``UploadedFile(…)``.
    """
    sites, parsed = _scan()

    assert {
        "modules/agent/tools/vectorize_file_tool.py",
        "modules/agent/tools/deep_analysis_tool.py",
        _SVC,
        _SQLITE,
        _SQLITE_LOADING,
        _SQLITE_SAVING,
        _SQLITE_ROWS,
        _PG,
    } <= parsed, f"the token filter excluded a module holding a known reader: {parsed}"

    census: dict[tuple[str, str, str, str], int] = {}
    for site in sites:
        census[site.key] = census.get(site.key, 0) + 1
    expected = {key: count for key, (_, count) in _EXPECTED.items()}
    assert census == expected, (
        "a read of data_type was added, removed or duplicated — classify it "
        "in _EXPECTED. "
        f"new/changed: {sorted((k, v) for k, v in census.items() if expected.get(k) != v)}; "
        f"gone: {sorted(set(expected) - set(census))}"
    )

    column_readers = {
        (m, s) for (m, s, _, _), (c, _) in _EXPECTED.items() if c != "other"
    }
    assert len(column_readers) == _N_COLUMN_READERS

    for site in sites:
        category, _ = _EXPECTED[site.key]
        if category == "other":
            continue
        if category == "boundary":
            consumers = _consumers(site)
            assert _BOUNDARY in consumers, (
                f"{site.key} (line {site.node.lineno}) needs the 6-valued type "
                f"but its value never reaches {_BOUNDARY}: {sorted(consumers)}"
            )
        parses = _one_vocabulary_parses(site)
        assert not parses, (
            f"{site.key} (line {site.node.lineno}) is filed as {category} but "
            f"its value is read as one vocabulary by {sorted(parses)} — "
            f"go through {_BOUNDARY}"
        )


#: #1646's mechanism probe, as a table: the body of ``f`` below, whose one
#: read of the column is ``uf.data_type``. Every positive reads the value as
#: ONE vocabulary; every negative shares their tokens and does not. P10 and
#: P12 are two statements. The same rows, planted in place of the opaque read
#: in ``turn_pipeline.generate_implicit_query``, were run through the census
#: test above.
_PROBE_POSITIVES = {
    "P0-get-control": 'data_type_label = {"a": "b"}.get(uf.data_type) or "u"',
    "P1-get-str-lower": (
        'data_type_label = {"a": "b"}.get(str(uf.data_type).lower()) or "u"'
    ),
    "P2-get-strip": 'data_type_label = {"a": "b"}.get(uf.data_type.strip())',
    "P3-eq-enum-member": (
        'data_type_label = "L" if uf.data_type == UnifiedDataType.LOGS else "x"'
    ),
    "P4-eq-enum-member-value": (
        'data_type_label = "L" if uf.data_type == EvidenceSourceType.LOGS.value'
        ' else "x"'
    ),
    "P5-in-enum-members": (
        'data_type_label = "L" if uf.data_type in'
        ' (UnifiedDataType.LOGS, UnifiedDataType.METRICS) else "x"'
    ),
    "P6-in-named-set": 'data_type_label = "L" if uf.data_type in _LOG_TYPES else "x"',
    "P7-parser-keyword": "data_type_label = UnifiedDataType(value=uf.data_type)",
    "P8-enum-subscript-upper": (
        "data_type_label = UnifiedDataType[uf.data_type.upper()]"
    ),
    "P9-startswith-literal": (
        'data_type_label = "L" if uf.data_type.startswith("log") else "x"'
    ),
    "P10-annotated-assignment-hop": (
        'dt: str = uf.data_type; data_type_label = {"a": "b"}.get(dt)'
    ),
    "P11-walrus-hop": (
        'data_type_label = {"a": "b"}.get(x) if (x := uf.data_type) else "u"'
    ),
    "P12-tuple-hop": (
        'dt, other = uf.data_type, 1; data_type_label = {"a": "b"}.get(dt)'
    ),
    "P13-get-casefold": 'data_type_label = {"a": "b"}.get(uf.data_type.casefold())',
    "P14-lower-eq-literal": (
        'data_type_label = "L" if uf.data_type.lower() == "logs" else "x"'
    ),
    "P15-str-in-literal-set": (
        'data_type_label = "L" if str(uf.data_type) in {"logs", "metrics"} else "x"'
    ),
    "P16-parser-str": "data_type_label = UnifiedDataType(str(uf.data_type))",
    "P17-ne-enum-member": (
        'data_type_label = "L" if uf.data_type != EvidenceSourceType.LOGS else "x"'
    ),
    "P18-walrus-eq-literal": (
        'data_type_label = "L" if (x := uf.data_type) == "logs" else "x"'
    ),
    "P19-not-in-module-constant": (
        'data_type_label = "L" if uf.data_type not in types_mod.LOG_TYPES else "x"'
    ),
    "P20-literal-eq-reversed": (
        'data_type_label = "L" if "logs" == uf.data_type else "x"'
    ),
    "P21-enum-eq-reversed": (
        'data_type_label = "L" if UnifiedDataType.LOGS == uf.data_type else "x"'
    ),
    "P22-get-strip-lower-chain": (
        'data_type_label = {"a": "b"}.get(uf.data_type.strip().lower())'
    ),
    "P23-parser-positional-and-keyword": (
        "data_type_label = DataType(uf.data_type, strict=True)"
    ),
}
_PROBE_NEGATIVES = {
    "N1-todays-label": 'data_type_label = uf.data_type or "unclassified data"',
    "N2-str-label": 'data_type_label = str(uf.data_type) or "u"',
    "N3-or-default-lower-label": (
        'data_type_label = (uf.data_type or "unclassified data").lower()'
    ),
    "N4-str-strip-label": 'data_type_label = str(uf.data_type).strip() or "u"',
    "N5-fstring": 'data_type_label = f"{uf.data_type}"',
    "N6-eq-snapshot-name": (
        'data_type_label = "same" if uf.data_type == previous else "x"'
    ),
    "N7-in-lowercase-local": (
        'data_type_label = "same" if uf.data_type in seen_types else "x"'
    ),
    "N9-replace-label": 'data_type_label = uf.data_type.replace("_", " ") or "u"',
    "N10-len": "data_type_label = len(uf.data_type)",
    "N11-startswith-variable": (
        'data_type_label = "L" if uf.data_type.startswith(prefix) else "x"'
    ),
    "N12-enum-near-not-compared": (
        "data_type_label = UnifiedDataType.LOGS.value if flag else uf.data_type"
    ),
    "N13-in-list-display": 'data_type_label = sorted([uf.data_type, "x"])',
    "N14-eq-other-attribute": (
        'data_type_label = "same" if uf.data_type == other.data_kind else "x"'
    ),
    "N15-is-none": 'data_type_label = "u" if uf.data_type is None else "x"',
    "N16-dict-keyword-label": "data_type_label = dict(label=uf.data_type)",
    "N17-logger-keyword": "data_type_label = log(extra=uf.data_type)",
}


@pytest.mark.parametrize(
    ("body", "is_parse"),
    [pytest.param(b, True, id=i) for i, b in _PROBE_POSITIVES.items()]
    + [pytest.param(b, False, id=i) for i, b in _PROBE_NEGATIVES.items()],
)
def test_the_per_site_rule_over_the_probe_table(body, is_parse):
    """#1646: the per-site rule, over every shape the probe ran.

    Each row is parsed as a module of its own and goes through the same
    walker (``_scan``) and the same per-site rule (``_one_vocabulary_parses``)
    as the census test, so a rule removed from either fails its own rows
    here, with no reader on the real tree having to be re-planted.
    """
    source = f"def f(uf, previous, seen_types, prefix, flag, other):\n    {body}\n"
    sites, parsed = _scan((("probe.py", ast.parse(source)),))

    assert parsed == {"probe.py"}
    # Exactly one read, of the column: a row that held none would pass as a
    # negative while checking nothing.
    assert [site.key for site in sites] == [("probe.py", "f", "attr", "uf")]
    parses = _one_vocabulary_parses(sites[0])
    if is_parse:
        assert parses, f"{body!r} reads the value as one vocabulary; no parse seen"
    else:
        assert not parses, f"{body!r} is no parse, but was read as {sorted(parses)}"
