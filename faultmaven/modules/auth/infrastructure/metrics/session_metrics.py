"""Telemetry for the session mint: who still mints anonymously (#1460).

``POST /api/v1/sessions`` mints an anonymous ``user_id`` whenever no bearer
resolves to a user. Two different callers take that branch, and a log line
could not tell them apart:

* ``bearer="absent"``: the request carried no ``Authorization`` header, or a
  blank one. This is a client that mints without a credential, which the
  server cannot refuse until that count reads zero (#1460 step 3).
* ``bearer="unresolved"``: it carried a non-blank header that resolved to
  nobody: not ``Bearer``, malformed, invalid, expired, or revoked but not yet
  expired. This series never reaches zero, by design.

``origin`` is read from the ``Origin`` header: ``extension`` for a
``chrome-extension`` or ``moz-extension`` scheme, ``web`` for any other
non-blank origin, and ``none`` when it is missing or blank. A browser sends
``Origin`` on every ``POST``, so a panel's mint carries one; an uptime probe or
a crawler does not.

The route counts when it takes the anonymous branch, before the store write.
It counts callers, not rows. No auth decision reads this counter.

All six series are created at import, so each exports ``0`` before its first
event: a series created by its first increment appears already at 1, and
``increase()`` misses that event (#1787). Per request, per replica: aggregate
with ``sum()``.
"""

from faultmaven.infrastructure.shims.metrics import Counter

BEARER_KINDS = ("absent", "unresolved")
ORIGIN_KINDS = ("extension", "web", "none")

#: ``POST /api/v1/sessions`` requests that minted an anonymous identity.
session_anonymous_mints_total = Counter(
    "faultmaven_auth_session_anonymous_mints_total",
    "Session mints (POST /api/v1/sessions) that resolved no user and minted an "
    "anonymous identity. bearer: absent (no or blank Authorization header) or "
    "unresolved (a header that resolved to nobody). origin: extension, web or "
    "none, from the Origin header.",
    ["bearer", "origin"],
)

for _bearer in BEARER_KINDS:
    for _origin in ORIGIN_KINDS:
        session_anonymous_mints_total.labels(bearer=_bearer, origin=_origin)
