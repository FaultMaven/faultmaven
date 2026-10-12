"""The placeholder email a user store gives an account registered without one.

Both user stores (``DatabaseUserStore``, ``RedisUserStore``) fill a missing
email with ``<username>@faultmaven.example``. Such an address was derived FROM
the username, so the username is not derived from any email, and the
display-name rule (#1924: no name derived from an email) does not judge a name
against it. ``is_synthesized_email`` is the one test for that; migration 016
freezes the same predicate as SQL.
"""

from __future__ import annotations

from typing import Optional

#: The reserved (RFC 2606) domain of a synthesized email.
SYNTHESIZED_EMAIL_DOMAIN = "faultmaven.example"


def synthesized_email(username: str) -> str:
    """The placeholder email for an account registered with none."""
    return f"{username}@{SYNTHESIZED_EMAIL_DOMAIN}"


def is_synthesized_email(email: Optional[str]) -> bool:
    """Whether ``email`` is a placeholder a store synthesized, not a real
    address. Case-insensitive, like the migration's ``lower(email) LIKE``."""
    return bool(email) and email.lower().endswith(f"@{SYNTHESIZED_EMAIL_DOMAIN}")
