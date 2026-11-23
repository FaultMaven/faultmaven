"""An ``fm-*`` command prints its own refusal whole and names any other exception
by class (#2889 S3i).

Principles §7 rule 1, as ruled on #2889: a refusal sentence a first-party
refusal class composes only from literals, the command's arguments and
identifiers our code holds is printed whole through ``refusal_text``
(``cli/_refusal.py``); every other print names the class. The audited prints
are compared with golden literals, the whole stdout each command printed on
``62fe2e23e`` for the same input, so a refusal's text is byte for byte what it
was. A store or driver failure carries :data:`MARKER` in its text, which no
print holds.
"""

import importlib
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.bootstrap.data_init import UnusableDataDirError
from faultmaven.cli import provision_sso_org, reassign_cases, set_turn_cap
from faultmaven.cli import wipe_deployment as wd
from faultmaven.cli._refusal import AUDITED_REFUSALS, refusal_text
from faultmaven.cli._tenant import EnterpriseRefused
from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.config.settings import set_env_var
from faultmaven.config.tls_root_set import OWNED_VARIABLES, PG_ROOT_INPUT_VARIABLES
from faultmaven.exceptions import UserLookupFailed
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    EnterpriseNotUsable,
    RemapRefused,
)
from faultmaven.infrastructure.redis_client import RedisUnavailableError
from tests.unit.cli.test_wipe_deployment_cli import _engine_only

pytestmark = [pytest.mark.unit, pytest.mark.security]

MARKER = "zqxmarker2889"

_RULE = "=" * 80


def _header(title: str) -> str:
    return f"{_RULE}\n{title}\n{_RULE}\n"


# ── refusal_text ──────────────────────────────────────────────────────────


def test_every_audited_name_is_a_class_that_exists():
    """A renamed or moved class would silently fall out of the audit."""
    for name in AUDITED_REFUSALS:
        module, _, qualname = name.rpartition(".")
        found = getattr(importlib.import_module(module), qualname)
        assert isinstance(found, type) and issubclass(found, BaseException), name
        assert f"{found.__module__}.{found.__qualname__}" == name


@pytest.mark.parametrize(
    "error",
    [
        EnterpriseRefused(f"No usable enterprise 'e' {MARKER}"),
        DeploymentCoherenceError(f"incoherent {MARKER}"),
        UnusableDataDirError(f"CHROMADB_KB_PERSIST_DIR is set but empty {MARKER}"),
        EnterpriseNotUsable(f"enterprise e does not exist or is retired {MARKER}"),
    ],
)
def test_an_audited_refusal_is_its_message(error):
    assert refusal_text(error) == str(error)


class _NotListed(EnterpriseRefused):
    """A subclass of an audited class that nobody audited."""


@pytest.mark.parametrize(
    ("error", "named"),
    [
        (
            UserLookupFailed(f"down {MARKER}", lookup="username", identifier="a"),
            "UserLookupFailed",
        ),
        (ValueError(f"bad {MARKER}"), "ValueError"),
        (LookupError(f"bad {MARKER}"), "LookupError"),
        (KeyError(MARKER), "KeyError"),
        (RedisUnavailableError(f"redis://u:{MARKER}@h"), "RedisUnavailableError"),
        (_NotListed(MARKER), "_NotListed"),
    ],
)
def test_anything_else_is_its_class(error, named):
    """A builtin, a first-party class not audited, a subclass of a builtin, and
    a subclass of an audited class that is not listed itself."""
    assert refusal_text(error).startswith(named)
    assert MARKER not in refusal_text(error)


def test_a_refusal_under_dash_m_prints_its_sentence(tmp_path):
    """``python -m faultmaven.cli.personal_tenant``: the command's classes are
    defined in ``__main__``, which ``refusal_text`` resolves to the module's
    spec name. An empty ``--subject`` is refused before any read."""
    root = Path(__file__).resolve().parents[3]
    skipped = set(OWNED_VARIABLES) | set(PG_ROOT_INPUT_VARIABLES)
    env = {k: v for k, v in os.environ.items() if k not in skipped}
    env.update(FAULTMAVEN_SKIP_DOTENV="1", TMPDIR=str(tmp_path), PYTHONPATH=str(root))
    env["HOME"] = str(tmp_path)
    set_env_var(env, "DATABASE_URL", f"sqlite:///{tmp_path}/fm.db")
    result = subprocess.run(
        [sys.executable, "-m", "faultmaven.cli.personal_tenant"]
        + ["retire", "--subject", "  "],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert result.returncode == 1, result.stdout + result.stderr
    assert "\n❌ --subject was given but is empty. " in result.stdout
    assert "❌ _Refused" not in result.stdout


# ── the prints, against 62fe2e23e ─────────────────────────────────────────


async def test_set_turn_cap_prints_main_s_refusal(capsys):
    """``EnterpriseRefused`` for an enterprise the repository does not hold."""
    repository = AsyncMock()
    repository.get_enterprise.return_value = None
    code = await set_turn_cap.set_turn_cap(
        organization_id="org-1",
        new_value=5,
        show_only=False,
        dry_run=True,
        enterprise_id="ent-missing",
        enterprises=repository,
    )
    assert code == 1
    assert capsys.readouterr().out == _header("Tenant Daily Turn Cap") + (
        "\n❌ No usable enterprise 'ent-missing' is visible.\n"
        "   Check the id (it is an id, not a slug), and note that a soft-deleted "
        "enterprise does not resolve. Nothing was written.\n"
    )


async def _reassign(path: str) -> int:
    return await reassign_cases.reassign_cases(
        enterprise_id="ent-1",
        from_identifier="alice",
        to_identifier="bob",
        case_ids_file=path,
        allow_no_team=False,
        move_unnamed_too=False,
        dry_run=True,
    )


async def test_reassign_cases_prints_main_s_refusal(tmp_path, capsys):
    """``_Refused`` for a case-id file listing an id twice."""
    path = tmp_path / "ids.txt"
    path.write_text("case-1\ncase-2\ncase-1\n")
    assert await _reassign(str(path)) == 1
    assert capsys.readouterr().out == _header("Reassign Cases") + (
        f"\n❌ --case-ids-file '{path}' lists case-1 more than once. Refusing: a "
        "duplicate means the file was assembled wrong, and the id count is the "
        "number an operator checks this run against.\n"
    )


async def test_reassign_cases_names_an_unreadable_file_s_error_by_class(
    tmp_path, capsys
):
    """``_Refused`` around an ``OSError``: ``main`` printed the error's text
    (``[Errno 2] No such file or directory: ...``); the class now."""
    path = tmp_path / f"absent-{MARKER}" / "ids.txt"
    assert await _reassign(str(path)) == 1
    out = capsys.readouterr().out
    assert out == _header("Reassign Cases") + (
        f"\n❌ could not read --case-ids-file '{path}': FileNotFoundError\n"
    )


def _provision(monkeypatch, raised=None):
    session = SimpleNamespace(get=AsyncMock(return_value=None))

    @asynccontextmanager
    async def get_db_session():
        yield session

    monkeypatch.setattr(
        provision_sso_org,
        "assert_provisioning_db_role_bypasses_rls",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(provision_sso_org, "get_db_session", get_db_session)
    if raised is not None:
        monkeypatch.setattr(
            provision_sso_org,
            "_get_or_create_enterprise",
            AsyncMock(side_effect=raised),
        )
    return provision_sso_org.provision(
        name="Acme",
        slug="acme",
        workos_org_id="org_01ACME",
        domain=None,
        enterprise_id="ent-requested",
    )


async def test_provision_sso_org_prints_main_s_remap_refusal(monkeypatch, capsys):
    """``RemapRefused``: its ids, read as attributes."""
    raised = RemapRefused("org_01ACME", "ent-mapped", "ent-requested")
    assert await _provision(monkeypatch, raised) is False
    assert capsys.readouterr().out == _header("Provision SSO Organization Mapping") + (
        "\n❌ workos organization 'org_01ACME' is already mapped to a different "
        "FaultMaven enterprise.\n"
        "   currently mapped to: ent-mapped\n"
        "   requested:           ent-requested\n"
        "\n   Remapping is a deliberate operator action — it changes which tenant\n"
        "   existing users land in on their next login. Nothing was written. See\n"
        "   docs/operations/sso-org-provisioning.md.\n"
    )


async def test_provision_sso_org_prints_main_s_unusable_enterprise(monkeypatch, capsys):
    """``EnterpriseNotUsable``, raised by the real ``get_or_create_enterprise``
    for an id the session does not hold (a ``LookupError`` on ``main``)."""
    assert await _provision(monkeypatch) is False
    assert capsys.readouterr().out == _header("Provision SSO Organization Mapping") + (
        "❌ enterprise ent-requested does not exist or is retired\n"
    )


async def test_wipe_deployment_names_a_driver_failure_by_class(monkeypatch, capsys):
    """The database preflight raising: the driver's text is on no line."""
    _engine_only(monkeypatch)

    async def _boom():
        raise OSError(f"connection refused {MARKER}")

    monkeypatch.setattr(wd, "preflight_database_role", _boom)
    code = await wd.wipe_deployment(
        mode="verify", confirm_target=None, redis_all_keys=False
    )
    out = capsys.readouterr().out
    assert code == 5
    assert "❌ The database could not be reached: OSError\n" in out
    assert MARKER not in out
