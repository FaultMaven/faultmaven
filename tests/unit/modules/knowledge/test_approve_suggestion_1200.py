"""Regression set for #1200 — `approve_suggestion` created nothing, silently.

`SuggestionService.approve_suggestion` called `KnowledgeService.upload_document`
with a `metadata=` keyword that the method does not accept. The `TypeError` was
caught by the broad `except Exception` immediately below, logged, and the method
returned `None` — which the route reports as
``400 "Cannot approve: PII scan not complete"``, a claim that is false on a
suggestion whose scan passed (it had to, to get past `is_ready_for_review`).

So the approval step of the knowledge flywheel — a resolved case becoming
reusable knowledge — created no knowledge item and said the wrong thing about
why.

**These tests bind against the REAL `upload_document` signature via
`create_autospec`.** A bare `Mock` advertises `(*args, **kwargs)` and accepts
`metadata=` happily, which would make every test here pass against the unfixed
code — the defect is precisely a signature mismatch, so a mock that does not
enforce signatures cannot see it.
"""

from unittest.mock import AsyncMock, create_autospec

import pytest

from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    InMemorySuggestionRepository,
)

pytestmark = pytest.mark.unit

ORG = "org_123"
SUGGESTION_ID = "sug_0001"


def _knowledge_service_double():
    """Autospecced against the real class, so the call must actually BIND.

    This is the whole point of the fixture: `create_autospec` gives
    `upload_document` the real signature, so passing an unsupported keyword
    raises `TypeError` here exactly as it does in production.
    """
    double = create_autospec(KnowledgeService, instance=True)
    # Set return_value on the AUTOSPECCED attribute. Replacing it with a fresh
    # ``AsyncMock(spec=KnowledgeService.upload_document)`` looks equivalent and
    # is not: `spec=<function>` does NOT enforce the call signature, so the
    # bogus ``metadata=`` binds happily and the whole file passes against the
    # unfixed code. Measured -- only ``create_autospec`` raises here.
    double.upload_document.return_value = {"document_id": "kb_abcdef0123456789"}
    return double


def _ready_suggestion() -> KnowledgeSuggestion:
    return KnowledgeSuggestion(
        suggestion_id=SUGGESTION_ID,
        organization_id=ORG,
        case_id="case_aabb11223344",
        suggested_title="Redis pool exhaustion",
        suggested_content="## Problem\nPool exhausted.\n",
        suggested_type="troubleshooting_guide",
        extracted_by="user_extractor",
        pii_scan_status=PIIScanStatus.CLEAN,
    )


def _service_with_one_ready_suggestion() -> SuggestionService:
    """A service whose store holds one review-ready suggestion.

    Since #1227 the store is a repository and the tenant-scoped lookup is a
    real query against it, so the fixture no longer stubs
    ``get_suggestion_visible``: seeding the store exercises the same path
    production takes, and a stub returning one live object would have hidden
    the detached-copy semantics these tests now rely on.
    """
    repository = InMemorySuggestionRepository()
    repository.seed(_ready_suggestion())
    return SuggestionService(
        knowledge_service=_knowledge_service_double(),
        suggestion_repository=repository,
    )


@pytest.fixture
def service():
    return _service_with_one_ready_suggestion()


class TestApprovalActuallyCreatesSomething:
    async def test_approval_returns_a_knowledge_item_id(self, service):
        result = await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        assert result is not None, (
            "approve_suggestion returned None on a CLEAN suggestion -- the "
            "route renders that as 'Cannot approve: PII scan not complete'"
        )
        assert result["knowledge_item_id"] == "kb_abcdef0123456789"
        assert result["status"] == "approved"

    async def test_upload_document_was_actually_called(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        service._knowledge_service.upload_document.assert_awaited_once()

    async def test_the_call_binds_to_the_real_signature(self, service):
        """The defect in one assertion: every kwarg must be a real parameter."""
        import inspect

        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        parameters = set(inspect.signature(KnowledgeService.upload_document).parameters)
        unsupported = set(kwargs) - parameters
        assert not unsupported, (
            f"approve_suggestion passes {sorted(unsupported)} to upload_document, "
            "which has no such parameter; the TypeError is swallowed by a broad "
            "except and the approval silently creates nothing"
        )

    async def test_the_suggestion_is_marked_approved(self, service):
        """Asserts the STATUS, which is the thing the name claims.

        An earlier draft only re-checked ``knowledge_item_id`` — a duplicate of
        the test above that would pass unchanged if ``approve()`` stopped
        setting the status, or set REJECTED. ``result["status"]`` does not
        cover it either: that is a hardcoded literal in the service, not the
        suggestion's state.
        """
        from faultmaven.modules.knowledge.domain.models.suggestion import (
            SuggestionStatus,
        )

        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        stored = service._repository.peek(SUGGESTION_ID)
        assert stored.status == SuggestionStatus.APPROVED
        assert stored.reviewed_by == "user_admin"
        assert stored.knowledge_item_id == "kb_abcdef0123456789"


class TestLineageSurvives:
    """The four fields the dropped `metadata=` was trying to record. They were
    lost twice over -- the call failed, so nothing was recorded at all. They now
    ride the parameters that exist; see the service docstring for why a real
    metadata sink is deferred to #878."""

    async def test_the_source_case_is_recorded(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert "case_aabb11223344" in kwargs["description"]

    async def test_the_source_suggestion_is_recorded(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert SUGGESTION_ID in kwargs["description"]

    async def test_the_extractor_is_recorded(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert "user_extractor" in kwargs["description"]

    async def test_the_platform_tier_is_still_stated(self, service):
        """#1166: the scope is named out loud, not inherited from a default."""
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert kwargs["scope"] == "global"

    async def test_the_item_is_tagged_as_case_derived(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert "case-derived" in kwargs["tags"]


class TestProgrammingErrorsAreNotSwallowed:
    """A `TypeError` from a call this service makes to its own collaborator is a
    programming error, not a runtime condition. Catching it in the same handler
    that catches an ingestion failure is what made this invisible for as long as
    it was."""

    async def test_every_kwarg_is_a_real_parameter_of_the_collaborator(self):
        """The signature check, done by BINDING rather than by a stub.

        An earlier draft replaced the autospecced attribute with
        ``AsyncMock(side_effect=TypeError)`` and asserted a TypeError came
        back — which is true of any exception and any argument list, and would
        pass against the unfixed code. It proved nothing about the signature.
        The real coverage is ``inspect.signature`` binding, below and at
        ``test_the_call_binds_to_the_real_signature``.
        """
        import inspect

        svc = _service_with_one_ready_suggestion()

        await svc.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = svc._knowledge_service.upload_document.await_args.kwargs
        # Binding is the assertion: it raises TypeError on an unsupported
        # keyword exactly as the real call does.
        inspect.signature(KnowledgeService.upload_document).bind(None, **kwargs)

    async def test_a_genuine_ingestion_failure_still_raises_rather_than_lying(
        self, service
    ):
        """A real ingestion failure is a server-side fault. Returning None sent
        it to the route's 400 'PII scan not complete', which is a false
        statement about a CLEAN suggestion and a client-error code for a server
        problem."""
        service._knowledge_service.upload_document = AsyncMock(
            side_effect=RuntimeError("chromadb unreachable")
        )

        with pytest.raises(RuntimeError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

    async def test_a_not_ready_suggestion_still_returns_none(self, service):
        """The one case the route's 400 is actually about, unchanged."""
        suggestion = _ready_suggestion()
        suggestion.pii_scan_status = PIIScanStatus.PII_DETECTED
        service.get_suggestion_visible = AsyncMock(return_value=suggestion)

        result = await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        assert result is None


class TestAttributionIsPersisted:
    """``description`` reaches no column — it is referenced ZERO times in
    ``upload_document``'s body, so the lineage it appears to carry is not
    recorded anywhere. ``owner_id`` is the parameter that does persist, into
    ``uploaded_files.uploaded_by``, ``conversion_jobs.user_id`` and
    ``conversion_drafts.verified_by``."""

    async def test_the_approving_admin_is_passed_as_owner_id(self, service):
        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        kwargs = service._knowledge_service.upload_document.await_args.kwargs
        assert kwargs["owner_id"] == "user_admin", (
            "without this the approving platform admin is recorded nowhere in "
            "the database; description= persists nothing"
        )

    def test_description_really_is_ignored_by_the_collaborator(self):
        """Pins the premise the comment rests on, so it cannot rot silently.

        If ``upload_document`` ever grows a real use for ``description``, this
        fails and the comment beside the call must be corrected.
        """
        import ast
        import inspect

        from faultmaven.modules.knowledge.domain.services import knowledge_service

        source = inspect.getsource(KnowledgeService.upload_document)
        tree = ast.parse(source.lstrip())
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
        assert names.count("description") == 0, (
            "upload_document now references `description`; the #1200 comment "
            "saying it records nothing is stale"
        )
        assert knowledge_service is not None


class TestReApprovalIsRefused:
    """``is_ready_for_review`` inspects ``pii_scan_status`` only, never
    ``status``. That was harmless while every approval raised; now that they
    succeed, a repeat would publish a SECOND item into the global corpus and
    orphan the first."""

    async def test_a_second_approval_raises_rather_than_publishing_again(self, service):
        from faultmaven.exceptions import ConflictError

        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )

        with pytest.raises(ConflictError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

    async def test_the_second_attempt_publishes_nothing(self, service):
        """The guard runs BEFORE upload_document, so the await count holds."""
        from faultmaven.exceptions import ConflictError

        await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )
        with pytest.raises(ConflictError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

        assert service._knowledge_service.upload_document.await_count == 1

    async def test_the_original_link_is_not_overwritten(self, service):
        from faultmaven.exceptions import ConflictError

        first = await service.approve_suggestion(
            suggestion_id=SUGGESTION_ID,
            reviewed_by="user_admin",
            organization_id=ORG,
        )
        with pytest.raises(ConflictError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

        stored = service._repository.peek(SUGGESTION_ID)
        assert stored.knowledge_item_id == first["knowledge_item_id"]


class TestApprovalNeverClaimsAnIdItDoesNotHave:
    async def test_a_response_without_document_id_raises(self, service):
        service._knowledge_service.upload_document.return_value = {"status": "queued"}

        with pytest.raises(RuntimeError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

    async def test_the_suggestion_is_not_marked_approved_without_an_id(self, service):
        from faultmaven.modules.knowledge.domain.models.suggestion import (
            SuggestionStatus,
        )

        service._knowledge_service.upload_document.return_value = {"status": "queued"}

        with pytest.raises(RuntimeError):
            await service.approve_suggestion(
                suggestion_id=SUGGESTION_ID,
                reviewed_by="user_admin",
                organization_id=ORG,
            )

        stored = service._repository.peek(SUGGESTION_ID)
        assert stored.status != SuggestionStatus.APPROVED
