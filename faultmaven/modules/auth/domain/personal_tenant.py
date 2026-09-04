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
    """The slug for a derived key — and the IdP organization's ``external_id``.

    One value serving both, deliberately: it is what makes an IdP organization
    and its FaultMaven tenant recognisable as the same thing from either side,
    and re-deriving it is how a retry finds the organization a failed attempt
    already created. A second function returning the same string only invited
    the two to drift.
    """
    return f"{PERSONAL_SLUG_PREFIX}{key}"


#: What separates a retired tenant's slug from the derived key it was built
#: from. Retirement has to free the derived slug — the next tenant for the same
#: subject derives exactly the same string, and ``enterprises.slug`` is unique
#: deployment-wide — while keeping the retired rows findable by an operator who
#: only knows the subject. Suffixing rather than renaming outright does both:
#: ``personal-<key>`` is free again, and ``personal-<key>-retired-%`` still
#: names every tenant that subject has ever had.
RETIRED_SLUG_MARKER = "-retired-"


def retired_slug(slug: str, discriminator: str) -> str:
    """The slug a retired organization or enterprise is renamed to.

    ``discriminator`` is the row's own id, so a subject retired twice does not
    collide with itself on the second retirement — which is not hypothetical:
    with ``fresh_tenant`` the subject provisions again under the derived slug
    the first retirement just freed, and that tenant can be retired in turn.

    Dashes are stripped from the id, not shortened away: a truncated
    discriminator would reintroduce exactly the collision this exists to
    prevent, and the full form still fits — 41 + 9 + 32 = 82 characters,
    inside the 100-character ``slug`` columns.
    """
    if not slug or not discriminator:
        raise ValueError("a retired slug needs both a slug and a discriminator")
    return f"{slug}{RETIRED_SLUG_MARKER}{discriminator.replace('-', '')}"


def retired_slug_pattern(slug: str) -> str:
    """SQL ``LIKE`` pattern matching every retired form of ``slug``.

    The derived slug is hex and dashes only, so it carries no ``LIKE``
    metacharacter and needs no escaping — asserted by the tests rather than
    assumed, because the day it stops being true this becomes a wildcard.
    """
    return f"{slug}{RETIRED_SLUG_MARKER}%"


def personal_key_of_slug(slug: str) -> str | None:
    """The derived key inside a personal tenant's slug, live or retired.

    The inverse of :func:`personal_org_slug` (composed with
    :func:`retired_slug`), and it is what lets an operator command addressed by
    **organization id** know it is looking at a personal tenant at all. Without
    it, pointing ``--organization-id`` at a company organization would retire a
    customer's tenant and stamp it with a marker naming a subject that does not
    own it.

    Returns None for anything that is not one of those two shapes. The check is
    exact rather than a prefix test: the key is 32 lowercase hex characters by
    construction, so a hand-made ``personal-acme`` slug is not a personal tenant
    and must not be read as one.
    """
    if not slug or not slug.startswith(PERSONAL_SLUG_PREFIX):
        return None
    remainder = slug[len(PERSONAL_SLUG_PREFIX) :]
    key, marker, _ = remainder.partition(RETIRED_SLUG_MARKER)
    if marker and not _:
        # "…-retired-" with nothing after it: not a slug this code produced.
        return None
    if len(key) != _DIGEST_BYTES * 2:
        return None
    if any(character not in "0123456789abcdef" for character in key):
        return None
    return key
