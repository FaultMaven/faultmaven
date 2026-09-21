"""The one implementation of "which token in a log line is a username".

Two consumers extract usernames from log content and they used to carry a
regex each:

* ``extractors/logs_extractor.py`` — the always-on entity profile rendered
  into ``file_extract`` / ``search_map``.
* ``entities/logs.py`` — the ``FAULTMAVEN_ENTITY_REGISTRY`` extractor whose
  ``EntityType.USER`` rows reach the investigation prompt through the
  Phase 4c entity-highlights block.

The second copy was written from the first and then never received any of
the guards the first grew, so on verbatim OpenSSH input it emitted
``ns.marryaldkfaczcz.com`` (a reverse-mapping PTR record), ``unknown`` and
``user`` (PAM structural words) and ``PnP`` / ``high-res`` (kernel
``for <thing>`` phrases) as login accounts (fm#522). A shared rule is the
only shape in which a guard added once holds in both places.

What stays with each consumer is *formatting* — how the counts are rendered
— which is what the duplication was originally there to keep independent.
The rule itself is here.

A username candidate survives when all of the following hold:

1. It came from ``user=<name>`` / ``user <name>`` (always applied), or from
   ``for [invalid user] <name>`` on a line carrying an explicit auth keyword.
   Kernel and service messages ("installed for high-res timesource",
   "activate device for PnP cards") have no such keyword, so the ``for``
   branch never sees them.
2. It is not a PAM/SSH structural word ("unknown", "publickey", "user", …).
3. It is not a reverse-DNS hostname — syslog's ``rhost`` carries the PTR
   record of the connecting address, and sshd echoes whatever login name a
   client offered, so a scanner offering a host-shaped name reaches the
   username branch.
4. It does not start with a digit and does not end in punctuation.
"""

from __future__ import annotations

import re

# ``user=<name>`` and ``user <name>``. Applied to every line.
#
# The leading ``(?<![\w=])`` is what separates a ``user`` that is a *key*
# from a ``user`` that is a *value*: in ``ruser=user rhost=203.0.113.9`` the
# second ``user`` is the value of ``ruser``, and a plain ``\b`` there let the
# next field's *name* be captured, putting ``rhost`` (and, from
# ``logname=user uid=0``, ``uid``) in the username list — the misclassification
# fm#522 reported. ``\w`` alone also covers the ``ruser``/``euser`` prefixes.
#
# ``(?:=|[ \t]+)`` requires the value to follow the delimiter directly. The
# older ``[= ]+`` let one repetition span ``"= "``, so an empty ``user=``
# field captured whatever key came next.
USER_FIELD_RE = re.compile(
    r"(?<![\w=])user(?:=|[ \t]+)([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
    re.IGNORECASE,
)

# ``for [invalid user] <name>`` — gated on AUTH_CONTEXT_RE.
USER_FOR_RE = re.compile(
    r"\bfor (?:invalid user )?([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
    re.IGNORECASE,
)

# Lines carrying these phrases are SSH/PAM auth events — the only context
# where "for <name>" is a reliable username signal.
AUTH_CONTEXT_RE = re.compile(
    r"Failed password|Accepted (?:password|publickey)|Invalid user"
    r"|authentication failure|session opened for",
    re.IGNORECASE,
)

# Reverse-DNS hostname pattern — syslog's rhost field stores the PTR record
# of the connecting IP (e.g. customer-187-141-143-180-sta.) which the entity
# extractor would otherwise count as a login username. Three or more numeric
# segments separated by hyphens or dots identify this pattern reliably.
REVERSE_DNS_RE = re.compile(r"\d{1,3}(?:[.-]\d{1,3}){2,}")

# SSH/TLS protocol terms the "for <word>" and "user <word>" branches would
# otherwise capture as usernames. These are structural keywords in auth log
# messages, never actual account names.
PROTOCOL_TERMS: frozenset[str] = frozenset(
    {
        "authentication",
        "publickey",
        "preauth",
        "key",
        "address",
        "the",
        "a",
        "an",
        # PAM/sshd structural words captured by the "user <word>" pattern
        # that are log-message tokens, never actual account names.
        "unknown",  # "check pass; user unknown" — PAM status, not username
        "invalid",  # "invalid user admin" — adjective, not username
        "user",  # "user=root" field name
        "none",
        "null",
        "request",  # "input_userauth_request" function name fragment
        "sshd",  # process name captured via "for sshd" in some PAM messages
    }
)


def is_username(candidate: str) -> bool:
    """Whether a captured token may be counted as a login account name."""
    return bool(
        candidate
        and candidate.lower() not in PROTOCOL_TERMS
        and not REVERSE_DNS_RE.search(candidate)
        and not candidate[0].isdigit()
        and candidate[-1].isalnum()
    )


def extract_usernames(line: str) -> list[str]:
    """Return the username mentions in one log line, in match order.

    Duplicates are kept: ``Failed password for invalid user test`` matches on
    both branches and counts as two mentions, which is what the entity
    profile has always reported.
    """
    candidates = USER_FIELD_RE.findall(line)
    if AUTH_CONTEXT_RE.search(line):
        candidates += USER_FOR_RE.findall(line)
    return [c for c in candidates if is_username(c)]
