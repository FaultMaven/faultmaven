"""Containment of the one runbook-file operation left: the standalone scan's
read of ``data/knowledge`` (#1950).

No draft is read from or written to a file any more — a draft's text is
``conversion_drafts.content`` — so the containment helper guards exactly one
caller: the scan's walk, which imports hand-placed files. ``rglob`` follows
symlinks, so a link planted inside the tree must not be read and minted into a
draft. The helper's own boundary shapes are pinned here too.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.utils.runbook_id import RunbookPathEscape
from tests.utils import regex_only_sanitizer

#: The standalone deployment's one enterprise — the ISOLATION key every row
#: below is stamped with (ADR-017 D1).
ENTERPRISE_ID = STANDALONE_ENTERPRISE_ID

#: A billing organization inside that enterprise (ADR-017 D2). Seeded only as a
#: parent row; visibility never consults it.
BILLING_ORG_ID = "org-billing-9999"

# No ``asyncio`` mark: ``asyncio_mode = auto``, and marking the module would
# also mark the synchronous helper tests.
pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.usefixtures("shipped_data_defaults"),
]

SENTINEL = "ORIGINAL CONTENT — MUST NOT BE READ\n"


@pytest.fixture
async def real_db():
    """A session factory over a real schema holding the enterprise and its
    billing organization.

    The engine is disposed at teardown, on the loop the test used: an
    undisposed engine's pooled aiosqlite connection is collected unclosed
    later, and its worker thread then reports to a closed loop.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from faultmaven.infrastructure.persistence.models import (
        Base,
        EnterpriseModel,
        OrganizationModel,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(EnterpriseModel(enterprise_id=ENTERPRISE_ID, name="E", slug="e"))
        await session.flush()
        session.add(
            OrganizationModel(
                organization_id=BILLING_ORG_ID,
                name="O",
                slug="o",
                enterprise_id=ENTERPRISE_ID,
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


def _real_service(factory) -> ConversionService:
    return ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=None,
    )


class TestTheHelperItself:
    """The two boundary shapes the call-site tests cannot reach."""

    def test_the_root_itself_is_refused(self, tmp_path):
        """A path is a runbook file only strictly inside the tree."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        with pytest.raises(RunbookPathEscape):
            resolve_runbook_path(root, source="probe", root=root)

    def test_a_symlink_out_of_the_tree_is_refused(self, tmp_path):
        """Containment is a property of where the path RESOLVES to, so a link
        inside the tree pointing out of it does not launder an escape."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text(SENTINEL, encoding="utf-8")
        link = root / "innocent.md"
        link.symlink_to(outside)

        with pytest.raises(RunbookPathEscape):
            resolve_runbook_path(link, source="probe", root=root)

    def test_an_unresolvable_path_is_refused_not_raised_raw(self, tmp_path):
        """``Path.resolve()`` raises ``RuntimeError`` on a symlink loop
        (measured, CPython 3.11.9) and ``OSError`` on other conditions.

        An unresolvable path is not a containable path, so it must arrive as
        ``RunbookPathEscape`` like any other refusal: a raw ``RuntimeError``
        would abort a whole scan."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        a, b = root / "a.md", root / "b.md"
        a.symlink_to(b)
        b.symlink_to(a)

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(a, source="probe", root=root)
        assert "unresolvable" in str(exc.value)
        assert isinstance(exc.value.__cause__, RuntimeError)

    def test_root_is_required(self):
        """The optional default was a bypass no test could see: every test
        redirects the root through ``_data_dir``, so a caller omitting ``root=``
        would check the real ``data/knowledge`` and pass every suite while
        guarding the wrong tree."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        with pytest.raises(TypeError):
            resolve_runbook_path("x.md", source="probe")

    def test_an_embedded_nul_is_refused_not_raised_raw(self, tmp_path):
        """A path with an embedded NUL raises ``ValueError: embedded null byte``
        from ``Path.resolve()`` — the type ``RunbookPathEscape`` subclasses, so
        a bare one sails straight past every degrading caller catching only
        ``RunbookPathEscape``. Must arrive typed."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path("data/knowledge/a\x00b.md", source="probe", root=root)
        assert "unresolvable" in str(exc.value)
        assert isinstance(exc.value.__cause__, ValueError)

    def test_an_unresolvable_root_names_the_root_not_the_path(self):
        """When ``root`` (``self._data_dir``) is what cannot resolve, the error
        must name the ROOT — an operator misconfiguration of the tree — not the
        innocent file path, or it sends them chasing the wrong file."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        class _BadRoot:
            def resolve(self):
                raise OSError("knowledge root is gone")

            def __repr__(self):
                return "BADROOT"

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(
                "data/knowledge/innocent.md", source="probe", root=_BadRoot()
            )
        message = str(exc.value)
        assert "knowledge root" in message
        assert "BADROOT" in message
        assert "innocent.md" not in message
        assert isinstance(exc.value.__cause__, OSError)


class TestTheScanWalkRefusesASymlinkOutOfTheTree:
    """``rglob`` follows symlinks, so a link planted inside the tree was read
    and minted into a draft — the shape every other path here refuses. Both
    halves of the module must agree on what a runbook file is."""

    async def test_a_planted_symlink_is_not_minted_as_a_draft(
        self, tmp_path, monkeypatch, caplog, real_db
    ):
        monkeypatch.chdir(tmp_path)
        kb = tmp_path / "data" / "knowledge" / "global"
        kb.mkdir(parents=True)
        secret = tmp_path / "outside" / "secret.md"
        secret.parent.mkdir(parents=True)
        secret.write_text(
            "---\nid: secret-runbook\ntitle: Secret\n---\n\n# Secret\n" + "x" * 300,
            encoding="utf-8",
        )
        (kb / "innocent.md").symlink_to(secret)
        # A legitimate sibling, so the assertion below distinguishes "refused
        # the symlink" from "the walk found nothing at all".
        (kb / "real.md").write_text(
            "---\nid: real-runbook\ntitle: Real\n---\n\n# Real\n" + "y" * 300,
            encoding="utf-8",
        )

        service = _real_service(real_db)
        with caplog.at_level("WARNING"):
            # ``is_platform_admin`` because the fixture writes into ``global/``
            # and a non-admin caller may not mint global-scope drafts — without
            # it BOTH files are skipped and the assertion below passes for the
            # wrong reason.
            result = await service.scan_for_runbooks(
                user_id="u1", is_platform_admin=True
            )

        # ``discovered`` is a COUNT; ``drafts`` is the list.
        minted = [d.get("runbook_id") for d in result.get("drafts", [])]
        assert "real-runbook" in minted, f"the walk must still work: {result}"
        assert (
            "secret-runbook" not in minted
        ), "a symlink out of the tree was read and minted as a draft"
        assert any("outside the tree" in r.getMessage() for r in caplog.records)
