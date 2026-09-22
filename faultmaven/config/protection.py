"""
Protection configuration for FaultMaven

**The rate limits, deduplication TTLs and timeouts are code, not configuration.**
They live in the two presets below — development and production — and
``setup_protection_middleware`` chooses between them by the **protection
profile** (``resolve_protection_profile``), never by environment name. No
environment variable sets a limit, a TTL or a timeout; the loader that once read
per-field variables was unreachable on every healthy deployment and was removed
rather than left looking configurable (fm#1023).

Three environment keys reach these presets directly, and one reaches them
through the composition root:

* ``PROTECTION_PROFILE`` — WHICH preset is installed *and* which degrade policy
  it runs, read by ``resolve_protection_profile``. Defaults to ``hardened``;
  only an explicit ``development`` selects the permissive preset and its bypass
  headers, and ``cloud`` (also implied by ``DEPLOYMENT_MODE=cloud``) is the
  multi-replica fleet posture.
* ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` — the Redis degrade policy, read by
  ``_fail_open_default`` and applied by ``resolve_rate_limit_fail_open``. The
  profile sets the DEFAULT — ``cloud`` fail-*closed*, the other two
  fail-*open* — and this key, when set, overrides it on all three (fm#1566).
* ``PROTECTION_TRUSTED_PROXIES`` — which proxies' forwarding headers may be
  believed, read by ``get_trusted_proxies``. Honoured by both presets, empty by
  default.

``DEPLOYMENT_MODE`` is the fourth, and it is **not read here**: ``main.py``
resolves it once through ``settings.is_cloud`` (ADR-004) and passes it down as
``is_cloud_deployment``. It can override all three keys above — it raises the
resolved profile to ``cloud``, which installs the hardened preset whatever
``PROTECTION_PROFILE`` says and pins the degrade policy whatever
``PROTECTION_RATE_LIMIT_FAIL_OPEN`` says. Only in the hardening direction: see
``resolve_protection_profile``.

Changing anything else means changing the preset.
"""

import logging
import os
from enum import Enum
from typing import Any, Dict

from ..models.protection import (
    DeduplicationConfig,
    ProtectionSettings,
    RateLimitConfig,
    TimeoutConfig,
)

logger = logging.getLogger(__name__)

#: Redis key namespace per preset. Named constants rather than literals inside
#: the preset constructors because they have a second consumer: the deployment
#: wipe (``fm-wipe-deployment``) must know EVERY namespace this deployment could
#: have written, and it cannot obtain them by constructing the presets — those
#: call ``get_trusted_proxies()`` / ``_fail_open_default()``, which read settings
#: and emit production warnings as a side effect.
#:
#: ``ALL_REDIS_KEY_PREFIXES`` is the enumeration the wipe consumes. It is the
#: whole set, not the one the *current* ``ENVIRONMENT`` selects, because keys
#: outlive the environment that wrote them: a preset change, an overlay roll, or
#: an execution context that simply does not carry ``ENVIRONMENT`` (the wipe runs
#: with the API scaled down, so it is not the API pod) leaves the other preset's
#: keys on the server. Matching only the current preset made those keys
#: unclassifiable, and ``--verify`` reports what the scoped wipe leaves — so the
#: two agreed with each other while both missed live rate-limit state (fm#1052).
#:
#: ⚠️ There are THREE namespaces and only TWO presets. Staging runs production's
#: preset but is re-pointed to its own namespace by
#: ``api.protection.setup_protection_middleware``, so it cannot be discovered by
#: enumerating the preset constructors. That is exactly why the value lives here
#: and the middleware reads it from here: a fourth namespace introduced the same
#: way must be added to this tuple, or the wipe will not know it either.
DEVELOPMENT_REDIS_KEY_PREFIX = "faultmaven_dev"
PRODUCTION_REDIS_KEY_PREFIX = "faultmaven_prod"
STAGING_REDIS_KEY_PREFIX = "faultmaven_staging"
ALL_REDIS_KEY_PREFIXES = (
    DEVELOPMENT_REDIS_KEY_PREFIX,
    PRODUCTION_REDIS_KEY_PREFIX,
    STAGING_REDIS_KEY_PREFIX,
)


#: The environment variable ``resolve_protection_profile`` reads. Named rather
#: than spelled inline because the deployment wipe's sibling constants above are
#: named for the same reason: a second spelling is a second source of truth.
PROTECTION_PROFILE_ENV_VAR = "PROTECTION_PROFILE"


class ProtectionProfile(str, Enum):
    """Which protection preset this deployment installs.

    **This is the axis, and it is the only one.** It answers one question —
    *is this a development checkout of the code, or a deployment of the
    product?* — and nothing else answers it.

    Before fm#985 item 15 the question was answered by ``ENVIRONMENT``, which
    conflates two things that are not the same: ``development`` describes who
    is editing the code, ``standalone`` describes how the product is deployed.
    One value answered both, and the answer that is right for a contributor's
    checkout — live ``X-Dev-Bypass`` / ``X-Test-Bypass`` headers, where the
    mere PRESENCE of either skips all rate limiting — was wrong for every
    self-hosted operator following the quickstart, because ``ENVIRONMENT`` is
    unset there and falls to the settings default ``development``.

    ``HARDENED`` is the default, so a deployment nobody classified is protected
    rather than opt-out.

    **Three members, two presets.** ``DEVELOPMENT`` →
    ``get_development_protection_settings``; ``HARDENED`` and ``CLOUD`` both →
    ``get_production_protection_settings``, with the same limits, the same
    namespace and no bypass header on either. What separates them is the one
    property fm#1566 moved onto this axis: the **Redis degrade policy**. The
    constructors keep their names because that is what the numbers in them are:
    production's. The *profile* is named ``hardened`` rather than ``production``
    deliberately — it is a posture, and calling it ``production`` would
    re-import the environment vocabulary this axis exists to separate from.

    ``CLOUD`` is the multi-replica fleet, and it is what pins fail-*closed*.
    ``HARDENED`` is the self-hosted deployment — one replica, one tenant, no
    fleet to shed onto — and it defaults fail-*open*, whatever ``ENVIRONMENT``
    says. That is fm#1566: ``api/protection.py`` used to compute the degrade
    policy as ``environment != Environment.DEVELOPMENT``, so a self-hosted
    operator who set ``ENVIRONMENT=production`` — the natural thing to do — was
    silently moved to fail-closed, which also disables the per-replica FakeRedis
    stand-in rung (``RedisRateLimiter.fallback_enabled``) and turns a Redis blip
    into a 503 on every request for a single user with nothing to shed onto.

    **The members are ordered by strictness** — ``DEVELOPMENT`` < ``HARDENED``
    < ``CLOUD`` — and ``resolve_protection_profile`` takes the *maximum* of what
    the key asks for and what the deployment shape implies. So every input can
    harden the result and none can loosen it, which is the same monotonicity
    ``ENVIRONMENT``'s veto has: no combination of keys is less protected than
    ``PROTECTION_PROFILE`` alone says.
    """

    HARDENED = "hardened"
    DEVELOPMENT = "development"
    CLOUD = "cloud"


#: Strictness order for the monotone resolution in ``resolve_protection_profile``.
#: A dict rather than the member order, because ``Enum`` declaration order is
#: not a promise and reordering the members above must not silently reorder the
#: postures.
_PROFILE_STRICTNESS = {
    ProtectionProfile.DEVELOPMENT: 0,
    ProtectionProfile.HARDENED: 1,
    ProtectionProfile.CLOUD: 2,
}


def _named_environment(environment: Any) -> str:
    """``environment`` as its lowercase name, however it was spelled.

    ``Environment`` subclasses ``str`` but ``str(member)`` renders
    "Environment.DEVELOPMENT", so ``.value`` is unwrapped first — the shape
    that once reached an append-only audit column (#827). ``None`` renders as
    the empty string, which is not ``"development"``, which is the fail-safe
    answer for a caller that named nothing.
    """
    return str(getattr(environment, "value", environment) or "").strip().lower()


def is_deployed_environment(
    environment: Any = None, *, is_cloud_deployment: bool = False
) -> bool:
    """Is this a DEPLOYED box, as opposed to somebody's development checkout?

    One question, one spelling, three consumers — and it lives here rather than
    being written out at each of them because two of the three had drifted
    apart, which is what fm#985 item 17 was:

    1. ``api.protection.setup_protection_middleware`` — whether a **setup
       failure refuses the boot** rather than booting unprotected, and whether
       the empty-trusted-proxies warning fires.
    2. ``main.setup_middleware`` — the same refusal at the composition root's
       carve-out, and **which CORS policy is installed**: strict origins, or
       development's appended localhost plus the RFC1918
       ``allow_origin_regex`` with ``allow_credentials`` on.
    3. Its own tests, which sweep both callers and assert they agree.

    **Not development, OR cloud.** The second disjunct is not redundant:
    ``DEPLOYMENT_MODE=cloud`` with ``ENVIRONMENT=development`` is a reachable
    configuration — ``config.deployment_coherence`` relates the deployment mode
    to auth, storage and tenancy, and never to the environment name — and it
    is the single worst shape to hand a development policy to, being a
    multi-tenant fleet. Before this disjunct such a fleet accepted the shipped
    wildcard CORS origins and installed the private-network regex with
    credentials on, so any host on any private network could call it
    cross-origin.

    Phrased as "not development" rather than "in (staging, production)" for the
    fail-safe reason fm#1023 chose one layer up: a fourth ``Environment``
    member added later is deployed until someone says otherwise, and so is a
    value that is not an ``Environment`` at all.

    It lives in this module because this module already owns the
    deployment-shape vocabulary (``ProtectionProfile``,
    ``resolve_protection_profile``) and is already imported by both callers —
    ``main.py`` imports ``get_trusted_proxies`` from here for the same reason.
    """
    return _named_environment(environment) != "development" or is_cloud_deployment


def resolve_protection_profile(
    environment: Any = None, *, is_cloud_deployment: bool = False
) -> ProtectionProfile:
    """The one reader of ``PROTECTION_PROFILE``, and the one selector of a preset.

    One reader for the same reason ``_fail_open_default`` and
    ``get_trusted_proxies`` are one reader each: no two consumers may disagree
    about the posture the deployment asked for. Here that matters more than for
    either of those, because the thing being decided is whether a header anyone
    can send switches the rate limiter off.

    **The default is ``hardened`` and an unrecognised value is ``hardened``.**
    Loosening has to be asked for by name; a typo, an empty string or a value
    nobody anticipated all fail safe, which is the same direction fm#1023 chose
    one layer up.

    ``ENVIRONMENT`` still participates, but **only to refuse** — it can never
    select the permissive preset, which is the whole of the decoupling. A
    development profile asked for on a deployed environment (``staging``,
    ``production``, or any value that is not ``development``) is refused and
    logged. The relation is monotone on purpose: every input can harden the
    result and none can loosen it, so no combination of the two keys is less
    protected than ``PROTECTION_PROFILE`` alone says.

    ``environment`` is the value the composition root already resolved
    (``settings.server.environment``), passed in rather than re-read here, so
    the veto cannot disagree with the environment the rest of the application
    ran with. ``None`` — a caller that named no environment — is treated as
    deployed, which is the same fail-safe default
    ``setup_protection_middleware`` gives its own parameter.

    ``is_cloud_deployment`` is the **deployment-shape floor**, and it is
    ``settings.is_cloud`` — ADR-004's single source of truth for "am I
    standalone or cloud?" — passed in rather than re-read here, for the same
    reason ``environment`` is. It can only raise the result: a cloud deployment
    resolves to ``CLOUD`` however the key is spelled, so a fleet cannot end up
    on the self-hosted degrade posture (fm#1566) and cannot arm the bypass
    headers by also naming ``ENVIRONMENT=development`` — a hole the veto alone
    left open, since the veto reads ``ENVIRONMENT`` and a cloud deployment is
    free to name any environment it likes.

    It defaults to ``False`` deliberately, and that is the same default
    ``DeploymentMode`` itself carries: a deployment that has not declared
    itself cloud is not treated as cloud for auth, storage, tenancy or the
    coherence gate either, and giving this one property a *different* answer to
    "am I a cloud deployment" is the conflation ADR-004 exists to prevent. A
    self-hosted fleet that wants the cloud degrade posture without claiming
    cloud mode says so directly with ``PROTECTION_PROFILE=cloud``.
    """
    floor = (
        ProtectionProfile.CLOUD
        if is_cloud_deployment
        else ProtectionProfile.DEVELOPMENT
    )

    def _hardest(*candidates: ProtectionProfile) -> ProtectionProfile:
        return max(candidates, key=_PROFILE_STRICTNESS.__getitem__)

    requested = os.getenv(PROTECTION_PROFILE_ENV_VAR, "").strip().lower()

    if not requested:
        return _hardest(ProtectionProfile.HARDENED, floor)

    try:
        profile = ProtectionProfile(requested)
    except ValueError:
        logger.warning(
            "%s=%r is not a recognised protection profile (%s); installing the "
            "hardened preset. Rate limiting stays on and no bypass header is "
            "honoured.",
            PROTECTION_PROFILE_ENV_VAR,
            requested,
            "/".join(member.value for member in ProtectionProfile),
        )
        return _hardest(ProtectionProfile.HARDENED, floor)

    if profile is not ProtectionProfile.DEVELOPMENT:
        hardened_by_shape = _hardest(profile, floor)
        if hardened_by_shape is not profile:
            # Read and overridden is not the same as read and honoured. This
            # module's rule is that a key is never silently ignored (the
            # ``cloud`` degrade pin warns for the same reason), and an operator
            # who wrote ``hardened`` on a cloud deployment has asked for the
            # self-hosted degrade posture and is not getting it.
            logger.warning(
                "%s=%s was requested on a cloud deployment "
                "(DEPLOYMENT_MODE=cloud); installing the '%s' profile instead. "
                "A multi-replica fleet's degraded rung is per-replica, so it "
                "is a floor rather than a substitute and the limiter pins "
                "fail-CLOSED. The deployment shape can only harden the "
                "profile, never loosen it.",
                PROTECTION_PROFILE_ENV_VAR,
                profile.value,
                hardened_by_shape.value,
            )
        return hardened_by_shape

    named = _named_environment(environment)
    if named != "development":
        logger.error(
            "%s=development was requested on ENVIRONMENT=%r. Refusing: the "
            "development preset carries live bypass headers (X-Dev-Bypass / "
            "X-Test-Bypass), whose mere presence skips all rate limiting, and "
            "that is a development-checkout affordance rather than a "
            "deployment one. Installing the hardened preset instead.",
            PROTECTION_PROFILE_ENV_VAR,
            named or None,
        )
        return _hardest(ProtectionProfile.HARDENED, floor)

    if floor is ProtectionProfile.CLOUD:
        logger.error(
            "%s=development was requested on a cloud deployment "
            "(DEPLOYMENT_MODE=cloud). Refusing for the same reason a deployed "
            "ENVIRONMENT refuses it — the preset arms X-Dev-Bypass / "
            "X-Test-Bypass, whose mere presence skips all rate limiting. "
            "Installing the cloud preset instead.",
            PROTECTION_PROFILE_ENV_VAR,
        )

    return _hardest(profile, floor)


def resolve_rate_limit_fail_open(profile: ProtectionProfile) -> bool:
    """The one decider of the Redis degrade policy (fm#1566).

    **The profile owns this axis**, the same axis fm#985 item 15 gave the limits
    and the bypass headers. ``ENVIRONMENT`` does not participate: it used to, as
    ``for_deployed_environment=(environment != Environment.DEVELOPMENT)`` in
    ``api/protection.setup_protection_middleware``, and the consequence was that
    a self-hosted operator setting ``ENVIRONMENT=production`` — the natural
    thing to do on a production install of a self-hosted product — was silently
    moved from fail-open to fail-closed on a **single replica**, where the
    argument for fail-closed barely applies.

    ``fail_open_on_redis_error`` is not only about refusing. It also feeds
    ``RedisRateLimiter.fallback_enabled``, so fail-closed **disables the
    per-replica FakeRedis stand-in rung**: a limiter whose client stops
    answering refuses instead of recovering. Measured during fm#1563's review —
    flipping that one flag took ``tests/integration/api/test_sessions_api.py``
    from 20 failed / 20 passed to 40 passed under the cloud shape. Fail-closed
    is a recovery posture, not only a refusal posture, which is why it belongs
    to the deployment shape rather than to an environment name.

    Two answers, one per posture:

    **The profile sets the DEFAULT; the key overrides it on every profile.**

    * ``CLOUD`` defaults fail-**closed**. Unchanged by fm#1566 and deliberately
      out of its scope: rung 2 is per-replica, so during a shared-Redis outage
      a fleet of N replicas enforces N independent copies of a limit whose
      configured value only means anything when it is shared, and the trade a
      fleet wants is a 503 over a hole in a control that is both a security and
      a cost boundary. The full argument is in
      ``get_production_protection_settings``' docstring.
    * ``HARDENED`` and ``DEVELOPMENT`` default fail-**open**.

    ``PROTECTION_RATE_LIMIT_FAIL_OPEN``, when it is **set**, wins on all three.
    That is the ruling's third point read as written — *"an explicit override,
    so an operator who wants the other posture is not forced to lie about their
    profile"* — and the first implementation of this function got it wrong by
    honouring the key on two profiles and ignoring it on the third. Nothing
    about the cloud posture moves: a cloud deployment that sets nothing still
    fails closed, which is every cloud deployment there is. What changes is
    that the posture is now *reachable*, and it has to be: since the deployment
    shape can RAISE a profile to ``cloud`` on its own
    (``DEPLOYMENT_MODE=cloud``), an unoverridable pin left a cloud-mode process
    with no shared Redis — the tenancy integration suites are exactly that —
    unable to obtain a working limiter by any configuration at all. It refused
    every request with a 503 instead, which is how this was found.

    One decider, for the same reason ``_fail_open_default`` is one reader and
    ``resolve_protection_profile`` is one selector: no two producers of a
    ``ProtectionSettings`` may disagree about the posture the deployment asked
    for.
    """
    if profile is ProtectionProfile.CLOUD and _fail_open_key() is None:
        return False

    # ``_fail_open_default`` for every profile that gets this far, so the key
    # has ONE interpretation. Spelling the comparison out again here — even
    # as the same ``== "true"`` — would be the second reader this module
    # forbids, and the two would have differed on the first attempt: this
    # branch had a ``.strip()`` the other does not.
    fail_open = _fail_open_default()

    if profile is ProtectionProfile.CLOUD and fail_open:
        # Only when the key actually MOVES the answer. An explicit ``false``
        # agrees with the cloud default and says nothing worth a line; this is
        # a fleet stepping off the posture its shape implies, which is worth
        # one — the more so because the previous behaviour was to ignore the
        # key outright, so an operator who set it and saw no effect needs to
        # know that changed.
        logger.warning(
            "%s overrides the '%s' profile's fail-CLOSED default: this "
            "deployment will serve unlimited rather than refuse on a Redis "
            "outage. A multi-replica fleet's degraded rung is the per-replica "
            "in-process stand-in, so it is a floor rather than a substitute "
            "for a shared limit.",
            FAIL_OPEN_ENV_VAR,
            profile.value,
        )

    return fail_open


#: The environment variable ``_fail_open_key`` reads, named for the same reason
#: ``PROTECTION_PROFILE_ENV_VAR`` is: a second spelling is a second source.
FAIL_OPEN_ENV_VAR = "PROTECTION_RATE_LIMIT_FAIL_OPEN"


def _fail_open_key() -> "str | None":
    """The ONE read of ``PROTECTION_RATE_LIMIT_FAIL_OPEN``. ``None`` means unset.

    Separated from ``_fail_open_default`` because two callers need two
    different things from the same key — the policy, and whether the operator
    stated one at all (``resolve_rate_limit_fail_open`` warns when the
    ``cloud`` profile is about to ignore a value somebody set). Reading
    ``os.getenv`` twice would be the second reader this module spends its
    docstrings forbidding, and the architecture sweep in
    ``tests/unit/architecture/test_configuration_compliance.py`` counts the
    calls, so it is also the second reader the build refuses.
    """
    return os.getenv(FAIL_OPEN_ENV_VAR)


def _fail_open_default() -> bool:
    """Whether the request-path protections fail open when Redis is unreachable.

    ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default ``true``) governs the
    rate-limiting and deduplication degrade policy, and nothing else.

    This is the key's one *interpretation* — ``_fail_open_key`` above is the
    one read. Whether a given deployment's policy comes from the key at all is
    ``resolve_rate_limit_fail_open``'s decision: the ``cloud`` profile pins
    fail-closed and never gets here.

    It is deliberately *not* ``PROTECTION_FAIL_OPEN``: that key binds to
    ``settings.protection.fail_open`` and governs PII-redaction fail-open
    (#654, default ``false``). The two policies are independent and must stay
    that way — an operator hardening redaction to fail closed must not thereby
    turn a Redis blip into a 503 on every request.

    One interpretation, so no producer of a ``ProtectionSettings`` can
    disagree with another about what the deployment asked for.
    """
    raw = _fail_open_key()
    return (raw if raw is not None else "true").lower() == "true"


def get_trusted_proxies() -> list:
    """Proxies whose forwarding headers may be believed when keying limits.

    ``PROTECTION_TRUSTED_PROXIES`` is a comma-separated list of addresses or
    CIDRs — for a Kubernetes deployment, the ingress controller's pod range.

    **Empty is the default and it is deliberate.** With no entry, no
    ``X-Forwarded-For`` header influences the rate-limit key and every limit is
    keyed on the socket peer. Before this existed the headers were honoured
    unconditionally, so the ``global`` limit — the only one that applies to
    unauthenticated traffic — could be evaded outright by rotating a header the
    limited party controls.

    The cost of the safe default is real and worth stating: a deployment that
    *is* behind a proxy and does not set this keys every request on the
    proxy's address, so all clients share one bucket. It is not silent, and it
    is reported twice over — ``get_production_protection_settings`` warns at
    startup when production leaves it empty, and
    ``client_ip.resolve_client_ip`` warns at request time (throttled) when
    forwarding headers arrive from an address that is not configured here.

    One reader, for the same reason ``_fail_open_default`` is one reader: no
    consumer may disagree with another about which proxies the deployment
    believes. The consumers, in full:

    1. ``get_development_protection_settings`` — populates
       ``ProtectionSettings.trusted_proxies`` for ``RateLimitMiddleware``.
    2. ``get_production_protection_settings`` — the same, and the one preset
       that warns when this key is left empty.
    3. ``PerformanceTrackingMiddleware`` (``api/middleware/performance.py``) —
       so the address a request is *labelled* with cannot disagree with the one
       it is *limited* by.
    4. The OAuth/SSO limiter (``modules/auth/api/rate_limiting.py``), which
       calls this directly rather than reading a ``ProtectionSettings``.
    5. ``LoggingMiddleware`` (``api/middleware/logging.py``), likewise.

    Production honours this key rather than pinning it — unlike the degrade
    policy, there is no value here that is right for every deployment.
    """
    return [
        entry.strip()
        for entry in os.getenv("PROTECTION_TRUSTED_PROXIES", "").split(",")
        if entry.strip()
    ]


def get_development_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for development

    - More lenient rate limits
    - Shorter timeouts for faster feedback
    - Bypass headers enabled
    - Redis degrade policy from ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default
      open), via ``resolve_rate_limit_fail_open``. Only the ``cloud`` profile
      pins fail-closed — see that function and fm#1566.

    **Reached only when ``PROTECTION_PROFILE=development`` is set explicitly**,
    and only on a box that also names ``ENVIRONMENT=development`` (or leaves it
    unset). ``ENVIRONMENT`` alone no longer reaches here: a standalone
    deployment is a deployment shape, not a development environment, and the
    quickstart leaves ``ENVIRONMENT`` unset — which used to land every
    self-hosted operator on these bypass headers (fm#985 item 15). See
    ``resolve_protection_profile``.
    """
    return ProtectionSettings(
        # General
        enabled=True,
        # Through the one decider rather than the one reader, so this preset
        # cannot disagree with the production preset about what a profile means
        # (fm#1566). For ``DEVELOPMENT`` the decider IS ``_fail_open_default()``.
        fail_open_on_redis_error=resolve_rate_limit_fail_open(
            ProtectionProfile.DEVELOPMENT
        ),
        protection_bypass_headers=["X-Dev-Bypass", "X-Test-Bypass"],
        trusted_proxies=get_trusted_proxies(),
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix=DEVELOPMENT_REDIS_KEY_PREFIX,
        # Rate limiting (more lenient for development)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=5000, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=50, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=500, window=3600
            ),
            "per_session_read": RateLimitConfig(enabled=True, requests=600, window=60),
            "per_session_read_hourly": RateLimitConfig(
                enabled=True, requests=6000, window=3600
            ),
            "title_generation": RateLimitConfig(enabled=True, requests=5, window=300),
        },
        # Deduplication (shorter TTLs for faster iteration)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=30),
        },
        # Timeouts (shorter for faster feedback)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=120,  # 2 minutes
            agent_phase=60,  # 1 minute
            llm_call=20,  # 20 seconds
            emergency_shutdown=180,  # 3 minutes
        ),
    )


def get_production_protection_settings(
    *,
    for_deployed_environment: bool = True,
    fail_open_on_redis_error: bool = False,
) -> ProtectionSettings:
    """
    Get protection settings optimized for production

    - Strict rate limits
    - Long timeouts for reliability
    - No bypass headers
    - Degrade policy supplied by the caller, defaulting to fail-**closed** —
      see ``fail_open_on_redis_error`` below

    **This is the default preset, not just production's.** Only an explicit
    ``PROTECTION_PROFILE=development`` selects the other one; every other
    value, and every deployment that sets nothing — including the standalone
    quickstart, whatever ``ENVIRONMENT`` says — lands here, so a deployment
    nobody classified is protected rather than unprotected (fm#1023, fm#985
    item 15). Read the numbers below as the floor every deployment runs on.

    **The degrade policy is no longer decided here** (fm#1566). It arrives as
    ``fail_open_on_redis_error`` from ``resolve_rate_limit_fail_open``, which
    keys it on the protection profile: ``cloud`` pins fail-closed, ``hardened``
    and ``development`` honour ``PROTECTION_RATE_LIMIT_FAIL_OPEN``. The
    argument for the cloud pin is below and is unchanged; what changed is that
    ``ENVIRONMENT`` no longer selects it, so a self-hosted operator setting
    ``ENVIRONMENT=production`` is not moved to fail-closed on a single replica.
    Read everything that follows as being about the **cloud** audience.

    Defaulting it open rests on the claim that the fail-open rung is nearly
    unreachable, because the ladder is shared Redis → per-replica FakeRedis →
    fail open and the first two rungs enforce limits. Both of the defects that
    once made that claim outright false have since been fixed: the sliding
    window counts requests rather than seconds, and the ``global`` key can no
    longer be rotated by a caller sending its own ``X-Forwarded-For``. The
    argument is no longer refuted, so the pin is now a posture decision rather
    than a precondition that has not been met, and it is recorded as one.

    It stays pinned for two reasons.

    First, rung 2 is *per-replica*. FakeRedis is in-process, so during a shared
    Redis outage a deployment of N replicas enforces N independent copies of a
    limit whose configured value only means anything when it is shared, and no
    replica can see a flood spread across its peers. "Limits still enforced" is
    true of rung 2 and materially weaker than it sounds; it is a floor, not a
    substitute.

    Second, the trade this settles — a Redis blip becoming a 503 on every
    request, against a hole in a control that is both a security boundary and a
    cost boundary — is a deliberate choice about which failure production would
    rather have, not a consequence of a bug. Reversing it should be its own
    change, argued on its own evidence, not a rider on whichever fix happens to
    clear the last stated blocker.

    The known cost of the pin is tracked: mid-flight Redis errors are currently
    served as ``429`` with a ``0/0 requests`` body before the ``503`` rung
    engages, which is a confusing way to say "unavailable". That is a defect in
    how the pinned path reports itself, and an argument for fixing the report —
    not for unpinning.

    The ``development`` and ``hardened`` profiles both honour
    ``PROTECTION_RATE_LIMIT_FAIL_OPEN``; only ``cloud`` opts out of it, and it
    does so by naming a posture rather than by naming an environment.
    ``PROTECTION_TRUSTED_PROXIES`` is *not* pinned here — unlike the
    degrade policy, no value for it is right for every deployment, and the
    empty default is already the safe one. It is, however, the one preset that
    warns when it is left empty: see below.

    **Two parameters, because there are now two questions.** They were one —
    ``for_deployed_environment``, computed as ``environment !=
    Environment.DEVELOPMENT`` — while both answers came from the environment
    name. fm#1566 moved one of them onto the protection profile, so the two
    predicates genuinely differ and collapsing them again would re-key the
    degrade policy on ``ENVIRONMENT`` by the back door:

    1. ``fail_open_on_redis_error`` — **the degrade policy**, decided by the
       *profile* (``resolve_rate_limit_fail_open``). Not cosmetic: the flag
       reaches ``RedisRateLimiter.fallback_enabled``, so fail-closed also
       **disables the per-replica stand-in rung** — a limiter whose client
       stops answering does not recover onto FakeRedis, it refuses. The default
       here is ``False`` (fail-closed) so that a caller who names nothing gets
       the strict posture; every caller in the application names it.
    2. ``for_deployed_environment`` — **the empty-trusted-proxies warning**
       only, and decided by the *deployment* rather than the profile, because
       that is the question it asks: is there something in front of this box
       whose forwarding headers we are declining to believe? A single-user box
       has nothing in front of it, so an empty list there is not merely safe
       but correct, and "empty in production" would be a false statement
       sending an operator to configure a proxy they do not run.

       The caller answers it with ``is_deployed_environment``, so the set of
       boxes this fires on gains exactly one shape: a cloud deployment naming
       ``ENVIRONMENT=development``, which is the one shape *guaranteed* to sit
       behind an ingress and was the one shape losing the warning. Everything
       else warns exactly as before.
    """
    trusted_proxies = get_trusted_proxies()

    if for_deployed_environment and not trusted_proxies:
        # Production is by definition a deployment behind something. Empty here
        # is safe but coarse: every external client resolves to the proxy's own
        # address and shares a single `global` bucket, so one caller crossing
        # 500/60s refuses everyone. The request-time warning in ``client_ip``
        # only fires once forwarding headers actually arrive and is throttled to
        # one per five minutes, which is too late and too quiet to notice during
        # a rollout — so say it once, plainly, at startup.
        #
        # It warns rather than refuses to boot deliberately: an unset value
        # degrades availability, and refusing to start would convert that into
        # a total outage, which is worse than the thing it guards against.
        logger.warning(
            "PROTECTION_TRUSTED_PROXIES is empty in production. Forwarding "
            "headers will be ignored and every rate limit keyed on the socket "
            "peer — behind an ingress that is a single address, so all clients "
            "share one 'global' bucket and one caller can refuse traffic for "
            "everyone. Set it to the proxy's address range (in Kubernetes, the "
            "pod CIDR). Leave it empty only if nothing proxies this service."
        )

    return ProtectionSettings(
        # General
        enabled=True,
        # Decided by the PROFILE and handed in (fm#1566). Defaults to
        # fail-closed for a caller that names nothing.
        fail_open_on_redis_error=fail_open_on_redis_error,
        protection_bypass_headers=[],  # No bypasses in production
        trusted_proxies=trusted_proxies,
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix=PRODUCTION_REDIS_KEY_PREFIX,
        # Rate limiting (strict for production)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=500, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=10, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=50, window=3600
            ),
            "per_session_read": RateLimitConfig(enabled=True, requests=120, window=60),
            "per_session_read_hourly": RateLimitConfig(
                enabled=True, requests=1200, window=3600
            ),
            "title_generation": RateLimitConfig(
                enabled=True, requests=1, window=600
            ),  # Once per 10 minutes
        },
        # Deduplication (longer TTLs for better protection)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=30),
        },
        # Timeouts (longer for reliability)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=600,  # 10 minutes
            agent_phase=300,  # 5 minutes
            llm_call=60,  # 1 minute
            emergency_shutdown=1200,  # 20 minutes
        ),
    )


def validate_protection_settings(settings: ProtectionSettings) -> Dict[str, Any]:
    """
    Validate protection settings and return validation report

    Returns:
        Dictionary with validation status and any issues found
    """
    validation = {"valid": True, "warnings": [], "errors": [], "recommendations": []}

    # No Redis check here. ``redis_url`` is ``None`` in the normal case — the
    # connection is resolved centrally by RedisClientFactory, which owns both
    # the credential lookup and the "nothing to connect to" refusal. Requiring
    # a URL here would fail validation on every ordinary deployment and leave
    # the app with no protection middleware installed at all.

    # Check rate limits
    for limit_name, limit_config in settings.rate_limits.items():
        if limit_config.enabled:
            if limit_config.requests <= 0:
                validation["errors"].append(
                    f"Rate limit {limit_name} must have positive request count"
                )
                validation["valid"] = False

            if limit_config.window <= 0:
                validation["errors"].append(
                    f"Rate limit {limit_name} must have positive window"
                )
                validation["valid"] = False

            # Warn about very permissive limits
            if limit_config.requests > 10000:
                validation["warnings"].append(
                    f"Rate limit {limit_name} is very high: {limit_config.requests}"
                )

            # Warn about very restrictive limits
            if limit_config.requests < 5 and limit_name != "title_generation":
                validation["warnings"].append(
                    f"Rate limit {limit_name} is very restrictive: {limit_config.requests}"
                )

    # Check deduplication settings
    for dedup_name, dedup_config in settings.deduplication.items():
        if dedup_config.enabled:
            if dedup_config.ttl <= 0:
                validation["errors"].append(
                    f"Deduplication {dedup_name} must have positive TTL"
                )
                validation["valid"] = False

            # Warn about very long TTLs
            if dedup_config.ttl > 3600:
                validation["warnings"].append(
                    f"Deduplication {dedup_name} TTL is very long: {dedup_config.ttl}s"
                )

    # Check timeout settings
    if settings.timeouts.enabled:
        if settings.timeouts.agent_total <= 0:
            validation["errors"].append("Agent total timeout must be positive")
            validation["valid"] = False

        if settings.timeouts.agent_phase <= 0:
            validation["errors"].append("Agent phase timeout must be positive")
            validation["valid"] = False

        if settings.timeouts.llm_call <= 0:
            validation["errors"].append("LLM call timeout must be positive")
            validation["valid"] = False

        # Check timeout hierarchy
        if settings.timeouts.agent_phase >= settings.timeouts.agent_total:
            validation["warnings"].append(
                "Agent phase timeout should be less than total timeout"
            )

        if settings.timeouts.llm_call >= settings.timeouts.agent_phase:
            validation["warnings"].append(
                "LLM call timeout should be less than phase timeout"
            )

        # Warn about very short timeouts
        if settings.timeouts.llm_call < 10:
            validation["warnings"].append(
                "LLM call timeout is very short, may cause premature failures"
            )

    # No "consider adding emergency bypass headers" recommendation. It used to
    # live here, fired on every fail-closed deployment, and was the opposite of
    # the posture: ``RateLimitMiddleware._should_bypass`` keys on header
    # PRESENCE, so an armed header name is an unauthenticated opt-out of the
    # entire limiter for anyone who learns it. fm#985 item 15 disarms those
    # headers everywhere but a declared development checkout; advice to add
    # them back does not belong next to that.

    return validation
