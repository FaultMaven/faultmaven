"""The environment the API reference is generated under — single source of truth.

Imported by ``scripts/generate_api_docs.py``, which applies it, and by
``tests/integration/api/test_openapi_generation_is_pinned.py``, which builds a
neutral baseline environment from ``_SYSTEM_ENVIRONMENT_KEYS``.

It lives in its own module because the test cannot import the generator: that
module calls ``_pin_generation_environment()`` at import time, which would empty
pytest's own ``os.environ``. The test previously kept a hand-copy of the key set
instead — and a stale copy is not a cosmetic problem. If the generator's set
gained a key the test did not have, the baseline subprocess would be starved of
something it needs, ``--check`` would fail for that reason, and the test would
``skip()`` with a "your venv drifted" message: the environment-independence gate
would go permanently green without ever asserting anything.

Nothing here may have import side effects — that is the whole point.
"""

# Environment the document is generated under. Several routers are mounted
# conditionally, so the spec is a function of configuration as well as of code:
# debug endpoints (``ENVIRONMENT``/``ENABLE_DEBUG_ENDPOINTS``), the OAuth router
# (``OAUTH_ENABLED``), the SSO router (``AUTH_MODE`` plus the WorkOS trio) and
# ``/metrics`` (``METRICS_EXPORTER``). Unpinned, the same commit yields a
# different document on a laptop than in CI and the drift gate reads that as a
# contract change.
#
# The reference documents the **maximal deployed surface**: every route the
# product can serve, so one generated client covers every deployment. Debug
# endpoints are the exception — they are development-only and are never part of
# a deployed API. Excluding OAuth and SSO would leave the document advertising
# `/auth/oauth/authorize` and `/auth/sso/login` from `GET /auth/config` while
# describing neither.
PINNED_ENVIRONMENT = {
    # Building a document must not reach a database, Redis or an LLM provider.
    "SKIP_SERVICE_CHECKS": "true",
    # Not development: excludes the debug router.
    "ENVIRONMENT": "production",
    # Only present to satisfy the startup validator that rejects wildcard CORS
    # in production. CORS is middleware — it appears nowhere in the document.
    "CORS_ALLOW_ORIGINS": '["https://app.faultmaven.com"]',
    # Mount the OAuth and SSO routers. The WorkOS values are placeholders that
    # satisfy `sso_configured`; no credential is contacted while building a
    # schema, and none of these reach the document.
    "AUTH_MODE": "oauth",
    "OAUTH_ENABLED": "true",
    "WORKOS_API_KEY": "placeholder-not-a-credential",
    "WORKOS_CLIENT_ID": "placeholder-not-a-credential",
    "WORKOS_REDIRECT_URI": "https://app.faultmaven.com/auth/sso/callback",
    # Mount /metrics.
    "METRICS_EXPORTER": "prometheus_http",
}

# Variables the interpreter and its imports need, kept when the environment is
# emptied. Everything else is discarded rather than enumerated, so a setting
# nobody thought to pin cannot reach the document — which is how
# ``METRICS_EXPORTER`` was missed the first time this was written.
_SYSTEM_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PWD",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "COMSPEC",
    }
)
