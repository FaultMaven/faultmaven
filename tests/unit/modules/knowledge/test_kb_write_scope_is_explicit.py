"""The KB write side names its tier; it does not inherit one (#1166).

``global`` is the platform corpus — the tier **every tenant reads**. Six
write-side sites defaulted to it, so a publish path that simply *neglected* to
set a scope published tenant-authored content platform-wide, and the omission
showed up nowhere in a diff:

=========================================================  ===========================
Site                                                       Old default
=========================================================  ===========================
``KnowledgeBaseDocument.scope``                            ``"global"``
``KnowledgeService.ingest_runbook(scope=...)``             ``"global"``
``KnowledgeService.upload_document(scope=...)``            ``"global"``
``KnowledgeService._index_document_in_vector_store``       ``getattr(d, "scope", "global") or "global"``
``KnowledgeIngester.ingest_document(scope=...)``           ``"global"``
``KnowledgeIngester._process_and_store``                   ``getattr(d, "scope", None) or "global"``
``KbPack.load`` (``bootstrap/kb_pack.py``)                 ``rb.get("scope", "global")``
=========================================================  ===========================

The two indexer reads reached ``global`` three separate ways — pass it, omit it,
or pass a falsy value. The read side already fails CLOSED on the same omission
(an unidentifiable principal collapses to ``{"scope": "global"}``, i.e. reads
LESS); these tests pin the write side doing the same.

**Two ChromaDB writers, not one.** The original fix guarded
``_index_document_in_vector_store`` and claimed every KB vector write funnelled
through it. It does not: ``KnowledgeIngester._process_and_store`` writes with
``collection.add`` directly — no ``knowledge_items`` row, hence no RLS write
policy. It is **dead** (nothing routes to it, pinned by the probe's
``test_the_unguarded_chroma_only_writer_still_has_no_live_caller``), so this was
never a live leak there either — but #1166 is specifically about the shape the
*next* publish path has, and a dead writer someone revives is that path.

**And ``kb_pack`` is the live one.** A ``pack.json`` entry omitting ``scope``
landed in the platform corpus by exactly the omission being closed. The pack is
built in another repository, so that omission is one nobody reviews in the same
diff as the code trusting it.

**Not a leak fix.** The adversarial probe that surfaced this (#1162, F3) found
no reachable leak: every global-authoring entry point refuses a tenant session,
at the route layer (``require_global_authoring_allowed``) and the service layer
(``ensure_global_authoring_allowed``). What changes is the failure's shape.

The platform tier stays reachable when it is asked for by name — the shipped
runbook bootstrap publishes genuinely global content. Its read consequence is
measured against a real ChromaDB in
``tests/integration/security/test_kb_tenant_isolation_probe.py``.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from faultmaven.models.api import KnowledgeBaseDocument
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.write_scope import require_write_scope

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Every tree whose code may publish to the KB. ``scripts/`` is in deliberately:
#: ``migration_backfill_scopes.py`` builds a ``KnowledgeBaseDocument`` and hands
#: it to the private indexer, so it is a publish path by any honest reading even
#: though the wheel excludes it.
_PUBLISHING_ROOTS = ("faultmaven", "scripts")

#: Callees the walk must FIND calls to, so a pin cannot report "no violations"
#: when it in fact scanned nothing. One is enough to prove the walk works; the
#: floors stay at 1 so that legitimately deleting a call site does not fail a
#: test about scanner health. See
#: ``test_the_ast_pins_are_actually_scanning_something``.
#:
#: ``ingest_document`` is deliberately ABSENT: it has zero calls by design (both
#: were deleted in #1166), and that zero is pinned by the probe's
#: ``test_the_unguarded_chroma_only_writer_still_has_no_live_caller``. Demanding
#: >=1 here would assert the opposite of what the codebase should hold. It stays
#: in the call-site pin below, which is what catches a NEW tierless caller.
_EXPECTED_AT_LEAST = {
    "KnowledgeBaseDocument": 1,
    "ingest_runbook": 1,
    "upload_document": 1,
}

_NOW = "2026-08-26T00:00:00Z"

#: "The attribute is absent entirely", distinct from any falsy value it could
#: hold. A plain string sentinel would silently mean "absent" if it ever became
#: a real tier.
_OMITTED = object()


# ---------------------------------------------------------------------------
# Sites 1-3 and 5: the signatures
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


@pytest.mark.parametrize(
    ("owner", "method"),
    [
        (KnowledgeService, "ingest_runbook"),
        (KnowledgeService, "upload_document"),
        # The second writer's entry point. Absent from the original fix, and
        # invisible to a pin that only knew about KnowledgeService.
        (KnowledgeIngester, "ingest_document"),
    ],
)
def test_every_publishing_signature_requires_a_tier(owner, method):
    """Sites 2, 3 and 5: no default to fall back on."""
    param = inspect.signature(getattr(owner, method)).parameters["scope"]
    assert param.default is inspect.Parameter.empty, (
        f"{owner.__name__}.{method} defaults scope to {param.default!r} — "
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


# ---------------------------------------------------------------------------
# Sites 4 and 6: BOTH ChromaDB writers
# ---------------------------------------------------------------------------


def _tierless_document(scope: Any = _OMITTED) -> Any:
    """A publishable document that is real in every way except its tier.

    Deliberately NOT a ``MagicMock``: a Mock's ``content`` is unchunkable, so a
    writer would refuse it for a reason that has nothing to do with scope and
    the test would pass against the very code it is meant to catch. This duck
    chunks, embeds and indexes cleanly — so pre-fix it is stamped ``global`` and
    written, which is exactly the failure being pinned.
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
async def test_the_live_indexer_refuses_a_document_with_no_tier(scope):
    """Site 4: the writer behind uploads, conversions and the bootstrap."""
    service, added = _service_with_capturing_store()

    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=_embed,
    ):
        with pytest.raises(KnowledgeBaseError) as exc:
            await service._index_document_in_vector_store(_tierless_document(scope))

    assert exc.value.error_code == "KNOWLEDGE_SCOPE_REQUIRED"
    assert added == [], "chunks were written for a document with no tier"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [_OMITTED, None, ""])
async def test_the_second_chroma_writer_refuses_a_document_with_no_tier(scope):
    """Site 6: ``KnowledgeIngester._process_and_store``.

    The writer the original fix missed. It reaches ChromaDB through
    ``collection.add`` with no ``knowledge_items`` row — so no RLS write policy
    either — and it carried the same three-ways-to-global read. Dead today; the
    guard is here because reviving it is the scenario #1166 describes.
    """
    ingester = KnowledgeIngester.__new__(KnowledgeIngester)
    ingester.logger = MagicMock()
    collection = MagicMock()
    ingester._collection = collection  # `collection` is a read-only property

    with pytest.raises(KnowledgeBaseError) as exc:
        await ingester._process_and_store(_tierless_document(scope))

    assert exc.value.error_code == "KNOWLEDGE_SCOPE_REQUIRED"
    collection.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "shape"),
    [
        ("   \n\t  \n", "whitespace-only, so it never reaches the chunk loop"),
        ("# Tenant runbook\n\nRemediation.", "content-bearing, the ordinary case"),
    ],
)
async def test_the_second_writer_refuses_before_chunking_or_embedding(content, shape):
    """The refusal must not depend on the document having survived this far.

    The guard originally sat AFTER ``if not chunks: return`` and after the embed,
    which meant it did not guard: a tierless document with whitespace-only
    content was ACCEPTED — returning None, writing nothing, raising nothing — and
    where the embedder was unavailable the caller got ``RuntimeError('BGE-M3
    model unavailable')`` instead of the scope refusal. Only the content-bearing
    case was pinned, so the hole was invisible.

    ``model_cache`` is left unpatched on purpose: reaching it at all means the
    guard ran too late, and this test would then either load BGE-M3 for real
    (~17s, live network) or raise the wrong error. Both are failures, and both
    are louder than an assertion.
    """
    ingester = KnowledgeIngester.__new__(KnowledgeIngester)
    ingester.logger = MagicMock()
    collection = MagicMock()
    ingester._collection = collection  # `collection` is a read-only property

    document = _tierless_document()
    type(document).content = content

    with pytest.raises(KnowledgeBaseError) as exc:
        await ingester._process_and_store(document)

    assert exc.value.error_code == "KNOWLEDGE_SCOPE_REQUIRED", shape
    collection.add.assert_not_called()


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
# The shared refusal itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "code"),
    [
        (None, "KNOWLEDGE_SCOPE_REQUIRED"),
        ("", "KNOWLEDGE_SCOPE_REQUIRED"),
        # Present but not a tier. The stamp is derived as
        # `"global" if scope == "global" else "personal"`, so these would be
        # silently demoted to a floor no read filter matches — the document
        # vanishes from retrieval under a row that looks healthy. Fail-closed,
        # but silent; the refusal is what makes it loud.
        ("Global", "KNOWLEDGE_SCOPE_INVALID"),
        (" ", "KNOWLEDGE_SCOPE_INVALID"),
        ("platform", "KNOWLEDGE_SCOPE_INVALID"),
    ],
)
def test_the_shared_refusal_distinguishes_absent_from_unrecognised(scope, code):
    with pytest.raises(KnowledgeBaseError) as exc:
        require_write_scope("doc-1", scope)
    assert exc.value.error_code == code


@pytest.mark.parametrize("scope", ["global", "team", "personal"])
def test_the_shared_refusal_returns_the_tier_it_validated(scope):
    """Callers stamp from the RETURNED value, never from a second read of the
    document — see ``test_the_stamp_comes_from_the_value_that_was_validated``."""
    assert require_write_scope("doc-1", scope) == scope


@pytest.mark.asyncio
async def test_the_stamp_comes_from_the_value_that_was_validated():
    """A property-backed ``scope`` must not be able to answer twice.

    The guard reads the document's tier and the stamp is derived from it. If the
    stamp re-read ``document.scope`` instead, a property could return ``team``
    to the check and ``global`` to the store — and duck-typed callers are
    exactly what this guard exists to catch. One read, or it is not a guard.
    """
    service, added = _service_with_capturing_store()
    reads: list[str] = []

    class _TwoFaced:
        document_id = "doc-1"
        title = "Tenant runbook"
        content = "# Tenant runbook\n\nRemediation."
        document_type = "runbook"
        tags: list[str] = []
        source_url = None
        owner_id = None
        created_at = _NOW
        updated_at = _NOW

        @property
        def scope(self):
            reads.append("read")
            # Honest once, then lies. If anything re-reads, it gets "global".
            return "personal" if len(reads) == 1 else "global"

    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=_embed,
    ):
        await service._index_document_in_vector_store(_TwoFaced())

    assert added[0][0]["metadata"]["scope"] == "personal", (
        "the stamp used a re-read of document.scope, not the validated value — "
        f"scope was evaluated {len(reads)} times"
    )


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
    ``global`` still stamps ``global``, and the org-owned tiers still collapse
    to the ``personal`` floor — team visibility lives in the share table, never
    in chunk metadata (ADR-013 §D4).
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
# Site 7: the pack, and the one default that was LIVE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_scope",
    [
        # Absent: the omission this issue is about.
        None,
        # Present but not a tier. Checking only presence let this load cleanly
        # and fail later inside ingest_runbook at KnowledgeScope('Global'),
        # where bootstrap_kb's per-runbook `except` records it in
        # `result.failed` and CONTINUES with the rest of the pack — the
        # opposite of the "ignore the whole pack" contract this guard states,
        # and the opposite of what the sibling vector_row guard does.
        "Global",
        "platform",
        " ",
    ],
)
def test_a_pack_entry_without_a_valid_tier_is_refused_rather_than_published(
    tmp_path, bad_scope
):
    """``kb_pack.py`` defaulted a pack entry's tier to ``"global"``.

    Presence AND validity — see the parametrization for why the second matters.

    This is the site that mattered in production: the pack is built by a
    different repository (faultmaven-kb-toolkit), so an entry that omitted
    ``scope`` would have been published to the platform corpus by an omission
    nobody reviews in the same diff as the code trusting it.

    Refusing the whole pack — rather than defaulting the entry — is how every
    other malformed-pack case here behaves: an already-populated KB keeps its
    last-good content instead of gaining a mis-tiered row.
    """
    import json

    import numpy as np

    from faultmaven.bootstrap.kb_pack import KbPack

    def _write_pack(scope_value: Any) -> pathlib.Path:
        d = tmp_path / ("good" if scope_value == "global" else "bad")
        (d / "runbooks" / "global").mkdir(parents=True)
        (d / "runbooks" / "global" / "rb.md").write_text("# RB\n\nBody.\n")
        entry = {
            "item_id": "kb_0123456789ab",
            "content_hash": "abc",
            "title": "RB",
            "relpath": "global/rb.md",
            "tags": [],
            "chunks": [{"chunk_index": 0, "text": "Body.", "vector_row": 0}],
        }
        if scope_value is not None:
            entry["scope"] = scope_value
        (d / "pack.json").write_text(
            json.dumps(
                {
                    "pack_format": 1,
                    "version": "2026-08-26",
                    "model": "BAAI/bge-m3",
                    "dim": 4,
                    "total_chunks": 1,
                    "runbooks": [entry],
                }
            )
        )
        np.savez(d / "vectors.npz", vectors=np.zeros((1, 4), dtype="float32"))
        return d

    # Positive control FIRST: this fixture really does load when the tier is
    # valid, so the refusal below cannot be a malformed-fixture artefact.
    ok = KbPack.load(_write_pack("global"))
    assert ok is not None and len(ok.runbooks) == 1
    assert ok.runbooks[0].scope == "global"

    assert KbPack.load(_write_pack(bad_scope)) is None, (
        f"a pack entry with scope={bad_scope!r} was accepted — an absent tier "
        "would be ingested into the platform corpus by omission, and an "
        "unrecognised one would fail per-runbook while the rest of the pack "
        "still ingested"
    )


def test_the_shipped_pack_states_a_tier_for_every_runbook():
    """The pack in this repository satisfies the requirement it is now held to,
    so the bootstrap keeps seeding. Measured, not assumed."""
    import json

    pack = _REPO_ROOT / "resources" / "knowledge" / "pack" / "pack.json"
    runbooks = json.loads(pack.read_text())["runbooks"]

    assert runbooks, "the shipped pack has no runbooks — fixture drift"
    missing = [rb.get("item_id") for rb in runbooks if not rb.get("scope")]
    assert not missing, f"shipped pack entries with no scope: {missing}"


# ---------------------------------------------------------------------------
# The durable guard: a NEW publish path cannot omit the tier either
# ---------------------------------------------------------------------------


def _walk_calls(callee: str) -> Iterator[tuple[str, int, ast.Call]]:
    """Yield ``(relative path, line, node)`` for every call to ``callee``.

    Mirrors the AST walk in the KB tenant-isolation probe, including its
    ``SyntaxError`` tolerance: an unparseable file is a build break that every
    other job reports far better than a scope test naming neither the file nor
    the property would.
    """
    for root_name in _PUBLISHING_ROOTS:
        root = _REPO_ROOT / root_name
        # NOT `continue`. A mis-resolved _REPO_ROOT (this file moving a level,
        # a packaging change) would otherwise make every pin below scan zero
        # files and pass — reporting "no violations" when it means "nothing
        # scanned". That is the silent-omission failure this whole PR exists to
        # close, so the pins must not be able to have it themselves.
        assert root.is_dir(), (
            f"AST pin cannot find {root} — _REPO_ROOT resolved to {_REPO_ROOT}, "
            "which is not this repository. The pin would pass while scanning "
            "nothing; fix the anchor rather than the assertion."
        )
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - a parse error is a build break
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == callee:
                    yield path.relative_to(_REPO_ROOT).as_posix(), node.lineno, node


@pytest.mark.parametrize("callee", sorted(_EXPECTED_AT_LEAST))
def test_the_ast_pins_are_actually_scanning_something(callee):
    """The pins' own positive control: they must FIND the calls they judge.

    Every pin below is an assertion that a filtered list is empty, and an empty
    list is exactly what a broken walk produces. Asserting the walk finds at
    least the calls known to exist today is what separates "nothing violates
    this" from "nothing was looked at" — a distinction the rest of this file is
    about, and one the pins were previously unable to make.
    """
    found = list(_walk_calls(callee))
    assert len(found) >= _EXPECTED_AT_LEAST[callee], (
        f"the AST walk found only {len(found)} call(s) to {callee}, expected at "
        f"least {_EXPECTED_AT_LEAST[callee]} — the walk is broken, so every pin "
        "keyed on it is passing vacuously"
    )


def test_no_publishing_construction_of_the_document_model_omits_its_tier():
    """The point of #1166 is the publish path nobody has written yet.

    A required field already makes such a path fail at runtime — but only once
    something runs it, and an untested write path is exactly the one that
    reaches production. This walks the shipped trees instead, so the omission is
    a failing test rather than a first-call traceback. Splats (``**kwargs``) are
    accepted as "cannot be read statically"; there are none today.
    """
    offenders = [
        f"{rel}:{line}"
        for rel, line, node in _walk_calls("KnowledgeBaseDocument")
        if "scope" not in {kw.arg for kw in node.keywords}
        and None not in {kw.arg for kw in node.keywords}
    ]

    assert not offenders, (
        "these build a KnowledgeBaseDocument without naming its knowledge tier "
        "— the omitted tier used to mean 'global', readable by every tenant: "
        f"{offenders}"
    )


@pytest.mark.parametrize(
    "callee", ["ingest_runbook", "upload_document", "ingest_document"]
)
def test_no_publishing_call_omits_its_tier(callee):
    """The same guard one level up, for the methods rather than the model.

    A new caller of any publishing entry point must name the tier. Without this,
    the signature pins above only prove the *default* is gone — they say nothing
    about a call site that reintroduces the ambiguity by passing the tier
    conditionally, or not at all in one branch.
    """
    offenders = [
        f"{rel}:{line}"
        for rel, line, node in _walk_calls(callee)
        # interfaces.py carries the abstract declaration and docstring examples,
        # not calls that reach a store.
        if not rel.endswith("models/interfaces.py")
        and "scope" not in {kw.arg for kw in node.keywords}
        and None not in {kw.arg for kw in node.keywords}
    ]

    assert (
        not offenders
    ), f"these call {callee} without naming the knowledge tier: {offenders}"


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
