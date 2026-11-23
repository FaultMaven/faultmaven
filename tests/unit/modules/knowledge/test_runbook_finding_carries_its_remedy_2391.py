"""A refused runbook's every security finding says what to do (#2391).

Invariant: every error ``find_security_hazards`` returns ends with its remedy
sentence, and its warnings carry none. A shape-detected credential already
said "Replace it with a placeholder such as <X>, and rotate the credential.";
the legacy-pattern credential (``_SECRET_PATTERNS``) and the three destructive
commands said nothing about what to do. The copy is the owner's ruling (a),
verbatim, and is written here as literals so the tests pin it.

The finding is the one string every route shows: the upload and the edit 422
``detail``, and verify-batch's per-draft ``error``.

Mutation: revert the product change in ``find_security_hazards``: every
positive test here fails; the ``TestUnchanged`` pins pass either way.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.infrastructure.persistence.models import ConversionDraftModel
from faultmaven.modules.knowledge.api import conversion_routes as cr
from faultmaven.modules.knowledge.api import routes
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    enforce_runbook_quality,
    enforce_runbook_security,
    find_security_hazards,
)
from tests.unit.modules.knowledge.test_runbook_body_refused_not_rewritten_2012 import (  # noqa: E501
    _AWS,
    _body_with,
    _client,
    _conversion,
    _fragments,
    _knowledge,
    _template,
    session_factory,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

#: The approved copy (ruling (a) on #2391).
CREDENTIAL_REMEDY = "Replace it with a placeholder, and rotate the credential."
DESTRUCTIVE_REMEDY = "Remove it, or replace it with a safe form."
#: Either remedy, or the shape finding's own ("… such as <X>, and rotate …").
_ENDS_WITH_A_REMEDY = re.compile(
    r"(?:Replace it with a placeholder(?: such as \S+)?, and rotate the "
    r"credential\.|Remove it, or replace it with a safe form\.)$"
)

#: ``(label, snippet, the one error)``: values no shape pattern matches, so
#: the legacy pattern is the only finding.
LEGACY = [
    (
        "password",
        'password = "hunter2-prod"',
        "Security: Potential hardcoded password (hardcoded credential). "
        + CREDENTIAL_REMEDY,
    ),
    (
        "api_key",
        'api_key = "k3y-for-staging"',
        "Security: Potential hardcoded API key (hardcoded credential). "
        + CREDENTIAL_REMEDY,
    ),
    (
        "secret",
        'secret = "s3cret-sauce"',
        "Security: Potential hardcoded secret (hardcoded credential). "
        + CREDENTIAL_REMEDY,
    ),
    (
        "token",
        'token = "t0ken-of-trust"',
        "Security: Potential hardcoded token (hardcoded credential). "
        + CREDENTIAL_REMEDY,
    ),
]
DESTRUCTIVE = [
    (
        "rm_rf",
        "rm -rf /",
        "Security: Dangerous command: recursive force-remove of a "
        "root/top-level/home path (rm -rf). " + DESTRUCTIVE_REMEDY,
    ),
    (
        "fork_bomb",
        ":(){ :|:& };:",
        "Security: Dangerous: fork bomb. " + DESTRUCTIVE_REMEDY,
    ),
    (
        "dd",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "Security: Potentially destructive: dd writing to a block device. "
        + DESTRUCTIVE_REMEDY,
    ),
]
_SHAPE = f"export AWS_ACCESS_KEY_ID={_AWS}"
#: What the routes are driven with: a legacy-only credential and a fork bomb.
ROUTED = [LEGACY[0], DESTRUCTIVE[1]]
ROUTED_IDS = [row[0] for row in ROUTED]


def _value(snippet: str) -> str:
    """The quoted value of a legacy fixture ('' for a command)."""
    quoted = re.search(r'"([^"]+)"', snippet)
    return quoted.group(1) if quoted else ""


def _leaked(snippet: str, text: str) -> list[str]:
    return [f for f in _fragments(_value(snippet), 6) if f in text]


class TestEveryErrorEndsWithItsRemedy:
    def test_over_every_hazard_kind(self):
        snippet = "\n".join([row[1] for row in LEGACY + DESTRUCTIVE] + [_SHAPE])
        body, _ = _body_with(snippet)

        errors, warnings = find_security_hazards(body)

        # One error per kind: four legacy, three destructive, one shape.
        assert len(errors) == 8, errors
        assert [e for e in errors if not _ENDS_WITH_A_REMEDY.search(e)] == []
        assert warnings == []

    @pytest.mark.parametrize(
        "label,snippet,expected",
        LEGACY + DESTRUCTIVE,
        ids=[r[0] for r in LEGACY + DESTRUCTIVE],
    )
    def test_each_kind_alone_is_one_error_with_the_approved_sentence(
        self, label, snippet, expected
    ):
        body, _ = _body_with(snippet)

        errors, warnings = find_security_hazards(body)

        assert errors == [expected]
        assert warnings == []


class TestTheRoutesSayIt:
    """What a client shows: the 422 ``detail`` of the upload and the edit, and
    verify-batch's per-draft ``error``, each with the sentence and no fragment
    of the value."""

    @pytest.mark.parametrize("label,snippet,expected", ROUTED, ids=ROUTED_IDS)
    def test_an_upload(self, label, snippet, expected):
        async def upload_document(content, **_):
            enforce_runbook_quality(content)  # the service's gate
            return {"document_id": "kb_x"}

        body, _ = _body_with(snippet)

        response = _client(SimpleNamespace(upload_document=upload_document)).post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={"file": ("rb.md", body.encode(), "text/markdown")},
        )

        assert response.status_code == 422, response.text
        assert expected in response.json()["detail"]
        assert _leaked(snippet, response.text) == []

    @pytest.mark.parametrize("label,snippet,expected", ROUTED, ids=ROUTED_IDS)
    def test_an_edit(self, monkeypatch, label, snippet, expected):
        async def update_document_metadata(document_id, **updates):
            enforce_runbook_security(updates["content"])  # the service's check
            return {"document_id": document_id}

        service = SimpleNamespace(
            get_document=AsyncMock(
                return_value={"document_id": "kb_x", "owner_id": "user-1"}
            ),
            update_document_metadata=update_document_metadata,
        )
        monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())
        body, _ = _body_with(snippet)

        response = _client(service).put(
            "/api/v1/knowledge/documents/kb_x", json={"content": body}
        )

        assert response.status_code == 422, response.text
        assert expected in response.json()["detail"]
        assert _leaked(snippet, response.text) == []

    @pytest.mark.parametrize("label,snippet,expected", ROUTED, ids=ROUTED_IDS)
    async def test_verify_batch(
        self, session_factory, tmp_path, monkeypatch, label, snippet, expected
    ):
        """A draft whose ``validation_passed`` predates the check is refused by
        the security check at publish, and the batch reports that finding."""
        monkeypatch.chdir(tmp_path)
        conversion = _conversion(session_factory, _knowledge(session_factory))
        created = await conversion.create_runbook_from_template(**_template())
        draft_id = created["draft"].draft_id
        body, _ = _body_with(snippet)
        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, draft_id)
            row.content = body
            row.validation_passed = True
            await session.commit()
        app = FastAPI()
        app.include_router(cr.router, prefix="/api/v1")
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        user = SimpleNamespace(user_id="u1", is_platform_admin=lambda: False)
        app.dependency_overrides[cr._require_auth] = lambda: user
        app.dependency_overrides[cr._get_conversion_service] = lambda: conversion

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/knowledge/drafts/verify-batch",
                json={
                    "draft_ids": [
                        {
                            "conversion_id": created["conversion_id"],
                            "draft_id": draft_id,
                        }
                    ]
                },
            )

        assert response.status_code == 200, response.text
        (item,) = response.json()["results"]
        assert item["status"] == "failed"
        assert expected in item["error"]
        assert _leaked(snippet, response.text) == []


class TestUnchanged:
    """What #2391 does not change."""

    def test_a_placeholder_is_a_warning_with_no_remedy(self):
        body, _ = _body_with('password = "<PASSWORD>"')

        errors, warnings = find_security_hazards(body)

        assert errors == []
        assert warnings
        assert not [
            w for w in warnings if CREDENTIAL_REMEDY in w or DESTRUCTIVE_REMEDY in w
        ]

    def test_the_shape_error_is_byte_identical(self):
        body, line = _body_with(_SHAPE)

        errors, _ = find_security_hazards(body)

        assert errors == [
            f"Security: hardcoded credential: an AWS access key ID on line {line} "
            "(AWS_ACCESS_KEY). Replace it with a placeholder such as "
            "<AWS_ACCESS_KEY_ID>, and rotate the credential."
        ]
        assert errors[0].count("rotate the credential") == 1

    def test_three_catastrophic_rms_are_one_error(self):
        body, _ = _body_with("rm -rf /\nrm -rf /etc\nrm -rf ~")

        errors, _ = find_security_hazards(body)

        assert len(errors) == 1 and "(rm -rf)" in errors[0], errors
