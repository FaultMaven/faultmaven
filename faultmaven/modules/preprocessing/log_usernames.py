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

Multiplicity is decided here too, and the same way for both consumers: a
mention is a LINE. The two branches overlap on ``for invalid user <name>``,
and counting each match ranked a scanner-sprayed account above a real one
(fm#1574). Both patterns are ``re.IGNORECASE``, so the de-duplication key is
case-folded as well — ``for Alice … user=alice`` is one account, once.
"""

from __future__ import annotations

import re

# ``user=<name>`` and ``user <name>``. Applied to every line.
#
# Two additions to the pattern fm#522 found, and the delimiter it already
# had. Both additions are needed: the lookbehind alone still read ``rhost``
# as an account, which is the exact symptom the issue reported.
#
# ``(?<![\w=])`` separates a ``user`` that is a *key* from a ``user`` that is
# a *value*. In ``ruser=user rhost=203.0.113.9`` the second ``user`` is the
# value of ``ruser``; a plain ``\b`` matched it as the key and captured the
# next field's NAME. ``\w`` also covers the ``ruser``/``euser`` prefixes.
#
# ``(?![\w.\-]*=)`` is what refuses a field NAME: a captured token
# immediately followed by ``=`` is the next key, not a value. One lookahead
# covers three shapes at once — the empty ``user=`` field, the bare ``user``
# token, and the ``for <key>=`` form on the other pattern.
#
# ``[= ]+`` is left exactly as it was. Narrowing it was the first attempt at
# the empty-``user=`` case: it cost ``user= alice`` and ``user = alice``,
# both real, and did not close the bare-``user`` case at all. With the
# lookahead in place it has nothing left to fix, and the mutation matrix says
# so — reverting the narrowing killed no test, which is what sent it back.
#
# What the lookahead cannot see, stated rather than discovered later: a bare
# ``user`` followed by a token that is not a ``key=`` — ``user rhost
# mail.example.com`` yields ``rhost``. Syslog key/value is ``=``-delimited,
# so no producer measured here emits that shape; the lookbehind covers the
# value-position half of it.
USER_FIELD_RE = re.compile(
    r"(?<![\w=])user[= ]+([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b(?![\w.\-]*=)",
    re.IGNORECASE,
)

# ``for [invalid user] <name>`` — gated on AUTH_CONTEXT_RE. Carries the same
# "not a field name" lookahead as the field pattern: the rule is a property of
# what a username is, not of which branch happened to capture it.
USER_FOR_RE = re.compile(
    r"\bfor (?:invalid user )?([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b(?![\w.\-]*=)",
    re.IGNORECASE,
)

# Lines carrying these phrases are SSH/PAM auth events — the only context
# where "for <name>" is a reliable username signal.
#
# The set is wider than the entity profile's original because fm#522 applied
# this gate to the entity-registry path, which had none, and the narrower set
# silently dropped usernames that path used to record: sshd's non-password
# authentication methods (``Failed publickey``, ``Failed none``, ``Failed
# keyboard-interactive/pam``), its ``Postponed <method>`` and ``maximum
# authentication attempts exceeded`` lines, and PAM's ``session closed for``.
# Measured cost of the widening: zero — none of eleven adversarial non-auth
# lines (kernel, systemd, nginx, dockerd, chronyd, postfix SASL, MySQL, the
# two POSSIBLE BREAK-IN shapes) is newly admitted.
AUTH_CONTEXT_RE = re.compile(
    r"(?:Failed|Accepted|Postponed) "
    r"(?:password|publickey|none|keyboard-interactive)"
    r"|maximum authentication attempts"
    r"|Invalid user"
    r"|authentication failure"
    r"|session (?:opened|closed) for",
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


# Trailing characters that mean the token was cut rather than ended. The
# 32-character cap on the capture groups can land mid-hostname, leaving a
# dangling ``.`` or ``-``; neither ends a real account name. ``_`` is NOT in
# this set — it is legal in a POSIX account name, and rejecting it dropped
# ``user=svc_`` (fm#522 review).
_TRUNCATION_TAIL = (".", "-")


def is_username(candidate: str) -> bool:
    """Whether a captured token may be counted as a login account name.

    Public, and tested directly: two of these clauses cannot be reached
    through ``USER_FIELD_RE`` / ``USER_FOR_RE``, whose capture groups both
    start ``[a-zA-Z_]`` and so never yield an empty string or a leading digit.
    They hold the contract for any other caller.
    """
    return bool(
        candidate
        and candidate.lower() not in PROTOCOL_TERMS
        and not REVERSE_DNS_RE.search(candidate)
        and not candidate[0].isdigit()
        and not candidate.endswith(_TRUNCATION_TAIL)
    )


def extract_usernames(line: str) -> list[str]:
    """Return the usernames mentioned in one log line, each once.

    **A mention is a LINE, not a match** (fm#1574). The two branches overlap
    on the shape a scanner produces most — ``Failed password for invalid user
    test`` is captured by ``USER_FIELD_RE`` (``user test``) *and* by
    ``USER_FOR_RE`` (``for invalid user test``) — so counting matches ranked
    an account that only ever appears as ``invalid user`` at twice the weight
    of one appearing as ``Accepted password for <name>``. That ranking is not
    cosmetic in either consumer: it orders the entity profile's ``Distinct
    usernames`` list, and on the registry path it is ``mention_count``, which
    ``list_top_entities`` sums (``SUM(mention_count) DESC``) and the Phase 4c
    highlights block prints into the investigation prompt.

    The same name reached twice on one line through genuinely different
    fields (``Failed password for alice … user=alice``) therefore counts
    once too. That is the deliberate reading of "per line": a line is one
    event, and one event is one mention of each account it names. Multiple
    lines still accumulate.

    De-duplication lives here rather than in either caller because this is
    where the two branches are concatenated — the only point at which "per
    line" is expressible once. There is deliberately no second,
    non-de-duplicating entry point: ``distinct_usernames`` was that, for the
    one release in which the two paths disagreed, and it is retired.

    **The key is case-folded; the value keeps the account's own spelling.**
    Both patterns are ``re.IGNORECASE``, so the two branches routinely
    disagree on case — ``Failed password for Alice … user=alice`` is one
    account on one line, and a case-sensitive key counted it twice, which is
    the very doubling this function exists to remove. Which spelling
    survives is **the first candidate in branch order**: ``USER_FIELD_RE``'s
    matches are considered before ``USER_FOR_RE``'s, so a ``user=`` field
    beats a ``for`` clause. That is deliberate rather than incidental — the
    field is the structured, canonical form, while the ``for`` clause echoes
    whatever the client offered.

    Two limits worth stating, because both are visible in the prompt:

    * Order is **branch order, not the line's left-to-right offsets**:
      ``for bob … user=alice`` returns ``['alice', 'bob']``. No caller
      depends on order today — both accumulate into a ``Counter`` — and the
      rendered profile sorts by count.
    * Case folding is **within one line only**. Across lines the spelling is
      the entity identity, so ``Alice`` on one line and ``alice`` on another
      remain two rows in ``case_entities``. Unifying them is a question about
      account identity (POSIX says they differ, Active Directory says they do
      not) and about a persisted key, not about this line's arithmetic.
    """
    candidates = USER_FIELD_RE.findall(line)
    if AUTH_CONTEXT_RE.search(line):
        candidates += USER_FOR_RE.findall(line)
    seen: set[str] = set()
    usernames: list[str] = []
    for candidate in candidates:
        if not is_username(candidate):
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        usernames.append(candidate)
    return usernames
