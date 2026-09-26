"""sshd auth events are read from what sshd wrote, not what its client wrote.

fm#1657. sshd copies the client's login name into its log lines verbatim, and
the event patterns searched the whole line, so a login name that spelled an
event phrase was counted as that event::

    sshd[1]: Invalid user Failed password for x from 7.7.7.7 from 1.2.3.4

was a password failure as well as an invalid-user probe — and, because every
IPv4 on a line was credited with every event on it, a password failure
against 7.7.7.7 too. A brute-force source could raise its own attempt count
or plant one against any address it spelled out, in a block whose header
tells the model to use these numbers as attempt totals.

Two rules, one per kind of line:

* A line whose header ``sshd_auth`` positively reads: **a login name cannot
  change the rendered counts.** Every crafted line is rendered beside the same
  line with an ordinary login name, and the two renderings must agree —
  events, breakdown rows, and the FILE SUMMARY sentences the categories drive.
  Each category has a positive control, so a test cannot pass on an extractor
  that counts nothing.
* Every other line is read exactly as before fm#1657. The oracle for "as
  before" is the extractor with every line forced unread, and it is pinned to
  main's output (``TestAHeaderThisModuleDoesNotReadIsReadAsBefore``).

``connection_closed`` is the one category left unanchored; its residual is
pinned at the bottom.
"""

from __future__ import annotations

import json
import re

import pytest

from faultmaven.modules.preprocessing.extractors import logs_extractor, sshd_auth
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.modules.preprocessing.extractors.sshd_auth import (
    read_sshd_auth_line,
    split_syslog_line,
)
from tests.wallclock import assert_linear_growth

# RFC 5737 documentation ranges.
SRC = "203.0.113.5"  # the connection's real source address
VICTIM = "198.51.100.7"  # an address a login name spells out
OTHER = "192.0.2.9"
UNIT_PEER = "198.51.100.99"  # an address inside a systemd unit name

BSD = "Dec 10 06:55:46 LabSZ sshd[24200]: "


def _render(lines: list[str]):
    return LogsAndErrorsExtractor().extract("\n".join(lines) + "\n")


def _render_as_before(lines: list[str], monkeypatch):
    """The extractor with every line forced unread: the pre-fm#1657 reading."""
    with monkeypatch.context() as patch:
        patch.setattr(
            logs_extractor, "read_sshd_auth_line", lambda line: sshd_auth._UNREAD
        )
        return _render(lines)


def _events(result) -> dict[str, int]:
    """The ``Event types`` block of the search map, as ``{event: count}``."""
    events: dict[str, int] = {}
    inside = False
    for raw in (result.search_map or "").split("\n"):
        if raw.lstrip().startswith("Event types"):
            inside = True
            continue
        if inside:
            if raw.startswith("      "):  # accepted_login's attacker split
                continue
            if not raw.startswith("    "):
                break
            name, _, rest = raw.strip().partition(": ")
            events[name] = int(rest.split()[0])
    return events


def _rows(result) -> dict[str, str]:
    """The IP auth breakdown rows, keyed by IP."""
    rows: dict[str, str] = {}
    inside = False
    for raw in (result.search_map or "").split("\n"):
        if raw.lstrip().startswith("IP auth breakdown"):
            inside = True
            continue
        if inside:
            if not raw.startswith("    ") or "→ auth total=" not in raw:
                break
            ip, _, rest = raw.strip().partition(": ")
            rows[ip] = rest
    return rows


def _counts(result) -> tuple[dict[str, int], dict[str, str], str]:
    """Everything a login name must not be able to move."""
    summary = result.file_extract.split("\n\n", 1)[0]
    flags = " | ".join(
        phrase
        for phrase in (
            "POSSIBLE BREAK-IN ATTEMPT warnings",
            "successful SSH session(s) opened",
            "brute-force",
            "from attacker IPs",
        )
        if phrase in summary or phrase in (result.search_map or "")
    )
    return _events(result), _rows(result), flags


# ---------------------------------------------------------------------------
# One crafted login name per category the extractor counts from sshd.
# ---------------------------------------------------------------------------

# (category, login name that spells its phrase, a genuine line of it).
SPOOFS = [
    pytest.param(
        "failed_password",
        f"Failed password for root from {VICTIM} port 22 ssh2",
        f"Failed password for root from {SRC} port 22 ssh2",
        id="failed_password",
    ),
    pytest.param(
        "accepted_login",
        f"Accepted password for root from {VICTIM} port 22 ssh2",
        f"Accepted password for root from {SRC} port 22 ssh2",
        id="accepted_login",
    ),
    pytest.param(
        # The fm#1627 outcome test, for a method the categories do not name.
        "other_method_outcome",
        f"Failed keyboard-interactive/pam for root from {VICTIM} port 22 ssh2",
        f"Failed keyboard-interactive/pam for root from {SRC} port 22 ssh2",
        id="outcome",
    ),
    pytest.param(
        "pam_auth_failure",
        "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0"
        f" tty=ssh ruser= rhost={VICTIM}",
        "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0"
        f" tty=ssh ruser= rhost={SRC}",
        id="pam_format_a",
    ),
    pytest.param(
        "pam_auth_failure",
        "sshd(pam_unix)[1]: authentication failure; logname= uid=0 euid=0"
        f" tty=NODEVssh ruser= rhost={VICTIM}",
        None,  # Format B's genuine line needs its tag: see the control below
        id="pam_format_b",
    ),
    pytest.param(
        "break_in_attempt",
        f"reverse mapping checking getaddrinfo for x [{VICTIM}] failed"
        " - POSSIBLE BREAK-IN ATTEMPT!",
        f"reverse mapping checking getaddrinfo for host.example [{SRC}] failed"
        " - POSSIBLE BREAK-IN ATTEMPT!",
        id="break_in",
    ),
    pytest.param(
        "ssh_session_opened",
        "sshd: session opened for user root by (uid=0)",
        None,  # a session line is written under the sshd(pam_unix) tag
        id="ssh_session_opened",
    ),
    pytest.param(
        # An invalid-user line carries every client login name there is, so a
        # name cannot ADD this category to a line; what it can do is plant the
        # line's own event against an address it spells.
        "invalid_user",
        f"x from {VICTIM} port 1",
        f"Invalid user admin from {SRC} port 50000",
        id="invalid_user",
    ),
]

# The genuine sshd lines a client's login name is written into. ``{user}`` is
# the client field; everything else is sshd's.
HOSTS = [
    pytest.param(f"Invalid user {{user}} from {SRC} port 50000", id="invalid-user"),
    pytest.param(
        f"Failed password for invalid user {{user}} from {SRC} port 50000 ssh2",
        id="failed-password",
    ),
    pytest.param(
        f"Failed none for invalid user {{user}} from {SRC} port 50000 ssh2",
        id="failed-none",
    ),
    pytest.param(
        f"Connection closed by invalid user {{user}} {SRC} port 50000 [preauth]",
        id="connection-closed-by",
    ),
    pytest.param(
        f"error: maximum authentication attempts exceeded for invalid user {{user}}"
        f" from {SRC} port 50000 ssh2 [preauth]",
        id="maxtries",
    ),
]

GENUINE_TAGGED = {
    "pam_format_b": "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication"
    f" failure; logname= uid=0 euid=0 tty=NODEVssh ruser= rhost={SRC}",
    "ssh_session_opened": "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: session"
    " opened for user alice by (uid=0)",
}


@pytest.mark.unit
class TestALoginNameCannotChangeTheCounts:
    @pytest.mark.parametrize("category, spoof, genuine", SPOOFS)
    def test_positive_control_the_category_is_counted(
        self, request, category, spoof, genuine
    ):
        """The genuine line IS counted — so the crafted test can see a miss."""
        line = BSD + genuine if genuine else GENUINE_TAGGED[request.node.callspec.id]
        events, rows, flags = _counts(_render([line]))
        if category == "other_method_outcome":
            assert rows == {SRC: "other_method_outcome=1 → auth total=1"}, rows
        else:
            assert events.get(category) == 1, events
        if category in {"failed_password", "accepted_login", "pam_auth_failure"}:
            assert f"{category}=1" in rows.get(SRC, ""), rows
        if category == "break_in_attempt":
            assert "POSSIBLE BREAK-IN ATTEMPT warnings" in flags
        if category == "ssh_session_opened":
            assert "successful SSH session(s) opened" in flags

    @pytest.mark.parametrize("host", HOSTS)
    @pytest.mark.parametrize("category, spoof, genuine", SPOOFS)
    def test_crafted_login_name_renders_like_an_ordinary_one(
        self, host, category, spoof, genuine
    ):
        crafted = _counts(_render([BSD + host.format(user=spoof)]))
        ordinary = _counts(_render([BSD + host.format(user="admin")]))
        assert crafted == ordinary, (category, crafted, ordinary)
        events, rows, _ = crafted
        assert VICTIM not in rows, rows
        # and the ordinary rendering is not empty — the host line counts.
        assert events.get("invalid_user") == 1, events
        assert set(rows) == {SRC}, rows

    def test_the_issue_line(self):
        """fm#1657 as filed: the name spells a password failure."""
        line = BSD + f"Invalid user Failed password for x from {VICTIM} from {SRC}"
        events, rows, _ = _counts(_render([line]))
        assert events == {"invalid_user": 1}, events
        assert rows == {SRC: "invalid_user=1 → auth total=0"}, rows

    def test_a_login_name_that_is_an_address_is_not_credited(self):
        """The name ``198.51.100.7`` is text, not the source; SRC is."""
        line = BSD + f"Failed password for invalid user {VICTIM} from {SRC} port 1 ssh2"
        events, rows, _ = _counts(_render([line]))
        assert events == {"failed_password": 1, "invalid_user": 1}, events
        assert rows == {SRC: "failed_password=1, invalid_user=1 → auth total=1"}, rows


@pytest.mark.unit
class TestOtherClientTextIsNotAnEvent:
    """The same rule for client fields that are not the login name."""

    def test_disconnect_reason_is_not_an_invalid_user_probe(self):
        """``Received disconnect … : <reason>`` — the reason is the client's."""
        reason = BSD + (
            f"Received disconnect from {SRC} port 50000:11: Invalid user x from"
            f" {VICTIM} [preauth]"
        )
        control = BSD + f"Invalid user x from {SRC}"
        assert _events(_render([control])) == {"invalid_user": 1}
        events, rows, _ = _counts(_render([reason]))
        assert events == {}, events
        assert rows == {}, rows

    def test_ptr_name_does_not_make_an_address_an_attacker(self):
        """The reverse-mapping check's PTR name is the remote side's to choose.

        A PTR embedding another address used to credit that address with a
        break-in, which moved its accepted login into "from attacker IPs".
        """
        lines = [
            BSD + f"reverse mapping checking getaddrinfo for {VICTIM}.evil.example"
            f" [{SRC}] failed - POSSIBLE BREAK-IN ATTEMPT!",
            BSD + f"Accepted password for alice from {VICTIM} port 22 ssh2",
        ]
        search_map = _render(lines).search_map
        assert "0 from attacker IPs" in search_map, search_map
        assert "1 from non-attacker IPs" in search_map, search_map

    def test_forward_mapping_form_credits_its_address_slot(self):
        lines = [
            BSD + f"Address {SRC} maps to {VICTIM}.evil.example, but this does not"
            " map back to the address - POSSIBLE BREAK-IN ATTEMPT!",
            BSD + f"Accepted password for alice from {SRC} port 22 ssh2",
            BSD + f"Accepted password for bob from {VICTIM} port 22 ssh2",
        ]
        result = _render(lines)
        assert _events(result)["break_in_attempt"] == 1
        assert "1 from attacker IPs" in result.search_map
        assert "1 from non-attacker IPs" in result.search_map

    def test_ptr_rhost_is_not_an_address(self):
        """PAM's ``rhost`` holds a PTR name when sshd resolved one.

        The address inside ``198.51.100.7.dyn.example`` is the name's
        spelling, which the remote side chooses — credited to nobody. Measured
        on real data: 10 loghub Linux_2k and 1 OpenSSH_2k PAM lines, none in
        a rendered row.
        """
        tail = "logname= uid=0 euid=0 tty=ssh ruser="
        ptr = (
            BSD
            + f"pam_unix(sshd:auth): authentication failure; {tail} rhost={VICTIM}.dyn.example"
        )
        literal = (
            BSD + f"pam_unix(sshd:auth): authentication failure; {tail} rhost={SRC}"
        )
        assert _rows(_render([literal])) == {SRC: "pam_auth_failure=1 → auth total=1"}
        events, rows, _ = _counts(_render([ptr]))
        assert events == {"pam_auth_failure": 1}, events
        assert rows == {}, rows


@pytest.mark.unit
class TestAmbiguousSlotCreditsNobody:
    """Client text on BOTH sides of the address slot can make two slots parse.

    A certificate's key id is appended after ``ssh2:``, so a login name and a
    key id together can spell a second, well-formed ``from <ip> port <n>
    ssh2``. Either choice could be the attacker's, so the line is credited
    to nobody — it still counts as an event.
    """

    def test_login_name_and_key_id_lookalikes(self):
        line = BSD + (
            f"Failed publickey for invalid user x from {VICTIM} port 1 ssh2: RSA"
            f" SHA256:z ID q from {SRC} port 22 ssh2: RSA-CERT SHA256:a ID k"
            " (serial 0) CA RSA SHA256:b"
        )
        events, rows, _ = _counts(_render([line]))
        assert events == {"invalid_user": 1}, events
        assert rows == {}, rows

    def test_login_name_alone_cannot_make_a_password_line_ambiguous(self):
        """password appends nothing after ``ssh2``, so only one slot parses."""
        line = BSD + (
            f"Failed password for invalid user x from {VICTIM} port 1 ssh2: RSA"
            f" SHA256:z ID q from {SRC} port 22 ssh2"
        )
        assert _rows(_render([line])) == {
            SRC: "failed_password=1, invalid_user=1 → auth total=1"
        }


# ---------------------------------------------------------------------------
# Every header shape this module reads.
# ---------------------------------------------------------------------------

# One genuine auth session (Format A), four sources.
SESSION = [
    f"Invalid user {{user}} from {SRC} port 50000",
    "pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 tty=ssh"
    f" ruser= rhost={SRC}",
    f"Failed password for invalid user {{user}} from {SRC} port 50000 ssh2",
    f"Connection closed by invalid user {{user}} {SRC} port 50000 [preauth]",
    f"Failed keyboard-interactive/pam for root from {OTHER} port 50001 ssh2",
    f"reverse mapping checking getaddrinfo for host.example [{OTHER}] failed"
    " - POSSIBLE BREAK-IN ATTEMPT!",
    f"Accepted publickey for alice from {VICTIM} port 50002 ssh2: ED25519"
    " SHA256:abc",
]
SESSION_EVENTS = {
    "invalid_user": 3,
    "pam_auth_failure": 1,
    "failed_password": 1,
    "connection_closed": 1,
    "break_in_attempt": 1,
    "accepted_login": 1,
}
SESSION_ROWS = {
    SRC: "failed_password=1, pam_auth_failure=1, invalid_user=3 → auth total=1",
    OTHER: "other_method_outcome=1 → auth total=1",
    VICTIM: "accepted_login=1 → auth total=1",
}
# Every category's phrase at once, spelled by one login name.
CRAFTED_USER = (
    f"Failed password for root from {VICTIM} port 22 ssh2 Accepted publickey"
    f" pam_unix(sshd:auth): authentication failure rhost={VICTIM}"
    f" POSSIBLE BREAK-IN ATTEMPT sshd: session opened for user root {VICTIM}"
)


def _session(wrap, user="admin"):
    return [wrap(m.format(user=user)) for m in SESSION]


def _json_string(msg: str) -> str:
    return json.dumps(msg)[1:-1]


def _journald_json(msg: str) -> str:
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1784713652755556",
            "_HOSTNAME": "srv0house",
            "SYSLOG_IDENTIFIER": "sshd-session",
            "MESSAGE": msg,
        }
    )


def _docker_json(msg: str) -> str:
    return json.dumps(
        {"log": msg + "\n", "stream": "stderr", "time": "2026-09-13T01:17:01Z"}
    )


READ_WRAPPERS = {
    "bsd": lambda m: f"Dec 10 06:55:46 LabSZ sshd[24200]: {m}",
    "sshd_session": lambda m: f"Sep 24 08:26:36 srv0house sshd-session[2984419]: {m}",
    "sshd_auth": lambda m: f"Sep 20 10:00:01 web1 sshd-auth[1234]: {m}",
    "rfc3339": lambda m: f"2026-09-13T01:17:01.494665+00:00 srv0house sshd[1]: {m}",
    "short_full": lambda m: f"Wed 2026-07-22 09:47:32 UTC srv0house sshd[1]: {m}",
    "short_unix": lambda m: f"1784713652.755556 srv0house sshd[1]: {m}",
    "short_monotonic": lambda m: f"[4158954.403152] srv0house sshd[1]: {m}",
    "short_delta": lambda m: f"[4158954.403152 <    0.130762 >] srv0house sshd[1]: {m}",
    "with_unit": lambda m: f"Wed 2026-07-22 09:47:32 UTC srv0house ssh.service[1]: {m}",
    "tag_only": lambda m: f"sshd[1234]: {m}",
    "no_pid": lambda m: f"Sep 21 11:00:01 web01 sshd: {m}",
    "level_column": lambda m: f"2023-06-14 12:00:00 ERROR host sshd: {m}",
    "pri_rfc3164": lambda m: f"<38>Dec 10 06:55:46 LabSZ sshd[24200]: {m}",
    "rfc5424": lambda m: f"<38>1 2026-09-13T01:17:01.494665Z srv0house sshd 24200 - - {m}",
    "thunderbird": lambda m: (
        f"- 1131566479 2005.11.09 tbird-admin1 Nov 9 12:01:19"
        f" local@tbird-admin1 sshd[19023]: {m}"
    ),
    "openwrt_logread": lambda m: f"Sat Sep 20 10:00:01 2026 authpriv.info sshd[1]: {m}",
    "solaris_msgid": lambda m: f"Dec 10 06:55:46 LabSZ sshd[1]: [ID 800047 auth.info] {m}",
    "repeated": lambda m: f"Dec 10 06:55:46 LabSZ sshd[1]: message repeated 2 times: [ {m}]",
    "message_only": lambda m: m,
    "indented": lambda m: f"    {m}",
    "win32_openssh": lambda m: f"4200 2026-09-20 10:00:01.123 {m}",
    "cri": lambda m: f"2026-09-13T01:17:01.494665123Z stderr F {m}",
    "journal_export": lambda m: f"MESSAGE={m}",
    "journal_verbose": lambda m: f"    MESSAGE={m}",
    "journald_json": _journald_json,
    "docker_json": _docker_json,
    "compose_prefix": lambda m: f"sshd-1  | {m}",
    "compose_prefix_syslog": lambda m: f"sshd-1  | Dec 10 06:55:46 LabSZ sshd[1]: {m}",
    "kubectl_prefix": lambda m: f"[pod/sshd-0/sshd] {m}",
    "offset_token": lambda m: f"2026-09-13 01:17:01 +0000 srv0house sshd[1]: {m}",
    "cisco_ts_colon": lambda m: f"Sep 20 10:00:01: web1 sshd[1234]: {m}",
    "program_path": lambda m: f"Dec 10 06:55:46 LabSZ /usr/sbin/sshd[1]: {m}",
    "macos_log_show_syslog": lambda m: (
        f"2026-09-20 10:00:01.123456-0700  mac sshd-session[1234]: {m}"
    ),
    "macos_log_show_compact": lambda m: (
        f"2026-09-20 10:00:01.123 Df sshd-session[1234:5a6b] {m}"
    ),
    "s6_log_tai64n": lambda m: f"@4000000065123abc12345678 {m}",
    "bracketed_iso": lambda m: f"[2026-09-20 10:00:01] web1 sshd[1234]: {m}",
    # A socket-activated sshd's unit name carries the connection's addresses;
    # only sshd's slot is credited, so they get no row.
    "socket_activated_unit": lambda m: (
        "Sat 2026-09-20 10:00:01 UTC web1"
        f" sshd@7-10.0.0.5:22-{UNIT_PEER}:40001.service[1234]: {m}"
    ),
}


@pytest.mark.unit
class TestEveryHeaderShapeThisModuleReads:
    """The same session through every header this module reads.

    Genuine: identical counts and rows in every shape. Crafted: identical to
    the same shape with an ordinary login name.
    """

    @pytest.mark.parametrize("shape", sorted(READ_WRAPPERS))
    def test_every_line_is_read(self, shape):
        lines = _session(READ_WRAPPERS[shape], CRAFTED_USER)
        assert all(read_sshd_auth_line(line).read for line in lines), shape

    @pytest.mark.parametrize("shape", sorted(READ_WRAPPERS))
    def test_genuine_session_counts_the_same(self, shape):
        result = _render(_session(READ_WRAPPERS[shape]))
        assert _events(result) == SESSION_EVENTS, shape
        assert _rows(result) == SESSION_ROWS, shape

    @pytest.mark.parametrize("shape", sorted(READ_WRAPPERS))
    def test_crafted_session_counts_like_the_ordinary_one(self, shape):
        crafted = _render(_session(READ_WRAPPERS[shape], CRAFTED_USER))
        assert _events(crafted) == SESSION_EVENTS, shape
        assert _rows(crafted) == SESSION_ROWS, shape

    @pytest.mark.parametrize(
        "tag_line",
        [
            "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: {m}",
            "- 1131566479 2005.11.09 dn228 Nov 9 12:01:01 dn228/dn228"
            " sshd(pam_unix)[2915]: {m}",
            "sshd(pam_unix)[19939]: {m}",
            "Jun 14 15:16:01 combo /usr/sbin/sshd(pam_unix)[19939]: {m}",
        ],
        ids=["bsd", "thunderbird", "tag_only", "program_path"],
    )
    def test_format_b_is_read_from_its_tag(self, tag_line):
        """loghub Linux: the module rides in the tag, the message opens bare."""
        lines = [
            tag_line.format(
                m="authentication failure; logname= uid=0 euid=0 tty=NODEVssh"
                f" ruser= rhost={SRC}  user=root"
            ),
            tag_line.format(m="session opened for user test by (uid=509)"),
        ]
        result = _render(lines)
        assert _events(result) == {"pam_auth_failure": 1, "ssh_session_opened": 1}
        assert _rows(result) == {SRC: "pam_auth_failure=1 → auth total=1"}

    def test_su_session_is_not_an_ssh_session(self):
        line = "Jun 14 15:16:01 combo su(pam_unix)[1]: session opened for user news by (uid=0)"
        assert _events(_render([line])) == {}


# ---------------------------------------------------------------------------
# Every other header: read exactly as before fm#1657.
# ---------------------------------------------------------------------------

_KIBANA_CSV = lambda m: f'"Sep 20, 2026 @ 10:00:01.000",web1,sshd,"{m}"'  # noqa: E731

# The two review rounds' shapes the positional grammar does not read, and one
# nobody has named. Each must count, and credit, exactly what main did.
UNREAD_SHAPES = {
    # fm#1657 review, round 1
    "grep_H": lambda m: f"/var/log/auth.log.1:Sep 20 10:00:01 web1 sshd[1234]: {m}",
    "grep_n": lambda m: f"1234:Sep 20 10:00:01 web1 sshd[1234]: {m}",
    "zgrep_H": lambda m: f"/var/log/auth.log.2.gz:Sep 20 10:00:01 web1 sshd[1]: {m}",
    "busybox_authpriv": lambda m: f"Sep 20 10:00:01 alpine authpriv.info sshd[1]: {m}",
    "busybox_auth": lambda m: f"Sep 20 10:00:01 alpine auth.info sshd[1234]: {m}",
    "aix": lambda m: f"Sep 20 10:00:01 aixhost auth|security:info sshd[1234]: {m}",
    "syslog_ng_facility": lambda m: f"Sep 20 10:00:01 web1 [auth.info] sshd[1]: {m}",
    "pdsh_host": lambda m: f"web1: Sep 20 10:00:01 web1 sshd[1234]: {m}",
    "socklog": lambda m: (
        f"2026-09-20T10:00:01.12345 auth.info: Sep 20 10:00:01 sshd[1234]: {m}"
    ),
    "macos_log_show": lambda m: (
        "2026-09-20 10:00:01.123456-0700 0x1a2b     Default     0x0"
        f"                  1234   0    sshd-session: {m}"
    ),
    "tsv_export": lambda m: f"2026-09-20T10:00:01Z\tweb1\tsshd\t{m}",
    "csv_export": lambda m: '"2026-09-20T10:00:01Z","web1","sshd","' + m + '"',
    "stern": lambda m: f"sshd-7f9c sshd {m}",
    "pipe_columns": lambda m: f"2026-09-20 10:00:01 | web1 | sshd | {m}",
    "unquoted_csv": lambda m: f"2026-09-20T10:00:01Z,web1,sshd,{m}",
    # fm#1657 review, round 2
    "kibana_csv": _KIBANA_CSV,
    "csv_quoted_ts": lambda m: f'"Sep 20, 2026 10:00:01",web1,sshd,{m}',
    "splunk_csv": lambda m: (
        '0,"2026-09-20T10:00:01.000+0000","/var/log/auth.log",linux_secure,web1,'
        f'"Sep 20 10:00:01 web1 sshd[1234]: {m}"'
    ),
    "csv_message_not_last": lambda m: (
        f'"2026-09-20T10:00:01Z","Sep 20 10:00:01 web1 sshd[1234]: {m}","web1"'
    ),
    "logcli_labels": lambda m: (
        '2026-09-20T10:00:01+00:00 {filename="/var/log/auth.log", job="varlogs"}'
        f" Sep 20 10:00:01 web1 sshd[1234]: {m}"
    ),
    "logfmt": lambda m: f'ts=2026-09-20T10:00:01Z host=web1 app=sshd msg="{m}"',
    "journal_json_pretty": lambda m: f'\t"MESSAGE" : "{_json_string(m)}",',
    "macos_ndjson": lambda m: json.dumps(
        {
            "timestamp": "2026-09-20 10:00:01.123456-0700",
            "processImagePath": "/usr/libexec/sshd-session",
            "eventMessage": m,
        }
    ),
    "windows_event_tsv": lambda m: (
        f"Information\t9/20/2026 10:00:01 AM\tOpenSSH\t4\tNone\tsshd: {m}"
    ),
    "windows_event_tsv_sshd": lambda m: (
        f"Information\t9/20/2026 10:00:01 AM\tsshd\t4\tNone\t{m}"
    ),
    "loki_json_line": lambda m: json.dumps(
        {"line": f"Sep 20 10:00:01 web1 sshd[1234]: {m}", "ts": "2026-09-20"}
    ),
    "grep_Hn_path_with_for": lambda m: (
        f"/srv/logs/for audit/auth.log:12:Sep 20 10:00:01 web1 sshd[1234]: {m}"
    ),
    "syslog_host_in_parens": lambda m: (
        f"Sep 20 10:00:01 web1 (10.0.0.5) sshd[1234]: {m}"
    ),
    # ... and a header nobody has seen.
    "never_seen": lambda m: f"~~ 20260920 <<web1/sshd>> ~~ {m}",
}


@pytest.mark.unit
class TestAHeaderThisModuleDoesNotReadIsReadAsBefore:
    """Never less than main, by construction: an unread line IS main's reading.

    Every line of every shape here is unread, and its rendering — counts AND
    rows — equals the extractor with every line forced unread. That oracle is
    the pre-fm#1657 per-line reading, moved unchanged into
    ``_searched_sshd_events``; ``test_the_oracle_is_the_search`` pins it to
    main's output. A crafted login name through an unread shape keeps main's
    exposure, and no more.
    """

    @pytest.mark.parametrize("shape", sorted(UNREAD_SHAPES))
    def test_no_line_is_read(self, shape):
        lines = _session(UNREAD_SHAPES[shape])
        assert not any(read_sshd_auth_line(line).read for line in lines), shape

    @pytest.mark.parametrize(
        "user", ["admin", CRAFTED_USER], ids=["genuine", "crafted"]
    )
    @pytest.mark.parametrize("shape", sorted(UNREAD_SHAPES))
    def test_counts_and_rows_are_mains(self, shape, user, monkeypatch):
        lines = _session(UNREAD_SHAPES[shape], user)
        expected = _render_as_before(lines, monkeypatch)
        actual = _render(lines)
        assert _events(actual) == _events(expected), shape
        assert _rows(actual) == _rows(expected), shape
        assert actual.search_map == expected.search_map, shape

    def test_the_oracle_is_the_search(self, monkeypatch):
        """Main's counts, measured with main's extractor, written down.

        An IP-free header: main counted the session as the anchored reading
        does, and counted the crafted name's password failure against the
        address it spells.
        """
        genuine = _render_as_before(_session(_KIBANA_CSV), monkeypatch)
        assert _events(genuine) == SESSION_EVENTS
        assert _rows(genuine) == SESSION_ROWS
        crafted = _render_as_before(
            [
                _KIBANA_CSV(
                    f"Invalid user Failed password for x from {VICTIM} from {SRC}"
                )
            ],
            monkeypatch,
        )
        assert _events(crafted) == {"failed_password": 1, "invalid_user": 1}
        assert _rows(crafted) == {
            VICTIM: "failed_password=1, invalid_user=1 → auth total=1",
            SRC: "failed_password=1, invalid_user=1 → auth total=1",
        }

    def test_an_unread_line_credits_every_address_on_it(self, monkeypatch):
        """Main's crediting, measured with main's extractor, written down.

        rsyslog's ``(%fromhost-ip%)`` puts the relay's address in the header;
        main credited it with every event of the session, and an unread line
        still does — exactly main, including where main is wrong. The oracle
        shares its code with the extractor, so this literal is what pins the
        unread branch's crediting.
        """
        result = _render(_session(UNREAD_SHAPES["syslog_host_in_parens"]))
        assert _events(result) == SESSION_EVENTS
        assert _rows(result) == {
            "10.0.0.5": "failed_password=1, pam_auth_failure=1, invalid_user=3,"
            " accepted_login=1, other_method_outcome=1 → auth total=3",
            SRC: "failed_password=1, pam_auth_failure=1, invalid_user=3 → auth total=1",
            OTHER: "other_method_outcome=1 → auth total=1",
            VICTIM: "accepted_login=1 → auth total=1",
        }


@pytest.mark.unit
class TestWhatIsRead:
    """A line is read when its header gives sshd's tag, or its message start
    opens with an sshd event. Nothing else."""

    @pytest.mark.parametrize(
        "line, read, events",
        [
            pytest.param(
                BSD + f"Received disconnect from {SRC} port 5:11: Failed password for"
                f" root from {VICTIM} port 22 ssh2 [preauth]",
                True,
                (),
                id="sshd-tag-no-event",
            ),
            pytest.param(
                f"Invalid user x from {SRC}",
                True,
                ("invalid_user",),
                id="headerless-event",
            ),
            pytest.param(
                f"Received disconnect from {SRC} port 5:11: Failed password for root"
                f" from {VICTIM} port 22 ssh2 [preauth]",
                False,
                (),
                id="headerless-no-event",
            ),
            pytest.param(
                f"Sep 20 10:00:01 web1 myapp[1]: said Failed password for root from"
                f" {VICTIM} port 22 ssh2",
                False,
                (),
                id="other-program-no-event",
            ),
            pytest.param(
                "Sep 20 10:00:01 web1 vsftpd[1]: pam_unix(vsftpd:auth): authentication"
                f" failure; logname= uid=0 euid=0 tty=ftp ruser=bob rhost={SRC}",
                True,
                ("pam_auth_failure",),
                id="other-program-event",
            ),
            pytest.param(
                "Sep 20 10:00:01 web1 CRON[1]: (root) CMD (x)",
                True,
                (),
                id="no-event-word",
            ),
        ],
    )
    def test_read(self, line, read, events):
        reading = read_sshd_auth_line(line)
        assert (reading.read, reading.events) == (read, events)

    def test_a_function_name_is_not_getpwnamallow(self):
        """Headerless ``input_userauth_request: invalid user X`` has no address.

        The function name is read as the tag there, so the message starts at
        the lower-case ``invalid user`` — which must not select the address
        slot of sshd's capitalised ``Invalid user X from <ip>``.
        """
        line = f"input_userauth_request: invalid user x from {VICTIM} port 1 [preauth]"
        reading = read_sshd_auth_line(line)
        assert (reading.read, reading.events, reading.address) == (
            True,
            ("invalid_user",),
            None,
        )


@pytest.mark.unit
class TestMessageOnlyLinesFailClosed:
    """With no header, the message is the line — a fake tag in it is text.

    ``journalctl -o cat`` and ``sshd -e`` write no syslog tag, so the tag
    cannot be found by searching for one: the first tag-shaped token would be
    the client's. Each line here opens with ``Invalid user``, so it is read,
    and the fake tag after it stays the login name it is.
    """

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(
                f"Invalid user sshd[1]: Failed password for y from {VICTIM} from {SRC}",
                id="tag-with-pid",
            ),
            pytest.param(
                f"Invalid user sshd: Accepted password for y from {VICTIM} from {SRC}",
                id="tag-without-pid",
            ),
            pytest.param(
                f"2026-09-13T01:17:01Z Invalid user sshd: Failed password for y from"
                f" {VICTIM} from {SRC}",
                id="after-a-timestamp",
            ),
            pytest.param(
                f"sshd-1  | Invalid user sshd[1]: Failed password for y from {VICTIM}"
                f" from {SRC}",
                id="after-a-compose-prefix",
            ),
        ],
    )
    def test_fake_tag_in_a_login_name(self, line):
        events, rows, _ = _counts(_render([line]))
        assert events == {"invalid_user": 1}, events
        assert rows == {SRC: "invalid_user=1 → auth total=0"}, rows

    @pytest.mark.parametrize(
        "line, program, text",
        [
            pytest.param(
                "Dec 10 06:55:46 LabSZ sshd[24200]: pam_unix(sshd:auth): x",
                "sshd",
                "pam_unix(sshd:auth): x",
                id="bsd",
            ),
            # The tag is the FIRST tag-shaped token: a host never ends in ``:``,
            # so ``sshd[1]:`` cannot be read as a host and the message's own
            # ``pam_unix(...)`` as the tag.
            pytest.param(
                "sshd[1]: pam_unix(sshd:auth): x",
                "sshd",
                "pam_unix(sshd:auth): x",
                id="tag-only",
            ),
            pytest.param("sshd[1]: error: maximum x", "sshd", "maximum x", id="level"),
            pytest.param(
                "Invalid user a: b from 1.2.3.4",
                "",
                "Invalid user a: b from 1.2.3.4",
                id="message-only",
            ),
            pytest.param("{not json", "", "{not json", id="broken-json"),
        ],
    )
    def test_where_the_message_starts(self, line, program, text):
        message = split_syslog_line(line)
        assert (message.program, message.text) == (program, text)


@pytest.mark.unit
class TestAddressSlotSpellings:
    """Ways a genuine slot is written that must keep their credit."""

    @pytest.mark.parametrize(
        "line, row",
        [
            pytest.param(
                "Sep 20 10:00:01 web1 vsftpd[1]: pam_unix(vsftpd:auth): authentication"
                f" failure; logname= uid=0 euid=0 tty=ftp ruser=admin rhost=::ffff:{SRC}",
                "pam_auth_failure=1 → auth total=1",
                id="vsftpd-listen_ipv6",
            ),
            pytest.param(
                "Sep 20 10:00:01 web1 auth-worker(1): pam_unix(dovecot:auth):"
                " authentication failure; logname= uid=0 euid=0 tty=dovecot ruser=bob"
                f" rhost=::ffff:{SRC}",
                "pam_auth_failure=1 → auth total=1",
                id="dovecot",
            ),
            pytest.param(
                f"Sep 20 10:00:01 web1 sshd[1]: Failed password for root from ::ffff:{SRC}"
                " port 4000 ssh2",
                "failed_password=1 → auth total=1",
                id="sshd-mapped",
            ),
            pytest.param(
                f"Sep 20 10:00:01 web1 sshd[1]: Invalid user x from ::FFFF:{SRC}",
                "invalid_user=1 → auth total=0",
                id="sshd-mapped-invalid-user",
            ),
            pytest.param(
                f"Sep 20 10:00:01 web1 sshd[1]: Failed password for root from {SRC} ssh2",
                "failed_password=1 → auth total=1",
                id="no-port",
            ),
            pytest.param(
                f"Sep 20 10:00:01 web1 sshd[1]: Accepted password for root from {SRC} ssh2",
                "accepted_login=1 → auth total=1",
                id="no-port-accepted",
            ),
        ],
    )
    def test_slot_is_credited(self, line, row):
        assert _rows(_render([line])) == {SRC: row}

    @pytest.mark.parametrize(
        "line",
        [
            # vsftpd fills PAM's ``ruser`` with the client's FTP ``USER``: an
            # earlier ``rhost=`` is the client's.
            pytest.param(
                "Sep 20 10:00:01 web1 vsftpd[1]: pam_unix(vsftpd:auth): authentication"
                f" failure; logname= uid=0 euid=0 tty=ftp ruser=a rhost={VICTIM}"
                f" rhost={SRC}",
                id="client-rhost-first",
            ),
            # pam_unix ``audit`` logs an unknown login as ``user=<name>`` after
            # the slot: a later ``rhost=`` is the client's.
            pytest.param(
                BSD + "pam_unix(sshd:auth): authentication failure; logname= uid=0"
                f" euid=0 tty=ssh ruser= rhost={SRC}  user=x rhost={VICTIM}",
                id="client-rhost-last",
            ),
        ],
    )
    def test_two_rhosts_credit_nobody(self, line):
        """Either could be the client's, so the event counts and nobody is credited."""
        events, rows, _ = _counts(_render([line]))
        assert events == {"pam_auth_failure": 1}, events
        assert rows == {}, rows


@pytest.mark.unit
def test_a_syslog_host_that_is_an_address_is_not_the_source():
    """A relay that logs its peer by address puts an IPv4 in the host field.

    Every IPv4 on the line used to be credited, so the log host got an
    accepted login beside the real source. Only sshd's slot is credited now.
    """
    line = f"Dec 10 06:55:46 {OTHER} sshd[1]: Accepted password for alice from {SRC} port 22 ssh2"
    assert _rows(_render([line])) == {SRC: "accepted_login=1 → auth total=1"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "owner, rule, phrase",
    [
        (sshd_auth, "FAILED_PASSWORD_RE", "Failed password"),
        (sshd_auth, "ACCEPTED_LOGIN_RE", "Accepted publickey"),
        (sshd_auth, "AUTH_OUTCOME_RE", "Failed hostbased for "),
        (sshd_auth, "AUTH_OUTCOME_RE", "Accepted gssapi-with-mic for "),
        (sshd_auth, "AUTH_OUTCOME_RE", "Failed keyboard-interactive/pam for "),
        (sshd_auth, "INVALID_USER_RE", "Connection closed by invalid user"),
        (
            sshd_auth,
            "PAM_AUTH_FAILURE_RE",
            "pam_unix(sshd:auth): authentication failure",
        ),
        (sshd_auth, "PAM_TAGGED_FAILURE_RE", "authentication failure"),
        (sshd_auth, "SSH_SESSION_RE", "session opened for user"),
        (
            sshd_auth,
            "BREAK_IN_ATTEMPT_RE",
            "reverse mapping checking getaddrinfo for x [1.2.3.4] failed"
            " - POSSIBLE BREAK-IN ATTEMPT",
        ),
        # ... and the search an unread line gets, which the pre-check decides
        # for too.
        (LogsAndErrorsExtractor, "_FAILED_PASSWORD_RE", "x Failed password"),
        (LogsAndErrorsExtractor, "_ACCEPTED_PASSWORD_RE", "x Accepted publickey"),
        (LogsAndErrorsExtractor, "_INVALID_USER_RE", "x invalid user"),
        (LogsAndErrorsExtractor, "_BREAK_IN_ATTEMPT_RE", "x POSSIBLE BREAK-IN ATTEMPT"),
        (
            LogsAndErrorsExtractor,
            "_PAM_AUTH_FAILURE_RE",
            "x sshd(pam_unix)[1]: authentication failure",
        ),
        (
            LogsAndErrorsExtractor,
            "_SSH_SESSION_RE",
            "x sshd[1]: session opened for user",
        ),
        (LogsAndErrorsExtractor, "_SSHD_AUTH_OUTCOME_RE", "x Failed gssapi-keyex for "),
    ],
)
def test_every_rule_needs_an_event_word(owner, rule, phrase):
    """The pre-check decides a line with no event word; no rule may need none.

    Each phrase matches its rule; stripping every event word out of it must
    make the rule stop matching, or the pre-check could hide that event.
    """
    pattern = getattr(owner, rule)
    assert pattern.search(phrase), (rule, phrase)
    stripped = phrase
    for word in sshd_auth._EVENT_WORDS:
        stripped = re.sub(re.escape(word), "x", stripped, flags=re.IGNORECASE)
    assert not pattern.search(stripped), (rule, stripped)


# ---------------------------------------------------------------------------
# A crafted line must not blank a file's extraction.
# ---------------------------------------------------------------------------

# Any local user can write one of these with ``logger``. The pipeline replaces
# a whole file's extraction with a text preview when the extractor raises, or
# runs past ``TIER1_TIMEOUT_SECONDS``.
#
# Each line is (prefix, repeated unit, suffix, repetitions at 64 KB), so a test
# can build it at any size: the reader's growth check below needs three sizes
# of the same shape, and ``tests/performance/test_extraction_speed.py`` times the
# 64 KB form against that timeout. ``ADVERSARIAL_LINES`` is the 64 KB form,
# byte for byte what this module used before #1579.
_64K = 65536
ADVERSARIAL_SHAPES = {
    "unclosed-bracket-spaced": ("[", "1 ", "password", _64K // 2),
    "unclosed-bracket": ("", "[1", " password", _64K // 2),
    "tag-chain": ("Sep 20 10:00:00 web1 bob: ", "a sshd ", "password", _64K // 7),
    "sshd-word-chain": ("", "sshd ", "password", _64K // 5),
    "unit-parens": ("sshd@", "(", " password", _64K),
    "csv-unterminated": ("", '"a",', '"password', _64K // 4),
    "tsv": ("", "a\t", "password", _64K // 2),
    "timestamps": ("", "10:00:01 ", "password", _64K // 9),
    "slot-lookalikes": (
        BSD + "Failed password for invalid user",
        f" from {VICTIM} port 1 ssh2:",
        f" from {SRC} port 22 ssh2",
        _64K // 32,
    ),
    "rhost-repeats": (
        BSD + "pam_unix(sshd:auth): authentication failure;",
        f" rhost={VICTIM}",
        "",
        _64K // 20,
    ),
    "nested-json": (
        '{"MESSAGE": "',
        json.dumps('{"MESSAGE": "password"}')[1:-1],
        '"}',
        2000,
    ),
}


def adversarial_line(name: str, repetitions: int) -> str:
    """The ``name`` shape with its hostile unit repeated ``repetitions`` times."""
    prefix, unit, suffix, _ = ADVERSARIAL_SHAPES[name]
    return prefix + unit * repetitions + suffix


ADVERSARIAL_LINES = {
    name: adversarial_line(name, shape[3]) for name, shape in ADVERSARIAL_SHAPES.items()
}


@pytest.mark.unit
class TestAdversarialLines:
    @pytest.mark.parametrize("name", sorted(ADVERSARIAL_LINES))
    def test_the_reader_is_linear(self, name):
        """The reader alone, as a growth SHAPE, from ~128 B of the unit up.

        It used to time 64 KB against an absolute 250 ms, which in both
        required gates is a question about the runner (#1579). Small sizes on
        purpose: a quadratic here fails in seconds at 8 KB, and at 64 KB it
        would sit for minutes before failing. Several shapes cost the reader
        almost nothing until they are long, and for those the helper moves the
        window up until the work shows. Mutation-checked — a host pattern of
        ``\\S*\\S*[^\\s:]`` reads 48-54 on the three shapes it makes
        quadratic, against at most 7.6 for any shape fixed (bound ~22.6).
        """
        assert_linear_growth(
            read_sshd_auth_line,
            lambda repetitions: adversarial_line(name, repetitions),
            small=max(1, ADVERSARIAL_SHAPES[name][3] // 512),
            label=f"read_sshd_auth_line on {name}",
        )

    @pytest.mark.parametrize("name", sorted(ADVERSARIAL_LINES))
    def test_extraction_survives_the_line(self, name):
        """The genuine line beside a 64 KB hostile one still gets its row.

        How LONG that takes against ``TIER1_TIMEOUT_SECONDS`` is a latency
        question with a product target, so it is asked in
        ``tests/performance/test_extraction_speed.py`` against a calibrated
        budget (#1579) rather than here against the raw timeout.
        """
        genuine = BSD + f"Failed password for root from {SRC} port 22 ssh2"
        result = _render([genuine, ADVERSARIAL_LINES[name]])
        assert _rows(result).get(SRC, "").startswith("failed_password=1"), name

    def test_no_recursion_through_repeated_program_words(self):
        """The line that raised RecursionError in the round-2 review."""
        line = "Sep 20 10:00:00 web1 bob: " + "a sshd " * 340 + "password"
        assert read_sshd_auth_line(line).read is False


@pytest.mark.unit
def test_connection_closed_is_still_searched_residual():
    """The one category left unanchored, pinned so a change is deliberate.

    sshd writes ``Connection closed``/``Connection reset`` mid-message
    (``fatal: Write failed: Connection reset by peer``, ``Read error from …:
    Connection reset by peer``) and so does every other network daemon, so
    there is no position to anchor it to. A login name therefore still
    raises it by one — but it is not an auth category, feeds no auth total,
    and the line's events are still credited only to sshd's address slot.
    """
    crafted = BSD + f"Invalid user Connection closed by {VICTIM} from {SRC} port 1"
    events, rows, _ = _counts(_render([crafted]))
    assert events == {"invalid_user": 1, "connection_closed": 1}, events
    assert rows == {SRC: "invalid_user=1 → auth total=0"}, rows
    genuine = BSD + "fatal: Write failed: Connection reset by peer [preauth]"
    assert _events(_render([genuine])) == {"connection_closed": 1}
