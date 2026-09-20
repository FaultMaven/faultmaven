"""No durable record names an account the caller picked (fm#1461).

``api/middleware/logging.py`` read a session id off the ``X-Session-ID``
header — or the ``session_id`` query parameter, or a ``session_id`` body field
— resolved it through the session store, and stamped the owner into ``user_id``
on the access-log line whenever the request's principal named nobody. A request
carrying no credential at all was therefore recorded against whoever owned the
session id it was handed. Nothing was authorized by it; the thing corrupted was
the record an incident review reads, and the name it carried was the one the
caller chose.

The ruling: **the actor field is never derived from a caller-supplied value.**
The caller's value is still recorded, because it is genuinely useful for
correlation, but under a name no reader mistakes for identity
(``claimed_session_id``). The naming is the load-bearing part — a checked and an
unchecked value sharing one field is how this happened.

This module is the census. It does not test one call site; it says how many
places in the whole package can put a caller-supplied value into an actor field,
and fails when that number moves. The analysis lives in
``tests/caller_supplied_actor_ast.py``; what is asserted here is the answer.

**The number, measured on the merge base of the branch that fixed this
(``origin/main`` at d0c2b7f), over 476 modules: 13.** Eight in
``api/middleware/logging.py`` — the whole reported chain, both access-log lines
and the request context that stamps every OTHER record of the request — three
in ``modules/auth/api/auth.py``, which the issue did not mention, and two in
``modules/auth/api/oauth.py`` that the conservative analysis over-approximates
and this module allowlists by name. The auth.py three are the same defect in a
different shape: ``POST /users/{user_id}/revoke-tokens`` logged its caller-typed
path parameter under the key ``user_id``, which *displaces* the verified actor,
because ``ClientConfig.add_request_context`` fills that key only when the record
does not already carry one.

A guard that only ever reports zero is indistinguishable from a guard that has
stopped working, so three things are asserted here and not just one: that the
real tree is clean, that the analysis still *recognises* actor sinks in the real
tree (:func:`scan_actor_sinks`), and that it still catches each shape of the
defect when one is put in front of it.
"""

import pathlib

import pytest

from tests.caller_supplied_actor_ast import (
    ACTOR_FIELDS,
    Finding,
    scan_actor_sinks,
    scan_package,
    scan_source,
)

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "faultmaven"

#: Sites the analysis reports that are verified by construction. Keyed without
#: a line number on purpose: a line number rots on the next edit and an
#: allowlist that rots stops being read. Every entry is asserted to still match
#: something, so a fixed or deleted site fails here rather than lingering.
VERIFIED_BY_CONSTRUCTION = {
    (
        "faultmaven/modules/auth/api/oauth.py",
        "user (message label)",
        "token_dto.user_id",
    ): (
        "The taint is real and the value is not: the OAuth token endpoint parses "
        "its parameters out of the request body, so everything downstream of "
        "`_read_oauth_params` is flagged by the one-hop rule. `token_dto` is what "
        "`exchange_code_for_token` / `refresh_access_token` MINTED after verifying "
        "the code or the refresh token against the server's own store — an "
        "unverifiable code yields no DTO at all. Narrowing the analysis to spare "
        "it would also spare `_get_user_id_from_session(request, session_id)`, "
        "which is the shape this guard exists for."
    ),
}

#: Directories where the rule can be violated: everything that terminates a
#: request. A scan that did not read these has not looked, whatever it reports.
MUST_HAVE_BEEN_SCANNED = (
    "faultmaven/api/middleware",
    "faultmaven/api/routes",
    "faultmaven/modules/auth/api",
    "faultmaven/modules/case/api",
    "faultmaven/infrastructure/logging",
)


def _keys(findings):
    return {(f.path, f.field, f.expression) for f in findings}


@pytest.mark.unit
@pytest.mark.security
class TestThePackageIsClean:
    def test_no_actor_field_can_hold_a_caller_supplied_value(self):
        findings, _ = scan_package(PACKAGE_ROOT)
        unexpected = [f for f in findings if _keys([f]) - set(VERIFIED_BY_CONSTRUCTION)]
        assert not unexpected, (
            "A caller-supplied value can reach an actor field of a durable "
            "record. Either source the value from the verified principal, or "
            "record it under a name that says it is a claim "
            "(`claimed_…`/`target_…`):\n  " + "\n  ".join(str(f) for f in unexpected)
        )

    def test_every_allowlisted_site_still_exists(self):
        """An allowlist nobody re-reads is how a guard goes quiet."""
        findings, _ = scan_package(PACKAGE_ROOT)
        stale = set(VERIFIED_BY_CONSTRUCTION) - _keys(findings)
        assert not stale, (
            "These sites are allowlisted but no longer reported — the entry is "
            f"stale and must be deleted: {sorted(stale)}"
        )


@pytest.mark.unit
@pytest.mark.security
class TestTheScanLookedWhereTheRuleCanBeViolated:
    def test_it_read_every_module_in_the_package(self):
        _, scanned = scan_package(PACKAGE_ROOT)
        expected = tuple(sorted(PACKAGE_ROOT.rglob("*.py")))
        assert scanned == expected
        assert len(scanned) > 400, (
            "The package is suddenly much smaller than the 476 modules this was "
            f"measured against — did the scan lose a directory? Got {len(scanned)}"
        )

    @pytest.mark.parametrize("directory", MUST_HAVE_BEEN_SCANNED)
    def test_it_read_the_directories_that_terminate_requests(self, directory):
        _, scanned = scan_package(PACKAGE_ROOT)
        root = PACKAGE_ROOT.parent
        under = [p for p in scanned if str(p.relative_to(root)).startswith(directory)]
        assert under, f"nothing was scanned under {directory}"

    def test_it_still_recognises_an_actor_sink_in_the_real_tree(self):
        """The liveness half.

        ``scan_source`` reporting nothing is the answer we want and also the
        answer a broken detector gives. This asserts the detector still finds
        the actor sinks that ARE there — so a refactor that renames the logging
        helper, or drops ``extra=`` handling, fails here instead of turning the
        guard into a tautology.
        """
        access_log = PACKAGE_ROOT / "api/middleware/logging.py"
        sinks = scan_actor_sinks(
            access_log.read_text(encoding="utf-8"), "api/middleware/logging.py"
        )
        assert {f.field for f in sinks} >= {
            "user_id",
            "enterprise_id",
            "organization_id",
        }, f"the access log's own actor fields went unseen: {sinks}"


VERIFIED_SHAPE = """
from faultmaven.api.middleware.principal import read_request_principal

def handler(request):
    principal = read_request_principal(request)
    logger.info("done", user_id=principal.user_id)
"""

CHANNEL_SHAPES = {
    "header": """
def handler(request):
    who = request.headers.get("x-session-id")
    logger.info("done", user_id=who)
""",
    "query_parameter": """
def handler(request):
    logger.info("done", user_id=request.query_params.get("user_id"))
""",
    "cookie": """
def handler(request):
    logger.info("done", user_id=request.cookies.get("uid"))
""",
    "json_body": """
async def handler(request):
    body = await request.json()
    logger.info("done", user_id=body.get("user_id"))
""",
}


@pytest.mark.unit
@pytest.mark.security
class TestTheAnalysisCatchesEachShapeOfTheDefect:
    """A detector nobody has seen fail is a detector nobody has tested."""

    @pytest.mark.parametrize("name,source", sorted(CHANNEL_SHAPES.items()))
    def test_a_value_straight_off_the_request(self, name, source):
        assert scan_source(source, "x.py"), f"{name} went unnoticed"

    def test_the_original_defect_resolving_a_claimed_id_to_an_owner(self):
        """The reported shape: two methods, and the taint crosses between them."""
        source = """
class Middleware:
    async def _extract_session_id(self, request):
        return request.headers.get("x-session-id")

    async def _lookup(self, request, session_id):
        session = await request.app.state.session_service.get_session(session_id)
        return session.user_id

    async def dispatch(self, request, call_next):
        session_id = await self._extract_session_id(request)
        user_id = await self._lookup(request, session_id)
        self.coordinator.start_request(session_id=session_id, user_id=user_id)
"""
        findings = scan_source(source, "x.py")
        assert [f.field for f in findings] == ["user_id"], findings

    def test_a_route_handlers_own_path_parameter(self):
        """The shape no ``request.headers`` scan would ever have seen."""
        source = """
@router.post("/users/{user_id}/revoke-tokens")
async def revoke(user_id: str, request: Request, operator = Depends(require_admin)):
    logger.info("revoked", extra={"user_id": user_id})
"""
        findings = scan_source(source, "x.py")
        assert [f.field for f in findings] == ["user_id"], findings

    def test_a_prose_label_in_the_message(self):
        """``[user: …]`` is as much a claim about the value as a structured key."""
        source = """
def handler(request):
    claimed = request.headers.get("x-session-id")
    logger.info(f"Request completed [user: {claimed}] -> 200")
"""
        findings = scan_source(source, "x.py")
        assert findings and findings[0].field == "user (message label)", findings

    def test_the_request_context_that_decorates_every_other_record(self):
        source = """
def handler(request, coordinator):
    coordinator.start_request(user_id=request.headers.get("x-user"))
"""
        assert scan_source(source, "x.py")


@pytest.mark.unit
@pytest.mark.security
class TestTheAnalysisLeavesCorrectCodeAlone:
    """Over-flagging is how a guard gets switched off, so pin the other side."""

    def test_the_verified_principal_is_not_a_finding(self):
        assert scan_source(VERIFIED_SHAPE, "x.py") == []

    def test_a_claimed_value_under_a_claimed_name_is_not_a_finding(self):
        source = """
def handler(request):
    claimed = request.headers.get("x-session-id")
    logger.info(f"started [claimed session: {claimed}]", claimed_session_id=claimed)
"""
        assert scan_source(source, "x.py") == []

    def test_a_caller_supplied_TARGET_is_not_an_actor(self):
        source = """
@router.post("/users/{user_id}/revoke-tokens")
async def revoke(user_id: str, operator = Depends(require_admin)):
    logger.info("revoked", extra={"operator_user_id": operator.user_id,
                                  "target_user_id": user_id})
"""
        assert scan_source(source, "x.py") == []

    def test_a_non_logging_sink_is_out_of_scope(self):
        """The rule is about the RECORD. Passing a claimed id to a lookup is
        the ordinary way to look something up."""
        source = """
def handler(request, store):
    store.get_session(request.headers.get("x-session-id"))
"""
        assert scan_source(source, "x.py") == []


@pytest.mark.unit
class TestTheVocabularyIsWhatItSaysItIs:
    def test_it_names_actors_and_not_resources(self):
        """``session_id``/``case_id`` are caller-supplied on this path too, and
        are deliberately NOT actor fields: they say what the request was about,
        not who made it. Keeping them out is what makes the guard's zero
        meaningful rather than a suppressed pile of correct lines."""
        assert "user_id" in ACTOR_FIELDS
        assert "session_id" not in ACTOR_FIELDS
        assert "case_id" not in ACTOR_FIELDS
        assert "correlation_id" not in ACTOR_FIELDS

    def test_a_finding_prints_where_to_look(self):
        rendered = str(Finding("a/b.py", 12, "user_id", "who"))
        assert "a/b.py:12" in rendered and "user_id" in rendered
