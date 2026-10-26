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

``origin`` is read from the ``Origin`` header. For a browser extension it is
the extension's scheme, lower-cased (``chrome-extension``, ``moz-extension``,
``safari-web-extension``), so each family the copilot ships shows on its own.
It is ``none`` when the header is missing, blank or ``null`` (an opaque origin:
a sandboxed frame, ``file://``, some redirects), and ``web`` for any other
origin. A browser sends ``Origin`` on every ``POST``, so a panel's mint carries
one; an uptime probe or a crawler does not.

The route counts after the session is stored, so it counts anonymous sessions
minted: a mint that fails counts nothing. No auth decision reads this counter.

All ten series are created at import, so each exports ``0`` before its first
event: a series created by its first increment appears already at 1, and
``increase()`` misses that event (#1787). Per process: aggregate with
``sum()``.
"""

from faultmaven.infrastructure.shims.metrics import Counter

BEARER_KINDS = ("absent", "unresolved")
EXTENSION_SCHEMES = ("chrome-extension", "moz-extension", "safari-web-extension")
ORIGIN_KINDS = (*EXTENSION_SCHEMES, "web", "none")

#: Anonymous sessions minted by ``POST /api/v1/sessions``.
session_anonymous_mints_total = Counter(
    "faultmaven_auth_session_anonymous_mints_total",
    "Anonymous sessions minted by POST /api/v1/sessions: the request resolved "
    "no user and the session was stored. bearer: absent (no or blank "
    "Authorization header) or unresolved (a header that resolved to nobody). "
    "origin: the extension's scheme (chrome-extension, moz-extension, "
    "safari-web-extension), web, or none (no, blank or null Origin).",
    ["bearer", "origin"],
)

for _bearer in BEARER_KINDS:
    for _origin in ORIGIN_KINDS:
        session_anonymous_mints_total.labels(bearer=_bearer, origin=_origin)
