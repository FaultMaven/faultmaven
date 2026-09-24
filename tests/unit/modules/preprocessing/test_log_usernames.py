"""fm#522 — syslog fields and remote hostnames must not be counted as usernames.

Every test here drives one of the two **real entry points** rather than the
rule's internals:

* ``LogsAndErrorsExtractor().extract()`` — the always-on entity profile.
* ``extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, ...)`` — the
  ``FAULTMAVEN_ENTITY_REGISTRY`` path, whose ``EntityType.USER`` rows reach
  the investigation prompt through the Phase 4c entity-highlights block.

Both are asserted for every case: the defect fm#522 reported survived
because the rule was implemented twice and only one copy carried the
guards. A test that exercises one path proves nothing about the other.

Each case names the guard it defends. Removing that guard from
``preprocessing/log_usernames.py`` must turn the case red — verified by
mutation, guard by guard, before this file was committed.
"""

from __future__ import annotations

import ast
import pathlib
import re
import warnings
from collections import Counter

import pytest

import faultmaven
from faultmaven.models.api import DataType
from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.registry import (
    extract_entities_for_data_type,
)
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.log_usernames import (
    USER_FIELD_RE,
    USER_FOR_RE,
    extract_usernames,
    is_username,
)

# The rendered row states its unit — "lines", not "mentions" — because the
# count is lines and the neighbouring IP block counts occurrences (fm#1574).
_USER_ROW = re.compile(r"^ {4}(\S.*?): (\d+) lines")


def profile_usernames(content: str) -> list[str]:
    """Usernames as ``LogsAndErrorsExtractor.extract()`` renders them.

    Expanded by the rendered mention count, for the same reason
    ``registry_usernames`` is: collecting one entry per row silently caps
    every count at 1 and makes a multiplicity assertion unfailable.
    """
    result = LogsAndErrorsExtractor().extract(content)
    blob = (result.file_extract or "") + "\n" + (result.search_map or "")
    found: list[str] = []
    grabbing = False
    for line in blob.split("\n"):
        if line.strip().startswith("Distinct usernames"):
            grabbing = True
            continue
        if grabbing:
            match = _USER_ROW.match(line)
            if match is None:
                grabbing = False
                continue
            found.extend([match.group(1)] * int(match.group(2)))
    return found


def registry_usernames(content: str) -> list[str]:
    """USER rows as ``extract_entities_for_data_type`` emits them.

    Expanded by ``mention_count`` so multiplicity survives into the
    comparison; a bare list of values hides a counting regression.
    """
    found: list[str] = []
    for obs in extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, content):
        if obs.entity_type == EntityType.USER:
            found.extend([obs.entity_value] * obs.mention_count)
    return found


def both_paths(content: str) -> tuple[list[str], list[str]]:
    return profile_usernames(content), registry_usernames(content)


# ---------------------------------------------------------------------------
# The issue as filed: the rhost VALUE, in every shape the field can carry.
# ---------------------------------------------------------------------------

# Guard: none of these reaches the username branch at all, because `\buser`
# cannot match inside `ruser=`. The cases are here so a future widening of
# the user-field pattern cannot quietly re-open fm#522.
RHOST_VALUES = [
    pytest.param("218.188.2.4", id="ipv4"),
    pytest.param("220-135-151-1.HINET-IP.hinet.net", id="ptr-with-octets"),
    pytest.param("mail.example.com", id="ptr-no-octets"),
    pytest.param("gateway.corp.internal", id="internal-tld"),
    pytest.param("host-10-1.example.com", id="two-numeric-segments"),
    pytest.param("2001:db8::1", id="ipv6"),
    pytest.param("dsl-189-188-56-244-dyn.prod-infinitum.com.mx", id="isp-ptr"),
]


@pytest.mark.unit
@pytest.mark.parametrize("rhost", RHOST_VALUES)
def test_rhost_value_is_never_a_username(rhost: str) -> None:
    """A PAM auth-failure line must yield ``root`` and nothing from rhost."""
    content = (
        "Sep 21 10:00:01 web01 sshd[1001]: pam_unix(sshd:auth): authentication "
        f"failure; logname= uid=0 euid=0 tty=ssh ruser= rhost={rhost}  user=root\n"
    )
    profile, registry = both_paths(content)
    assert profile == ["root"], profile
    assert registry == ["root"], registry


@pytest.mark.unit
@pytest.mark.parametrize("rhost", RHOST_VALUES)
def test_rhost_value_is_never_a_username_without_a_user_field(rhost: str) -> None:
    """PAM omits ``user=`` when the account is unknown — nothing may be named."""
    content = (
        "Sep 21 10:00:06 web01 sshd[1006]: pam_unix(sshd:auth): authentication "
        f"failure; logname= uid=0 euid=0 tty=ssh ruser= rhost={rhost}\n"
    )
    profile, registry = both_paths(content)
    assert profile == [], profile
    assert registry == [], registry


# ---------------------------------------------------------------------------
# The issue as filed, second reading: the rhost FIELD NAME.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_field_names_are_never_usernames() -> None:
    """Guard: the ``(?<![\\w=])`` lookbehind on the user-field pattern.

    ``ruser=user`` puts the token ``user`` in *value* position. With a plain
    ``\\b`` the pattern matched it as the ``user`` key and captured the next
    field's NAME, so ``rhost`` — and, from ``logname=user uid=0``, ``uid`` —
    were reported as login accounts. That is fm#522's title, verbatim.
    """
    content = (
        "Sep 21 11:00:01 web01 sudo: pam_unix(sudo:auth): authentication failure; "
        "logname=user uid=1000 euid=0 tty=/dev/pts/0 ruser=user "
        "rhost=mail.example.com  user=root\n"
    )
    profile, registry = both_paths(content)
    assert profile == ["root"], profile
    assert registry == ["root"], registry
    assert "rhost" not in profile and "rhost" not in registry
    assert "uid" not in profile and "uid" not in registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            "Sep 21 10:00:00 h sshd[1]: pam_unix(sshd:auth): authentication "
            "failure; logname= uid=0 user rhost=mail.example.com",
            id="bare-user-then-rhost",
        ),
        pytest.param(
            "Sep 21 10:00:00 h sshd[1]: pam_unix(sshd:auth): authentication "
            "failure; user logname=x rhost=1.2.3.4",
            id="bare-user-then-logname",
        ),
    ],
)
def test_bare_user_token_does_not_capture_the_next_key(line: str) -> None:
    """Guard: the ``(?![\\w.\\-]*=)`` lookahead after the capture.

    The ``(?<![\\w=])`` lookbehind only closed the ``=user`` door. A *bare*
    ``user`` token still satisfied the whitespace alternative, and the capture
    then ran to the next field's name and stopped at its ``=`` — so ``rhost``
    was still read as a login account, which is fm#522's title. The sibling
    test above passed throughout, because it only ever exercised the
    ``ruser=user`` value-position shape.
    """
    profile, registry = both_paths(line + "\n")
    assert profile == [], profile
    assert registry == [], registry


@pytest.mark.unit
def test_empty_user_field_does_not_capture_the_next_key() -> None:
    """Guard: ``(?:=|[ \\t]+)`` in place of ``[= ]+``.

    One repetition of ``[= ]+`` could span ``"= "``, so an empty ``user=``
    swallowed its own delimiter and the following key became the username.
    """
    content = (
        "Sep 21 11:00:03 web01 sshd[2003]: pam_unix(sshd:auth): authentication "
        "failure; logname= uid=0 euid=0 tty=ssh user= rhost=mail.example.com\n"
    )
    profile, registry = both_paths(content)
    assert profile == [], profile
    assert registry == [], registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("user=alice", id="tight"),
        pytest.param("user= alice", id="space-after"),
        pytest.param("user=  alice", id="two-spaces-after"),
        pytest.param("user = alice", id="spaces-both-sides"),
        pytest.param("user alice", id="no-equals"),
        pytest.param("user  alice", id="two-spaces-no-equals"),
    ],
)
def test_every_user_field_spelling_still_yields_the_account(spelling: str) -> None:
    """The delimiter is left exactly as fm#522 found it, and this says why.

    The first attempt at the empty-``user=`` case narrowed ``[= ]+`` to
    ``(?:=[ \\t]*|[ \\t]+)``. That silently cost ``user= alice`` and
    ``user = alice`` — both real — and closed nothing the lookahead does not
    close, which the mutation matrix showed by killing no test when the
    narrowing was reverted. These are the spellings that narrowing broke.
    """
    line = (
        "Sep 21 11:00:20 web01 sshd[1]: pam_unix(sshd:auth): authentication "
        f"failure; {spelling}"
    )
    profile, registry = both_paths(line + "\n")
    assert profile == ["alice"], profile
    assert registry == ["alice"], registry


@pytest.mark.unit
def test_value_position_user_is_never_the_key() -> None:
    """Guard: the ``(?<![\\w=])`` lookbehind, isolated.

    Once the lookahead landed, every *realistic* input that the lookbehind
    used to hold was also held by the lookahead — re-running the mutation
    matrix showed the lookbehind killing nothing, which is a guard the next
    change can delete in silence. This input isolates it: a value-position
    ``user`` followed by a bare token rather than another ``key=``. No
    producer measured here emits that shape (PAM's stream is all
    ``key=value``), so this pins the rule's intent — a ``user`` in value
    position is never the key — rather than a measured symptom.
    """
    line = (
        "Sep 21 11:00:09 web01 sudo: pam_unix(sudo:auth): authentication "
        "failure; logname=user tty=pts/0 ruser=user root"
    )
    profile, registry = both_paths(line + "\n")
    assert profile == [], profile
    assert registry == [], registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            "Sep 21 11:00:10 web01 sshd[1]: pam_unix(sshd:auth): "
            "authentication failure for logname=alice",
            id="for-logname",
        ),
        pytest.param(
            "Sep 21 11:00:11 web01 sshd[2]: Failed password for rhost=1.2.3.4",
            id="for-rhost",
        ),
    ],
)
def test_for_branch_does_not_capture_a_field_name(line: str) -> None:
    """Guard: the same ``(?![\\w.\\-]*=)`` lookahead, on the ``for`` branch.

    "A username is not a field name" is a property of the rule, not of
    whichever branch captured the token, so both patterns carry it. Isolated
    for the same reason as the test above — without these two inputs the
    ``for``-branch copy killed nothing in the matrix.
    """
    profile, registry = both_paths(line + "\n")
    assert profile == [], profile
    assert registry == [], registry


# ---------------------------------------------------------------------------
# The class: anything host-shaped or structural reaching the username list.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reverse_dns_login_name_is_rejected() -> None:
    """Guard: ``REVERSE_DNS_RE``.

    sshd echoes whatever login name a client offered, so a scanner offering a
    host-shaped name puts a PTR record in username position on an
    auth-context line — the one shape that reaches the guard.
    """
    content = (
        "Dec 10 06:55:46 LabSZ sshd[24200]: Failed password for invalid user "
        "host-187-141-143-180-sta.mx from 173.234.31.186 port 38926 ssh2\n"
    )
    profile, registry = both_paths(content)
    assert profile == [], profile
    assert registry == [], registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            "Dec 10 09:12:49 LabSZ sshd[24498]: reverse mapping checking getaddrinfo "
            "for ns.marryaldkfaczcz.com [173.234.31.186] failed - POSSIBLE BREAK-IN "
            "ATTEMPT!",
            id="ptr-no-octets",
        ),
        pytest.param(
            "Dec 10 09:32:20 LabSZ sshd[24680]: reverse mapping checking getaddrinfo "
            "for customer-187-141-143-180-sta.prod-infinitum.com.mx "
            "[187.141.143.180] failed - POSSIBLE BREAK-IN ATTEMPT!",
            id="ptr-with-octets",
        ),
    ],
)
def test_reverse_mapping_hostname_is_not_a_username(line: str) -> None:
    """Guard: ``AUTH_CONTEXT_RE`` gating the ``for <name>`` branch.

    Verbatim OpenSSH lines. The registry path had no gate and reported both
    of these hostnames as login accounts — fm#522's class, on real input.

    Only ``ptr-no-octets`` measures the gate on its own: dropping the gate
    still leaves ``REVERSE_DNS_RE`` to catch ``customer-187-141-143-180-sta.``,
    which is the belt-and-braces working, and is why a PTR with no numeric
    octets had to be in this list.
    """
    profile, registry = both_paths(line + "\n")
    assert profile == [], profile
    assert registry == [], registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,token",
    [
        pytest.param(
            "Sep 21 12:00:01 web01 kernel: Time: tsc clocksource has been installed "
            "for high-res timesource",
            "high-res",
            id="kernel-high-res",
        ),
        pytest.param(
            "Sep 21 12:00:02 web01 kernel: pnp: Failed to activate device for "
            "PnP cards",
            "PnP",
            id="kernel-pnp",
        ),
    ],
)
def test_kernel_for_phrases_are_not_usernames(line: str, token: str) -> None:
    """Guard: ``AUTH_CONTEXT_RE``. No auth keyword, so ``for <word>`` is off."""
    profile, registry = both_paths(line + "\n")
    assert token not in profile, profile
    assert token not in registry, registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,token",
    [
        pytest.param(
            "Jun 23 07:07:00 combo sshd(pam_unix)[26635]: check pass; user unknown",
            "unknown",
            id="pam-user-unknown",
        ),
        pytest.param(
            "2026-09-21T10:00:00Z 12 [Note] Access denied for user 'root'@'localhost'",
            "user",
            id="mysql-for-user",
        ),
    ],
)
def test_pam_structural_words_are_not_usernames(line: str, token: str) -> None:
    """Guard: ``PROTOCOL_TERMS``.

    ``pam-user-unknown`` measures it alone. ``mysql-for-user`` is held by two
    guards at once — the auth-context gate keeps ``for user`` out of reach,
    and ``PROTOCOL_TERMS`` catches it if the gate ever opens — so it goes red
    only when both are removed. It is here as a real-world negative, not as a
    single guard's witness.
    """
    profile, registry = both_paths(line + "\n")
    assert token not in profile, profile
    assert token not in registry, registry


# ---------------------------------------------------------------------------
# The narrowing must not cost a real username.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,expected",
    [
        pytest.param(
            "Jul 27 14:41:59 combo sshd(pam_unix)[26483]: Failed password for "
            "invalid user test from 211.72.151.162 port 55568 ssh2",
            "test",
            id="failed-password-invalid-user",
        ),
        pytest.param(
            "Dec 10 11:04:45 LabSZ sshd[25138]: Accepted password for fztu from "
            "119.137.62.142 port 49116 ssh2",
            "fztu",
            id="accepted-password",
        ),
        pytest.param(
            "Jun 15 04:06:18 combo su(pam_unix)[21416]: session opened for user "
            "cyrus by (uid=0)",
            "cyrus",
            id="session-opened-for-user",
        ),
        pytest.param(
            "Sep 21 10:00:00 web01 sshd[1234]: pam_faillock(sshd:auth): Consecutive "
            "login failures for user root account temporarily locked",
            "root",
            id="faillock-for-user",
        ),
        pytest.param(
            "Sep 21 10:00:07 web01 su: pam_unix(su-l:auth): authentication failure; "
            "logname=alice uid=1000 euid=0 tty=pts/2 ruser=alice rhost=  user=root",
            "root",
            id="empty-rhost-then-user",
        ),
    ],
)
def test_real_usernames_still_captured(line: str, expected: str) -> None:
    profile, registry = both_paths(line + "\n")
    assert expected in profile, profile
    assert expected in registry, registry


@pytest.mark.unit
def test_both_paths_agree_on_a_mixed_auth_log() -> None:
    """The two implementations became one; their answers must not diverge.

    Membership *and* multiplicity now match, because fm#1574 settled both
    paths to the same semantics at the extraction root: a mention is a LINE.
    Until then they deliberately differed — the profile counted *matches* and
    the registry counted lines — and this case is where that showed.

    The counts are pinned explicitly as well as compared between the paths.
    A between-paths comparison alone passes when both drift the same way, and
    a set comparison discards multiplicity entirely, which is how a doubling
    regression got through once already.
    """
    content = "\n".join(
        [
            "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; "
            "logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=218.188.2.4 ",
            "Jun 15 02:04:59 combo sshd(pam_unix)[20882]: authentication failure; "
            "logname= uid=0 euid=0 tty=NODEVssh ruser= "
            "rhost=220-135-151-1.HINET-IP.hinet.net  user=root",
            "Jun 15 04:06:18 combo su(pam_unix)[21416]: session opened for user "
            "cyrus by (uid=0)",
            "Jun 23 07:07:00 combo sshd(pam_unix)[26635]: check pass; user unknown",
            "Dec 10 09:12:49 LabSZ sshd[24498]: reverse mapping checking getaddrinfo "
            "for ns.marryaldkfaczcz.com [173.234.31.186] failed - POSSIBLE BREAK-IN "
            "ATTEMPT!",
            "Jul 27 14:41:59 combo sshd(pam_unix)[26483]: Failed password for "
            "invalid user test from 211.72.151.162 port 55568 ssh2",
        ]
    )
    profile, registry = both_paths(content + "\n")
    assert set(profile) == set(registry) == {"root", "cyrus", "test"}
    # "Failed password for invalid user test" matches on BOTH branches —
    # USER_FIELD_RE on "user test", USER_FOR_RE on "for invalid user test".
    # The profile rendered "test: 2 mentions" until fm#1574; one line is now
    # one mention on both paths, so the two dicts are the same dict.
    expected = {"root": 1, "cyrus": 1, "test": 1}
    assert Counter(profile) == expected, profile
    assert Counter(registry) == expected, registry
    assert Counter(profile) == Counter(registry)


# ---------------------------------------------------------------------------
# The registry path's before/after, pinned as data rather than prose.
# ---------------------------------------------------------------------------

# fm#522 moved ``entities/logs.py`` onto the shared rule, which newly applied
# the auth-context gate to that path. These are lines the registry path
# recorded BEFORE that move, taken from ``origin/main``'s local ``_USER_RE``.
# Each must still be recorded, or the move silently cost the entity registry
# usernames it used to have. Three of them regressed on the first attempt and
# were only found because this table was written out.
REGISTRY_MUST_STILL_RECORD = [
    pytest.param(
        "Dec 10 07:10:00 LabSZ sshd[1]: error: maximum authentication attempts "
        "exceeded for root from 1.2.3.4 port 22 ssh2 [preauth]",
        ["root"],
        id="maximum-authentication-attempts",
    ),
    pytest.param(
        "Dec 10 07:10:01 LabSZ sshd[2]: Postponed publickey for alice from "
        "1.2.3.4 port 22 ssh2 [preauth]",
        ["alice"],
        id="postponed-publickey",
    ),
    pytest.param(
        "Dec 10 07:10:02 LabSZ sshd[3]: Failed publickey for bob from 1.2.3.4 "
        "port 22 ssh2",
        ["bob"],
        id="failed-publickey",
    ),
    pytest.param(
        "Dec 10 07:10:04 LabSZ sshd[4]: Failed none for invalid user carol "
        "from 1.2.3.4 port 22 ssh2",
        ["carol"],
        id="failed-none",
    ),
    pytest.param(
        "Dec 10 07:10:05 LabSZ sshd[5]: Failed keyboard-interactive/pam for "
        "dave from 1.2.3.4 port 22 ssh2",
        ["dave"],
        id="failed-keyboard-interactive",
    ),
    pytest.param(
        "Sep 21 10:00:00 h sshd[6]: pam_unix(sshd:session): session closed for "
        "user frank",
        ["frank"],
        id="session-closed-for-user",
    ),
    pytest.param(
        "Sep 21 10:00:01 h sshd[7]: pam_unix(sshd:auth): authentication "
        "failure; user=svc_",
        ["svc_"],
        id="trailing-underscore-account",
    ),
    pytest.param(
        "Sep 21 10:00:02 h sshd[8]: pam_unix(sshd:auth): authentication "
        "failure; user=  alice",
        ["alice"],
        id="spaces-after-equals",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("line,expected", REGISTRY_MUST_STILL_RECORD)
def test_registry_path_still_records_what_it_recorded_before(
    line: str, expected: list[str]
) -> None:
    """Applying the auth gate to the registry path must not cost it usernames."""
    assert registry_usernames(line + "\n") == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,was,now",
    [
        pytest.param(
            "Jun 15 04:06:18 combo su(pam_unix)[1]: session opened for user "
            "cyrus by (uid=0)",
            "user",
            "cyrus",
            id="session-opened",
        ),
        pytest.param(
            "Sep 21 10:00:00 h sshd[2]: pam_unix(sshd:session): session closed "
            "for user frank",
            "user",
            "frank",
            id="session-closed",
        ),
    ],
)
def test_registry_now_records_the_account_not_the_word_user(
    line: str, was: str, now: str
) -> None:
    """A deliberate registry-path improvement, pinned so it is not incidental.

    ``origin/main``'s local pattern was a single alternation: on
    ``session opened for user cyrus`` its ``for`` branch matched first,
    consumed ``for user``, returned the literal ``user`` — and scanning
    resumed past ``cyrus``, so the real account was never seen. The shared
    rule runs the two branches separately, so ``cyrus`` is captured by the
    field branch and the literal ``user`` is dropped by ``PROTOCOL_TERMS``.
    """
    found = registry_usernames(line + "\n")
    assert found == [now], found
    assert was not in found


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,expected",
    [
        pytest.param(
            "Jul 27 14:41:59 combo sshd[1]: Failed password for invalid user "
            "test from 211.72.151.162 port 55568 ssh2",
            {"test": 1},
            id="failed-password-invalid-user",
        ),
        pytest.param(
            "Dec 10 09:32:20 LabSZ sshd[2]: Failed password for invalid user "
            "admin from 1.2.3.4 port 22 ssh2",
            {"admin": 1},
            id="invalid-user-admin",
        ),
        # The two branches reaching the same name through two GENUINELY
        # different fields — a "for" clause and a "user=" field — not the
        # "invalid user" overlap. Per-line semantics counts it once; see
        # ``test_one_line_is_one_mention_even_across_different_fields``.
        pytest.param(
            "Dec 10 09:33:00 LabSZ sshd[3]: Failed password for alice from "
            "1.2.3.4 port 2222 ssh2 user=alice",
            {"alice": 1},
            id="same-user-in-two-different-fields",
        ),
        # De-duplication must be by VALUE, not a blanket one-per-line: two
        # different accounts named on one line are two mentions.
        pytest.param(
            "Dec 10 09:34:00 LabSZ sshd[4]: Failed password for bob from "
            "1.2.3.4 port 22 ssh2 user=alice",
            {"alice": 1, "bob": 1},
            id="two-different-users-one-line",
        ),
        # And it must be PER LINE, not per file: the same account on two
        # lines is two mentions. A ``dict.fromkeys`` hoisted out of the
        # per-line call would floor every account at 1 and this is what
        # notices.
        pytest.param(
            "Dec 10 09:35:00 LabSZ sshd[5]: Failed password for carol from "
            "1.2.3.4 port 22 ssh2\n"
            "Dec 10 09:35:01 LabSZ sshd[6]: Failed password for carol from "
            "1.2.3.4 port 22 ssh2",
            {"carol": 2},
            id="same-user-on-two-lines-accumulates",
        ),
    ],
)
def test_mention_counts_are_per_line_on_both_paths(
    line: str, expected: dict[str, int]
) -> None:
    """One line, one mention — on the registry path AND the entity profile.

    Both branches match the same token on an ``invalid user`` line, so the
    concatenation used to return it twice. fm#522 had to stop that reaching
    the registry, where it was a fresh doubling; fm#1574 settled the profile
    the same way and folded the de-duplication into ``extract_usernames``, so
    the expectation below is now one expectation for both paths.

    ``expected`` is what each path reports: ``{"test": 1}`` on the line the
    profile used to render as ``test: 2 mentions``.

    The count is not cosmetic on either path. On the registry it is
    ``mention_count``: ``list_top_entities`` orders by
    ``SUM(mention_count) DESC`` and ``fetch_entity_highlights`` prints the
    top five with their counts into the investigation prompt. On the profile
    it orders the rendered ``Distinct usernames`` list. Either way a
    scanner-sprayed ``invalid user`` account outranked a real one 2:1.
    """
    obs = {
        o.entity_value: o.mention_count
        for o in extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, line + "\n")
        if o.entity_type == EntityType.USER
    }
    assert obs == expected
    assert Counter(profile_usernames(line + "\n")) == expected


@pytest.mark.unit
def test_one_line_is_one_mention_even_across_different_fields() -> None:
    """The judgement call fm#1574 made, written down so it is not incidental.

    ``Failed password for invalid user test`` is the easy case: one *event*,
    named once, captured twice because the two branches overlap. Nobody wants
    that counted as two.

    This line is the case someone will eventually ask about — the same
    account reached through two fields that are not an overlap at all, a
    ``for`` clause and a ``user=`` field::

        Failed password for alice from 10.0.0.1 port 2222 ssh2 user=alice

    A mention is a LINE, so it counts **once**. A line is one event, and one
    event is one mention of each account it names; how many times that line's
    own syntax repeats the name is a property of the log format, not of the
    account's activity. Counting it twice is what let a scanner-sprayed
    account outrank a real one, and that is true whichever pair of fields
    produced the repeat.

    What this does NOT do: collapse two different accounts on one line, or
    collapse the same account across lines. Both are pinned as parameters of
    ``test_mention_counts_are_per_line_on_both_paths``.
    """
    line = (
        "Dec 10 09:33:00 LabSZ sshd[3]: Failed password for alice from "
        "1.2.3.4 port 2222 ssh2 user=alice\n"
    )

    # Both branches do reach the name — the de-duplication is what makes it
    # one, not a gap in the patterns. Without this the test would pass on a
    # line that simply never matched twice.
    assert USER_FIELD_RE.findall(line) == ["alice"]
    assert USER_FOR_RE.findall(line) == ["alice"]

    profile, registry = both_paths(line)
    assert Counter(profile) == {"alice": 1}, profile
    assert Counter(registry) == {"alice": 1}, registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,expected,winner",
    [
        pytest.param(
            "Dec 10 09:34:00 h sshd[4]: Failed password for Alice from "
            "1.2.3.4 port 22 ssh2 user=alice",
            {"alice": 1},
            "alice",
            id="for-clause-uppercase-field-lowercase",
        ),
        pytest.param(
            "Dec 10 09:34:00 h sshd[4]: Failed password for alice from "
            "1.2.3.4 port 22 ssh2 user=Alice",
            {"Alice": 1},
            "Alice",
            id="for-clause-lowercase-field-uppercase",
        ),
        pytest.param(
            "Dec 10 09:34:00 h sshd[4]: Failed password for invalid user "
            "TEST from 1.2.3.4 port 22 ssh2",
            {"TEST": 1},
            "TEST",
            id="the-overlap-shape-preserves-its-own-case",
        ),
    ],
)
def test_the_dedup_key_is_case_folded_and_the_field_spelling_wins(
    line: str, expected: dict[str, int], winner: str
) -> None:
    """One account on one line stays one even when the branches disagree on case.

    Both patterns are ``re.IGNORECASE``, so ``for Alice … user=alice`` reaches
    the concatenation as two candidates that differ only in case. A
    case-sensitive key counted that as two mentions — the exact doubling
    fm#1574 exists to remove, surviving the fix. The ``for`` clause echoing a
    different case from the ``user=`` field is the normal shape in
    application and Windows-style auth logs.

    Which spelling survives is pinned, because it is what the prompt shows:
    the **first candidate in branch order**, and ``USER_FIELD_RE`` is
    considered before ``USER_FOR_RE``, so the structured ``user=`` field
    beats the ``for`` clause whichever way round the cases fall. Both
    directions are parametrized so the test cannot pass by accident on a
    rule that simply prefers lowercase.
    """
    assert extract_usernames(line) == [winner]
    profile, registry = both_paths(line + "\n")
    assert Counter(profile) == expected, profile
    assert Counter(registry) == expected, registry


@pytest.mark.unit
def test_case_folding_does_not_reach_across_lines() -> None:
    """The declared limit of the rule above, pinned so a widening is deliberate.

    Within a line the key is case-folded. ACROSS lines the spelling is the
    entity identity — ``case_entities`` is keyed on ``entity_value`` — and
    whether ``Alice`` and ``alice`` are one account is a question about
    POSIX versus Active Directory semantics and about a persisted key, not
    about this line's arithmetic. Two rows, one each.
    """
    content = (
        "Dec 10 09:34:00 h sshd[1]: Failed password for Alice from 1.2.3.4 "
        "port 22 ssh2\n"
        "Dec 10 09:35:00 h sshd[2]: Failed password for alice from 1.2.3.4 "
        "port 22 ssh2\n"
    )
    profile, registry = both_paths(content)
    assert Counter(profile) == {"Alice": 1, "alice": 1}, profile
    assert Counter(registry) == {"Alice": 1, "alice": 1}, registry


@pytest.mark.unit
@pytest.mark.parametrize(
    "ending",
    [
        pytest.param("\n", id="LF"),
        pytest.param("\r", id="CR"),
        pytest.param("\r\n", id="CRLF"),
    ],
)
def test_a_line_is_a_line_under_every_line_ending(ending: str) -> None:
    """Per-line counting is only as right as what the caller calls a line.

    Both callers split on ``content.split("\\n")``, which reads a bare-``\r``
    file — classic Mac endings, and what some exporters still emit — as ONE
    physical line. Counting matches papered over that (five records gave 5);
    counting lines exposed it (five records gave **1**), destroying on that
    input the very ranking fm#1574 exists to protect.

    Five records naming one account must be five mentions under all three
    endings. The error-line annotation is asserted too, because the profile's
    line index has to stay in step with the error-line set that
    ``extract()`` computes — splitting the two loops differently would give
    the right count with the wrong ``(N on error lines)`` beside it.
    """
    record = (
        "Dec 10 09:3{i}:00 h sshd[{i}]: error: Failed password for alice "
        "from 1.2.3.4 port 22 ssh2"
    )
    content = ending.join(record.format(i=i) for i in range(5)) + ending

    profile, registry = both_paths(content)
    assert Counter(profile) == {"alice": 5}, (ending, profile)
    assert Counter(registry) == {"alice": 5}, (ending, registry)

    rendered = LogsAndErrorsExtractor().extract(content)
    blob = (rendered.file_extract or "") + "\n" + (rendered.search_map or "")
    assert "alice: 5 lines  (5 on error lines)" in blob, blob


@pytest.mark.unit
def test_the_rendered_username_block_states_its_unit() -> None:
    """The count changed unit, so the search map has to say so.

    This block is a search map: the number sets the model's expectation for
    what ``search_file`` will return. It now counts LINES while the
    ``Distinct IPs`` block beside it counts occurrences, and on
    ``invalid user test`` it reports 1 where the text holds two occurrences.
    An unlabelled number next to a differently-labelled one is a trap.
    """
    content = (
        "Jul 27 14:4{i}:00 combo sshd[264{i}]: Failed password for invalid "
        "user test from 211.72.151.162 port 555{i}8 ssh2"
    )
    blob = str(
        LogsAndErrorsExtractor()
        .extract("\n".join(content.format(i=i) for i in range(5)) + "\n")
        .search_map
    )
    assert "count = LINES the account appears on, not text occurrences" in blob
    assert "test: 5 lines" in blob
    assert "test: 5 mentions" not in blob


@pytest.mark.unit
def test_file_summary_root_login_attempts_counts_lines() -> None:
    """``Includes N root login attempts.`` is the one ABSOLUTE number affected.

    Every other consumer of this count uses it as a rank. ``_build_summary``
    prints it as a quantity the model may quote directly, and it reads the
    same ``user_all_counts`` the ranking does — so it moved with fm#1574 and
    nothing covered it. Five ``Failed password for invalid user root`` lines:
    ``main`` rendered **10** (both branches matched each line), this renders
    **5**.
    """
    record = (
        "Jul 27 14:4{i}:00 combo sshd(pam_unix)[264{i}]: Failed password for "
        "invalid user root from 211.72.151.162 port 555{i}8 ssh2"
    )
    summary = str(
        LogsAndErrorsExtractor()
        .extract("\n".join(record.format(i=i) for i in range(5)) + "\n")
        .file_extract
    )
    assert "Includes 5 root login attempts." in summary, summary
    assert "Includes 10 root login attempts." not in summary


@pytest.mark.unit
def test_returned_order_is_branch_order_not_text_order() -> None:
    """The docstring's order contract, pinned because it reads as the other one.

    ``USER_FIELD_RE``'s matches come before ``USER_FOR_RE``'s, so the list is
    NOT the line's left-to-right ordering: ``for bob … user=alice`` yields
    ``['alice', 'bob']``. No caller depends on it — both accumulate into a
    ``Counter``, and the profile renders by count — but it is the contract
    the next caller reads, and it is what decides the case tie-break above.
    """
    line = (
        "Dec 10 09:34:00 h sshd[4]: Failed password for bob from 1.2.3.4 "
        "port 22 ssh2 user=alice"
    )
    assert line.index("bob") < line.index("alice")
    assert extract_usernames(line) == ["alice", "bob"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate,accepted",
    [
        pytest.param("svc_", True, id="trailing-underscore-is-legal-posix"),
        pytest.param("root", True, id="plain"),
        pytest.param("host-187-141-143-180-sta.mx", False, id="reverse-dns"),
        pytest.param("unknown", False, id="protocol-term"),
        pytest.param("9abc", False, id="leading-digit"),
        pytest.param("truncated.", False, id="trailing-dot-from-truncation"),
        pytest.param("truncated-", False, id="trailing-hyphen-from-truncation"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_username_predicate(candidate: str, accepted: bool) -> None:
    """``is_username`` is public, so its contract is tested directly.

    Two of its clauses are unreachable through the two patterns, whose capture
    groups both start ``[a-zA-Z_]`` and so can never yield a leading digit or
    an empty string. Without this test the mutation matrix would read as
    complete while saying nothing about them.
    """
    assert is_username(candidate) is accepted


# ---------------------------------------------------------------------------
# N: how many implementations of the rule exist.
# ---------------------------------------------------------------------------

_RULE_HOME = pathlib.Path("faultmaven/modules/preprocessing/log_usernames.py")
_PACKAGE_ROOT = pathlib.Path(faultmaven.__file__).resolve().parent

# Where the rule can be violated. A scan that stops looking at these
# directories passes vacuously, so it must prove it reached them.
_MUST_SCAN = (
    pathlib.Path("faultmaven/modules/preprocessing/extractors"),
    pathlib.Path("faultmaven/modules/preprocessing/entities"),
)

# The scan must see every shape this codebase actually writes a regex in, not
# just the one the rule happens to use. Measured on the tree when it was
# written: 205 ``re.compile`` sites, 191 inline ``re.<method>(pattern, ...)``
# sites, 6 ``re.compile(<module-level const>)`` sites, 2 ``regex.<method>``
# sites and 1 ``import re as _re``. A scan watching only ``re.compile`` with a
# literal misses 199 of them, including the inline form, which is the house
# idiom.
_RE_MODULES = frozenset({"re", "regex"})
_RE_FUNCTIONS = frozenset(
    {
        "compile",
        "findall",
        "search",
        "match",
        "fullmatch",
        "finditer",
        "sub",
        "subn",
        "split",
    }
)
_CALL_SPELLINGS = tuple(f"{name}(" for name in sorted(_RE_FUNCTIONS))

# Patterns mentioning "user" that are NOT implementations of the rule. Read
# individually and kept by exact text, so a rewrite of one has to come back
# through here. The scan fails when an entry stops matching anything, because
# a stale allowlist is how a guard goes quiet.
#
# There is deliberately no "has a capture group" test. ``re.findall`` and
# ``re.finditer`` return group 0 when a pattern has none, so
# ``re.findall(r"(?<=\buser=)[a-zA-Z_][\w.\-]{0,31}", line)`` is a complete
# second implementation with no group at all — and a group test also fired
# spuriously on escaped parens (``user\(id\)``). Allowlisting three read
# exceptions is the cheaper and more honest trade.
_NOT_THE_RULE = {
    (
        "faultmaven/modules/preprocessing/extractors/command_output_extractor.py",
        r"PID\s+USER\s+%CPU\s+%MEM\s+VSZ\s+RSS",
    ): "matches the ps(1) header row, extracts no name",
    # The sshd event rules moved to ``sshd_auth`` and are matched at the
    # message start there (fm#1657). The address-slot regexes beside them
    # read past the client's login name to the address and deliberately
    # spell no ``user``, so they are not here.
    (
        "faultmaven/modules/preprocessing/extractors/sshd_auth.py",
        r"(?:[\w-]+:\s+)?"
        r"(?:(?:Failed|Accepted|Partial|Postponed)\s+\S+\s+for\s+"
        r"|(?:maximum authentication attempts exceeded|Too many authentication failures)"
        r"\s+for\s+"
        r"|(?:Connection (?:closed|reset) by|Connection from|Disconnected from"
        r"|Disconnecting|Received disconnect from|Timeout, client not responding from"
        r"|Unable to negotiate with)\s+"
        r")?invalid user\b",
    ): "presence test for the invalid_user counter, extracts no name",
    (
        "faultmaven/modules/preprocessing/extractors/sshd_auth.py",
        r"session opened for user\b",
    ): "event matcher for the session counter, extracts no name",
    (
        "faultmaven/modules/preprocessing/extractors/sshd_auth.py",
        r"Invalid user\s",
    ): "selects the getpwnamallow address-slot shape, extracts no name",
    (
        "faultmaven/modules/preprocessing/extractors/sshd_auth.py",
        r"(?<![^\s,;|])(?:Failed|Accepted|Partial|Postponed|Invalid user"
        r"|input_userauth_request:|maximum authentication|Too many authentication"
        r"|Connection (?:closed|reset|from)|Disconnected from|Disconnecting"
        r"|Received disconnect|Timeout, client|Unable to negotiate|pam_unix\("
        r"|reverse mapping|Address\s|(?:error|fatal|debug\d?):)",
    ): "words that can open an sshd message (the header fallback), extracts no name",
    (
        "faultmaven/modules/preprocessing/extractors/sshd_auth.py",
        r"""['"()]|\bport\s+\d|:\s*\d+:\s|\b(?:user|for|from|by)\s""",
    ): "text that refuses the header fallback, extracts no name",
}


def _repo_root() -> pathlib.Path:
    """Anchor on the imported package, not on cwd.

    An editable install can resolve ``faultmaven`` to a different checkout
    than the test file lives in; scanning the tree that was *imported* is
    the only anchor that cannot disagree with what the other tests measured.
    """
    return _PACKAGE_ROOT.parent


def _pattern_text(node: ast.AST, consts: dict[str, str] | None = None) -> str:
    """Flatten a regex argument built from string parts or a module constant."""
    known = consts or {}
    parts: list[str] = []

    def walk(inner: ast.AST) -> None:
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            parts.append(inner.value)
        elif isinstance(inner, ast.BinOp):
            walk(inner.left)
            walk(inner.right)
        elif isinstance(inner, ast.JoinedStr):
            for value in inner.values:
                walk(value)
        elif isinstance(inner, ast.Name) and inner.id in known:
            parts.append(known[inner.id])

    walk(node)
    return "".join(parts)


def _collect(
    tree: ast.AST,
) -> tuple[dict[str, str], set[str], set[str], list[ast.Call]]:
    """One walk: constants, re-module bindings, and every call node.

    Three separate ``ast.walk`` passes cost about as much as the parse does;
    the scan is already the slowest test in this file and there is no reason
    to pay for the tree three times.

    ``import re as _re`` exists once in this codebase and ``from re import
    search`` does not, but both cost one branch to cover and a scan keyed on
    the literal name ``re`` is defeated by either.
    """
    consts: dict[str, str] = {}
    modules: set[str] = set()
    functions: set[str] = set()
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            text = _pattern_text(node.value)
            if text:
                consts[node.targets[0].id] = text
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _RE_MODULES:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in _RE_MODULES:
            for alias in node.names:
                if alias.name in _RE_FUNCTIONS:
                    functions.add(alias.asname or alias.name)
    return consts, modules, functions, calls


def _regex_pattern_arg(node: ast.Call) -> ast.AST | None:
    """The pattern argument, positional or as the ``pattern=`` keyword."""
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "pattern":
            return keyword.value
    return None


def _parse(path: pathlib.Path) -> ast.AST | None:
    try:
        with warnings.catch_warnings():
            # Some modules carry docstrings with regex escapes; the
            # SyntaxWarning they raise at parse time is not this scan's
            # business.
            warnings.simplefilter("ignore")
            return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return None


@pytest.mark.unit
@pytest.mark.architecture
def test_exactly_one_implementation_of_the_username_rule() -> None:
    """N == 1. Two copies is how fm#522 outlived the guards that fixed it.

    ``entities/logs.py`` carried a second username regex written from the
    first and never given any of the guards it grew. Before this scan
    existed, N was 2 and only one copy was correct.

    Declared blind spots, measured rather than assumed: a pattern assembled
    at runtime from non-literal parts (``re.compile("|".join(parts))``)
    flattens to the empty string and is invisible — 1 such site exists in
    ``faultmaven/`` and it is a URL redactor. And the scan keys on the
    literal token ``user``, so a rule spelled around ``acct=`` or ``login=``
    is out of reach; widening to a vocabulary of account-ish keys has not
    been measured and is not guessed at here.
    """
    root = _repo_root()
    scanned: list[pathlib.Path] = []
    offenders: list[str] = []
    allowlist_seen: set[tuple[str, str]] = set()

    for path in sorted((root / "faultmaven").rglob("*.py")):
        scanned.append(path)
        source = path.read_text(encoding="utf-8", errors="replace")
        # A file holding a username regex must mention "user" AND spell one of
        # the re call names. Both are necessary conditions of the thing being
        # looked for, so the prefilter loses no reach; it takes 478 files to
        # 82 and the parse from 3.2s to 1.1s. Every file that currently holds
        # a "user" pattern survives it.
        if "user" not in source.lower() or not any(
            call in source for call in _CALL_SPELLINGS
        ):
            continue
        tree = _parse(path)
        if tree is None:  # pragma: no cover - defensive
            continue
        consts, modules, functions, calls = _collect(tree)
        relative = str(path.relative_to(root))
        for node in calls:
            func = node.func
            if isinstance(func, ast.Attribute):
                qualifies = (
                    isinstance(func.value, ast.Name)
                    and func.value.id in modules
                    and func.attr in _RE_FUNCTIONS
                )
            elif isinstance(func, ast.Name):
                qualifies = func.id in functions
            else:
                qualifies = False
            if not qualifies:
                continue
            arg = _regex_pattern_arg(node)
            if arg is None:  # pragma: no cover - defensive
                continue
            pattern = _pattern_text(arg, consts)
            if "user" not in pattern.lower():
                continue
            if pathlib.Path(relative) == _RULE_HOME:
                continue
            if (relative, pattern) in _NOT_THE_RULE:
                allowlist_seen.add((relative, pattern))
                continue
            offenders.append(f"{relative}:{node.lineno}  {pattern!r}")

    # The scan must have reached the places the rule can be re-introduced.
    for directory in _MUST_SCAN:
        assert any(
            str(p.relative_to(root)).startswith(str(directory)) for p in scanned
        ), f"scan never visited {directory}"

    stale = set(_NOT_THE_RULE) - allowlist_seen
    assert not stale, (
        "allowlisted patterns no longer exist; delete them so the list stays "
        f"honest: {sorted(stale)}"
    )

    assert not offenders, (
        "a username regex lives outside "
        f"{_RULE_HOME}; import from it instead:\n  " + "\n  ".join(offenders)
    )
