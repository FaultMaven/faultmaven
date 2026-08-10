"""Request hashing for duplicate-submit detection.

The property under test is *exactness*: two requests share a digest when they
are the same request and not otherwise. The regression these tests exist for is
the opposite -- an earlier hasher normalized content before digesting, so
distinct user messages collided and the second one was refused with a 409.
"""

import hashlib

import pytest

from faultmaven.infrastructure.protection.request_hasher import RequestHasher


@pytest.fixture
def hasher():
    return RequestHasher()


def _h(hasher, **overrides):
    """Hash a request, overriding fields of a fixed baseline."""
    call = {
        "session_id": "sess-1",
        "endpoint": "/api/v1/agent/query",
        "method": "POST",
        "body": '{"query":"why is the db slow"}',
        "query_params": {},
    }
    call.update(overrides)
    return hasher.hash_request(**call)


@pytest.mark.unit
class TestDistinctRequestsStayDistinct:
    """Each component must be able to change the digest on its own.

    Written as a parametrized sweep rather than one case per field so that a
    component silently dropped from the digest fails here instead of shipping.
    """

    @pytest.mark.parametrize(
        "field,other",
        [
            ("session_id", "sess-2"),
            ("endpoint", "/api/v1/agent/other"),
            ("method", "PUT"),
            ("body", '{"query":"why is the cache slow"}'),
            ("query_params", {"verbose": "true"}),
        ],
    )
    def test_changing_any_component_changes_the_digest(self, hasher, field, other):
        assert _h(hasher) != _h(hasher, **{field: other})

    @pytest.mark.parametrize(
        "one,two",
        [
            # The exact pairs the old normalizer collapsed. Each is a plausible
            # pair of consecutive troubleshooting turns.
            ('{"q":"check order 4232342342"}', '{"q":"check order 9994442211"}'),
            (
                '{"q":"error at 2026-08-10T01:02:03"}',
                '{"q":"error at 2026-08-10T22:33:44"}',
            ),
            ('{"q":"see /tmp/crash-alpha.log"}', '{"q":"see /tmp/crash-beta.log"}'),
            (
                '{"q":"trace 550e8400e29b41d4a716446655440000"}',
                '{"q":"trace 6ba7b8109dad11d180b400c04fd430c8"}',
            ),
            # Fields the old hasher dropped wholesale by name.
            ('{"version":"v1"}', '{"version":"v2"}'),
            ('{"uuid":"a"}', '{"uuid":"b"}'),
            ('{"t":"1"}', '{"t":"2"}'),
        ],
    )
    def test_content_that_normalization_used_to_collapse(self, hasher, one, two):
        assert _h(hasher, body=one) != _h(hasher, body=two)

    def test_no_component_boundary_can_be_forged(self):
        """Length prefixing, not delimiters, separates components.

        With a delimiter-joined digest these two hash alike; the assertion
        fails if someone reintroduces ``"|".join(...)``.
        """
        h = RequestHasher()
        assert h.hash_request(
            session_id="a", endpoint="b|c", body=None
        ) != h.hash_request(session_id="a|b", endpoint="c", body=None)


@pytest.mark.unit
class TestIdenticalRequestsCollide:
    """The other half of the property: real double-submits must be caught."""

    def test_identical_requests_agree(self, hasher):
        assert _h(hasher) == _h(hasher)

    def test_query_parameter_order_does_not_matter(self, hasher):
        assert _h(hasher, query_params={"a": "1", "b": "2"}) == _h(
            hasher, query_params={"b": "2", "a": "1"}
        )

    def test_method_case_does_not_matter(self, hasher):
        assert _h(hasher, method="post") == _h(hasher, method="POST")

    def test_absent_and_empty_body_agree(self, hasher):
        assert _h(hasher, body=None) == _h(hasher, body="")


@pytest.mark.unit
class TestDigestShape:
    def test_is_hex_sha256(self, hasher):
        digest = _h(hasher)
        assert len(digest) == 64
        int(digest, 16)  # raises if not hex

    def test_is_a_plain_sha256_not_a_kdf(self, hasher):
        """Pins the cost, not just the shape.

        A password KDF here cost ~72-85 ms per request on the event loop. The
        digest is a Redis key, never a secret and never returned to a client,
        so the plain hash is the correct primitive. Recomputing it
        independently proves no iterated derivation is in the path -- a timing
        assertion would be flaky, this cannot be.
        """
        expected = hashlib.sha256()
        for component in ("sess-1", "POST", "/api/v1/agent/query", "", "x"):
            encoded = component.encode("utf-8")
            expected.update(str(len(encoded)).encode("ascii"))
            expected.update(b":")
            expected.update(encoded)
        assert _h(hasher, body="x") == expected.hexdigest()

    def test_unicode_bodies_hash_without_error(self, hasher):
        assert _h(hasher, body='{"q":"disque plein — 磁盘已满"}') != _h(
            hasher, body='{"q":"x"}'
        )
