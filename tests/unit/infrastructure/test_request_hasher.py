"""Request hashing for duplicate-submit detection.

The property under test is *exactness*: two requests share a digest when they
are the same request and not otherwise. The regression these tests exist for is
the opposite -- an earlier hasher normalized content before digesting, so
distinct user messages collided and the second was classified as a duplicate.
"""

import hashlib
import json

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
        "body": b'{"query":"why is the db slow"}',
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
            ("body", b'{"query":"why is the cache slow"}'),
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
            (b'{"q":"check order 4232342342"}', b'{"q":"check order 9994442211"}'),
            (
                b'{"q":"error at 2026-08-10T01:02:03"}',
                b'{"q":"error at 2026-08-10T22:33:44"}',
            ),
            (b'{"q":"see /tmp/crash-alpha.log"}', b'{"q":"see /tmp/crash-beta.log"}'),
            (
                b'{"q":"trace 550e8400e29b41d4a716446655440000"}',
                b'{"q":"trace 6ba7b8109dad11d180b400c04fd430c8"}',
            ),
            # Fields the old hasher dropped wholesale by name.
            (b'{"version":"v1"}', b'{"version":"v2"}'),
            (b'{"uuid":"a"}', b'{"uuid":"b"}'),
            (b'{"t":"1"}', b'{"t":"2"}'),
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
        """An absent body and an empty one are the same request.

        Distinct from the non-UTF-8 case below: unreadable bodies used to fold
        onto this same digest too, which is what made the conflation unsafe.
        """
        assert _h(hasher, body=None) == _h(hasher, body=b"")


@pytest.mark.unit
class TestDigestShape:
    def test_is_hex_sha256(self, hasher):
        digest = _h(hasher)
        assert len(digest) == 64
        int(digest, 16)  # raises if not hex

    def test_digest_is_plain_sha256_over_verbatim_components(self, hasher):
        """One golden vector standing in for the whole removed normalizer.

        Enumerating the pairs the old code collapsed only covers the instances
        someone thought to list -- the old ``excluded_fields`` set had 27 names
        and ``_normalize_endpoint`` also lowercased and stripped trailing
        slashes, so a *partial* reintroduction slips past a list of examples.
        This input instead carries every removed surface at once: mixed-case
        endpoint with a trailing slash, and a body holding excluded field names
        (``timestamp``, ``request_id``, ``correlation_id``, ``cache_buster``,
        ``_``, ``v``), a 13-digit epoch, a 10-digit epoch, a UUID, 32-char hex,
        ``req_``/``trace_`` prefixes, a ``/tmp`` and a ``/var/log`` path, and
        collapsible whitespace.

        Because the expected digest is recomputed from the raw inputs, *any*
        transformation reinstated anywhere in the path changes the result. It
        also pins the primitive: a KDF in the path fails here, so the ~72-85 ms
        per-request event-loop stall cannot come back silently. A timing
        assertion would be flaky; this cannot be.
        """
        endpoint = "/API/v1/Agent/Query/"
        body = (
            b'{"timestamp":"2026-08-10T01:02:03","request_id":"req_abc123",'
            b'"correlation_id":"trace_xyz","cache_buster":1754800000,'
            b'"_":"1","v":"2","uuid":"550e8400-e29b-41d4-a716-446655440000",'
            b'"digest":"5d41402abc4b2a76b9719d911017c592",'
            b'"epoch_ms":1754800000123,"epoch_s":1754800000,'
            b'"log":"/var/log/syslog","tmp":"/tmp/crash.log",'
            b'"q":"disk   full"}'
        )
        query = {"b": "2", "a": "1"}

        expected = hashlib.sha256()
        for component in (
            b"sess-1",
            b"POST",
            endpoint.encode(),
            json.dumps(sorted(query.items()), separators=(",", ":")).encode(),
            body,
        ):
            expected.update(str(len(component)).encode("ascii"))
            expected.update(b":")
            expected.update(component)

        assert (
            _h(hasher, endpoint=endpoint, body=body, query_params=query)
            == expected.hexdigest()
        )

    def test_headers_are_not_part_of_the_digest(self, hasher):
        """Header exclusion is a deliberate contract, not an oversight.

        The old digest mixed in content-type/accept/accept-language/
        accept-encoding, so an identical body retried under a corrected
        content-type counted as a different request. Pinned via the signature:
        reinstating the parameter fails here rather than silently narrowing
        what counts as a duplicate.
        """
        with pytest.raises(TypeError):
            hasher.hash_request(
                session_id="sess-1",
                endpoint="/api/v1/agent/query",
                body=b"{}",
                headers={"content-type": "application/json"},
            )

    def test_unicode_bodies_hash_without_error(self, hasher):
        assert _h(hasher, body='{"q":"disque plein — 磁盘已满"}'.encode()) != _h(
            hasher, body=b'{"q":"x"}'
        )

    @pytest.mark.parametrize(
        "one,two",
        [
            (b"caf\xe9", b"na\xefve"),  # two different latin-1 bodies
            (b"caf\xe9", b""),  # ... and against a genuinely empty one
        ],
    )
    def test_non_utf8_bodies_stay_distinct(self, hasher, one, two):
        """Bodies are hashed as bytes, never decoded.

        Decoding folded every undecodable body onto the empty digest, so two
        unrelated binary payloads deduplicated against each other.
        """
        assert _h(hasher, body=one) != _h(hasher, body=two)
