"""The name an account shows other people (#1924).

A display name is published to people other than its owner: a shared case's
creator and driver, the driver-candidate list, a verified runbook's
``verified_by``. So no display name may carry text derived from an email
address: the whole address, its local part, or a title-cased rendering of
either. For most corporate addresses the local part *is* the address minus the
domain.

Owner ruling (2026-10-12, #1924): an account with no real name gets ``Member``
plus the first four hex characters of its user id (``Member 3f9a``), stored as
the display name at sign-up. A name the IdP sends later overwrites it.

This module is the one owner of that fallback. Every writer of
``users.display_name`` that has no real name in hand calls
:func:`neutral_display_name`, and every writer handed a name from outside (an
IdP, a registration form) passes it through :func:`usable_display_name` first.
"""

from __future__ import annotations

from typing import Optional

#: ``users.display_name`` is ``String(200)``.
MAX_DISPLAY_NAME_LENGTH = 200

_NEUTRAL_PREFIX = "Member"


def usable_display_name(raw: object) -> Optional[str]:
    """``raw`` stripped and capped at the column width, or ``None`` when it
    holds no name.

    Whitespace-only counts as no name: the ``users`` CHECK
    ``LENGTH(TRIM(display_name)) > 0`` refuses it, so writing one fails the
    write rather than keeping the name already stored.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name:
        return None
    return name[:MAX_DISPLAY_NAME_LENGTH].rstrip()


def neutral_display_name(user_id: str) -> str:
    """The label of an account with no real name: ``Member`` and the first four
    characters of its user id (a ``uuid4``, so four hex digits)."""
    if not user_id:
        raise ValueError("a neutral display name needs the account's user id")
    return f"{_NEUTRAL_PREFIX} {user_id[:4]}"
