"""``auth total`` in the IP auth breakdown counts attempts (fm#1627).

The owner's ruling on fm#1627, per IP:

* if the IP has any ``failed_password`` or ``accepted_login`` lines, the
  total is their count (one outcome line per attempt);
* otherwise it is the ``pam_auth_failure`` count, because Format B logs
  (loghub Linux, ``sshd(pam_unix)[PID]: authentication failure; ...
  rhost=IP``) write no outcome line at all.

Row membership stays on ANY auth line, and the per-category counts stay
beside the total. The three-line OpenSSH shape itself is pinned in
``tests/unit/modules/preprocessing/test_mention_unit_is_one_line.py``
(``test_three_line_sshd_attempts_count_as_attempts``); this file pins the
other shapes the ruling names.
"""

from __future__ import annotations

from collections import Counter

import pytest

from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)


def _rows(content: str) -> dict[str, str]:
    """The breakdown table's rows, keyed by IP — nothing else in search_map.

    Substring-searching the whole search_map would also hit the header and
    the crime-scene excerpt, so the rows are read from the block itself.
    """
    search_map = LogsAndErrorsExtractor().extract(content).search_map or ""
    rows: dict[str, str] = {}
    inside = False
    for raw in search_map.split("\n"):
        if raw.lstrip().startswith("IP auth breakdown"):
            inside = True
            continue
        if inside:
            if not raw.startswith("    ") or "→ auth total=" not in raw:
                break
            ip, _, rest = raw.strip().partition(": ")
            rows[ip] = rest
    return rows


def _format_b(ip: str, n: int, *, pid0: int = 19939) -> list[str]:
    return [
        f"Jun 14 15:{i:02d}:01 combo sshd(pam_unix)[{pid0 + i}]: "
        "authentication failure; logname= uid=0 euid=0 tty=NODEVssh "
        f"ruser= rhost={ip}"
        for i in range(n)
    ]


def _openssh_attempt(ip: str, i: int, user: str) -> list[str]:
    return [
        f"Jul 27 14:4{i}:57 combo sshd[10{i}]: Invalid user {user} from {ip}",
        f"Jul 27 14:4{i}:58 combo sshd[10{i}]: pam_unix(sshd:auth): "
        "authentication failure; logname= uid=0 euid=0 tty=ssh ruser= "
        f"rhost={ip}",
        f"Jul 27 14:4{i}:59 combo sshd[10{i}]: Failed password for invalid "
        f"user {user} from {ip} port 22 ssh2",
    ]


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.unit
class TestAuthTotalCountsAttempts:
    def test_format_b_falls_back_to_pam_failures(self):
        """Format B has no outcome line; the PAM count IS the attempt count.

        Read literally, "count outcome lines" renders 0 here — and the
        fixture the project tests against is Format B.
        """
        rows = _rows(_text(_format_b("218.188.2.4", 3)))
        assert rows["218.188.2.4"] == "pam_auth_failure=3 → auth total=3", rows

    def test_outcome_lines_win_over_pam_lines_on_one_ip(self):
        """An IP with both outcome and PAM lines counts only the outcomes.

        Format A: each failure is a PAM line plus a ``Failed password``
        line — two lines, one attempt. Adding the fallback on top would
        double it.
        """
        ip = "61.177.172.9"
        lines: list[str] = []
        for i in range(4):
            lines += [
                f"Jul 27 15:0{i}:58 combo sshd[20{i}]: pam_unix(sshd:auth): "
                "authentication failure; logname= uid=0 euid=0 tty=ssh "
                f"ruser= rhost={ip}  user=root",
                f"Jul 27 15:0{i}:59 combo sshd[20{i}]: Failed password for "
                f"root from {ip} port 22 ssh2",
            ]
        rows = _rows(_text(lines))
        assert rows[ip] == (
            "failed_password=4, pam_auth_failure=4 → auth total=4"
        ), rows

    def test_the_fallback_is_per_ip_not_per_file(self):
        """One IP's outcome lines do not switch another IP's fallback off.

        The ruling applies this per IP, and since fm#1654 the file-level
        summary's PAM figure follows the same rule
        (``TestTheSummaryPamFigureIsDecidedPerIp``). A pam-only IP sharing a
        file with an OpenSSH one still reads its PAM count, not 0.
        """
        lines = _format_b("218.188.2.4", 2)
        for i, user in enumerate(("admin", "oracle")):
            lines += _openssh_attempt("5.36.59.76", i, user)
        rows = _rows(_text(lines))
        assert rows["218.188.2.4"] == "pam_auth_failure=2 → auth total=2", rows
        assert rows["5.36.59.76"] == (
            "failed_password=2, pam_auth_failure=2, invalid_user=4 → auth total=2"
        ), rows

    def test_accepted_login_is_an_attempt(self):
        """An ``Accepted`` line ends an attempt as surely as a failure does."""
        ip = "10.1.1.1"
        lines = [
            f"Jul 27 16:00:0{i} combo sshd[30{i}]: Failed password for deploy "
            f"from {ip} port 22 ssh2"
            for i in range(2)
        ] + [
            f"Jul 27 16:01:00 combo sshd[310]: Accepted password for deploy "
            f"from {ip} port 22 ssh2",
            f"Jul 27 16:02:00 combo sshd[311]: Accepted publickey for deploy "
            f"from {ip} port 22 ssh2",
        ]
        rows = _rows(_text(lines))
        assert rows[ip] == "failed_password=2, accepted_login=2 → auth total=4", rows

    def test_accepted_only_ip_counts_its_logins(self):
        """No failure at all: the outcome count is the accepted count."""
        ip = "10.1.1.2"
        rows = _rows(
            _text(
                [
                    f"Jul 27 16:0{i}:00 combo sshd[40{i}]: Accepted password "
                    f"for admin from {ip} port 22 ssh2"
                    for i in range(3)
                ]
            )
        )
        assert rows[ip] == "accepted_login=3 → auth total=3", rows

    def test_invalid_user_only_ip_keeps_its_row_at_zero(self):
        """A pre-auth probe offered no credential: total 0, row still shown.

        ``Invalid user X`` then a disconnect is not an attempt, and has
        neither an outcome nor a PAM line, so the ruling gives 0. Row
        membership stays on ANY auth line, so the row renders rather than
        vanishing — the probe is still something the IP did.
        """
        ip = "45.9.20.1"
        lines = []
        for i in range(3):
            lines += [
                f"Jul 27 17:0{i}:00 combo sshd[50{i}]: Invalid user test{i} "
                f"from {ip} port 4000{i}",
                f"Jul 27 17:0{i}:01 combo sshd[50{i}]: Connection closed by "
                f"{ip} port 4000{i} [preauth]",
            ]
        rows = _rows(_text(lines))
        assert rows[ip] == "invalid_user=3 → auth total=0", rows


@pytest.mark.unit
class TestEveryMethodsOutcomeIsAnAttempt:
    """sshd writes one outcome line per attempt for EVERY auth method.

    ``Failed <method> for`` / ``Accepted <method> for`` (OpenSSH auth.c
    ``auth_log``), not only ``Failed password``. The fm#1627 review found an
    IP whose keyboard-interactive failures read as zero attempts.
    """

    def test_keyboard_interactive_failures_then_accepted_publickey(self):
        """Five failed keyboard-interactive tries then a key login: 6 attempts.

        Each failure is a PAM line plus a ``Failed keyboard-interactive/pam``
        line. Before the widening this rendered ``auth total=1`` — only the
        ``Accepted publickey`` line was recognised as an outcome.
        """
        ip = "203.0.113.7"
        lines: list[str] = []
        for i in range(5):
            lines += [
                f"Jul 28 09:0{i}:10 combo sshd[70{i}]: pam_unix(sshd:auth): "
                "authentication failure; logname= uid=0 euid=0 tty=ssh "
                f"ruser= rhost={ip}  user=ops",
                f"Jul 28 09:0{i}:11 combo sshd[70{i}]: Failed "
                f"keyboard-interactive/pam for ops from {ip} port 5100{i} ssh2",
            ]
        lines.append(
            f"Jul 28 09:06:00 combo sshd[710]: Accepted publickey for ops "
            f"from {ip} port 51010 ssh2: RSA SHA256:abc"
        )
        rows = _rows(_text(lines))
        assert rows[ip] == (
            "pam_auth_failure=5, accepted_login=1, other_outcome=5" " → auth total=6"
        ), rows

    def test_more_pam_lines_than_outcome_lines(self):
        """Outcome lines are the total even when PAM lines outnumber them.

        Neither the larger of the two nor their sum: 5 PAM lines beside 2
        ``Failed password`` lines are 2 attempts.
        """
        ip = "198.51.100.4"
        lines = _format_b(ip, 5) + [
            f"Jul 28 10:0{i}:00 combo sshd[80{i}]: Failed password for root "
            f"from {ip} port 4200{i} ssh2"
            for i in range(2)
        ]
        rows = _rows(_text(lines))
        assert rows[ip] == (
            "failed_password=2, pam_auth_failure=5 → auth total=2"
        ), rows

    def test_failed_publickey_only_ip_gets_a_row_and_its_attempts(self):
        """An outcome no category names still earns a row, and says why."""
        ip = "192.0.2.50"
        lines = [
            f"Jul 28 11:0{i}:00 combo sshd[90{i}]: Failed publickey for git "
            f"from {ip} port 3300{i} ssh2: ED25519 SHA256:xyz"
            for i in range(3)
        ]
        rows = _rows(_text(lines))
        assert rows[ip] == "other_outcome=3 → auth total=3", rows

    def test_failed_none_is_not_an_attempt(self):
        """``Failed none`` is the client's method query: no credential offered.

        The line shape is verbatim from loghub OpenSSH_2k, which carries four
        of them. The one ``Failed password`` line beside it is the attempt.
        """
        ip = "5.188.10.180"
        lines = [
            f"Dec 10 08:24:40 LabSZ sshd[24363]: Failed none for invalid user 0 "
            f"from {ip} port 49811 ssh2",
            f"Dec 10 08:24:42 LabSZ sshd[24363]: Failed password for invalid "
            f"user 0 from {ip} port 49811 ssh2",
        ]
        rows = _rows(_text(lines))
        assert rows[ip] == ("failed_password=1, invalid_user=2 → auth total=1"), rows


@pytest.mark.unit
class TestAuthAttemptCount:
    """The per-IP rule on its own, for the branches a log cannot isolate."""

    def test_outcome_lines_are_the_total_when_present(self):
        count = LogsAndErrorsExtractor._auth_attempt_count(
            2, Counter({"pam_auth_failure": 7})
        )
        assert count == 2

    def test_pam_failures_are_the_total_without_outcomes(self):
        count = LogsAndErrorsExtractor._auth_attempt_count(
            0, Counter({"pam_auth_failure": 7, "invalid_user": 3})
        )
        assert count == 7


def _search_map(lines: list[str]) -> str:
    return LogsAndErrorsExtractor().extract(_text(lines)).search_map or ""


def _summary(lines: list[str]) -> str:
    return LogsAndErrorsExtractor().extract(_text(lines)).file_extract.split("\n\n")[0]


def _event_lines(lines: list[str]) -> dict[str, int]:
    """The ``Event types`` block's LINE counts."""
    events: dict[str, int] = {}
    inside = False
    for raw in _search_map(lines).split("\n"):
        if raw.lstrip().startswith("Event types"):
            inside = True
            continue
        if inside:
            if raw.startswith("      "):
                continue
            if not raw.startswith("    "):
                break
            name, _, rest = raw.strip().partition(": ")
            events[name] = int(rest.split()[0])
    return events


_REPEAT_NOTE = 'A "message repeated N times" line counts N times'


@pytest.mark.unit
class TestARepeatedMessageIsNAttempts:
    """rsyslog's ``message repeated N times: [ … ]`` stands for N occurrences
    (fm#1669). The per-IP tallies and ``auth total`` count it N times; the
    ``Event types`` LINE count counts it once, because ``search_file`` finds
    one line — and the header says so where both are shown."""

    # loghub OpenSSH_2k, lines 29-30.
    LOGHUB = [
        "Dec 10 07:13:43 LabSZ sshd[24227]: Failed password for root from"
        " 5.36.59.76 port 42393 ssh2",
        "Dec 10 07:13:56 LabSZ sshd[24227]: message repeated 5 times: [ Failed"
        " password for root from 5.36.59.76 port 42393 ssh2]",
    ]

    def test_the_loghub_lines_are_six_attempts(self):
        assert _rows(_text(self.LOGHUB)) == {
            "5.36.59.76": "failed_password=6 → auth total=6"
        }

    def test_event_types_still_counts_lines(self):
        assert _event_lines(self.LOGHUB) == {"failed_password": 2}

    def test_the_header_says_which_count_is_which(self):
        assert _REPEAT_NOTE in _search_map(self.LOGHUB)

    def test_no_repeat_line_no_note(self):
        """Said only where it applies: without a repeat the header is as before."""
        search_map = _search_map(self.LOGHUB[:1] * 2)
        assert "IP auth breakdown" in search_map
        assert _REPEAT_NOTE not in search_map

    def test_the_pam_fallback_sees_the_weighted_count(self):
        """Format B has no outcome line, so the weighted PAM count is the total."""
        tagged = "Jun 14 15:16:0{i} combo sshd(pam_unix)[1993{i}]: "
        pam = (
            "authentication failure; logname= uid=0 euid=0 tty=NODEVssh ruser="
            " rhost=218.188.2.4"
        )
        lines = [
            tagged.format(i=1) + pam,
            tagged.format(i=2) + f"message repeated 3 times: [ {pam}]",
        ]
        assert _rows(_text(lines)) == {
            "218.188.2.4": "pam_auth_failure=4 → auth total=4"
        }
        assert _event_lines(lines) == {"pam_auth_failure": 2}
        # FILE SUMMARY counts lines, like "Event types".
        assert "Dominant activity: pam auth failure (2)." in _summary(lines)

    def test_an_unread_repeat_line_is_one_line(self):
        """The weight is ``sshd_auth``'s reading; an unread line is read as before."""
        lines = [f"/var/log/auth.log.1:{line}" for line in self.LOGHUB]
        assert _rows(_text(lines)) == {"5.36.59.76": "failed_password=2 → auth total=2"}


# fm#1654's review probe: a Format A (OpenSSH) host beside a Format B (loghub
# Linux) host.
_FORMAT_A = [
    line
    for i in range(3)
    for line in (
        f"Dec 10 07:0{i}:01 h sshd[1{i}]: Invalid user u{i} from 1.1.1.1",
        f"Dec 10 07:0{i}:02 h sshd[1{i}]: pam_unix(sshd:auth): authentication"
        " failure; logname= uid=0 euid=0 tty=ssh ruser= rhost=1.1.1.1",
        f"Dec 10 07:0{i}:03 h sshd[1{i}]: Failed password for invalid user u{i}"
        " from 1.1.1.1 port 22 ssh2",
    )
]
_FORMAT_B = _format_b("2.2.2.2", 4)


@pytest.mark.unit
class TestTheSummaryPamFigureIsDecidedPerIp:
    """FILE SUMMARY's PAM figure follows the breakdown's per-IP rule (fm#1654).

    It used to drop every PAM line when any line in the file was a
    ``Failed password``, so a Format B host's failures vanished from the
    summary while its breakdown row counted them.
    """

    def test_the_issue_probe(self):
        lines = _FORMAT_A + _FORMAT_B
        assert _rows(_text(lines)) == {
            "1.1.1.1": "failed_password=3, pam_auth_failure=3, invalid_user=6"
            " → auth total=3",
            "2.2.2.2": "pam_auth_failure=4 → auth total=4",
        }
        # main: "invalid user (6), failed password (3)."
        assert (
            "Dominant activity: invalid user (6), pam auth failure (4),"
            " failed password (3)." in _summary(lines)
        ), _summary(lines)

    def test_a_format_a_only_file_is_unchanged(self):
        """Every PAM line accompanies an outcome line: no PAM figure. As main."""
        assert "Dominant activity: invalid user (6), failed password (3)." in (
            _summary(_FORMAT_A)
        )

    def test_a_format_b_only_file_is_unchanged(self):
        """No outcome line anywhere: every PAM line is an attempt. As main."""
        assert "Dominant activity: pam auth failure (4)." in _summary(_FORMAT_B)

    def test_an_uncredited_pam_line_counts_only_without_outcome_lines(self):
        """A PAM line whose ``rhost`` is a PTR name is credited to no IP. It is
        an attempt only where nothing in the file is an outcome line."""
        uncredited = (
            "Jun 14 15:20:01 combo sshd(pam_unix)[20000]: authentication failure;"
            " logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=1.1.1.1.dyn.example"
        )
        assert "Dominant activity: pam auth failure (5)." in _summary(
            _FORMAT_B + [uncredited]
        )
        assert "pam auth failure (4)" in _summary(_FORMAT_A + _FORMAT_B + [uncredited])


# One attempt through a multi-step login: its ``Postponed``/``Partial`` lines
# are steps, and only the final ``Accepted`` line is its outcome (fm#1656).
_POSTPONED = [
    "Postponed keyboard-interactive for root from {ip} port 22 ssh2 [preauth]",
    "Postponed keyboard-interactive/pam for root from {ip} port 22 ssh2 [preauth]",
    "Accepted keyboard-interactive/pam for root from {ip} port 22 ssh2",
]
_PARTIAL = [
    "Partial publickey for root from {ip} port 22 ssh2: ED25519 SHA256:abc",
    "Accepted keyboard-interactive/pam for root from {ip} port 22 ssh2",
]
# The two readers: a header ``sshd_auth`` reads (``AUTH_OUTCOME_RE`` decides)
# and ``grep -H`` output, which it does not (``_SSHD_AUTH_OUTCOME_RE``).
_READERS = {
    "read": "Dec 10 06:55:46 LabSZ sshd[24200]: {m}",
    "searched": "/var/log/auth.log.1:Dec 10 06:55:46 LabSZ sshd[24200]: {m}",
}


@pytest.mark.unit
class TestPartialAndPostponedAreNotOutcomes:
    """``Partial``/``Postponed`` are steps of one attempt, not attempts."""

    @pytest.mark.parametrize("reader", sorted(_READERS))
    @pytest.mark.parametrize(
        "session", [_POSTPONED, _PARTIAL], ids=["postponed", "partial"]
    )
    def test_one_attempt_is_one(self, reader, session):
        from faultmaven.modules.preprocessing.extractors.sshd_auth import (
            read_sshd_auth_line,
        )

        ip = "203.0.113.8"
        lines = [_READERS[reader].format(m=m.format(ip=ip)) for m in session]
        assert {read_sshd_auth_line(line).read for line in lines} == {reader == "read"}
        assert _rows(_text(lines)) == {ip: "other_outcome=1 → auth total=1"}
