"""The deployment coherence gate's refusals of local auth from settings alone (#2705).

Two rules of the gate (``faultmaven.config.deployment_coherence``) hold whatever
else the settings configure: a cloud deployment requires ``AUTH_MODE=oauth``,
and ``TENANT_PROVIDER=multi`` requires cloud. The gate reads each rule in its
own check; the local HS256 secret's mint (``resolve_local_jwt_secret``), which
runs while the settings are built and before the gate, reads them through
``coherence_refuses_local_auth``. A leaf: it imports nothing from faultmaven,
so both the settings module and the gate can import it.
"""

from __future__ import annotations

from typing import Any

#: ``TENANT_PROVIDER``'s multi-tenant name. A literal, not the tenancy factory's
#: ``BUILTIN_MULTI``: the factory imports the settings, which import this
#: module. A test pins the two to agree.
_MULTI = "multi"


def _auth_mode_name(auth: Any) -> str:
    """Return the configured auth mode as a plain lowercase name.

    ``AuthMode`` is a ``(str, Enum)``, so ``str(member)`` yields
    ``'AuthMode.OAUTH'`` — matching on that is how the cloud auth gate refused
    every correctly-configured cloud boot while the standalone warning stayed
    dead (#881). Read ``.value`` when present, and normalize so a plain-string
    override matches too. Every call site shares this one predicate so they
    cannot drift apart again.
    """
    mode = getattr(auth, "auth_mode", None)
    if mode is None:
        return "local"
    return str(getattr(mode, "value", mode)).strip().lower()


def _cloud_without_oauth(settings: Any) -> bool:
    """A cloud deployment whose ``AUTH_MODE`` is not ``oauth`` (the gate's check 1)."""
    return bool(settings.is_cloud) and _auth_mode_name(settings.auth) != "oauth"


def _multi_tenant_outside_cloud(settings: Any) -> bool:
    """``TENANT_PROVIDER=multi`` outside cloud.

    The value is read as the factory's ``coerce_provider_name`` reads it: unset
    is not multi, an enum by its value, a string lower-cased.
    """
    providers = getattr(settings, "providers", None)
    requested = getattr(providers, "tenant_provider", None)
    if requested is None:
        return False
    name = str(getattr(requested, "value", requested)).lower()
    return name == _MULTI and not settings.is_cloud


def coherence_refuses_local_auth(settings: Any) -> bool:
    """Whether the coherence gate refuses a local-auth run from its settings alone.

    True for a cloud deployment not on OAuth and for ``TENANT_PROVIDER=multi``
    outside cloud. A local-auth run of such settings never starts, so the mint
    reads, writes, sets and exports no secret for it (#2705); one predicate
    keeps the mint from drifting from the gate.
    """
    return _cloud_without_oauth(settings) or _multi_tenant_outside_cloud(settings)
