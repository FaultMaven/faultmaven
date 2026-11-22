"""The platform startup, container, config and Jobs areas name an exception by
its class, never its text (#2889 S3h).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, frames
included, and the class name is on the site's line. A refusal string and a
print are read where the interpreter would write them. How each site is driven
is stated on its case.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.lifespan import _bootstrap_kb_step
from faultmaven.bootstrap.middleware import setup_middleware
from faultmaven.config import tls_root_set
from faultmaven.config.investigation_capability import (
    resolve_schema_capacity,
    validate_structured_output_capacity,
)
from faultmaven.config.llm_config_overrides import apply_overrides_to_settings
from faultmaven.config.settings import Environment, get_settings
from faultmaven.container.providers.services import create_share_repository
from faultmaven.jobs import case_cleanup
from faultmaven.jobs import run as jobs_run
from faultmaven.providers.tenancy.factory import BUILTIN_SINGLE
from tests.unit.bootstrap.test_kb_init import _make_session_factory, _write_pack
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _raise(*_args, **_kwargs):
    raise RuntimeError(f"broken {MARKER}")


# ── bootstrap/ ────────────────────────────────────────────────────────────


def test_a_protection_setup_failure_is_named_by_class(lines):
    """``setup_middleware`` on a development box, ``setup_protection_middleware``
    stubbed to raise: the carve-out boots unprotected with its warning."""
    settings = get_settings().model_copy(deep=True)
    settings.server.environment = Environment.DEVELOPMENT
    settings.deployment_mode = "standalone"
    with (
        patch("faultmaven.config.settings.get_settings", return_value=settings),
        patch("faultmaven.api.protection.setup_protection_middleware", _raise),
    ):
        setup_middleware(FastAPI())
    _named(lines(), "Failed to setup protection middleware", ": RuntimeError")


async def test_a_raising_kb_bootstrap_is_named_by_class(lines):
    """``_bootstrap_kb_step``, the lifespan's KB step, with ``bootstrap_kb``
    stubbed to raise: non-fatal, logged with frames."""
    app = FastAPI()
    app.state.knowledge_service = object()
    with (
        patch(
            "faultmaven.container.container",
            SimpleNamespace(knowledge_vector_store=object()),
        ),
        patch(
            "faultmaven.providers.tenancy.factory.requested_tenant_provider",
            return_value=BUILTIN_SINGLE,
        ),
        patch(
            "faultmaven.bootstrap.kb_init.bootstrap_kb",
            new=AsyncMock(side_effect=RuntimeError(f"pack broke {MARKER}")),
        ),
    ):
        await _bootstrap_kb_step(app)
    raised = _named(lines(), "KB bootstrap raised", ": RuntimeError")
    assert raised["frames"].startswith("RuntimeError")


async def test_a_runbook_failure_reason_is_named_by_class(lines, tmp_path):
    """``bootstrap_kb`` over a one-runbook pack whose ingest raises: the reason
    in ``BootstrapResult.failed``, which the lifespan, ``kb_seed`` and
    ``fm-reset-kb`` write out, is the class, and so is the line."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(
        side_effect=RuntimeError(f"index refused {MARKER}")
    )
    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        enterprise_id="ent-test",
        project_root=tmp_path,
        pack_dir=_write_pack(tmp_path),
    )
    assert result.failed == [("global/example.md", "RuntimeError")]
    _named(lines(), "Bootstrap failed for global/example.md", ": RuntimeError")


# ── container/ ────────────────────────────────────────────────────────────


def test_a_factory_construction_failure_is_named_by_class(lines):
    """``create_share_repository`` with the repository's constructor stubbed to
    raise. Only its line is asserted here (#2342 owns what it returns)."""
    with patch(
        "faultmaven.infrastructure.persistence.sessionless_share_repository."
        "SessionlessShareRepository",
        _raise,
    ):
        create_share_repository()
    _named(lines(), "ShareRepository initialization failed", ": RuntimeError")


# ── config/ ───────────────────────────────────────────────────────────────


async def test_a_raising_override_read_is_named_by_class(lines):
    """``apply_overrides_to_settings`` with the override read stubbed to raise:
    nothing is applied."""
    llm = SimpleNamespace()
    with patch(
        "faultmaven.infrastructure.persistence.llm_config_repository."
        "get_all_overrides",
        new=AsyncMock(side_effect=RuntimeError(f"db down {MARKER}")),
    ):
        await apply_overrides_to_settings(SimpleNamespace(llm=llm))
    assert vars(llm) == {}
    _named(lines(), "Could not read LLM config overrides", ": RuntimeError")


def _capacity_settings():
    llm = SimpleNamespace(
        structured_output_provider=None,
        get_structured_output_provider=lambda: "gemini",
        get_structured_output_model=lambda: "gemini-test",
    )
    return SimpleNamespace(llm=llm)


class _RaisingProbe:
    def supports_engine_response_schemas(self, _model):
        raise RuntimeError(f"probe broke {MARKER}")


@pytest.mark.parametrize(
    "registry, reason",
    [
        (
            SimpleNamespace(get_provider=_raise),
            "could not resolve provider 'gemini': RuntimeError",
        ),
        (
            SimpleNamespace(get_provider=lambda _name: _RaisingProbe()),
            "capacity probe raised for gemini/gemini-test: RuntimeError",
        ),
    ],
    ids=["provider-resolution", "capacity-probe"],
)
def test_an_inconclusive_capacity_reason_is_named_by_class(lines, registry, reason):
    """``resolve_schema_capacity`` and ``validate_structured_output_capacity``
    with a registry that raises resolving the provider, or a provider whose
    probe raises: inconclusive, so the gate fails open and logs the reason."""
    capacity = resolve_schema_capacity(_capacity_settings(), registry)
    assert capacity.inconclusive and capacity.can_serve_schemas
    assert capacity.reason == reason
    validate_structured_output_capacity(_capacity_settings(), registry)
    _named(lines(), "schema capacity inconclusive", reason)


def test_a_tls_set_write_refusal_names_the_error_by_class(tmp_path):
    """``_write_set`` under a directory whose parent is a file (named with the
    marker): ``os.makedirs`` raises, and the refusal the interpreter prints
    keeps its fixed text and names the error by class."""
    blocker = tmp_path / f"file-{MARKER}"
    blocker.write_text("")
    with pytest.raises(SystemExit) as refused:
        tls_root_set._write_set(str(blocker / "set" / "roots.pem"), b"")
    cause = refused.value.__cause__
    assert isinstance(cause, OSError) and MARKER in str(cause)
    assert str(refused.value) == (
        f"TLS root set: cannot write the set's file ({type(cause).__name__}); "
        "TMPDIR must name a writable directory."
    )


# ── jobs/ ─────────────────────────────────────────────────────────────────


async def test_a_job_module_that_fails_to_import_is_named_by_class(
    lines, monkeypatch, tmp_path
):
    """``run_job`` for a registered job whose module raises ``ImportError`` on
    import (logging setup stubbed out, so the test's capture stands)."""
    (tmp_path / "s3h_failing_job.py").write_text(
        f"raise ImportError('missing dependency {MARKER}')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(jobs_run.AVAILABLE_JOBS, "s3h_failing", "s3h_failing_job")
    monkeypatch.setattr(jobs_run, "setup_logging", lambda verbose=False: None)
    with pytest.raises(ImportError):
        await jobs_run.run_job("s3h_failing")
    _named(lines(), "Failed to import job module", ": ImportError")


def test_a_failed_job_run_prints_the_error_by_class(capsys, monkeypatch):
    """``main`` with ``run_job`` stubbed to raise (process outputs and the TLS
    root set stubbed out): the error print names the class, exit code 1."""
    monkeypatch.setattr(jobs_run, "install_process_outputs", lambda: None)
    monkeypatch.setattr(tls_root_set, "apply_root_set", lambda: None)
    monkeypatch.setattr(
        jobs_run, "run_job", AsyncMock(side_effect=RuntimeError(f"boom {MARKER}"))
    )
    assert jobs_run.main(["case_cleanup"]) == 1
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    assert captured.err == "Error: RuntimeError\n"


async def test_a_failed_job_result_names_the_error_by_class(lines):
    """``case_cleanup.run`` with the case repository's read raising: the
    result ``run_job`` logs whole ("Job completed: ...") holds the class."""
    repository = AsyncMock()
    repository.list_all_case_ids.side_effect = RuntimeError(f"db down {MARKER}")
    container = SimpleNamespace(
        case_vector_store=AsyncMock(), case_repository=repository
    )
    result = await case_cleanup.run(settings=MagicMock(), container=container)
    assert (result["status"], result["error"]) == ("failed", "RuntimeError")
    assert MARKER not in repr(result)
    _named(lines(), "Case cleanup job failed", ": RuntimeError")
