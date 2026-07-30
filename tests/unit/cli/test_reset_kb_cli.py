"""``fm-reset-kb``'s documented exit codes are real (#887).

The module docstring promises 0 / 1 / 2, and the console-script wrapper is
``sys.exit(main())`` — so a ``main()`` that returned instead of exiting would
still "work" through the wrapper while ``python -m faultmaven.cli.reset_kb``
silently reported success. These pin the refusal codes without a database.
"""

from __future__ import annotations

import sys

import pytest

from faultmaven.cli import reset_kb

pytestmark = pytest.mark.unit


def _run_main(argv):
    original = sys.argv
    sys.argv = argv
    try:
        reset_kb.main()
    finally:
        sys.argv = original


def test_refusing_without_yes_exits_1(capsys):
    """The destructive-action guard. Nothing is imported or connected first."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-reset-kb"])

    assert exc.value.code == 1
    assert "Refusing to run without --yes" in capsys.readouterr().out


def test_unknown_flag_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-reset-kb", "--wipe-everything"])
    assert exc.value.code == 2


def test_help_works_without_docstrings(capsys):
    """``python -OO`` strips ``__doc__``. argparse's description is a literal
    for exactly that reason, so ``--help`` cannot raise AttributeError."""
    assert reset_kb._SUMMARY and isinstance(reset_kb._SUMMARY, str)

    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-reset-kb", "--help"])

    assert exc.value.code == 0
    assert reset_kb._SUMMARY in capsys.readouterr().out.replace("\n", " ")


async def test_refuses_under_multi_tenant_with_code_1(monkeypatch, capsys):
    """A multi-tenant database holds every tenant's KB; the blanket wipe is
    refused in favour of the audited kb_seed job (#770)."""
    from faultmaven.providers.tenancy import factory

    monkeypatch.setattr(
        factory, "requested_tenant_provider", lambda: factory.BUILTIN_MULTI
    )

    code = await reset_kb.reset_kb(
        dry_run=True, all_drafts=False, rebuild=False, keep_chroma=False
    )

    assert code == 1
    assert "TENANT_PROVIDER=multi" in capsys.readouterr().out
