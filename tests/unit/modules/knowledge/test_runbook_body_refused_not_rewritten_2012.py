"""A runbook body is refused, never rewritten, when it holds a secret (#2012).

Invariant: on every path that writes a non-shipped runbook body (upload,
``verify_draft``, template, case-authored draft publish, update), the stored
body equals the author's text with only line endings normalised, and no stored
body holds a span a ``names_a_value=False`` ``SECRET_PATTERNS`` pattern detects
unless its secret portion is a placeholder; a body that would is refused with
422 before any row or vector is written, and the refusal never echoes the
secret. Non-shipped is every caller but ``bootstrap/kb_init.py``
(``shipped=True``).

Why refuse and not redact: the sanitizer rewrote 20 of the 91 shipped bodies
(IP addresses, and commands cut: ``get-login-password <PASSWORD_…> "$REGION"``),
and its keyed families alone changed 17 lines, 16 of them false positives.

These tests drive the REAL services over in-memory SQLite with a REAL
sanitizer (regex half, pinned key). Only the ChromaDB write is a double, and a
refusal is asserted on it too: never awaited.

=================================================  =============================
path                                               test
=================================================  =============================
``ingest_runbook`` (every non-shipped publish)     ``TestTheChokePoint``
``upload_document``                                ``TestUpload``
``verify_draft``: a template draft                 ``TestTemplateDraft``
``verify_draft``: a draft validated before the     ``TestADraftValidatedBefore``
check (template, conversion, case)
``update_document_metadata``                       ``TestUpdate``
``bulk_update_documents``                          ``TestBulkUpdate``
the upload and update routes                       ``TestTheRoutesAnswer422``
the KB pack (``shipped=True``)                     ``TestShippedPack`` (exempt)
``KnowledgeIngester`` (dead)                       ``TestDeadIngesterPaths``
=================================================  =============================

Mutations, each restored from a ``cp`` backup:

* drop the call in ``ingest_runbook``: ``TestTheChokePoint`` and
  ``TestADraftValidatedBefore`` fail;
* drop the call in ``update_document_metadata``: ``TestUpdate``'s refusal fails;
* select the keyed families too: ``TestShippedPack`` fails (and the
  false-positive rows);
* drop the placeholder filter: the placeholder rows fail.

The review of f137dfc2c added ``REVIEW``: each row was published at that head
(F2 a JWT read past its own characters, F3 a placeholder's spelling at the
start of a real password, F4 key material wrapped short, F9 a truncated key
past its BEGIN line, F10 a GitLab token), and is refused now. The routes are
read through the app's own ``http_exception_handler`` (F6).
"""

from __future__ import annotations

import base64
import json
import logging
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.infrastructure.security.redaction import SECRET_PATTERNS
from faultmaven.models import KnowledgeBaseDocument
from faultmaven.modules.knowledge.api import routes
from faultmaven.modules.knowledge.api.routes import (
    RUNBOOK_SECURITY_HELP,
    get_knowledge_service,
)
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    SourceType,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (  # noqa: E501
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    _SHAPE_PATTERNS,
    RunbookQualityError,
    RunbookValidator,
    enforce_runbook_quality,
    enforce_runbook_security,
    find_security_hazards,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base, pytest.mark.security]

SCOPES = ["personal", "team", "global"]
TEAM_ID = "11111111-1111-4111-8111-111111111111"
PACK = pathlib.Path(__file__).resolve().parents[4] / "resources/knowledge/pack/runbooks"

# --- the secrets, each assembled at runtime (push protection reads literals) ---

_GITHUB = "gh" + "p_" + "1234567890abcdefghijklmnopqrstuvwxyz"
_AWS = "AK" + "IA" + "Q3EGRPBZ7MXT4LKD"
#: A 52-character base64 line: real key material (a real key's lines are 64).
_KEY_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
_PEM = "\n".join(
    ("-----BEGIN " + "PRIVATE KEY-----", _KEY_LINE, "-----END " + "PRIVATE KEY-----")
)
_DB_PASSWORD = "S3cr3t" + "Winter24"
_DB_URL = "postgres://admin:" + _DB_PASSWORD + "@db.example.com/app"
#: The jwt.io sample: a full header.payload.signature.
_JWT = ".".join(
    (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    )
)

#: ``(label, the snippet written into a bash block, its type, the secret)``.
REAL = [
    (
        "github_token",
        f"curl -H 'Authorization: token {_GITHUB}' https://api.github.com/user",
        "GITHUB_TOKEN",
        _GITHUB,
    ),
    ("aws_key", f"export AWS_ACCESS_KEY_ID={_AWS}", "AWS_ACCESS_KEY", _AWS),
    ("pem_block", _PEM, "PRIVATE_KEY", _KEY_LINE),
    ("database_url", f"psql {_DB_URL}", "DATABASE_URL", _DB_PASSWORD),
    (
        "jwt",
        f"curl -H 'Authorization: Bearer {_JWT}' https://api.example.com",
        "JWT_TOKEN",
        _JWT,
    ),
]
REAL_IDS = [row[0] for row in REAL]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


#: A real-shaped JWT, assembled: header.payload.signature.
_REVIEW_JWT = ".".join(
    (
        _b64url(b'{"alg":"HS256","typ":"JWT"}'),
        _b64url(b'{"sub":"1234567890","name":"prod-svc","iat":1716239022}'),
        _b64url(bytes(range(32))),
    )
)
#: 1,024 characters of real-shaped key material.
_KEY_B64 = base64.b64encode(bytes(range(256)) * 3).decode()
_PEM_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
_PEM_END = "-----END RSA " + "PRIVATE KEY-----"


def _pem(width: int, sep: str = "\n") -> str:
    lines = [_KEY_B64[i : i + width] for i in range(0, len(_KEY_B64), width)]
    return _PEM_BEGIN + sep + sep.join(lines) + sep + _PEM_END


_GITLAB = "gl" + "pat-" + "Zq7Wm2Kx9Lp4Rt6Yv3Bn"
_TRUNCATED_LINES = [_KEY_B64[i : i + 64] for i in range(0, 448, 64)]

#: The review of f137dfc2c (``scratchpad/rev2031/a1.py``): each one was
#: published, and each is refused now. ``(label, snippet, type, secret)``.
REVIEW = [
    # F2: a JWT read on to the next blank took a query string's or a JSON
    # document's `example` into its parts, which excused it.
    (
        "jwt-in-a-query-string",
        f"curl 'https://api.internal/cb?id_token={_REVIEW_JWT}"
        "&next=https://app.example.com/'",
        "JWT_TOKEN",
        _REVIEW_JWT,
    ),
    (
        "jwt-in-compact-json",
        '{"id_token":"' + _REVIEW_JWT + '","iss":"https://auth.example.com"}',
        "JWT_TOKEN",
        _REVIEW_JWT,
    ),
    ("jwt-dot-xxx", _REVIEW_JWT + ".xxx", "JWT_TOKEN", _REVIEW_JWT),
    # F3: a placeholder's spelling at the start of a real password.
    (
        "url-dollar-word",
        "psql 'postgres://app:$uperS3cret@db.internal/app'",
        "DATABASE_URL",
        "$uperS3cret",
    ),
    (
        "url-example-word",
        "psql postgres://app:example-Pr0d-pass@db.internal/app",
        "DATABASE_URL",
        "example-Pr0d-pass",
    ),
    (
        "url-your-word",
        "psql postgres://app:your_Pr0d_s3cret@db.internal/app",
        "DATABASE_URL",
        "your_Pr0d_s3cret",
    ),
    (
        "url-placeholder-word",
        "psql postgres://app:placeholder_9fK2x@db.internal/app",
        "DATABASE_URL",
        # The tail: "placeholder" is a word the message itself uses.
        "lder_9fK2x",
    ),
    (
        "url-bind-shaped",
        "psql postgres://app::hunter2x@db.internal/app",
        "DATABASE_URL",
        ":hunter2x",
    ),
    (
        "url-open-substitution",
        "psql postgres://app:$(s3cretpw@db.internal/app",
        "DATABASE_URL",
        "$(s3cretpw",
    ),
    (
        "github-example-tail",
        "gh" + "p_example" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
        "GITHUB_TOKEN",
        "exampleA1b2C3d4E5f6G7h8I9j0K1l2M3n4",
    ),
    (
        "bearer-your-word",
        "Authorization: Bearer your_9fK2xA7bQ1zR",
        "BEARER_TOKEN",
        "your_9fK2xA7bQ1zR",
    ),
    (
        "bearer-example-word",
        "Authorization: Bearer example-9fK2xA7bQ1zR",
        "BEARER_TOKEN",
        "example-9fK2xA7bQ1zR",
    ),
    # F4: key material wrapped short, spaced or JSON-escaped.
    ("pem-wrapped-39", _pem(39), "PRIVATE_KEY", _KEY_B64),
    ("pem-wrapped-32", _pem(32), "PRIVATE_KEY", _KEY_B64),
    ("pem-spaced-groups", _pem(32, " "), "PRIVATE_KEY", _KEY_B64),
    ("pem-json-escapes", _pem(32, "\\n"), "PRIVATE_KEY", _KEY_B64),
    # F9: a truncated key the sanitizer reads as its BEGIN line only.
    (
        "pem-truncated-with-a-comment",
        _PEM_BEGIN + "\n# prod key\n" + "\n".join(_TRUNCATED_LINES),
        "PRIVATE_KEY",
        _KEY_B64[:448],
    ),
    (
        "pem-truncated-in-a-blockquote",
        _PEM_BEGIN + "\n" + "\n".join("> " + line for line in _TRUNCATED_LINES),
        "PRIVATE_KEY",
        _KEY_B64[:448],
    ),
    # F10: GitLab access tokens.
    ("gitlab-token", f"git push uses {_GITLAB}", "GITLAB_TOKEN", _GITLAB),
    (
        "gitlab-token-as-url-user",
        f"git clone https://{_GITLAB}@gitlab.com/acme/app.git",
        "GITLAB_TOKEN",
        _GITLAB,
    ),
]
REVIEW_IDS = [row[0] for row in REVIEW]

#: The 16 false positives the keyed families measured on the shipped bodies
#: (#2012 ruling): CLI flags read as keys, a ``\\`` continuation, a ``>``
#: redirect, ``$VAR"``, ``...``, ``false```, ``os.environ[...]``,
#: ``file://token.jwt``, ``AccessDenied:``. Each must be stored as written.
FALSE_POSITIVES = [
    "  sudo sed -i 's/^api_key:.*/api_key: <VALID_API_KEY>/' /etc/datadog-agent/datadog.yaml",
    "  curl -v \"https://api.$(datadog-agent config | awk '/^site:/{print $2}')/api/v1/"
    'validate" -H "DD-API-KEY: $DD_API_KEY"',
    "#   export MIMIR_API_KEY=...",
    'gcloud auth application-default print-access-token > /dev/null && echo "GCP OK"',
    '  aws ecr get-login-password --region "$REGION" | docker login --username AWS '
    '--password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"',
    "could not get token: AccessDenied: Access denied",
    "aws eks get-token --cluster-name my-cluster --region us-east-1",
    "  aws eks get-token --cluster-name my-cluster --region us-west-2",
    "  aws ecr get-login-password --region <region> | kubectl create secret "
    "docker-registry ecr-creds \\",
    "  aws sts get-session-token --serial-number <mfa-arn> --token-code <code>",
    "  aws sts assume-role-with-web-identity --role-arn ... --web-identity-token "
    "file://token.jwt --role-session-name verify",
    '      headers = {"X-Aws-Parameters-Secrets-Token": os.environ["AWS_SESSION_TOKEN"]}',
    "  aws secretsmanager rotate-secret \\",
    "  aws secretsmanager rotate-secret \\",
    "5. Set `automountServiceAccountToken: false` on ServiceAccounts that do not need it.",
    'curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/',
]
#: What the full sanitizer also rewrote on update: addresses.
ADDRESSES = [
    "  server srv1 10.0.0.11:8080 check",
    "  ssh admin@192.168.1.20 'systemctl status postgresql'",
]

#: Placeholders the A1 filter turns into warnings (Fable's list and probe):
#: stored as written, never a 422.
PLACEHOLDERS = [
    "postgres://user:<password>@host/db",
    "postgresql://${DB_USER}:${DB_PASS}@db:5432/app",
    "redis://:$REDIS_PASSWORD@redis:6379/0",
    "https://<user>:<token>@github.com/org/repo.git",
    "git clone https://oauth2:<token>@gitlab.com/group/repo.git",
    "DATABASE_URL=postgres://$USER:$PASSWORD@$HOST/$DB",
    "The file starts with -----BEGIN " + "PRIVATE KEY----- and ends with -----END "
    "PRIVATE KEY-----.",
    "-----BEGIN " + "PRIVATE KEY-----\n...\n-----END " + "PRIVATE KEY-----",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA "
    "PRIVATE KEY-----",
    "Authorization: Bearer YOUR_TOKEN_HERE",
    "gh" + "p_" + "X" * 36,
    "xo" + "xb-your-token-here",
    "AK" + "IA" + "X" * 16,
    "sk" + "-" + "x" * 40,
    "aws_secret_access_key = " + "X" * 40,
    # Still placeholders after the review of f137dfc2c: a JWT whose signature
    # part is `<…>` (F2), an illustrative key quoted in markdown (F9), and a
    # GitLab token of `X`s (F10).
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.<signature>",
    # ... and inside JSON: the token stops at its own characters, so the `"}`
    # after `<signature>` is not part of its third part (F2).
    '{"id_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0'
    '.<signature>"}',
    "> -----BEGIN " + "PRIVATE KEY-----\n> MIIEpAIBAAKCAQEA...\n> -----END "
    "PRIVATE KEY-----",
    "gl" + "pat-" + "X" * 20,
]
#: Shapes no shape pattern detects at all (Fable's "ok" rows).
UNDETECTED = [
    "Authorization: Bearer <token>",
    "Authorization: Bearer $TOKEN",
    "Authorization: Bearer ${TOKEN}",
    "-H 'Authorization: Bearer $(cat token.txt)'",
    "Authorization: Basic <base64-credentials>",
    "Authorization: Basic $(echo -n user:pass | base64)",
    "gh" + "p_<your-token>",
    "AKIA...",
    "aws_secret_access_key = <your-secret>",
    "sk-...",
    "sk-<your-key>",
    "sk-proj-" + "X" * 32,
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.<payload>.<signature>",
    "eyJhbGci...",
    "k8s_secret_<id>",
    "api_key_<hex>",
    "apikey<32 hex chars>",
    "curl -u user:password https://api.example.com",
    "--password <password>",
    "export AWS_ACCESS_KEY_ID=AKIA<16 chars>",
    "export AWS_ACCESS_KEY_ID=<your-key-id>",
    "proxy-authorization: basic xxxxxxxxxxxx",
]

_ROW_TABLES = (
    "knowledge_items",
    "conversion_drafts",
    "conversion_jobs",
    "uploaded_files",
)


def _body_with(snippet: str) -> tuple[str, int]:
    """``valid_runbook`` with ``snippet`` in its Step 1 bash block, and the line
    number of the snippet's first line."""
    anchor = 'psql -c "SELECT 1"'
    body = valid_runbook("Postgres Credential Rotation Runbook").replace(
        anchor, anchor + "\n" + snippet, 1
    )
    line = body.split("\n").index(anchor) + 2
    return body, line


def _fragments(secret: str, size: int = 8) -> set[str]:
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


def _assert_named_not_echoed(
    refusal: RunbookQualityError, entity_type: str, line: int, secret: str
) -> None:
    """The refusal names the type and the line, and holds no 8-character
    fragment of the secret, in its message or its details."""
    named = [e for e in refusal.errors if f"({entity_type})" in e]
    assert named, refusal.errors
    assert f"line {line}" in named[0], named[0]
    rendered = str(refusal) + json.dumps(refusal.details)
    leaked = [f for f in _fragments(secret) if f in rendered]
    assert not leaked, leaked[:3]


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    yield factory
    await engine.dispose()


def _knowledge(session_factory) -> KnowledgeService:
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=5)
    return service


def _conversion(session_factory, knowledge) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=knowledge,
        sanitizer=regex_only_sanitizer(),
    )


async def _row_counts(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (
                KnowledgeItemModel,
                ConversionDraftModel,
                ConversionJobModel,
                UploadedFileModel,
            )
        }


async def _published(session_factory) -> list[str]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(KnowledgeItemModel.content))).scalars()
        )


async def _draft_status(session_factory, draft_id: str) -> str:
    async with session_factory() as session:
        return (await session.get(ConversionDraftModel, draft_id)).status


def _ingest_args(scope: str, **override) -> dict:
    args = dict(
        document_id="kb_0123456789ab",
        title="Postgres Credential Rotation",
        content=valid_runbook(),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope=scope,
        owner_id="u1",
        team_id=TEAM_ID if scope == "team" else None,
    )
    args.update(override)
    return args


def _template(**override) -> dict:
    fields = dict(
        title="Connection Pool Exhausted On The API",
        domain="database",
        service_name="postgresql",
        symptom_class=["resource_exhaustion"],
        severity="high",
        scope="personal",
        tags=["postgres"],
        difficulty="intermediate",
        symptom_recognition="Requests fail with pool timeout errors under load.",
        applicability="Any service using a bounded PostgreSQL connection pool.",
        diagnostic_steps=(
            "### Step 1: Check pool usage\n```bash\npsql -c 'select count(*) "
            "from pg_stat_activity'\n```\nLook for a count near the pool ceiling."
        ),
        causes=(
            "### Cause A: Pool leak\n"
            "**Statement:** connections are never returned to the pool.\n"
            "**Indicators:**\n- root: [Step 1] active connections pinned\n"
            "**Interventions:**\n- **remediation** (root): fix the leak.\n"
            "  **Verification:** Re-run Step 1.\n\n"
            "### Cause Z: Unidentified\n"
            "**Statement:** none of the causes match.\n"
            "**Indicators:**\n- [Default]\n"
            "**Interventions:**\n- **mitigation** (D): consult an SME.\n"
        ),
        prevention="Alert on pool saturation above eighty percent.",
        user_id="u1",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )
    fields.update(override)
    return fields


# =============================================================================
# The choke point every non-shipped publish converges on
# =============================================================================


class TestTheChokePoint:
    """``ingest_runbook`` creates every ``knowledge_items`` row. A body holding a
    secret is refused there, at every tier, before the row or the vectors.

    Mutation: drop the ``aenforce_runbook_security`` call in ``ingest_runbook``.
    """

    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_a_body_holding_a_secret_is_refused_before_any_write(
        self, session_factory, label, snippet, entity_type, secret, scope
    ):
        service = _knowledge(session_factory)
        body, line = _body_with(snippet)

        with pytest.raises(RunbookQualityError) as refused:
            await service.ingest_runbook(**_ingest_args(scope, content=body))

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        assert await _row_counts(session_factory) == dict.fromkeys(_ROW_TABLES, 0)
        service._index_document_in_vector_store.assert_not_awaited()

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REVIEW, ids=REVIEW_IDS)
    async def test_each_secret_the_review_found_is_refused(
        self, session_factory, label, snippet, entity_type, secret
    ):
        """The review of f137dfc2c (F2, F3, F4, F9, F10): each was published."""
        service = _knowledge(session_factory)
        body, line = _body_with(snippet)

        with pytest.raises(RunbookQualityError) as refused:
            await service.ingest_runbook(**_ingest_args("personal", content=body))

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        assert await _row_counts(session_factory) == dict.fromkeys(_ROW_TABLES, 0)
        service._index_document_in_vector_store.assert_not_awaited()

    @pytest.mark.parametrize(
        "lines",
        [FALSE_POSITIVES, ADDRESSES, PLACEHOLDERS, UNDETECTED],
        ids=["false_positives", "addresses", "placeholders", "undetected"],
    )
    async def test_everything_else_is_stored_as_written(self, session_factory, lines):
        service = _knowledge(session_factory)
        body, _ = _body_with("\n".join(lines))

        await service.ingest_runbook(**_ingest_args("personal", content=body))

        assert await _published(session_factory) == [body]
        indexed = service._index_document_in_vector_store.await_args.args[0]
        assert indexed.content == body

    async def test_a_destructive_command_is_refused_too(self, session_factory):
        """It refuses exactly what ``find_security_hazards`` reports as errors."""
        service = _knowledge(session_factory)
        body, _ = _body_with("rm -rf /")

        with pytest.raises(RunbookQualityError, match="rm -rf"):
            await service.ingest_runbook(**_ingest_args("personal", content=body))

        assert await _published(session_factory) == []


class TestUpload:
    """``POST /knowledge/documents`` runs the full gate first, which now sees the
    secret through the validator; nothing is written."""

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_a_secret_is_refused_and_nothing_is_written(
        self, session_factory, label, snippet, entity_type, secret
    ):
        service = _knowledge(session_factory)
        body, line = _body_with(snippet)

        with pytest.raises(RunbookQualityError) as refused:
            await service.upload_document(
                content=body,
                title="Postgres Credential Rotation",
                document_type="runbook",
                scope="personal",
                owner_id="u1",
            )

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        assert await _row_counts(session_factory) == dict.fromkeys(_ROW_TABLES, 0)
        service._index_document_in_vector_store.assert_not_awaited()

    async def test_the_false_positives_and_placeholders_are_stored_as_written(
        self, session_factory
    ):
        service = _knowledge(session_factory)
        body, _ = _body_with("\n".join(FALSE_POSITIVES + ADDRESSES + PLACEHOLDERS))

        await service.upload_document(
            content=body.replace("\n", "\r\n"),
            title="Postgres Credential Rotation",
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        assert await _published(session_factory) == [body]


class TestTemplateDraft:
    """A runbook written from the template is a draft first: its validation sees
    the secret, and publishing it is refused."""

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_the_draft_fails_validation_and_is_not_published(
        self,
        session_factory,
        label,
        snippet,
        entity_type,
        secret,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        steps = f"### Step 1: Check pool usage\n```bash\n{snippet}\n```\nLook for it."

        created = await conversion.create_runbook_from_template(
            **_template(diagnostic_steps=steps)
        )

        draft = created["draft"]
        assert not draft.validation.passed
        named = [e for e in draft.validation.errors if f"({entity_type})" in e]
        assert named, draft.validation.errors
        assert not any(f in named[0] for f in _fragments(secret))
        with pytest.raises(ValidationException):
            await conversion.verify_draft(
                created["conversion_id"], draft.draft_id, "u1"
            )
        assert await _published(session_factory) == []
        knowledge._index_document_in_vector_store.assert_not_awaited()


class TestADraftValidatedBefore:
    """A draft's ``validation_passed`` was recorded when it was written. One
    written before the secret check existed (or by any other path) still says
    True, so the publish itself must refuse: ``ingest_runbook`` does, and the
    draft stays a draft.

    Mutation: drop the call in ``ingest_runbook``: these publish the secret.
    """

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_a_template_draft(
        self,
        session_factory,
        label,
        snippet,
        entity_type,
        secret,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        created = await conversion.create_runbook_from_template(**_template())
        draft_id = created["draft"].draft_id
        body, line = _body_with(snippet)
        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, draft_id)
            row.content = body
            row.validation_passed = True
            await session.commit()

        with pytest.raises(RunbookQualityError) as refused:
            await conversion.verify_draft(created["conversion_id"], draft_id, "u1")

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        assert await _published(session_factory) == []
        knowledge._index_document_in_vector_store.assert_not_awaited()
        assert await _draft_status(session_factory, draft_id) == DraftStatus.DRAFT.value

    @pytest.mark.parametrize("source_type", [SourceType.CASE, SourceType.DOCUMENT])
    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_a_conversion_or_case_draft(
        self, session_factory, label, snippet, entity_type, secret, source_type
    ):
        """The model-written draft of a case (or a document): the persistence
        path a case conversion takes, then the publish."""
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        body, line = _body_with(snippet)
        draft_id = "draft_case_2012"
        await _persist_job(
            session_factory,
            None,
            conversion_id="conv_case_2012",
            user_id="u1",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(
                filename="Case c1", size_bytes=1, content_type="x"
            ),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[],
                source_assessment=SourceAssessment(
                    content_type="resolved_case",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[
                ConversionDraft(
                    draft_id=draft_id,
                    runbook_id="rb-case-2012",
                    title="Postgres Credential Rotation",
                    scope="personal",
                    status=DraftStatus.DRAFT,
                    source_type=source_type,
                    validation=ValidationResult(passed=True),
                    quality_score=QualityScore(
                        overall=80.0,
                        grade="B",
                        completeness=80.0,
                        clarity=80.0,
                        actionability=80.0,
                        comprehensiveness=80.0,
                    ),
                    content_preview="preview",
                    content=body,
                )
            ],
            created_at=datetime.now(timezone.utc),
            source_type=source_type.value,
            case_id="case-x" if source_type == SourceType.CASE else None,
            sanitizer=regex_only_sanitizer(),
        )

        with pytest.raises(RunbookQualityError) as refused:
            await conversion.verify_draft("conv_case_2012", draft_id, "u1")

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        assert await _published(session_factory) == []
        knowledge._index_document_in_vector_store.assert_not_awaited()
        assert await _draft_status(session_factory, draft_id) == DraftStatus.DRAFT.value

    async def test_a_clean_draft_is_published_as_written(
        self, session_factory, tmp_path, monkeypatch
    ):
        """The control: the same path publishes a body that holds only the false
        positives and placeholders, byte for byte (but the frontmatter
        ``verify_draft`` stamps)."""
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        created = await conversion.create_runbook_from_template(**_template())
        draft_id = created["draft"].draft_id
        lines = FALSE_POSITIVES + ADDRESSES + PLACEHOLDERS
        body, _ = _body_with("\n".join(lines))
        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, draft_id)
            row.content = body
            row.validation_passed = True
            await session.commit()

        await conversion.verify_draft(created["conversion_id"], draft_id, "u1")

        (published,) = await _published(session_factory)
        after_frontmatter = published.split("# Runbook: Sample", 1)[1]
        assert after_frontmatter.rstrip("\n") == body.split("# Runbook: Sample", 1)[
            1
        ].rstrip("\n")


class TestUpdate:
    """``PUT /knowledge/documents/{id}``: the edit of a published body. It used
    to be run through the full sanitizer, which rewrote it; now it is stored as
    sent, and refused when it holds a secret.

    Mutation: drop the call in ``update_document_metadata``: the refusal test
    fails (the secret is stored); restore the ``asanitize``: the stored-as-sent
    tests fail.
    """

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    async def test_a_secret_is_refused_and_the_runbook_is_unchanged(
        self, session_factory, label, snippet, entity_type, secret
    ):
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal"))
        service._index_document_in_vector_store.reset_mock()
        body, line = _body_with(snippet)

        with pytest.raises(RunbookQualityError) as refused:
            await service.update_document_metadata(
                "kb_0123456789ab", content=body, title="Login fails (password=hunter2)"
            )

        _assert_named_not_echoed(refused.value, entity_type, line, secret)
        async with session_factory() as session:
            row = await session.get(KnowledgeItemModel, "kb_0123456789ab")
        assert row.content == valid_runbook()
        assert row.title == "Postgres Credential Rotation"
        service._index_document_in_vector_store.assert_not_awaited()

    async def test_a_clean_edit_is_stored_as_sent_and_its_title_scrubbed(
        self, session_factory
    ):
        """The full sanitizer turned the addresses into ``<IP_ADDRESS_…>`` and
        cut ``get-login-password --region``; none of it is touched now. The
        title, a label, is still scrubbed (#1981)."""
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal"))
        body, _ = _body_with("\n".join(FALSE_POSITIVES + ADDRESSES + PLACEHOLDERS))

        result = await service.update_document_metadata(
            "kb_0123456789ab",
            content=body.replace("\n", "\r\n"),
            title="Login fails (password=hunter2)",
        )

        assert result["content"] == body
        assert await _published(session_factory) == [body]
        assert "hunter2" not in result["title"] and "<PASSWORD_" in result["title"]
        indexed = service._index_document_in_vector_store.await_args.args[0]
        assert indexed.content == body

    async def test_no_shipped_body_becomes_uneditable(self, session_factory):
        """An admin's edit of a shipped runbook goes through the check: every
        one of the 91 bodies passes it, re-saved as it is."""
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("global", shipped=True))
        bodies = sorted(PACK.rglob("*.md"))
        assert len(bodies) == 91

        for path in bodies:
            text = path.read_text(encoding="utf-8")
            await service.update_document_metadata("kb_0123456789ab", content=text)
            assert await _published(session_factory) == [text], path.name


class TestShippedPack:
    """The pack is the one exemption (``shipped=True``, ``kb_init`` only), and it
    is exempt for nothing: none of its 91 bodies holds an error.

    Mutation: select the keyed families too: the shipped bodies are refused.
    """

    def test_no_shipped_body_is_refused_and_two_warn(self):
        bodies = sorted(PACK.rglob("*.md"))
        assert len(bodies) == 91
        warned = []
        for path in bodies:
            text = path.read_text(encoding="utf-8")
            enforce_runbook_security(text)
            errors, warnings = find_security_hazards(text)
            assert errors == [], (path.name, errors)
            if warnings:
                warned.append(path.name)
        # The two placeholder warnings the validator gave before this change
        # (`token = "..."`-shaped placeholders); they never block.
        assert warned == ["terraform-plan-failures.md", "mysql-replication-broken.md"]

    def test_the_shape_set_is_every_pattern_that_names_no_value(self):
        """Selected by the attribute, so a pattern added later is covered."""
        assert _SHAPE_PATTERNS == tuple(
            p for p in SECRET_PATTERNS if not p.names_a_value
        )
        assert _SHAPE_PATTERNS and all(not p.names_a_value for p in _SHAPE_PATTERNS)

    async def test_a_shipped_body_is_stored_as_written_unchecked(self, session_factory):
        service = _knowledge(session_factory)
        body, _ = _body_with(REAL[0][1])

        await service.ingest_runbook(
            **_ingest_args("global", content=body, shipped=True)
        )

        assert await _published(session_factory) == [body]


# =============================================================================
# The routes
# =============================================================================


def _client(service) -> TestClient:
    """The real knowledge router with the app's real exception handlers."""
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.state.team_service = None
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    # As `main.py` registers it: a dict `detail` is answered with its
    # `message` alone, which is what a client shows (#2012 review F6).
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _service():
        return service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            enterprise_id="ent-1",
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[get_knowledge_service] = _service
    app.dependency_overrides[require_authentication] = _user
    return TestClient(app, raise_server_exceptions=False)


class TestTheRoutesAnswer422:
    """The refusal reaches the caller as the structured 422 the upload route has
    always answered — not the update route's blanket 500 — and the response
    holds no fragment of the secret.

    Mutation: delete the ``except RunbookQualityError`` clause in
    ``update_document``: the update answers 500.
    """

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    def test_an_update(self, monkeypatch, label, snippet, entity_type, secret):
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
        body, line = _body_with(snippet)

        response = _client(service).put(
            "/api/v1/knowledge/documents/kb_x", json={"content": body}
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        # What the dashboard shows: the finding's type, line and the advice.
        assert detail.startswith("Runbook does not meet quality standards: ")
        assert f"line {line} ({entity_type})" in detail, detail
        assert "rotate the credential" in detail
        assert not [f for f in _fragments(secret) if f in response.text]

    @pytest.mark.parametrize("label,snippet,entity_type,secret", REAL, ids=REAL_IDS)
    def test_an_upload(self, label, snippet, entity_type, secret):
        async def upload_document(content, **_):
            enforce_runbook_quality(content)  # the service's gate
            return {"document_id": "kb_x"}

        body, line = _body_with(snippet)

        response = _client(SimpleNamespace(upload_document=upload_document)).post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={"file": ("rb.md", body.encode(), "text/markdown")},
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail.startswith("Runbook does not meet quality standards: ")
        assert f"line {line} ({entity_type})" in detail, detail
        assert not [f for f in _fragments(secret) if f in response.text]

    def test_the_structured_detail_carries_the_errors_and_the_help(self):
        """For a handler that keeps a dict detail: the errors, the help for a
        credential, and the same errors in the message."""
        body, line = _body_with(REAL[0][1])
        with pytest.raises(RunbookQualityError) as refused:
            enforce_runbook_security(body)

        detail = routes.runbook_quality_detail(
            refused.value, help_text=RUNBOOK_SECURITY_HELP
        )

        assert detail["help"] == RUNBOOK_SECURITY_HELP
        assert detail["errors"] == refused.value.errors
        assert all(error in detail["message"] for error in detail["errors"])


class TestBulkUpdate:
    """``POST /knowledge/documents/bulk-update`` loops over the update. A refused
    body is the caller's content, not a fault: logged at INFO, never ERROR,
    and reported with the endpoint's generic per-item text (#866).

    Mutation: log the refusal through the generic ``except Exception`` arm.
    """

    async def test_a_refused_body_is_a_generic_failure_logged_at_info(
        self, session_factory, caplog
    ):
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal"))
        body, _ = _body_with(REAL[0][1])
        name = "faultmaven.modules.knowledge.domain.services.knowledge_service"

        with caplog.at_level(logging.INFO, logger=name):
            result = await service.bulk_update_documents(
                ["kb_0123456789ab"], {"content": body}
            )

        assert result["updated_count"] == 0
        assert result["errors"] == ["Document kb_0123456789ab: update failed"]
        assert await _published(session_factory) == [valid_runbook()]
        records = [r for r in caplog.records if r.name == name]
        assert [r for r in records if "Refused" in r.getMessage()], records
        assert not [r for r in records if r.levelno >= logging.ERROR]
        assert not [
            # The token's tail: its digits are the document id's too.
            f
            for r in records
            for f in _fragments(_GITHUB[14:])
            if f in r.getMessage()
        ]


# =============================================================================
# What the refusal says
# =============================================================================


class TestTheMessage:
    """One error per type, naming every line it is on and never its text, and
    telling the author what to do. A placeholder is a warning."""

    def test_one_error_per_type_with_every_line(self):
        snippet = "\n".join(
            [f"export A={_AWS}", f"export B={_AWS}"]
            + [f"echo {_GITHUB}" for _ in range(12)]
        )
        body, line = _body_with(snippet)

        errors, _ = find_security_hazards(body)

        aws = [e for e in errors if "(AWS_ACCESS_KEY)" in e]
        github = [e for e in errors if "(GITHUB_TOKEN)" in e]
        assert len(aws) == len(github) == 1, errors
        assert f"lines {line} and {line + 1} (AWS_ACCESS_KEY)" in aws[0]
        # Ten lines listed, then how many more: the 422 stays bounded.
        listed = ", ".join(str(n) for n in range(line + 2, line + 12))
        assert f"lines {listed} and 2 more (GITHUB_TOKEN)" in github[0]
        assert "<AWS_ACCESS_KEY_ID>" in aws[0] and "rotate" in aws[0]

    @pytest.mark.parametrize("text", PLACEHOLDERS)
    def test_a_placeholder_is_a_warning_naming_its_line(self, text):
        body, line = _body_with(text)

        errors, warnings = find_security_hazards(body)

        assert errors == []
        assert any(f"line {line}" in w and "not blocking" in w for w in warnings)

    @pytest.mark.parametrize("text", UNDETECTED + FALSE_POSITIVES + ADDRESSES)
    def test_what_no_shape_detects_says_nothing(self, text):
        body, _ = _body_with(text)

        assert find_security_hazards(body) == ([], [])

    def test_a_real_secret_inside_a_placeholder_url_is_still_refused(self):
        """Each shape is judged by its own portion: a URL's placeholder password
        does not excuse an AWS key id written as its user."""
        body, line = _body_with(f"psql postgres://{_AWS}:<password>@db/app")

        errors, _ = find_security_hazards(body)

        assert [e for e in errors if f"on line {line} (AWS_ACCESS_KEY)" in e], errors

    def test_the_validator_and_the_draft_see_it(self):
        """The draft validation and the upload route read the same function."""
        body, line = _body_with(REAL[0][1])

        result = RunbookValidator().validate_content(body.replace("\n", "\r\n"))

        assert not result.passed
        assert any(f"line {line} (GITHUB_TOKEN)" in e for e in result.errors)


class TestDeadIngesterPaths:
    """``KnowledgeIngester.ingest_document`` / ``ingest_document_object`` have no
    caller, and no longer hold the opposite policy: the body goes through as
    written, the title is still scrubbed (#1981).

    Mutation: restore the body ``asanitize`` in either method.
    """

    _BODY = "\n".join(["# Body", *FALSE_POSITIVES, *ADDRESSES])

    @classmethod
    def _ingester(cls) -> KnowledgeIngester:
        ingester = KnowledgeIngester.__new__(KnowledgeIngester)
        ingester.logger = MagicMock()
        ingester.sanitizer = regex_only_sanitizer()
        ingester._process_and_store = AsyncMock()
        ingester._extract_text = AsyncMock(return_value=cls._BODY)
        return ingester

    async def test_ingest_document(self):
        ingester = self._ingester()

        await ingester.ingest_document(
            "/dev/null", "Login fails (password=hunter2)", scope="personal"
        )

        document = ingester._process_and_store.await_args.args[0]
        assert document.content == self._BODY
        assert "hunter2" not in document.title

    async def test_ingest_document_object(self):
        ingester = self._ingester()
        document = KnowledgeBaseDocument(
            document_id="d1",
            title="Login fails (password=hunter2)",
            content=self._BODY,
            document_type="runbook",
            tags=[],
            scope="personal",
            owner_id="u1",
            created_at="2026-10-15T00:00:00+00:00",
            updated_at="2026-10-15T00:00:00+00:00",
        )

        await ingester.ingest_document_object(document)

        stored = ingester._process_and_store.await_args.args[0]
        assert stored.content == self._BODY
        assert "hunter2" not in stored.title
