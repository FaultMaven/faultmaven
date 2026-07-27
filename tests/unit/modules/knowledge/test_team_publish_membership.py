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
                team_id="team_a",
            )

    async def test_convert_from_case_refuses_non_member_team(self):
        service = _conversion_service(team_service=_team_service(["team_a"]))
        request = MagicMock()
        request.case_id = "case_1"
        request.scope = "team"
        with pytest.raises(AuthorizationError, match="team you belong to"):
            await service.convert_from_case(
                request=request, user_id="u1", team_id="team_other"
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
            )


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
        case.organization_id = "org_1"
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
                team_id="team_a",
            )
