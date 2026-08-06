"""No non-terminal 409 may go out unlabelled.

Several unrelated conflicts share HTTP 409 and a client cannot tell them apart
from the body. The Slack agent resolves that by treating an **unlabelled** 409
on the turn POST as "this case is terminal", because the terminal-case rejection
is the one 409 the route raises with no ``x-error-code``:

    if not error_code and not polled:
        raise CaseTerminalError(...)      # faultmaven-slack-agent/faultmaven/client.py

That inference is only sound while the premise holds. It reads *absence* as
evidence, and the conclusion it draws is a claim about the user's investigation:
a wrongly-unlabelled 409 makes the agent tell someone with a live, open case
that it is closed. Nothing in this repo enforced the premise, and it had already
been broken once — the deduplication middleware's dead
``_create_duplicate_error_response`` emitted exactly that shape.

So the premise is pinned here, on the side that owns it. A new 409 emitter must
either carry ``x-error-code`` or be added to ``UNLABELLED_BY_DESIGN`` with a
reason.
"""

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "faultmaven"

# The single 409 that is unlabelled on purpose. It is the terminal-case
# rejection: `ConflictError` raised by the case service when a turn targets a
# resolved/closed case, rendered by `conflict_exception_handler`. The Slack
# agent's inference exists precisely to catch this one.
UNLABELLED_BY_DESIGN = {
    "faultmaven/api/exception_handlers.py",
}


def _iter_409_responses():
    """Every ``JSONResponse(status_code=409, …)`` construction under faultmaven/.

    Yields ``(relative_path, lineno, has_x_error_code_header)``. Parsed rather
    than executed: the point is to catch an emitter that no test exercises,
    which is exactly how the dead deduplication path survived.
    """
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "JSONResponse":
                continue

            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            status = kwargs.get("status_code")
            is_409 = (
                isinstance(status, ast.Constant) and status.value == 409
            ) or (
                # `status.HTTP_409_CONFLICT`
                isinstance(status, ast.Attribute) and "409" in status.attr
            )
            if not is_409:
                continue

            headers = kwargs.get("headers")
            labelled = bool(
                isinstance(headers, ast.Dict)
                and any(
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.lower() == "x-error-code"
                    for key in headers.keys
                )
            )
            yield str(path.relative_to(REPO_ROOT)), node.lineno, labelled


def test_the_scan_finds_the_409_emitters():
    """Guards the guard: an AST scan that matches nothing passes vacuously."""
    found = list(_iter_409_responses())
    assert found, "the 409 scan resolved nothing — it is no longer a guard"
    assert any(labelled for _, _, labelled in found), "no labelled 409 found"
    assert any(not labelled for _, _, labelled in found), "no unlabelled 409 found"


def test_every_non_terminal_409_carries_x_error_code():
    offenders = [
        f"{path}:{lineno}"
        for path, lineno, labelled in _iter_409_responses()
        if not labelled and path not in UNLABELLED_BY_DESIGN
    ]

    assert not offenders, (
        "These emit HTTP 409 without an `x-error-code` header. The Slack agent "
        "reads an unlabelled 409 on the turn POST as 'this case is terminal' "
        "and tells the user their investigation is closed — so an unlabelled "
        "non-terminal conflict becomes a false claim about a live case. Add a "
        "header (an unrecognized label degrades safely to the client's generic "
        "4xx handling), or add the file to UNLABELLED_BY_DESIGN with a reason: "
        f"{offenders}"
    )


def test_the_terminal_409_is_still_unlabelled():
    """The other half. If it ever gains a label, the agent's rule stops firing.

    Silently: the agent would fall through to generic 4xx advice and a user
    whose case really is closed would be told to try again, forever.
    """
    handlers = [
        (path, lineno, labelled)
        for path, lineno, labelled in _iter_409_responses()
        if path in UNLABELLED_BY_DESIGN
    ]

    assert handlers, "the terminal-case 409 emitter was not found"
    assert all(not labelled for _, _, labelled in handlers), (
        "the terminal-case 409 gained an `x-error-code`. The Slack agent "
        "identifies it by the header being ABSENT, so labelling it here without "
        "updating faultmaven-slack-agent stops it recognizing a closed case."
    )


def test_deduplication_no_longer_has_an_unlabelled_409_path():
    """The path that had already broken the premise, kept from coming back.

    `DuplicateRequestError` is constructed but never raised, so its handler was
    dead — but it emitted a 409 with no header, and dedup runs on the turn POST.
    """
    path = SOURCE_ROOT / "api/middleware/deduplication.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Checked on the AST, not as substrings: the removal is explained in a
    # comment in that file, and a substring scan would match the explanation.
    defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_create_duplicate_error_response" not in defs

    handled = {
        handler.type.id
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
    }
    assert "DuplicateRequestError" not in handled

    # The live path keeps both signals.
    source = path.read_text(encoding="utf-8")
    assert '"x-error-code": error_response.error_code' in source
    assert '"Retry-After": str(ttl_remaining)' in source
