"""The confirmation gate the writing operator commands share.

It was written out three times verbatim before this — ``fm-remove-org-member``,
``fm-reassign-cases``, ``fm-set-turn-cap`` — and the two rules it encodes are
the kind that rot when copied. Pinned here once, and pinned again per command by
each command's own exit-code cases, so a command that stopped calling it fails
its own module rather than this one.
"""

from __future__ import annotations

import argparse

import pytest

from faultmaven.cli._confirmation import require_confirmation

pytestmark = pytest.mark.unit

CONSEQUENCE = "This changes something an operator would want back."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fm-probe")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def _args(**flags):
    return argparse.Namespace(**{"dry_run": False, "yes": False, **flags})


def test_dry_run_alone_passes():
    require_confirmation(_parser(), _args(dry_run=True), CONSEQUENCE)


def test_yes_alone_passes():
    require_confirmation(_parser(), _args(yes=True), CONSEQUENCE)


def test_both_flags_is_a_usage_error(capsys):
    """Not a preference. Silently taking the dry-run branch would exit 0 and
    read as "it was written" when nothing was — the one failure that looks
    exactly like success."""
    with pytest.raises(SystemExit) as raised:
        require_confirmation(_parser(), _args(dry_run=True, yes=True), CONSEQUENCE)
    assert raised.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_neither_flag_refuses_with_the_callers_own_sentence(capsys):
    """The shape is shared; the consequence is not — only the caller knows it."""
    with pytest.raises(SystemExit) as raised:
        require_confirmation(_parser(), _args(), CONSEQUENCE)
    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert CONSEQUENCE in out
    assert "--dry-run" in out


@pytest.mark.parametrize(
    "module,command",
    [
        ("faultmaven.cli.remove_org_member", "fm-remove-org-member"),
        ("faultmaven.cli.reassign_cases", "fm-reassign-cases"),
        ("faultmaven.cli.set_turn_cap", "fm-set-turn-cap"),
    ],
)
def test_every_writing_command_uses_the_shared_guard(module, command):
    """A fourth copy would make the extraction pointless.

    Asserted on the imported name rather than by grepping the source: a command
    that kept the import but stopped calling it fails its own exit-code cases,
    and a command that re-inlined the rule fails here.
    """
    import importlib

    imported = importlib.import_module(module)
    assert hasattr(
        imported, "require_confirmation"
    ), f"{command} does not use the shared confirmation guard"
