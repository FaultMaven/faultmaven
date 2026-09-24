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

        The file-level summary applies its PAM rule once per file
        (``_build_summary``); the ruling applies this one per IP. A
        pam-only IP sharing a file with an OpenSSH one still reads its PAM
        count, not 0.
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
