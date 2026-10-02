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


#: Methods that return the string transformed but still in its vocabulary.
#: Called with no arguments, one passes the value on (``_flow_step``).
_NORMALISERS = frozenset(
    {"lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title"}
)


def _unpacking(seq: ast.AST) -> ast.Assign | None:
    """The statement, when *seq* is the ``x, y`` of ``a, b = x, y``: each
    element there binds its own name rather than the tuple carrying both."""
    stmt = getattr(seq, "_fm_parent", None)
    if (
        isinstance(seq, (ast.Tuple, ast.List))
        and isinstance(stmt, ast.Assign)
        and stmt.value is seq
        and isinstance(stmt.targets[0], (ast.Tuple, ast.List))
    ):
        return stmt
    return None


def _flow_step(node: ast.AST) -> tuple[ast.AST, str | None] | None:
    """One step up from *node* while the read's VALUE itself flows on, as
    ``(parent, the name the step binds or None)``; ``None`` where it stops.

    The value flows through ``x or d``; either BRANCH of ``a if c else b``
    (the test is a use of its own, not the value); ``(n := x)``, which also
    binds ``n``; ``str(x)`` and a bare ``f"{x}"``; a normalising method
    called with no arguments; and a container that holds it — a dict VALUE,
    a list, tuple or set element, a comprehension's element or a dict
    comprehension's value. A container carries the value, so what consumes
    the CONTAINER is what is classified. Nothing else passes it on: a call,
    a method with arguments, a subscript or an operator is a use, and stops
    the walk (``_use_shape``).
    """
    parent = getattr(node, "_fm_parent", None)
    if isinstance(parent, ast.BoolOp):
        return parent, None
    if isinstance(parent, ast.IfExp) and parent.test is not node:
        return parent, None
    if isinstance(parent, ast.NamedExpr) and parent.value is node:
        return parent, parent.target.id
    if (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id == "str"
        and len(parent.args) == 1
        and parent.args[0] is node
        and not parent.keywords
    ):
        return parent, None
    if isinstance(parent, ast.FormattedValue) and not parent.format_spec:
        joined = getattr(parent, "_fm_parent", None)
        if isinstance(joined, ast.JoinedStr) and len(joined.values) == 1:
            return joined, None  # ``f"{x}"`` is ``str(x)``
    if (
        isinstance(parent, ast.Attribute)
        and parent.value is node
        and parent.attr in _NORMALISERS
        and isinstance(getattr(parent, "_fm_parent", None), ast.Call)
        and parent._fm_parent.func is parent
        and not parent._fm_parent.args
    ):
        return parent._fm_parent, None
    if isinstance(parent, ast.Dict) and any(v is node for v in parent.values):
        return parent, None
    if (
        isinstance(parent, (ast.List, ast.Tuple, ast.Set))
        and isinstance(getattr(parent, "ctx", ast.Load()), ast.Load)
        and _unpacking(parent) is None
    ):
        return parent, None
    if isinstance(parent, ast.DictComp) and parent.value is node:
        return parent, None
    if (
        isinstance(parent, (ast.ListComp, ast.SetComp, ast.GeneratorExp))
        and parent.elt is node
    ):
        return parent, None
    return None


def _flow_top(node: ast.AST) -> tuple[ast.AST, list[str]]:
    """Where *node*'s value stops flowing, and every walrus name it bound on
    the way."""
    names: list[str] = []
    while (step := _flow_step(node)) is not None:
        node, name = step
        if name:
            names.append(name)
    return node, names


def _uses(site: _Site) -> list[ast.AST]:
    """The read, plus every load of a local name bound to its VALUE in the
    same function (one hop).

    A name is bound only where the value itself reaches it, after flowing as
    ``_flow_step`` says: ``n = v`` (every target a plain name, ``a = b = v``
    included), ``n: T = v``, ``(n := v)``, and a name's own element of
    ``a, b = v, y`` (equal lengths). A name bound to a CALL's result holds
    something else — ``data_type = unified_data_type_of(x)`` holds the
    boundary's output, and comparing that against ``UnifiedDataType.LOGS`` is
    the boundary's normal use, not a parse of the column. Any other target
    (an attribute, a subscript, ``+=``, a ``for``, a starred or mismatched
    unpacking) is a use of its own, and fails as unlisted.
    """
    reached: list[ast.AST] = [site.node]
    if site.func is None:
        return reached
    top, names = _flow_top(site.node)
    parent = getattr(top, "_fm_parent", None)
    if (
        isinstance(parent, ast.Assign)
        and parent.value is top
        and all(isinstance(t, ast.Name) for t in parent.targets)
    ):
        names += [t.id for t in parent.targets]
    elif (
        isinstance(parent, ast.AnnAssign)
        and parent.value is top
        and isinstance(parent.target, ast.Name)
    ):
        names.append(parent.target.id)
    elif (stmt := _unpacking(parent)) is not None:
        target = stmt.targets[0]
        if len(stmt.targets) == 1 and len(target.elts) == len(parent.elts):
            mine = target.elts[[e is top for e in parent.elts].index(True)]
            if isinstance(mine, ast.Name):
                names.append(mine.id)
    reached += [
        n
        for n in ast.walk(site.func)
        if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load)
    ]
    return reached


def _callee_name(call: ast.AST) -> str | None:
    fn = getattr(call, "func", None)
    return getattr(fn, "id", None) or getattr(fn, "attr", None)


def _use_shape(use: ast.AST) -> str:
    """What *use* hands the value to, once it stops flowing, as one word
    ``_ALLOWED_USES`` can name.

    ``bind`` (a plain-name target ``_uses`` follows), ``fstring``, ``return``,
    ``truthiness`` (the test of an ``if`` / ``while`` / ``assert`` /
    ``a if c else b``, or ``not``), ``presence`` (``is`` / ``is not`` /
    ``==`` / ``!=`` against ``None`` or ``""`` only), ``kw:<callee>``,
    ``spread:<callee>`` and ``call:<callee>``; and, never allowed,
    ``compare`` (any other comparison), ``store`` (a target ``_uses`` does
    not follow), ``dict-key`` and ``callee``. Anything else is the parent's
    node type — a method call is ``Attribute``, a subscript or slice
    ``Subscript``, ``+=`` ``AugAssign``, a ``for`` ``For``, a comprehension's
    iterable ``comprehension`` — and no node type is listed.
    """
    top, _ = _flow_top(use)
    parent = getattr(top, "_fm_parent", None)
    if isinstance(parent, (ast.IfExp, ast.If, ast.While, ast.Assert)) and (
        parent.test is top
    ):
        return "truthiness"
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return "truthiness"
    if isinstance(parent, ast.keyword):
        call = getattr(parent, "_fm_parent", None)
        return f"{'kw' if parent.arg else 'spread'}:{_callee_name(call)}"
    if isinstance(parent, ast.Call):
        if any(arg is top for arg in parent.args):
            return f"call:{_callee_name(parent)}"
        return "callee"
    if isinstance(parent, ast.FormattedValue):
        return "fstring"
    if isinstance(parent, (ast.Dict, ast.DictComp)):
        return "dict-key"  # a value would have flowed on
    if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
        stmt = _unpacking(parent)
        if (
            stmt is not None
            and len(stmt.targets) == 1
            and len(stmt.targets[0].elts) == len(parent.elts)
            and all(isinstance(t, ast.Name) for t in stmt.targets[0].elts)
        ):
            return "bind"
        return "store"
    if isinstance(parent, ast.Return):
        return "return"
    if isinstance(parent, ast.Assign) and parent.value is top:
        if all(isinstance(t, ast.Name) for t in parent.targets):
            return "bind"
        return "store"
    if isinstance(parent, ast.AnnAssign) and parent.value is top:
        return "bind" if isinstance(parent.target, ast.Name) else "store"
    if isinstance(parent, ast.Compare):
        others = [s for s in [parent.left, *parent.comparators] if s is not top]
        if all(
            isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) for op in parent.ops
        ) and all(
            isinstance(o, ast.Constant) and o.value in (None, "") for o in others
        ):
            return "presence"
        return "compare"
    return type(parent).__name__


#: Every use a classified read's value may have, each with the reason it does
#: not read the value as one vocabulary. Measured on the tree (#1646): these
#: are the uses the 17 functions with a non-SQL read of the column make, so a
#: use nobody listed — a parse in any shape, or a non-parse nobody has looked
#: at — fails closed. Callees are trusted by NAME (``call:info`` is any
#: ``….info(x)``); adding one is a claim about what that callee does.
_ALLOWED_USES: dict[str, str] = {
    "bind": "a plain local name; its own loads are checked in turn (one hop)",
    "fstring": "rendered among other text (what reads that text: stated limit)",
    "return": "handed to the caller, which is another function (stated limit)",
    "truthiness": "tested for presence, never for which value",
    "presence": "compared with None or the empty string, never with a type",
    "kw:UploadedFile": "the column rebuilt from its own row, verbatim",
    "spread:UploadedFile": "the column rebuilt from its own row, verbatim",
    "call:unified_data_type_of": "the read boundary itself, which takes both",
    "call:_attr": "context_builder/evidence.py's prompt attribute, verbatim",
    "call:_not_indexed": (
        "vectorize_file's refused-index ToolResult, the value as data verbatim"
    ),
    "call:info": "logged",
    "call:debug": "logged",
    "call:warning": "logged",
    "call:append": (
        "appended to a list another function reads (stated limit): the "
        "repositories' rows, list_evidence_by_time's result rows"
    ),
    "call:execute": "bound as a SQL parameter: the column written back verbatim",
    "kw:ToolResult": "a tool result's data, rendered for the model verbatim",
    "kw:store_in_vector_db_background": (
        "vectorize_file's chunk metadata (``file_data_type``), stored verbatim"
    ),
}


def _disallowed_uses(site: _Site) -> list[str]:
    """The per-site rule: each use of the site's value that ``_ALLOWED_USES``
    does not name, as ``"<shape> (line N)"``. Empty is what every
    ``boundary`` / ``opaque`` / ``passthrough`` site must be. SQL text is
    exempt — it names the column, and holds no Python value to follow."""
    if site.key[2] == "sql":
        return []
    return sorted(
        {
            f"{shape} (line {use.lineno})"
            for use in _uses(site)
            if (shape := _use_shape(use)) not in _ALLOWED_USES
        }
    )


def _reaches_boundary(site: _Site) -> bool:
    """Whether a use of the site's value is ``unified_data_type_of(…)``."""
    return any(_use_shape(use) == f"call:{_BOUNDARY}" for use in _uses(site))


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
#:   ``unified_data_type_of`` (one read per entry is enough; every read's
#:   uses must be ones ``_ALLOWED_USES`` names);
#: - ``opaque`` — a read of the column used as an uninterpreted string (a
#:   label, a key nobody reads, a value handed back to a caller); every use
#:   of its value must be one ``_ALLOWED_USES`` names;
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

    **Checked per SITE, and failing closed** (#1646). A parse has more
    spellings than any list of them — ``UnifiedDataType(x)``,
    ``to_unified_data_type(x)``, ``MAP.get(str(x).lower())``,
    ``x == UnifiedDataType.LOGS``, ``x in _LOG_TYPES``, ``x.split(":")[0]``
    — so the rule lists the USES a classified read may have instead, and
    fails on any other. Each site's value is followed:

    - **as it flows** — through ``x or d``, either branch of
      ``a if c else b`` (its test is a use), ``(n := x)``, ``str(x)`` and a
      bare ``f"{x}"``, a normalising method with no arguments (``lower``,
      ``upper``, ``strip``, ``lstrip``, ``rstrip``, ``casefold``, ``title``),
      and a container holding it (a dict value, a list, tuple or set
      element, a comprehension's element): what consumes the container is
      the use;
    - **one hop** — to every load of a local name the VALUE is bound to:
      ``n = v`` (``a = b = v`` included), ``n: T = v``, ``(n := v)``, or a
      name's own element of ``a, b = v, y``. A name holding a CALL's result
      is not followed: ``data_type = unified_data_type_of(x)`` holds the
      boundary's output, and ``data_type == UnifiedDataType.LOGS`` is that
      output's normal use;
    - **to its uses**, each of which must be one ``_ALLOWED_USES`` names
      with its reason: a binding, an f-string, a ``return``, a truth test, a
      comparison with ``None`` or ``""``, ``unified_data_type_of(x)``,
      ``UploadedFile(data_type=x)`` / ``UploadedFile(**…)``, a log call, and
      the verbatim sinks the classified functions use (a list ``append``, a
      SQL parameter, a tool result, chunk metadata, two label helpers).
      Anything else fails — a parse in any spelling, and equally a
      non-parse nobody has looked at (``x == previous``, ``x.replace(…)``,
      ``len(x)``). The fix is to go through the boundary, or to name the use
      with the reason it is not a parse.

    Per category:

    - ``boundary``: at least one read of the entry reaches
      ``unified_data_type_of``, and every read has only allowed uses
      (``vectorize_file_tool`` also tests the column as an ``if`` condition);
    - ``opaque`` / ``passthrough``: every read has only allowed uses. SQL text
      is exempt: it names the column, and holds no Python value to follow;
    - ``other``: unchecked by construction. That is why the object read from
      is in the key: a read of ``res.uploaded_file`` added beside an
      ``other`` read of ``intent`` is a new site, and fails the census.

    **WHAT IT STILL MISSES**, stated rather than implied:

    - a constant KEY: ``entry.get(OFFERED_DATA_TYPE_KEY)`` (suggestion_liveness)
      names no ``"data_type"`` literal — it reads a suggestion entry, not the
      column, but a column read spelled through a constant would escape too;
    - a helper whose attribute name arrives in a variable or keyword;
    - a reader in ANOTHER function. The value leaves by ``return`` in 3
      functions (``suggestion_liveness.file_data_types``,
      ``_engine_attachment_metadata``, ``_published_source_type``'s
      fallback), in a list ``append``-ed for a caller in 3
      (``_format_unpromoted_files`` and SQLite's two ``_load_uploaded_files*``),
      and into a log line in 1 (``vectorize_file``); a parse in that caller,
      or in a helper the value is passed to, is unchecked;
    - allowed callees are trusted by NAME: ``call:append`` is any
      ``….append(x)``, ``call:info`` any ``….info(x)`` — so a list appended
      to and then parsed in the same function passes;
    - text built AROUND the value: ``fstring`` is allowed whatever consumes
      the text, so ``UnifiedDataType(f"{x}_and_errors")`` passes. 0 today:
      the only two are ``generate_implicit_query``'s returned prompt text;
    - dataflow beyond the one hop above (``b = a`` after ``a = x.data_type``);
    - a one-vocabulary predicate in SQL text (``WHERE data_type IN ('logs',
      …)``). 0 today: the two SQL texts with ``data_type`` before ``=`` are
      the upserts' ``SET data_type = COALESCE(…)``;
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

    # A boundary ENTRY needs one of its reads to reach the boundary. Its other
    # reads (``vectorize_file_tool`` tests the column as an ``if`` condition
    # beside the read it folds) are held to the allow-list like any read.
    boundary = {key for key, (cat, _) in _EXPECTED.items() if cat == "boundary"}
    reaching = {site.key for site in sites if _reaches_boundary(site)}
    assert boundary <= reaching, (
        f"needs the 6-valued type, but no read reaches {_BOUNDARY}: "
        f"{sorted(boundary - reaching)}"
    )

    for site in sites:
        category, _ = _EXPECTED[site.key]
        if category == "other":
            continue
        unlisted = _disallowed_uses(site)
        assert not unlisted, (
            f"{site.key} (read on line {site.node.lineno}) is filed as "
            f"{category}, and its value has a use _ALLOWED_USES does not list: "
            f"{unlisted}. An unlisted use may be read as one vocabulary — go "
            f"through {_BOUNDARY}, or name the use in _ALLOWED_USES with the "
            f"reason it is not a parse"
        )


#: #1646's probe, as a table. Each row is the body of ``f`` below, whose one
#: read of the column is ``uf.data_type``; multi-statement rows are joined by
#: ``;``. The same rows, planted in place of the opaque read in
#: ``turn_pipeline.generate_implicit_query``, were run through the census test.
#:
#: A row ``fails`` when a use of the value is one ``_ALLOWED_USES`` does not
#: name: every parse (P: the first probe's positives; M: shapes a review found
#: the first, deny-list rule missed; R: the review's added rows), and every
#: non-parse nobody has listed (U), which fails closed rather than being
#: guessed at.
_PROBE_FAILS = {
    "P0-get": 'data_type_label = {"a": "b"}.get(uf.data_type) or "u"',
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
    "M1-to-unified-data-type": "data_type_label = to_unified_data_type(uf.data_type)",
    "M2-eq-constant": 'data_type_label = "L" if uf.data_type == LOGS_VALUE else "x"',
    "M3-model-keyword": (
        "data_type_label = PreprocessingResult(data_type=uf.data_type)"
    ),
    "M4-strip-replace-then-parse": (
        'data_type_label = UnifiedDataType(uf.data_type.strip().replace(" ", "_"))'
    ),
    "M5-fstring-then-parse": 'data_type_label = UnifiedDataType(f"{uf.data_type}")',
    "M6-split-index-lookup": (
        'data_type_label = _TYPE_MAP[uf.data_type.split(":")[0]]'
    ),
    "M7-slice-eq": 'data_type_label = "L" if uf.data_type[:4] == "logs" else "x"',
    "M8-cast-then-parse": (
        "data_type_label = UnifiedDataType(cast(str, uf.data_type))"
    ),
    "M9-getattr-value": (
        'data_type_label = "L" if getattr(uf.data_type, "value", "") == "logs"'
        ' else "x"'
    ),
    "M10-startswith-constant": (
        'data_type_label = "L" if uf.data_type.startswith(LOG_PREFIX) else "x"'
    ),
    "M11-in-tuple-of-constants": (
        'data_type_label = "L" if uf.data_type in (LOGS, METRICS) else "x"'
    ),
    "M12-multi-target-binding": (
        'dt = label = uf.data_type; data_type_label = {"a": "b"}.get(dt)'
    ),
    "M13-attribute-store": (
        'ns.dt = uf.data_type; data_type_label = {"a": "b"}.get(ns.dt)'
    ),
    "M14-for-over-tuple": (
        "data_type_label = [UnifiedDataType(t) for t in (uf.data_type,)]"
    ),
    "M15-with-context": "data_type_label = ctx(uf.data_type)",
    "M16-augmented-assignment": (
        'dt = ""; dt += uf.data_type; data_type_label = {"a": "b"}.get(dt)'
    ),
    "M17-starred-unpacking": (
        'dt, *rest = uf.data_type, 1, 2; data_type_label = {"a": "b"}.get(dt)'
    ),
    "M18-list-then-comprehension": (
        "types = [uf.data_type]; data_type_label = [UnifiedDataType(t) for t in types]"
    ),
    "M19-next-over-enum": (
        "data_type_label = next(t for t in UnifiedDataType if t.value == uf.data_type)"
    ),
    "M20-aliased-enum": (
        'data_type_label = "L" if uf.data_type == DT.LOGS_AND_ERRORS else "x"'
    ),
    "M21-in-enum-class": (
        'data_type_label = "L" if uf.data_type in UnifiedDataType else "x"'
    ),
    "M22-str-eq-none-text": (
        'data_type_label = "L" if str(uf.data_type) == "None" else "x"'
    ),
    "M23-dict-then-parse": (
        'd = {"k": uf.data_type}; data_type_label = UnifiedDataType(d["k"])'
    ),
    "M24-in-mixed-tuple": (
        'data_type_label = "L" if uf.data_type in ("logs", UnifiedDataType.METRICS)'
        ' else "x"'
    ),
    "M25-removesuffix-eq": (
        'data_type_label = "L" if uf.data_type.removesuffix("_errors") == "logs"'
        ' else "x"'
    ),
    "R2-multi-target-second-name": (
        "dt = label = uf.data_type; data_type_label = UnifiedDataType(label)"
    ),
    "R3-attribute-store-alone": "self.data_type_seen = uf.data_type",
    "R4-container-into-parse": (
        'data_type_label = PreprocessingResult(**{"data_type": uf.data_type})'
    ),
    "U6-eq-snapshot-name": (
        'data_type_label = "same" if uf.data_type == previous else "x"'
    ),
    "U7-in-lowercase-local": (
        'data_type_label = "same" if uf.data_type in seen_types else "x"'
    ),
    "U9-replace-label": 'data_type_label = uf.data_type.replace("_", " ") or "u"',
    "U10-len": "data_type_label = len(uf.data_type)",
    "U11-startswith-variable": (
        'data_type_label = "L" if uf.data_type.startswith(prefix) else "x"'
    ),
    "U13-in-list-display": 'data_type_label = sorted([uf.data_type, "x"])',
    "U14-eq-other-attribute": (
        'data_type_label = "same" if uf.data_type == other.data_kind else "x"'
    ),
    "U16-dict-keyword-label": "data_type_label = dict(label=uf.data_type)",
    "U17-logger-keyword": "data_type_label = log(extra=uf.data_type)",
}

#: A row ``passes`` when every use of the value is one ``_ALLOWED_USES``
#: names (A): the shapes the classified readers really use, so that removing
#: any flow step, use shape or allowed entry fails a row here — all but
#: ``spread:UploadedFile``, whose row would hold a second read (the
#: ``construct_spread`` site itself). R1 is the boundary's OUTPUT bound to a
#: name and compared with an enum member: that output's normal use, and no
#: read of the column.
_PROBE_PASSES = {
    "A1-todays-label": 'data_type_label = uf.data_type or "unclassified data"',
    "A2-str-label": 'data_type_label = str(uf.data_type) or "u"',
    "A3-or-default-lower-label": (
        'data_type_label = (uf.data_type or "unclassified data").lower()'
    ),
    "A4-fstring-label": 'data_type_label = f"type: {uf.data_type}"',
    "A5-is-none": 'data_type_label = "u" if uf.data_type is None else "x"',
    "A6-eq-empty": 'data_type_label = "u" if uf.data_type == "" else "x"',
    "A7-branch-value": (
        "data_type_label = UnifiedDataType.LOGS.value if flag else uf.data_type"
    ),
    "A8-not": 'data_type_label = "x" if not uf.data_type else "y"',
    "A9-str-strip-label": 'data_type_label = str(uf.data_type).strip() or "u"',
    "A10-bare-fstring": 'data_type_label = f"{uf.data_type}"',
    "A11-return": "return uf.data_type",
    "A12-if-test": 'if uf.data_type: data_type_label = "x"',
    "A13-uploaded-file-keyword": "data_type_label = UploadedFile(label=uf.data_type)",
    "A14-attr-label": 'data_type_label = _attr("type", uf.data_type)',
    "A15-not-indexed-label": (
        "data_type_label = self._not_indexed(outcome, evidence_id, uf.data_type)"
    ),
    "A16-logged-info": 'data_type_label = logger.info("t=%s", uf.data_type)',
    "A17-logged-debug": 'data_type_label = logger.debug("t=%s", uf.data_type)',
    "A18-logged-warning": 'data_type_label = logger.warning("t=%s", uf.data_type)',
    "A19-appended-row": 'rows.append({"data_type": uf.data_type})',
    "A20-sql-parameter": 'db.execute(query, {"data_type": uf.data_type})',
    "A21-tool-result": (
        'data_type_label = ToolResult(success=True, data={"data_type": uf.data_type})'
    ),
    "A22-chunk-metadata": (
        "data_type_label = store_in_vector_db_background("
        'metadata={"file_data_type": uf.data_type})'
    ),
    "A23-walrus-label": 'data_type_label = (x := uf.data_type) or "u"',
    "A24-list-element": "data_type_label = [uf.data_type]",
    "A25-dict-comprehension-value": (
        "data_type_label = {k: uf.data_type for k in flag}"
    ),
    "A26-comprehension-element": "data_type_label = [uf.data_type for _ in flag]",
    "A27-tuple-unpacking-label": (
        'dt, o = uf.data_type, 1; data_type_label = f"type: {dt}"'
    ),
    "A28-annotated-label": 'dt: str = uf.data_type or "u"',
    "R1-boundary-output-bound": (
        "data_type = unified_data_type_of(uf.data_type) or UnifiedDataType.TEXT; "
        'data_type_label = "M" if data_type == UnifiedDataType.METRICS else "x"'
    ),
}


@pytest.mark.parametrize(
    ("body", "verdict"),
    [pytest.param(b, "fails", id=i) for i, b in _PROBE_FAILS.items()]
    + [pytest.param(b, "passes", id=i) for i, b in _PROBE_PASSES.items()],
)
def test_the_per_site_rule_over_the_probe_table(body, verdict):
    """#1646: the per-site rule, over every row the probes ran.

    Each row is parsed as a module of its own and goes through the same
    walker (``_scan``) and the same per-site rule (``_disallowed_uses``) as
    the census test, so a flow step, binding form, use shape or allow-list
    entry removed from the rule fails its own rows here — and this runs where
    the census scan cannot (a shadowing install skips that one).
    """
    source = f"def f(uf, previous, seen_types, prefix, flag, other):\n    {body}\n"
    sites, parsed = _scan((("probe.py", ast.parse(source)),))

    assert parsed == {"probe.py"}
    # Exactly one read, of the column: a row that held none would pass while
    # checking nothing.
    assert [site.key for site in sites] == [("probe.py", "f", "attr", "uf")]
    unlisted = _disallowed_uses(sites[0])
    if verdict == "fails":
        assert unlisted, f"{body!r} has a use _ALLOWED_USES does not list"
    else:
        assert not unlisted, f"{body!r} has only allowed uses, but got {unlisted}"
