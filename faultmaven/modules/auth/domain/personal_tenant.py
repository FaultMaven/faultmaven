"""What sign-up derives: the enterprise an address belongs to (#1045, ADR-017 D3).

Sign-up derives exactly one fact — the domain of the IdP-verified email — and
turns it into one of two enterprises:

* a **personal domain** (a consumer mail provider, ``PERSONAL_EMAIL_DOMAINS``)
  yields a **private enterprise per account**, named and slugged from the IdP
  subject, so the account is an island by construction: there is nobody else in
  its enterprise to invite;
* **every other domain** yields the **enterprise for that domain**, which the
  first sign-up from it creates and every later one joins. Joining grants
  nothing — a colleague's cases stay invisible until that colleague consents to
  a team (ADR-017 D2/D4).

Both derivations live here, together, because they are one decision with two
outcomes and a call site that could reach only one of them would be choosing by
omission.

The subject-derived half is one module so the name, the slug and the IdP
external id cannot drift apart — they are three renderings of a single derived
key, and the WorkOS half of provisioning is idempotent *because* that key is
deterministic.

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

#: What a personal enterprise is called. The architect's working decision
#: (ADR-016 D5); the owner may rename it, which is why every surface reads this
#: constant rather than spelling the word. It is deliberately not the person's
#: name or email: an enterprise named after its member leaks that member's
#: identity wherever an enterprise name is rendered.
PERSONAL_ENTERPRISE_NAME = "Personal"

#: Prefix on every derived identifier, so a personal tenant is recognisable as
#: one in the database, in WorkOS, and in operator output without consulting
#: ``sso_personal_enterprises``.
PERSONAL_SLUG_PREFIX = "personal-"

#: Prefix on a DOMAIN enterprise's slug. Distinct from the personal one and
#: unmistakable for it: ``personal_key_of_slug`` is what tells an operator
#: command it is looking at somebody's private tenant, and a domain enterprise
#: must never answer to that test — retiring acme.com's enterprise as though it
#: were one person's is the failure this prefix rules out by construction.
DOMAIN_SLUG_PREFIX = "domain-"

#: 16 bytes → 32 hex characters. With the prefix that is 41 characters, inside
#: the 100-character ``enterprises.slug`` column.
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


def personal_enterprise_slug(key: str) -> str:
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
    """The slug a retired enterprise is renamed to.

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

    The inverse of :func:`personal_enterprise_slug` (composed with
    :func:`retired_slug`), and it is what lets an operator command addressed by
    **enterprise id** know it is looking at a personal tenant at all. Without
    it, pointing ``--enterprise-id`` at a company enterprise would retire a
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


# =============================================================================
# The other half of D3: which enterprise a domain names
# =============================================================================


def email_domain(email: str) -> str | None:
    """The case-folded domain of ``email``, or ``None`` when there is none.

    Case-folded rather than lowercased: ``str.casefold`` is the comparison the
    Unicode standard defines for caseless matching, and a domain reaching here
    is IdP-verified but not necessarily ASCII. Lowercasing would leave two
    spellings of one domain as two enterprises.

    Splits on the LAST ``@``, because the local part may legitimately contain
    one inside quotes and the domain may not contain one at all. Anything that
    does not look like ``local@domain`` answers ``None``, which every caller
    treats as "no domain could be derived" rather than as a domain named "".
    """
    if not email:
        return None
    local, separator, domain = email.rpartition("@")
    if not separator or not local or not domain:
        return None
    domain = domain.strip().casefold()
    # A trailing dot is the DNS root and names the same domain; keeping it would
    # make ``acme.com.`` a second enterprise for the same company.
    domain = domain.rstrip(".")
    return domain or None


def is_personal_domain(domain: str | None, personal_domains) -> bool:
    """Whether ``domain`` yields a private enterprise per account (D3).

    **Exact, case-folded match against the configured list** — never a suffix
    test. A suffix rule would fold ``notgmail.com`` into ``gmail.com`` and put
    two unrelated companies' accounts in one enterprise, which is the one
    direction of this decision that has a security consequence.

    A ``domain`` of ``None`` — no domain could be derived — answers True: an
    address that names no organisation cannot be evidence that its owner
    belongs to one, so the account gets an enterprise of its own. That is the
    conservative direction; the permissive one would put every
    domainless identity into a single shared enterprise.
    """
    if domain is None:
        return True
    folded = domain.casefold()
    return any(candidate.strip().casefold() == folded for candidate in personal_domains)


def domain_enterprise_slug(domain: str) -> str:
    """The slug for a domain's enterprise — deterministic, and not a personal one.

    A digest rather than the domain itself: ``enterprises.slug`` is 100
    characters and a domain may be longer, may hold characters a slug should
    not, and — being the company's own name — is exactly the sort of value that
    should not be rendered wherever a slug is. The domain itself is stored, in
    ``enterprises.domain``, which is the column the lookup keys on; this is an
    identifier, not the lookup key.

    Domain-separated from :func:`personal_enterprise_slug` by its own prefix, so
    ``personal_key_of_slug`` cannot mistake a domain enterprise for somebody's
    private tenant.
    """
    if not domain:
        raise ValueError("a domain enterprise slug needs a domain")
    digest = hashlib.blake2b(
        domain.casefold().encode("utf-8"), digest_size=_DIGEST_BYTES
    ).hexdigest()
    return f"{DOMAIN_SLUG_PREFIX}{digest}"
