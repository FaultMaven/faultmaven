"""Every call a module makes to its own methods must still bind (#1143 family).

A method's parameter list is a contract with its own file first. Rename a
parameter, reorder two, or make a defaulted one required, and the call sites in
that same module are the first things that break — except when they *don't*
break, which is the case this guards.

The shape that bites: a helper is called positionally, its signature is
reordered, and the argument list still has the right arity. Nothing raises at
import, nothing raises at collection, and the values silently land in the wrong
slots — or collide with a keyword and raise ``TypeError: got multiple values``
the first time a user reaches the path. That is what happened while ADR-017 was
being landed: ``ConversionService._convert_all_failure_modes`` and
``_convert_single_failure_mode`` gained a required ``enterprise_id`` ahead of
``team_id`` (a defaulted tenancy parameter is the #1143 trap and had to go), and
three call sites in the same file went on passing ``…, user_id, team_id`` and
also ``enterprise_id=``. ``convert_document`` raised on every invocation — the
whole document→runbook pipeline dead, not degraded — and no unit test caught it,
because each of those tests drove the pipeline and therefore failed for what
looked like its own reason.

So this binds every self-call in the modules below against the live signature,
using ``inspect.Signature.bind`` — the same resolution Python performs at call
time, minus the execution. It answers one question and answers it statically:
*could this call be made at all?* It deliberately says nothing about the VALUES
being right; a positional argument landing in the wrong slot of a same-typed
signature still binds, and only a test of the behaviour can see that.

Placed here rather than beside the service because it is a property of a file's
internal consistency, like the contract-conformance rules next door, and because
the list below is meant to grow: any module where a method calls its siblings
with more than a couple of arguments is a candidate.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

#: ``module import path`` → the class whose methods it calls on ``self``.
#:
#: Kept explicit rather than discovered: a sweep over every module would spend
#: real time to re-derive a mostly-empty answer, and the modules that matter are
#: the ones with wide, positional, tenancy-carrying helper calls.
_MODULES_UNDER_GUARD = {
    "faultmaven.modules.knowledge.domain.services.conversion_service": (
        "ConversionService"
    ),
}


def _self_calls(tree: ast.AST) -> Iterator[ast.Call]:
    """Every ``self.<name>(...)`` call in the module."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            yield node


def _binding_failures(module_path: str, class_name: str) -> list[str]:
    """Every self-call in ``module_path`` that cannot bind to its signature."""
    module = __import__(module_path, fromlist=[class_name])
    owner = getattr(module, class_name)
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")

    failures: list[str] = []
    for call in _self_calls(ast.parse(source)):
        name = call.func.attr
        target = getattr(owner, name, None)
        if not callable(target):
            # A self-attribute that happens to be callable data (an injected
            # port, a lambda) is not this rule's business.
            continue
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):  # pragma: no cover - builtins etc.
            continue

        # Whether the receiver occupies a slot depends on what kind of method
        # this is, and getting it wrong makes the rule lie in both directions.
        # ``self.f(x)`` on a **staticmethod** passes NOTHING for a receiver, and
        # on a **classmethod** the descriptor supplies ``cls`` — in both cases
        # the signature read off the class already omits it. Only a plain
        # function accessed through the class still shows ``self``.
        static = inspect.getattr_static(owner, name, None)
        takes_receiver = not isinstance(static, (staticmethod, classmethod))

        # One placeholder per positional argument, plus the receiver where one
        # is consumed; keywords by name. Placeholders, because this rule is
        # about ARITY AND NAMES — whether the call is well-formed — never about
        # types or values.
        positional = (["<self>"] if takes_receiver else []) + ["<positional>"] * len(
            call.args
        )
        keywords = {kw.arg: "<keyword>" for kw in call.keywords if kw.arg}
        if any(kw.arg is None for kw in call.keywords) or any(
            isinstance(a, ast.Starred) for a in call.args
        ):
            # ``*args`` / ``**kwargs`` at the call site make the arity unknown
            # statically; binding a placeholder would be a guess.
            continue
        try:
            signature.bind(*positional, **keywords)
        except TypeError as exc:
            failures.append(f"{module_path}:{call.lineno} self.{name}(...) — {exc}")
    return failures


@pytest.mark.parametrize("module_path,class_name", sorted(_MODULES_UNDER_GUARD.items()))
def test_every_self_call_binds_to_its_own_signature(
    module_path: str, class_name: str
) -> None:
    """The guard. A call that cannot bind is a runtime ``TypeError`` waiting."""
    failures = _binding_failures(module_path, class_name)
    assert failures == [], (
        "these calls cannot bind to the signature they name, so they raise "
        "TypeError the first time they run:\n  " + "\n  ".join(failures)
    )


def test_the_detector_finds_the_calls_it_is_supposed_to_check() -> None:
    """Positive control: the walker sees real self-calls.

    Without it, an AST change that stopped matching ``self.<name>(...)`` would
    make the guard above pass vacuously — which is the same failure mode it
    exists to prevent, one level up.
    """
    module_path, class_name = sorted(_MODULES_UNDER_GUARD.items())[0]
    module = __import__(module_path, fromlist=[class_name])
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")

    calls = list(_self_calls(ast.parse(source)))
    assert len(calls) > 20, f"only {len(calls)} self-calls found — walker drifted"
    # And the specific helpers the #1143 reorder went through are among them.
    named = {call.func.attr for call in calls}
    assert "_convert_all_failure_modes" in named
    assert "_convert_single_failure_mode" in named


def test_the_detector_reports_a_call_that_cannot_bind() -> None:
    """Positive control: a genuinely unbindable call IS reported.

    The regression itself, in miniature — a helper called positionally through
    the slot a later parameter now occupies, plus the same name as a keyword.
    Asserted against a hand-built pair rather than against the live module,
    because the live module is (and must stay) clean, so an instance drawn from
    it could only ever prove the absence.
    """

    class _Reordered:
        def helper(self, text, user_id, enterprise_id, team_id=None): ...

    signature = inspect.signature(_Reordered.helper)
    with pytest.raises(TypeError, match="multiple values"):
        signature.bind(
            "self",
            "<positional>",
            "<positional>",
            "<positional>",
            enterprise_id="<keyword>",
        )
    # And the well-formed shape binds, so the check is not simply always red.
    signature.bind("<self>", "<positional>", "<positional>", enterprise_id="x")


def test_a_staticmethod_called_through_self_is_not_miscounted() -> None:
    """The receiver rule, in both directions.

    ``self.f(x)`` on a staticmethod passes no receiver, so prepending one
    reports "too many positional arguments" for a call that is perfectly legal —
    which is exactly what this detector did on its first run against
    ``ConversionService._duplicate_draft_conflict``. A guard that cries wolf on
    correct code gets muted, so the distinction is pinned rather than assumed.
    """

    class _Mixed:
        @staticmethod
        def plain(value): ...

        def method(self, value): ...

    assert isinstance(inspect.getattr_static(_Mixed, "plain"), staticmethod)
    assert not isinstance(inspect.getattr_static(_Mixed, "method"), staticmethod)

    # The staticmethod takes one argument and no receiver...
    inspect.signature(_Mixed.plain).bind("<positional>")
    with pytest.raises(TypeError):
        inspect.signature(_Mixed.plain).bind("<self>", "<positional>")
    # ...while the ordinary method takes the receiver as well.
    inspect.signature(_Mixed.method).bind("<self>", "<positional>")
