"""
Request hashing for duplicate-submit detection.

The hash identifies a request *exactly*: same session, same method, same path,
same query, same body bytes. Anything else is a different request.

Two deliberate non-features, both removed after they were found to misfire:

* **No content normalization.** An earlier version rewrote timestamps, UUIDs,
  10-digit numbers and ``/tmp`` paths to placeholders and dropped body fields
  named ``uuid``/``version``/``t``/``_`` before hashing. For a troubleshooting
  product whose request bodies are mostly IDs, epochs and log fragments, that
  collapsed genuinely different messages onto one hash: "check order
  4232342342" and "check order 9994442211" produced the same digest, so the
  user's second message was classified as a duplicate of the first.
  Normalization can only ever manufacture false duplicates here -- a
  double-submit is byte-identical by construction.

* **No password KDF.** The digest is a Redis key derived from request content,
  never a secret and never returned to the client, so there is nothing for a
  rainbow table to attack. The previous PBKDF2-HMAC-SHA256 at 100,000
  iterations cost ~72-85 ms per request *synchronously inside async dispatch*,
  stalling the whole event loop rather than one request -- and it keyed off a
  salt hardcoded in this file, so it was not even doing the thing it claimed.

Request headers are deliberately *not* part of the digest. The old version mixed
in ``content-type``/``accept``/``accept-language``/``accept-encoding``, which
made an identical body resubmitted under a different ``Accept-Encoding`` count as
a different request. This is the one change that can merge requests the old
digest kept apart -- a body retried after a content-type correction now counts as
the same submit.

Deliberate double-submits are the idempotency middleware's job; this only has
to catch the accidental immediate resubmit (double-click, client retry).
"""

import hashlib
import json
import logging
from typing import Any, Dict, Optional


class RequestHasher:
    """Hashes a request for exact duplicate-submit detection."""

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)

    def hash_request(
        self,
        session_id: str,
        endpoint: str,
        method: str = "POST",
        body: Optional[bytes] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return the hex SHA-256 identifying this exact request.

        ``body`` is raw bytes and is hashed undecoded, so two bodies that differ
        only outside UTF-8 stay distinct.

        Components are length-prefixed rather than joined by a delimiter, so no
        component's content can be made to look like a field boundary: without
        it, ``session="a"``/``endpoint="b|c"`` and ``session="a|b"``/
        ``endpoint="c"`` would hash alike.
        """
        digest = hashlib.sha256()
        for component in (
            (session_id or "").encode("utf-8"),
            method.upper().encode("utf-8"),
            (endpoint or "").encode("utf-8"),
            self._canonical_query(query_params).encode("utf-8"),
            body or b"",
        ):
            digest.update(str(len(component)).encode("ascii"))
            digest.update(b":")
            digest.update(component)
        return digest.hexdigest()

    @staticmethod
    def _canonical_query(params: Optional[Dict[str, Any]]) -> str:
        """Serialize query parameters order-independently.

        ``?a=1&b=2`` and ``?b=2&a=1`` are the same request; sorting makes the
        digest agree. Values are kept verbatim -- only the ordering is
        canonical, never the content.
        """
        if not params:
            return ""
        return json.dumps(sorted(params.items()), separators=(",", ":"))
