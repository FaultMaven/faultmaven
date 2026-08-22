"""Tests for scripts/check_contract_version.py.

The script is the gate that makes publishing a contract change deliberate:
`api-contract-drift` already keeps openapi.json honest about the code, but it
is *satisfied by regenerating*, which a route change does automatically. This
one refuses a structural surface change whose `info.version` did not move.

The tests below pin the three outcomes that matter — caught, published,
ignored-as-prose — plus the one that must never be a silent pass: a base
contract the check could not read at all.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_contract_version.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("check_contract_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec(version="1.0.0", *, paths=None, schemas=None):
    return {
        "openapi": "3.1.0",
        "info": {"title": "FaultMaven API", "version": version},
        "paths": paths if paths is not None else {},
        "components": {"schemas": schemas if schemas is not None else {}},
    }


_TOKEN_OP = {
    "/token": {
        "post": {
            "summary": "Exchange a grant",
            "requestBody": {"content": {"application/json": {}}},
            "responses": {"200": {}, "422": {}},
        }
    }
}


@pytest.fixture
def check(mod, tmp_path, monkeypatch):
    """Run the check over two in-memory contracts."""

    def _run(base, head):
        spec_path = tmp_path / "openapi.json"
        spec_path.write_text(json.dumps(head))
        monkeypatch.setattr(mod, "SPEC_PATH", spec_path)
        monkeypatch.setattr(mod, "read_base_spec", lambda ref: base)
        return mod.run_check("origin/main")

    return _run


class TestTheGateBites:
    """A surface change with a standing version is the case this exists for."""

    def test_a_response_code_change_without_a_bump_fails(self, check):
        head_paths = json.loads(json.dumps(_TOKEN_OP))
        head_paths["/token"]["post"]["responses"] = {"200": {}, "400": {}}

        code, report = check(_spec(paths=_TOKEN_OP), _spec(paths=head_paths))

        assert code == 1
        text = "\n".join(report)
        assert "info.version stayed at 1.0.0" in text
        # The reader has to choose MINOR or MAJOR and tell the client owners
        # what they are accepting, so the delta must be in the refusal.
        assert "responses ['200', '422'] -> ['200', '400']" in text

    def test_a_removed_schema_without_a_bump_fails(self, check):
        code, report = check(
            _spec(schemas={"TokenRequest": {"type": "object"}}),
            _spec(schemas={}),
        )

        assert code == 1
        assert "- schema    TokenRequest" in "\n".join(report)

    def test_a_new_endpoint_without_a_bump_fails(self, check):
        code, report = check(_spec(), _spec(paths=_TOKEN_OP))

        assert code == 1
        assert "+ path      /token" in "\n".join(report)


class TestPublishing:
    """A moved version is the act of publishing; the check reports and passes."""

    def test_a_surface_change_with_a_bump_passes(self, check):
        code, report = check(_spec(paths=_TOKEN_OP), _spec("1.1.0", paths={}))

        assert code == 0
        text = "\n".join(report)
        assert "1.0.0 -> 1.1.0" in text
        assert "- path      /token" in text
        # Publishing is not adoption: the clients pin a ref and move it
        # themselves, so a merge must not be described as reaching them.
        assert "does not reach them on merge" in text

    def test_an_unchanged_contract_passes(self, check):
        code, report = check(_spec(paths=_TOKEN_OP), _spec(paths=_TOKEN_OP))

        assert code == 0
        assert "unchanged" in "\n".join(report)


class TestProseIsNotSurface:
    """No client is written against a description."""

    def test_a_reworded_description_needs_no_bump(self, check):
        head_paths = json.loads(json.dumps(_TOKEN_OP))
        head_paths["/token"]["post"]["summary"] = "Completely reworded"
        head_paths["/token"]["post"]["description"] = "New prose entirely"

        code, report = check(_spec(paths=_TOKEN_OP), _spec(paths=head_paths))

        assert code == 0
        assert "unchanged" in "\n".join(report)

    def test_prose_is_stripped_at_every_depth(self, mod):
        stripped = mod._strip_prose(
            {
                "type": "object",
                "description": "top",
                "properties": {
                    "field": {"type": "string", "description": "nested"},
                    "list": [{"title": "deep", "type": "integer"}],
                },
            }
        )

        assert stripped == {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "list": [{"type": "integer"}],
            },
        }

    def test_dropping_a_field_named_title_is_still_caught(self, check):
        """The regression this class exists for.

        Filtering prose by key name alone reached inside `properties`, where
        the keys are author-chosen field names. Cases, runbooks and knowledge
        documents all carry `title` and `description` fields, so removing one
        from a response passed the gate while breaking every client reading it.
        """
        base = _spec(
            schemas={
                "CaseSummary": {
                    "properties": {
                        "case_id": {"type": "string"},
                        "title": {"type": "string"},
                    }
                }
            }
        )
        head = _spec(
            schemas={"CaseSummary": {"properties": {"case_id": {"type": "string"}}}}
        )

        code, report = check(base, head)

        assert code == 1
        assert "~ schema    CaseSummary" in "\n".join(report)

    def test_prose_inside_a_name_colliding_property_is_still_stripped(self, check):
        """The mirror of the regression above, and the harder direction.

        Deciding "is this a name map?" from the child's *name* gets it wrong
        both ways. A property called `title` looked like prose (dropping it was
        invisible); a property called `content` looks like a name map, so its
        description survived stripping and rewording it demanded a version bump
        for prose — the false positive this class exists to prevent. Six real
        schemas have a `content` property: CaseReport, ReportResponse,
        ReportUpdateRequest, DraftUpdateRequest, Message, KnowledgeBaseDocument.
        """
        base = _spec(
            schemas={
                "CaseReport": {
                    "properties": {"content": {"type": "string", "description": "old"}}
                }
            }
        )
        head = _spec(
            schemas={
                "CaseReport": {
                    "properties": {
                        "content": {"type": "string", "description": "REWORDED"}
                    }
                }
            }
        )

        code, report = check(base, head)

        assert code == 0, "\n".join(report)

    def test_dropping_a_name_colliding_property_is_still_caught(self, check):
        code, _ = check(
            _spec(schemas={"Message": {"properties": {"content": {"type": "string"}}}}),
            _spec(schemas={"Message": {"properties": {}}}),
        )

        assert code == 1

    def test_a_field_named_like_prose_is_not_confused_with_it(self, mod):
        """`_PROSE_KEYS` names schema keywords, not a property called 'title'."""
        surface = mod._surface(
            _spec(schemas={"Doc": {"properties": {"title": {"type": "string"}}}})
        )

        assert surface["components"]["schemas"]["Doc"]["properties"] == {
            "title": {"type": "string"}
        }


class TestTheRefusalAlwaysNamesWhatMoved:
    """A refusal with no delta is the failure it exists to prevent."""

    def test_a_change_outside_schemas_still_reports_a_delta(self, check):
        """`_surface` compares all of `components`, so the describer must too.

        A change confined to `securitySchemes` (or `responses`, `parameters`,
        `headers`) otherwise failed the gate with an empty list under
        "the surface changed" — leaving the reader to diff a generated
        document by hand, which is what the report exists to spare them.
        """
        base = _spec()
        head = _spec()
        head["components"]["securitySchemes"] = {"OAuth2": {"type": "oauth2"}}

        code, report = check(base, head)

        assert code == 1
        assert any("OAuth2" in line for line in report), report


class TestTheVersionMustMoveForward:
    def test_a_backwards_version_is_refused(self, check):
        """Publishing moves forward. A client comparing what it pinned against
        what is published would read a lowered version as already adopted."""
        code, report = check(_spec("1.2.0", paths=_TOKEN_OP), _spec("1.1.0"))

        assert code == 1
        assert "BACKWARDS" in "\n".join(report)

    def test_a_version_that_cannot_be_ordered_is_refused(self, check):
        """`1.O.0` — letter O — is a typo, not a publication."""
        code, report = check(_spec("1.0.0", paths=_TOKEN_OP), _spec("1.O.0"))

        assert code == 1
        assert "MAJOR.MINOR.PATCH" in "\n".join(report)

    def test_a_forward_version_publishes(self, check):
        code, _ = check(_spec("1.0.0", paths=_TOKEN_OP), _spec("1.1.0"))

        assert code == 0


class TestItNeverPassesBlind:
    def test_an_unreadable_base_is_an_error_not_a_pass(
        self, mod, tmp_path, monkeypatch
    ):
        """A gate that skips when it cannot see is worse than no gate.

        In CI this is a shallow checkout that never fetched the base branch —
        which would otherwise "pass" having compared nothing.
        """
        spec_path = tmp_path / "openapi.json"
        spec_path.write_text(json.dumps(_spec()))
        monkeypatch.setattr(mod, "SPEC_PATH", spec_path)

        def _raise(ref):
            raise RuntimeError(f"could not read the contract at '{ref}'")

        monkeypatch.setattr(mod, "read_base_spec", _raise)

        code, report = mod.run_check("origin/main")

        assert code == 2
        assert "could not read" in "\n".join(report)

    def test_a_corrupt_committed_contract_is_an_error_not_a_bump_demand(
        self, mod, tmp_path, monkeypatch
    ):
        """Exit 2, not a traceback exiting 1.

        1 is the code that means "a surface change needs a version bump", so an
        unreadable document must not borrow it.
        """
        spec_path = tmp_path / "openapi.json"
        spec_path.write_text("{not json")
        monkeypatch.setattr(mod, "SPEC_PATH", spec_path)
        monkeypatch.setattr(mod, "read_base_spec", lambda ref: _spec())

        code, report = mod.run_check("origin/main")

        assert code == 2
        assert "could not be read" in "\n".join(report)

    def test_a_missing_committed_contract_is_an_error(self, mod, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "SPEC_PATH", tmp_path / "absent.json")
        monkeypatch.setattr(mod, "read_base_spec", lambda ref: _spec())

        code, report = mod.run_check("origin/main")

        assert code == 2
        assert "missing" in "\n".join(report)


class TestAgainstTheRealRepository:
    def test_the_committed_contract_is_consistent_with_its_base(self, mod):
        """The live invariant, over the real files — what CI runs.

        Skipped rather than failed when the base branch is absent locally: the
        authoritative run is the CI step, which fetches it first.
        """
        try:
            mod.read_base_spec("origin/main")
        except RuntimeError as exc:
            pytest.skip(f"base contract unavailable locally: {exc}")

        code, report = mod.run_check("origin/main")

        assert code == 0, "\n".join(report)

    def test_the_script_runs_as_a_command(self):
        """CI invokes it as a script, so an import-time break must show here."""
        result = subprocess.run(
            ["python", str(SCRIPT), "--base", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        # Case-insensitive and phrase-free on purpose: this test exists to
        # catch an import-time break, and pinning one of the four verdict
        # phrasings made it fail the first time a real publication took the
        # "re-publication" branch instead of the "unchanged" one.
        assert "contract" in result.stdout.lower(), result.stdout
