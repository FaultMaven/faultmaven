"""A ``sub`` that names nobody must not become a principal.

THE CLASS, stated before the fix. The house shape for scoping a query is
``if user_id:``, so a falsy caller id does not NARROW a query — it drops the
predicate and leaves the query unscoped:

    case_scope_where(params, "")        -> None          (no owner clause)
    AuthSessionService.list_sessions("") -> every session

Both measured. ``case_scope_where``'s ``None`` is the cross-tenant
platform-admin path's own return value, reached by an ordinary caller. So an
empty ``user_id`` is not a narrower identity; it is the absence of one, wearing
the type of one.

HOW MANY PLACES COULD PRODUCE IT, and the scan that found the number. Two
questions, two scans, both shipped below as tests rather than left as a claim
in a commit message:

1. **Principal constructors** — ``grep -rn 'claims\\["sub"\\]|claims.get("sub"'``
   over ``faultmaven/``, narrowed to sites whose value becomes a principal's
   ``user_id``. **N = 2**, and ONE was guarded when #1447's review began:
   ``DevUser`` in ``api/v1/auth_dependencies`` (guarded) and
   ``AuthenticatedUser.from_jwt_claims`` (``claims.get("sub", "")`` — defaulting
   a missing subject to the empty string, which is the value the scope checks
   read as "no owner"). That second one serves
   ``api/middleware/auth.get_current_user``, and therefore the admin routers and
   ``api/routes/sessions.py``. Both refuse now, and
   :func:`test_every_principal_constructor_refuses_a_subjectless_claim_set`
   is the scan.

2. **Decode sites** — the property that makes fixing the constructors
   sufficient is that there is ONE place a request principal's claims come
   from, ``AuthService.verify_token``. That is only true while nothing else
   decodes a token and builds a principal from it, so the inventory of
   ``jwt.decode`` calls is pinned: **N = 10**, classified in
   :data:`DECODE_SITES`. A new decode path fails
   :func:`test_the_decode_inventory_is_classified` until someone says which
   kind it is — which is the point, because a second principal source is
   exactly how "the single point" stops being single.

WHY THE ROOT IS THE VERIFIER AND NOT THE CONSTRUCTORS. Guarding N constructors
fixes N constructors; guarding the verifier fixes the class, including the
constructor nobody has written yet. ``AuthService.verify_token`` is where PyJWT's
``require`` list already asserts the claim is PRESENT, so presence and content
are now decided on adjacent lines rather than one being inferred from the other.
The constructors keep their own guards as a second layer, each with its own
test, because a guard that exists only upstream is one refactor from absent.
"""

import ast
import pathlib
import time

import jwt
import pytest

from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Subjects that name nobody. ``None`` and absence are refused by PyJWT's
#: ``require`` list; the strings are not, which is the gap this module is about.
SUBJECTLESS = ["", "   ", "\t", None]

#: ``jwt.decode`` call sites in ``faultmaven/``, and what each one is for.
#:
#: ``PRINCIPAL`` — its claims become a request principal. There must be exactly
#: one, because "the subject is checked once, where the token is verified" is
#: only true while that is so.
#: ``TOKEN_LIFECYCLE`` — the generators' own validate/verify/revocation paths.
#: They do not build principals; the three that read ``sub`` from their payloads
#: fail closed on their own (``oauth_service`` raises ``InvalidGrantError``, and
#: the refresh route resolves through ``user_store.get_user``, which finds
#: nobody for a blank id).
#: ``METADATA`` — reads a claim other than the subject, for display.
DECODE_SITES: dict[tuple[str, str], str] = {
    (
        "faultmaven/modules/auth/domain/services/auth_service.py",
        "AuthService::verify_token",
    ): "PRINCIPAL",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "RS256JWTTokenGenerator::verify_password_reset_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "RS256JWTTokenGenerator::validate_access_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "RS256JWTTokenGenerator::validate_refresh_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "RS256JWTTokenGenerator::_decode_for_revocation",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "HS256JWTTokenGenerator::verify_password_reset_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "HS256JWTTokenGenerator::validate_access_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "HS256JWTTokenGenerator::validate_refresh_token",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/jwt_token_generator.py",
        "HS256JWTTokenGenerator::_decode_for_revocation",
    ): "TOKEN_LIFECYCLE",
    (
        "faultmaven/modules/auth/domain/services/service_account_provisioning.py",
        "_expiry_of",
    ): "METADATA",
}


def _decode_sites() -> dict[tuple[str, str], int]:
    """Every ``jwt.decode(...)`` in ``faultmaven/``, by file and enclosing scope.

    An AST walk rather than a grep: ``jwt.decode`` inside a docstring or a
    comment is not a decode, and the enclosing function is what makes an entry
    readable — a line number would churn on every edit above it.
    """
    found: dict[tuple[str, str], int] = {}
    for path in sorted((PROJECT_ROOT / "faultmaven").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        scope: list[str] = []
        relative = str(path.relative_to(PROJECT_ROOT))

        class Walker(ast.NodeVisitor):
            def _scoped(self, node):
                scope.append(node.name)
                self.generic_visit(node)
                scope.pop()

            visit_FunctionDef = _scoped
            visit_AsyncFunctionDef = _scoped
            visit_ClassDef = _scoped

            def visit_Call(self, node):
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "decode"
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "jwt"
                ):
                    key = (relative, "::".join(scope) or "<module>")
                    found[key] = found.get(key, 0) + 1
                self.generic_visit(node)

        Walker().visit(tree)
    return found


def _access_claims(subject, secret="unit-test-secret-not-a-real-key-000000"):
    now = int(time.time())
    claims = {
        "iss": "faultmaven",
        "aud": "faultmaven-api",
        "exp": now + 600,
        "iat": now,
        "jti": "unit-test-jti",
        "type": "access",
    }
    if subject is not None:
        claims["sub"] = subject
    return claims, secret


@pytest.mark.unit
@pytest.mark.security
def test_the_decode_inventory_is_classified():
    """A new token-decode path must be classified before it can ship.

    This is what protects "the subject is checked once": the check lives in the
    one ``PRINCIPAL`` decode, and a second one would route around it silently.
    Fails in both directions — an unclassified site is unreviewed, and a
    classified site that no longer exists is an entry describing nothing.
    """
    live = _decode_sites()

    unclassified = sorted(set(live) - set(DECODE_SITES))
    assert not unclassified, (
        "these jwt.decode sites are not in DECODE_SITES. If one builds a "
        "request principal, the subject check in AuthService.verify_token does "
        "not cover it:\n"
        + "\n".join(f"  {path}  {where}" for path, where in unclassified)
    )

    stale = sorted(set(DECODE_SITES) - set(live))
    assert (
        not stale
    ), "these DECODE_SITES entries no longer name a decode site:\n" + "\n".join(
        f"  {path}  {where}" for path, where in stale
    )

    principals = [key for key, kind in DECODE_SITES.items() if kind == "PRINCIPAL"]
    assert len(principals) == 1, (
        "a request principal's claims must come from exactly one decode, or "
        f"'checked once, where the token is verified' is not true: {principals}"
    )


@pytest.mark.unit
@pytest.mark.security
def test_the_root_refuses_a_token_whose_subject_names_nobody():
    """``AuthService.verify_token`` — the single point, and the actual fix.

    Driven with real signed tokens. The positive control comes first: a token
    that names somebody must still verify, or every refusal below is just a
    broken verifier.
    """
    from types import SimpleNamespace

    # The service module defines its OWN ``AuthenticationError`` (line 45) —
    # distinct from ``faultmaven.exceptions.AuthenticationError`` — and that is
    # the class this path raises. Imported from where it is raised, so the test
    # cannot pass by catching a different exception of the same name.
    from faultmaven.modules.auth.domain.services.auth_service import (
        AuthenticationError,
        AuthService,
    )

    claims, secret = _access_claims("a-real-subject")

    # ``_algorithm``, ``_issuer`` and ``_audience`` are PROPERTIES over
    # ``self._settings.security``, so the double goes in at the settings seam
    # rather than over the properties — and it is a double rather than real
    # settings because settings read the process environment, which would make
    # this test a function of the machine.
    service = AuthService.__new__(AuthService)
    service._private_key = None
    service._public_key = None
    service._settings = SimpleNamespace(
        auth=SimpleNamespace(auth_mode="local"),
        security=SimpleNamespace(
            jwt_algorithm="HS256",
            jwt_issuer="faultmaven",
            jwt_audience="faultmaven-api",
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: secret),
        ),
    )

    verified = service.verify_token(
        jwt.encode(claims, secret, algorithm="HS256"), token_type="access"
    )
    assert verified["sub"] == "a-real-subject"

    for subject in SUBJECTLESS:
        blank, _ = _access_claims(subject)
        with pytest.raises(AuthenticationError) as refusal:
            service.verify_token(
                jwt.encode(blank, secret, algorithm="HS256"), token_type="access"
            )
        # An absent or null ``sub`` is refused by PyJWT's ``require`` list; a
        # blank string reaches the new check. Both are refusals, and asserting
        # WHICH one fired keeps this honest about where the gap actually was.
        expected = "INVALID_TOKEN" if subject is None else "INVALID_TOKEN_SUBJECT"
        assert refusal.value.error_code == expected, (
            f"sub={subject!r} was refused by the wrong arm: "
            f"{refusal.value.error_code}"
        )


@pytest.mark.unit
@pytest.mark.security
def test_every_principal_constructor_refuses_a_subjectless_claim_set():
    """The scan for question 1, as a test: N = 2, and both refuse.

    ``DevUser``'s constructor is exercised through
    ``get_current_user_optional`` — the only thing that builds one from claims —
    and ``AuthenticatedUser`` through its own classmethod. A third constructor
    added later is not caught by THIS test; it is caught by the decode
    inventory above, because it would need claims from somewhere.
    """
    import asyncio
    from types import SimpleNamespace

    from faultmaven.api.v1.auth_dependencies import get_current_user_optional

    class _Service:
        def __init__(self, claims):
            self._claims = claims

        async def verify_token_with_revocation_check(self, token, token_type):
            return self._claims

    def dev_user(subject):
        claims, _ = _access_claims(subject)
        return asyncio.run(
            get_current_user_optional(
                request=SimpleNamespace(), token="a.b.c", auth_service=_Service(claims)
            )
        )

    # Positive controls: both constructors still build a principal for a real
    # subject, so the refusals below are about the subject.
    assert dev_user("a-real-subject").user_id == "a-real-subject"
    assert (
        AuthenticatedUser.from_jwt_claims({"sub": "a-real-subject"}).user_id
        == "a-real-subject"
    )

    for subject in SUBJECTLESS:
        assert dev_user(subject) is None, f"DevUser built from sub={subject!r}"

        # An ``AuthenticationError``, not a ``ValueError`` — but the auth
        # MODULE's, because the constructor cannot import the service's without
        # making ``auth_service -> models.auth -> auth_service``. THREE classes
        # share this name (this one, ``auth_service``'s, and
        # ``faultmaven.exceptions``'), so each assertion below names the one it
        # means: catching the wrong one would pass while the contract stayed
        # broken.
        from faultmaven.modules.auth.exceptions import (
            AuthenticationError as AuthModuleError,
        )

        claims = {} if subject is None else {"sub": subject}
        with pytest.raises(AuthModuleError) as refusal:
            AuthenticatedUser.from_jwt_claims(claims)
        assert refusal.value.error_code == "INVALID_TOKEN_SUBJECT"


@pytest.mark.unit
@pytest.mark.security
def test_a_blank_subject_would_have_unscoped_the_query_it_reached():
    """Why the guards above matter, asserted rather than asserted-about.

    If this ever stops holding — if ``case_scope_where`` grows a fail-closed
    arm of its own — the guards are belt and braces rather than the only thing
    standing between a caller and an unscoped read, and this test should say so
    by failing.
    """
    from faultmaven.modules.case.infrastructure.case_scope import case_scope_where

    params: dict = {}
    assert case_scope_where(params, "a-real-subject") == "user_id = :user_id"
    assert params == {"user_id": "a-real-subject"}

    blank: dict = {}
    assert case_scope_where(blank, "") is None, (
        "case_scope_where no longer drops the owner clause on a blank id — "
        "good, but the comment in AuthService.verify_token cites this"
    )
    assert blank == {}


@pytest.mark.unit
@pytest.mark.security
def test_the_service_publishes_its_own_exception_vocabulary():
    """The documented contract, asserted where it is published.

    ``AuthService.extract_user_from_token`` and its revocation-checking sibling
    both document ``Raises: AuthenticationError`` — meaning the class defined in
    ``auth_service``, which is what ``api/v1/auth_dependencies`` and
    ``api/middleware/auth`` import and catch. The constructor underneath raises
    the auth MODULE's class instead, because it cannot import the service's
    without a cycle, so the service translates. This asserts the translation
    rather than the arrangement that makes it necessary.

    Driven through ``_principal_from`` with claims that bypass ``verify_token``,
    since the verifier now refuses such a token first — which is exactly why
    this arm would otherwise never be exercised.
    """
    from faultmaven.modules.auth.domain.services.auth_service import (
        AuthenticationError as ServiceAuthError,
    )
    from faultmaven.modules.auth.domain.services.auth_service import AuthService

    service = AuthService.__new__(AuthService)

    # Positive control: a real subject still produces a principal.
    assert service._principal_from({"sub": "a-real-subject"}).user_id == (
        "a-real-subject"
    )

    for subject in SUBJECTLESS:
        claims = {} if subject is None else {"sub": subject}
        with pytest.raises(ServiceAuthError) as refusal:
            service._principal_from(claims)
        assert refusal.value.error_code == "INVALID_TOKEN_SUBJECT", (
            "the translated refusal must carry the verifier's own code, or a "
            f"handler can tell the two apart: {refusal.value.error_code}"
        )
