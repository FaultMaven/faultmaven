"""The KB write side names its tier; it does not inherit one (#1166).

Four write-side sites defaulted ``scope`` to ``"global"`` — the platform tier
that **every tenant reads**:

===========================================================  ===================
Site                                                         Old default
===========================================================  ===================
``KnowledgeBaseDocument.scope``                               ``"global"``
``KnowledgeService.ingest_runbook(scope=...)``                ``"global"``
``KnowledgeService.upload_document(scope=...)``               ``"global"``
``_index_document_in_vector_store``'s scope read              ``getattr(document, "scope", "global") or "global"``
===========================================================  ===================

The last reached ``global`` three separate ways: pass it, omit it, or pass a
falsy value. So a publish path that simply *neglected* to set a scope published
tenant-authored content platform-wide — and an omission shows up nowhere in a
diff, which is the one failure shape review is worst at catching. The read side
already fails CLOSED on the same omission (an unidentifiable principal collapses
to ``{"scope": "global"}``, i.e. reads LESS). These tests pin the write side
doing the same.

**Not a leak fix.** The adversarial probe that surfaced this (#1162, finding F3)
found no reachable leak: every global-authoring entry point refuses a tenant
session, at the route layer (``require_global_authoring_allowed``) and at the
service layer (``ensure_global_authoring_allowed``). What changes here is the
shape of the failure the *next* publish path can have, not a live exposure.

The platform tier stays reachable when it is asked for by name — the shipped
runbook bootstrap (``bootstrap/kb_init.py`` → ``ingest_runbook``) publishes
genuinely global content and is covered by ``test_kb_init.py`` plus the positive
control below.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from faultmaven.models.api import KnowledgeBaseDocument
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[4] / "faultmaven"

_NOW = "2026-08-26T00:00:00Z"

# Sentinel for "the attribute is absent entirely", distinct from a falsy value.
_OMITTED = object()


# ---------------------------------------------------------------------------
# The four sites, one test each
# ---------------------------------------------------------------------------


def test_the_document_model_requires_a_tier():
    """Site 1. A default here is inherited by every construction that stays
    quiet, which is precisely the caller this issue is about."""
    field = KnowledgeBaseDocument.model_fields["scope"]
    assert field.is_required(), (
        "KnowledgeBaseDocument.scope has a default again "
        f"({field.default!r}) — a publish path can once more omit its tier"
    )

    with pytest.raises(ValidationError):
        KnowledgeBaseDocument(
            document_id="doc-1",
            title="Tenant runbook",
            content="# Tenant runbook",
            document_type="runbook",
            created_at=_NOW,
            updated_at=_NOW,
        )


@pytest.mark.parametrize("method", ["ingest_runbook", "upload_document"])
def test_the_publishing_service_methods_require_a_tier(method):
    """Sites 2 and 3, at the signature: no default to fall back on."""
    param = inspect.signature(getattr(KnowledgeService, method)).parameters["scope"]
    assert param.default is inspect.Parameter.empty, (
        f"KnowledgeService.{method} defaults scope to {param.default!r} — "
        "a caller that says nothing publishes to that tier"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["ingest_runbook", "upload_document"])
async def test_a_publish_call_that_omits_the_tier_raises_instead_of_publishing(
    method,
):
    """Sites 2 and 3, behaviourally: the omission is refused before anything is
    written. TypeError from the interpreter, so no amount of service-internal
    rearrangement can quietly restore the old behaviour."""
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = MagicMock()
    service._db_session_factory = MagicMock()

    kwargs: dict[str, Any] = {
        "ingest_runbook": dict(
            document_id="doc-1",
            title="Tenant runbook",
            content="# Tenant runbook\n\nRemediation.",
            organization_id="org-1",
        ),
        "upload_document": dict(
            content="# Tenant runbook\n\nRemediation.",
            title="Tenant runbook",
            document_type="runbook",
        ),
    }[method]

    with pytest.raises(TypeError, match="scope"):
        await getattr(service, method)(**kwargs)


def _tierless_document(scope: Any = _OMITTED) -> Any:
    """A publishable document that is real in every way except its tier.

    Deliberately NOT a ``MagicMock``: a Mock's ``content`` is unchunkable, so
    the indexer would refuse it for a reason that has nothing to do with scope
    and the test would pass against the very code it is meant to catch. This
    duck chunks, embeds and indexes cleanly — so on the pre-fix code it is
    stamped ``global`` and written, which is exactly the failure being pinned.
    """

    class _Doc:
        document_id = "tenant-authored-0001"
        title = "Tenant runbook"
        content = "# Tenant runbook\n\nInternal-only remediation."
        document_type = "runbook"
        tags: list[str] = []
        source_url = None
        owner_id = None
        created_at = _NOW
        updated_at = _NOW

    if scope is not _OMITTED:
        _Doc.scope = scope
    return _Doc()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [_OMITTED, None, ""])
async def test_the_indexer_refuses_a_document_with_no_usable_tier(scope):
    """Site 4: the choke point every KB vector write funnels through.

    The model's requirement stops an omission at construction. This is the belt
    to that brace — for anything that reaches the indexer without having gone
    through the model (a duck-typed stand-in, a future DTO), and for the falsy
    value the old ``getattr(..., "global") or "global"`` swallowed. Those were
    the two of its three routes to the platform tier that were not a decision.
    """
    service, added = _service_with_capturing_store()

    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=_embed,
    ):
        with pytest.raises(KnowledgeBaseError) as exc:
            await service._index_document_in_vector_store(_tierless_document(scope))

    assert exc.value.error_code == "KNOWLEDGE_SCOPE_REQUIRED"
    assert added == [], (
        "chunks were written for a document with no tier: "
        f"{[d[0]['metadata']['scope'] for d in added]}"
    )


@pytest.mark.asyncio
async def test_the_indexer_refuses_before_it_looks_for_a_vector_store():
    """The refusal is about the document, not the deployment.

    ``_index_document_in_vector_store`` returns 0 when no store is configured.
    Ordering the scope check after that would make it fire only where a store
    happens to exist — a guard that some deployments do not have is not a
    guard, and the caller would learn nothing before its next write.
    """
    service = KnowledgeService.__new__(KnowledgeService)
    service._vector_store = None

    with pytest.raises(KnowledgeBaseError) as exc:
        await service._index_document_in_vector_store(_tierless_document())
    assert exc.value.error_code == "KNOWLEDGE_SCOPE_REQUIRED"


# ---------------------------------------------------------------------------
# The positive control — the tier is still reachable when it is stated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stated", "stamped"),
    [("global", "global"), ("team", "personal"), ("personal", "personal")],
)
async def test_a_stated_tier_is_stamped_exactly_as_before(stated, stamped):
    """A refusal that refused everything would pass every test above while
    breaking the shipped-runbook bootstrap. Nothing about the stamping changed:
    ``global`` still stamps ``global`` (so ``bootstrap/kb_init.py`` still seeds
    the platform corpus), and the org-owned tiers still collapse to the
    ``personal`` floor — team visibility lives in the share table, never in
    chunk metadata (ADR-013 §D4).
    """
    service, added = _service_with_capturing_store()

    document = KnowledgeBaseDocument(
        document_id="doc-1",
        title="Draining a node",
        content="# Draining a node\n\nCordon, then drain.",
        document_type="runbook",
        scope=stated,
        created_at=_NOW,
        updated_at=_NOW,
    )

    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=_embed,
    ):
        chunks = await service._index_document_in_vector_store(document)

    assert chunks == 1
    assert added[0][0]["metadata"]["scope"] == stamped


# ---------------------------------------------------------------------------
# The durable guard: a NEW publish path cannot omit the tier either
# ---------------------------------------------------------------------------


def test_no_production_construction_of_the_document_model_omits_its_tier():
    """The point of #1166 is the publish path nobody has written yet.

    A required field already makes such a path fail at runtime — but only once
    something runs it, and an untested write path is exactly the one that
    reaches production. This walks the shipped tree instead, so the omission is
    a failing test rather than a first-call traceback. Splats (``**kwargs``) are
    accepted as "cannot be read statically"; there are none today.
    """
    offenders: list[str] = []

    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "KnowledgeBaseDocument":
                continue
            names = {kw.arg for kw in node.keywords}
            if "scope" in names or None in names:
                continue
            rel = path.relative_to(_SOURCE_ROOT)
            offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "these build a KnowledgeBaseDocument without naming its knowledge tier "
        f"— the omitted tier used to mean 'global', readable by every tenant: {offenders}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _embed(texts, **_kwargs):
    return [[0.5] * 8 for _ in texts]


def _service_with_capturing_store() -> tuple[KnowledgeService, list[Any]]:
    added: list[Any] = []

    async def _add_documents(documents, embeddings=None):
        added.append(documents)

    service = KnowledgeService.__new__(KnowledgeService)
    store = MagicMock()
    store.delete_documents_by_parent_id = AsyncMock(return_value=0)
    store.add_documents = AsyncMock(side_effect=_add_documents)
    service._vector_store = store
    service._extract_frontmatter_for_rag = staticmethod(lambda content: {})
    return service, added
