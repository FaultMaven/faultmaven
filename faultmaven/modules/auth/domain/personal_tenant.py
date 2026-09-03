"""Naming and identifier derivation for personal tenants (#1045, ADR-016 D5).

One module so the name, the slug and the IdP external id cannot drift apart —
they are three renderings of a single derived key, and the WorkOS half of
provisioning is idempotent *because* that key is deterministic.

The key is a keyless BLAKE2b digest of ``provider:provider_user_id``:

* **No PII.** The input is the IdP's own opaque subject handle (``user_01H…``),
  never an email or a display name, and the digest is one-way besides. The slug
  appears in operator tooling and in any surface that shows an organization by
  slug, so it must not carry a person's identity — the organization is called
  "Personal" and named after nobody.
* **Cannot collide across users.** Distinct subjects give distinct digests up to
  a 128-bit collision, and the pair is domain-separated by the provider prefix
  so two providers' identically-spelled subjects are different tenants.
* **Deterministic**, which is the load-bearing property for retries. A first
  sign-in that mints the IdP organization and then fails to commit the database
  half must, on the next attempt, re-derive the *same* external id and find the
  organization it already created rather than minting a second one.

Hashing is not a secrecy claim — an operator holding the subject can recompute
the slug, which is exactly what makes a tenant reconcilable against the IdP.
It is a claim about what the *slug* discloses to everyone else.
"""

from __future__ import annotations

import hashlib

#: What a personal organization is called. The architect's working decision
#: (ADR-016 D5); the owner may rename it, which is why every surface reads this
#: constant rather than spelling the word. It is deliberately not the person's
#: name or email: an organization named after its member leaks that member's
#: identity wherever an organization name is rendered.
PERSONAL_ORG_NAME = "Personal"

#: Prefix on every derived identifier, so a personal tenant is recognisable as
#: one in the database, in WorkOS, and in operator output without consulting
#: ``sso_personal_orgs``.
PERSONAL_SLUG_PREFIX = "personal-"

#: 16 bytes → 32 hex characters. With the prefix that is 41 characters, inside
#: the 100-character ``organizations.slug`` / ``enterprises.slug`` columns.
_DIGEST_BYTES = 16


def personal_tenant_key(provider: str, provider_user_id: str) -> str:
    """Return the PII-free, deterministic key for one subject's personal tenant.

    The two fields are **length-prefixed**, not joined by a separator. Any
    separator can appear inside a value, and then the encoding is ambiguous:
    with a NUL delimiter ``("a", "b\0c")`` and ``("a\0b", "c")`` hash the same
    bytes and therefore the same tenant. IdP subjects are hostile input on this
    path, so the encoding has to be injective by construction rather than by
    an assumption about what a subject may contain. Prefixing each field with
    its own byte length makes the concatenation uniquely decodable, so distinct
    pairs give distinct inputs whatever characters they hold.
    """
    if not provider or not provider_user_id:
        raise ValueError("personal tenant key needs both a provider and a subject")
    parts = (provider.encode("utf-8"), provider_user_id.encode("utf-8"))
    payload = b"".join(len(part).to_bytes(8, "big") + part for part in parts)
    return hashlib.blake2b(payload, digest_size=_DIGEST_BYTES).hexdigest()


def personal_org_slug(key: str) -> str:
    """The FaultMaven organization/enterprise slug for a derived key."""
    return f"{PERSONAL_SLUG_PREFIX}{key}"


def personal_external_id(key: str) -> str:
    """The IdP organization's ``external_id`` for a derived key.

    The same string as the slug, deliberately: it is what makes an
    IdP organization and its FaultMaven tenant recognisable as the same thing
    from either side, and re-deriving it is how a retry finds the organization a
    failed attempt already created.
    """
    return personal_org_slug(key)
