"""#854: KB team publish requires membership in the target team.

A caller-supplied ``team_id`` becomes a ``resource_shares`` row on verify, so
the conversion/publish path must enforce the same rule as the case-share path:
you may only share/publish into a team you belong to. Both surfaces resolve
membership through the shared fail-closed predicate
:func:`faultmaven.modules.auth.contracts.is_team_member`; these tests pin

* the predicate's fail-closed semantics,
* the guard on every ConversionService mint point (``convert_document``,
  ``convert_from_case``, ``create_runbook_from_template``),
* the standalone degrade (no team service ⇒ team publish refused, never
  silently allowed), and
* parity with ``CaseService.share_case_with_team`` on the same fixture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import AuthorizationError, ValidationException
from faultmaven.modules.auth.contracts import is_team_member
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)

#: The isolation boundary these fixtures live in (ADR-017 D1). Every tenancy
#: fact the code under test reads is keyed on this, never on the organization.
ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"

#: Billing attribution only (ADR-017 D2) — never a visibility predicate. It is
#: here because ``share_case_with_team`` carries it onto the share row, not
#: because anything decides access with it.
BILLING_ORG_ID = "org-billing-1111"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _team_service(teams):
    svc = AsyncMock()
    svc.list_all_user_team_ids = AsyncMock(return_value=teams)
    return svc


def _conversion_service(team_service=None) -> ConversionService:
    settings = MagicMock()
    # Sentinel: the LLM-availability check right after the membership guard
    # fails with LLM_UNAVAILABLE, proving the guard PASSED without running
    # the real pipeline.
    settings.llm.get_knowledge_model.return_value = None
    return ConversionService(
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=None,
        knowledge_service=None,
        team_service=team_service,
    )


# ===========================================================================
# Shared predicate: fail-closed membership resolution
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestIsTeamMember:
    async def test_member_true(self):
        assert await is_team_member(_team_service(["t1", "t2"]), "u1", "t2") is True

    async def test_non_member_false(self):
        assert await is_team_member(_team_service(["t1"]), "u1", "t2") is False

    async def test_no_team_service_false(self):
        assert await is_team_member(None, "u1", "t1") is False

    async def test_missing_ids_false(self):
        svc = _team_service(["t1"])
        assert await is_team_member(svc, None, "t1") is False
        assert await is_team_member(svc, "u1", "") is False

    async def test_resolution_error_false(self):
        svc = AsyncMock()
        svc.list_all_user_team_ids = AsyncMock(side_effect=RuntimeError("db down"))
        assert await is_team_member(svc, "u1", "t1") is False


# ===========================================================================
# ConversionService mint points enforce the guard
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestConversionTeamPublishGuard:
    async def test_convert_document_refuses_non_member_team(self, tmp_path):
        service = _conversion_service(team_service=_team_service(["team_a"]))
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await service.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="team",
                user_id="u1",
                enterprise_id=None,
                team_id="team_other",
            )

    async def test_convert_document_member_passes_guard(self, tmp_path):
        # Membership clears the guard → flow proceeds to the LLM-availability
        # check (sentinel: returns None) → ConversionRejectedError, NOT
        # AuthorizationError. Proves the guard did not block a member.
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionRejectedError,
        )

        service = _conversion_service(team_service=_team_service(["team_a"]))
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(ConversionRejectedError, match="LLM provider"):
            await service.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="team",
                user_id="u1",
                enterprise_id=None,
                team_id="team_a",
            )

    async def test_convert_document_standalone_refuses_team_scope(self, tmp_path):
        # No team service wired (TENANT_PROVIDER=single): a team-scoped publish
        # is refused, never silently accepted with an unresolvable target.
        service = _conversion_service(team_service=None)
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(ValidationException, match="not available"):
            await service.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="team",
                user_id="u1",
                enterprise_id=None,
                team_id="team_a",
            )

    async def test_convert_from_case_refuses_non_member_team(self):
        service = _conversion_service(team_service=_team_service(["team_a"]))
        request = MagicMock()
        request.case_id = "case_1"
        request.scope = "team"
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await service.convert_from_case(
                request=request, user_id="u1", enterprise_id=None, team_id="team_other"
            )

    async def test_create_runbook_refuses_non_member_team(self):
        service = _conversion_service(team_service=_team_service(["team_a"]))
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await service.create_runbook_from_template(
                title="A title long enough",
                domain="databases",
                service_name="postgres",
                symptom_class=["connectivity"],
                severity="high",
                scope="team",
                tags=[],
                difficulty="intermediate",
                symptom_recognition="x" * 20,
                applicability="x" * 20,
                diagnostic_steps="x" * 20,
                causes="x" * 20,
                prevention="x" * 20,
                user_id="u1",
                enterprise_id=None,
                team_id="team_other",
            )

    async def test_personal_scope_never_gated(self, tmp_path):
        # Personal scope carries no team target — no team service needed and
        # no membership consulted (reaches the LLM-availability sentinel).
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionRejectedError,
        )

        service = _conversion_service(team_service=None)
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(ConversionRejectedError, match="LLM provider"):
            await service.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="personal",
                user_id="u1",
                enterprise_id=None,
            )


# ===========================================================================
# Verify-time re-check: the share row is minted at verify, so membership is
# re-validated at the point of effect (verifier may differ from the minter,
# or have left the team since the job was created)
# ===========================================================================


def _verify_ready_service(*, team_service, tmp_path, monkeypatch, job_team="team_a"):
    """A ConversionService whose verify_draft reaches the team-share transfer:
    team-scoped user-owned job, valid DRAFT with a real file on disk, a share
    repo answering ``job_team``, and a knowledge service whose ingest_runbook
    records whether publication happened."""
    from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus

    # Pin the knowledge root at the tmp dir the draft is written into.
    # ``verify_draft`` containment-checks ``conversion_drafts.file_path``
    # against this root (#1213 follow-up); in production the draft always lives
    # under it.
    monkeypatch.setattr(ConversionService, "_data_dir", property(lambda self: tmp_path))

    draft_file = tmp_path / "runbook.md"
    draft_file.write_text("---\nstatus: draft\n---\n\n# Runbook\n")

    job = MagicMock()
    job.user_id = "u1"
    job.scope = "team"
    # ``verify_draft`` stamps the published item with the JOB's isolation key
    # (ADR-017 D1). ``organization_id`` is billing attribution and is not read
    # on this path, so naming it here said nothing about what is under test.
    job.enterprise_id = ENTERPRISE_ID

    dm = MagicMock()
    dm.id = "d1"
    dm.runbook_id = "rb-1"
    dm.status = DraftStatus.DRAFT.value
    dm.validation_passed = True
    dm.file_path = str(draft_file)
    dm.title = "T"

    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = job if calls["n"] == 1 else dm
        return result

    session = AsyncMock()
    session.execute = _execute

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    share = MagicMock()
    share.scope_type = "team"
    share.scope_id = job_team
    share_repo = AsyncMock()
    share_repo.list_scopes_for_resource = AsyncMock(return_value=[share])

    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=3)

    service = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_Factory(),
        knowledge_service=knowledge_service,
        share_repository=share_repo,
        team_service=team_service,
    )
    return service, knowledge_service


@pytest.mark.unit
@pytest.mark.asyncio
class TestVerifyTimeMembershipRecheck:
    async def test_non_member_verifier_refused_before_publication(
        self, tmp_path, monkeypatch
    ):
        service, ks = _verify_ready_service(
            team_service=_team_service(["team_other"]),
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await service.verify_draft(
                conversion_id="c",
                draft_id="d1",
                user_id="u1",
                username="u1",
            )
        ks.ingest_runbook.assert_not_awaited()

    async def test_member_verifier_publishes(self, tmp_path, monkeypatch):
        service, ks = _verify_ready_service(
            team_service=_team_service(["team_a"]),
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
        result = await service.verify_draft(
            conversion_id="c",
            draft_id="d1",
            user_id="u1",
            username="u1",
        )
        assert result is not None and result.status == "verified"
        ks.ingest_runbook.assert_awaited_once()
        assert ks.ingest_runbook.await_args.kwargs["team_id"] == "team_a"

    async def test_stale_share_with_no_team_service_refused(
        self, tmp_path, monkeypatch
    ):
        # A share row exists (e.g. minted before teams were unwired) but no
        # team service: fail-closed — the share must not transfer.
        service, ks = _verify_ready_service(
            team_service=None, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        with pytest.raises(AuthorizationError):
            await service.verify_draft(
                conversion_id="c",
                draft_id="d1",
                user_id="u1",
                username="u1",
            )
        ks.ingest_runbook.assert_not_awaited()


# ===========================================================================
# Route passthrough: typed refusals reach the client as 403/422, not 500
# ===========================================================================


def _route_client(service):
    from datetime import datetime, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.contracts import DevUser
    from faultmaven.modules.knowledge.api.conversion_routes import (
        _get_conversion_service,
    )
    from faultmaven.modules.knowledge.api.conversion_routes import (
        router as conversion_router,
    )

    user = DevUser(
        user_id="u1",
        username="u1",
        email="u1@example.com",
        display_name="U1",
        created_at=datetime.now(timezone.utc),
        roles=["user"],
    )
    app = FastAPI()
    app.include_router(conversion_router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[_get_conversion_service] = lambda: service
    app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app)


@pytest.mark.unit
class TestRouteRefusalPassthrough:
    """The convert / runbooks-create routes wrap the service call in a broad
    exception net; the typed refusals must pass through it to the global
    handlers (403/422), never collapse into the 500 arm."""

    def _convert(self, client):
        return client.post(
            "/knowledge/convert",
            files={"file": ("doc.md", b"# content", "text/markdown")},
            data={"scope": "team", "team_id": "team_other"},
        )

    def test_convert_membership_refusal_is_403(self):
        service = MagicMock()
        service.convert_document = AsyncMock(
            side_effect=AuthorizationError("not your team")
        )
        assert self._convert(_route_client(service)).status_code == 403

    def test_convert_unavailable_teams_is_422(self):
        service = MagicMock()
        service.convert_document = AsyncMock(
            side_effect=ValidationException("Team publishing is not available")
        )
        assert self._convert(_route_client(service)).status_code == 422

    def test_create_runbook_membership_refusal_is_403(self):
        service = MagicMock()
        service.create_runbook_from_template = AsyncMock(
            side_effect=AuthorizationError("not your team")
        )
        body = {
            "title": "A title long enough",
            "domain": "databases",
            "service": "postgres",
            "symptom_class": ["connectivity"],
            "severity": "high",
            "scope": "team",
            "team_id": "team_other",
            "symptom_recognition": "x" * 20,
            "applicability": "x" * 20,
            "diagnostic_steps": "x" * 20,
            "causes": "x" * 20,
            "prevention": "x" * 20,
        }
        resp = _route_client(service).post("/knowledge/runbooks/create", json=body)
        assert resp.status_code == 403


# ===========================================================================
# Parity: the two sharing surfaces agree on the membership rule
# ===========================================================================


@pytest.mark.unit
@pytest.mark.asyncio
class TestSharingSurfacesAgree:
    """Same principal, same team fixture → both surfaces refuse a non-member
    target and both accept a member target (case side stops at its next
    dependency, proving its membership check passed)."""

    def _case_service(self, team_service):
        from faultmaven.modules.case.domain.services.case_service import CaseService

        case = MagicMock()
        case.case_id = "case_1"
        case.user_id = "u1"
        # ``share_case_with_team`` copies both onto the share row: the
        # enterprise is the isolation key (ADR-017 D1), the organization is
        # billing attribution (D2). Both are stated so the fixture says which
        # is which.
        case.enterprise_id = ENTERPRISE_ID
        case.organization_id = BILLING_ORG_ID
        repo = MagicMock()
        repo.get = AsyncMock(return_value=case)
        svc = CaseService(
            case_repository=repo,
            session_store=MagicMock(),
            team_service=team_service,
            share_repository=AsyncMock(),
        )
        return svc

    async def test_both_surfaces_refuse_non_member(self, tmp_path):
        teams = _team_service(["team_a"])

        case_svc = self._case_service(teams)
        with pytest.raises(ValidationException, match="team you belong to"):
            await case_svc.share_case_with_team("case_1", "team_other", "u1")

        kb_svc = _conversion_service(team_service=teams)
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await kb_svc.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="team",
                user_id="u1",
                enterprise_id=None,
                team_id="team_other",
            )

    async def test_both_surfaces_accept_member(self, tmp_path):
        from faultmaven.modules.knowledge.domain.services.conversion_service import (
            ConversionRejectedError,
        )

        teams = _team_service(["team_a"])

        case_svc = self._case_service(teams)
        await case_svc.share_case_with_team("case_1", "team_a", "u1")
        case_svc.share_repository.share.assert_awaited_once()

        kb_svc = _conversion_service(team_service=teams)
        f = tmp_path / "doc.md"
        f.write_text("content")
        with pytest.raises(ConversionRejectedError, match="LLM provider"):
            await kb_svc.convert_document(
                file_path=f,
                content_type="text/markdown",
                original_filename="doc.md",
                scope="team",
                user_id="u1",
                enterprise_id=None,
                team_id="team_a",
            )
