"""The ``Idempotency-Key`` grammar, and the two names its answers carry.

One copy, read by every party that touches a key (#1888):

- ``IdempotencyMiddleware`` validates the header with it before it caches or
  replays anything;
- the turn route declares it on its ``Idempotency-Key`` header parameter, so a
  bad key is a published 422 there (the middleware does not see turns: the
  route owns their replay);
- ``turn_receipts.idempotency_key`` is sized from it, so a key the grammar
  admits always fits the column that records it.

A second spelling of any of these would let the three disagree about which
keys exist, and the disagreement surfaces as a key one layer accepts and the
next refuses (or truncates). Dependency-free on purpose, and in ``config``
rather than ``api``: the ORM models and the settings import it, and both are
reachable from ``faultmaven.services``, which may not import the API layer
(``.importlinter`` contract 2).
"""

import re

#: The bounds of a key, inclusive. Eight characters keeps out the accidental
#: (``"1"``, ``"retry"``); 255 is the column that records a turn's key.
IDEMPOTENCY_KEY_MIN_LENGTH = 8
IDEMPOTENCY_KEY_MAX_LENGTH = 255

#: UUID-like: letters, digits, hyphen, underscore. No separator a Redis key or
#: a log line could misparse, and every first-party key fits it (the copilot's
#: ``opt_msg_<ms>_<n>``, a UUID).
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]+$"

_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)

#: ``x-error-code`` of the 409 for a key presented with a different request
#: than the one it was first used with. The same condition answers the same
#: code whichever layer detects it (the middleware's cache, or a turn receipt).
IDEMPOTENCY_KEY_REUSE = "IDEMPOTENCY_KEY_REUSE"

#: Set ``true`` on a response that is a replay of an earlier one rather than a
#: new execution. Listed in ``cors_expose_headers`` so a browser client can
#: tell the two apart.
IDEMPOTENCY_REPLAYED_HEADER = "X-Idempotency-Replayed"


def is_valid_idempotency_key(key: str) -> bool:
    """Whether ``key`` is inside the grammar above.

    ``fullmatch``, not ``match``: Python's ``$`` also matches before a trailing
    newline, which the header parameter's (Rust) regex does not, so ``match``
    would admit a key the route refuses.
    """
    return bool(
        key
        and IDEMPOTENCY_KEY_MIN_LENGTH <= len(key) <= IDEMPOTENCY_KEY_MAX_LENGTH
        and _KEY_RE.fullmatch(key)
    )
