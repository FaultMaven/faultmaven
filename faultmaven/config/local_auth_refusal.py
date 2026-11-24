"""The coherence gate's refusals of local auth from settings alone (#2705).

A cloud deployment requires ``AUTH_MODE=oauth``, and ``TENANT_PROVIDER=multi``
requires cloud. The gate (``deployment_coherence``) reads each rule; the local
secret's mint, which runs before the gate, reads ``coherence_refuses_local_auth``.
A leaf, importing nothing from faultmaven, so the settings module can import it.
"""

from __future__ import annotations

from typing import Any

#: The factory's ``BUILTIN_MULTI``, as a literal (the factory imports the
#: settings); a test pins the two to agree.
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
    """``TENANT_PROVIDER=multi`` outside cloud, read as ``coerce_provider_name`` reads it."""
    providers = getattr(settings, "providers", None)
    requested = getattr(providers, "tenant_provider", None)
    if requested is None:
        return False
    name = str(getattr(requested, "value", requested)).lower()
    return name == _MULTI and not settings.is_cloud


def coherence_refuses_local_auth(settings: Any) -> bool:
    """Whether the coherence gate refuses a local-auth run from its settings alone.

    True for a cloud deployment not on OAuth and for ``TENANT_PROVIDER=multi``
    outside cloud. The server and the jobs runner refuse such settings at the
    gate, and an operator command that reaches the signer refuses there, so
    the mint reads, writes, sets and exports no secret for them (#2705); one
    predicate keeps the mint from drifting from the gate.
    """
    return _cloud_without_oauth(settings) or _multi_tenant_outside_cloud(settings)
