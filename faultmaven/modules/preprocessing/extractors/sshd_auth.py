"""What sshd wrote on an auth log line, as distinct from what its client wrote.

sshd copies client-supplied text into its log lines verbatim — the login name
above all (``Invalid user <name> from <ip>``), but also a certificate key id,
the ``client user``/``client host`` of hostbased auth, and a disconnect
reason. A pattern searched for anywhere in the line therefore also matches
inside that text (fm#1657)::

    sshd[1]: Invalid user Failed password for x from 7.7.7.7 from 1.2.3.4

is one invalid-user probe from 1.2.3.4, and read with ``search`` it was also a
password failure — attributed to 7.7.7.7 as well, because every IPv4 on the
line was credited with every event on it. A login name therefore raised its
sender's attempt count or planted one against any address it spelled out.

Two places on an sshd line are fixed by sshd, and this module reads only
those:

* the START of the message — the words before the first client field. Every
  event rule is matched there (``re.match`` on the message), never searched
  for.
* the ADDRESS SLOT — where sshd writes the connection's remote address. An
  event is credited to the address in that slot, never to an IPv4 that merely
  appears on the line. Where client text sits on BOTH sides of the slot
  (certificate key ids, hostbased ``client host``, a disconnect reason) a
  lookalike can make two slots parse; such a line is credited to nobody,
  because either choice could be the attacker's.

THE RULE. Finding the message start means removing the header a log pipeline
writes in front of it, and there is always another export format. So this
reading applies only to a line it POSITIVELY reads: one whose header gives
sshd's own tag (``sshd[pid]:``, ``sshd-session[pid]:``, ``sshd(pam_unix)[pid]:``,
``sshd@<unit>[pid]:``), or whose message start, as read, opens with an sshd
event. Every other line is ``SshdAuthLine.read = False``, and the logs
extractor reads it exactly as it did before fm#1657 — the same patterns
searched anywhere, every IPv4 credited. An unread format therefore never
counts less than that, by construction, and keeps that reading's exposure;
every format read here gets the protection. A line with no word any rule of
either reading needs is decided here as "no event" (``_EVENT_WORDS``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The header in front of the message.
# ---------------------------------------------------------------------------

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_TIMEZONE = r"(?:UTC|GMT|[A-Z]{1,4}[DS]?T)"
_LEVEL_WORD = (
    r"(?:EMERG|ALERT|CRIT(?:ICAL)?|ERR(?:OR)?|WARN(?:ING)?|NOTICE|INFO|DEBUG|TRACE"
    r"|FATAL)"
)
# One whitespace-delimited token of a timestamp as log pipelines spell them:
# BSD (``Dec 10 06:55:46``), ISO/RFC 3339, ``journalctl -o short-full``
# (``Wed 2026-07-22 09:47:32 UTC``), ``-o short-unix`` epoch seconds,
# ``-o short-monotonic``/``short-delta`` and any other bracketed timestamp
# (``[2026-09-20 10:00:01]``), s6-log's TAI64N label, the BGL/Thunderbird nil
# flag ``-``, and a level column some exporters put before the host. Every
# alternative starts with a digit, a bracket, ``@`` or ``-``, or is a fixed
# name — none is a word an sshd message starts with, so this run cannot
# extend into a message. The bracket's first run excludes digits so an
# unclosed ``[`` costs one pass over the line, not one per digit in it.
_TIME_TOKEN = (
    rf"(?:{_MONTH}|{_WEEKDAY}|{_TIMEZONE}|{_LEVEL_WORD}|-"
    r"|\[[^\]\n\d]*\d[^\]\n]*\]"
    r"|@[0-9A-Fa-f]{24}"
    r"|[+-]?\d[\dTZ:.,/+-]*)"
)
# ``program[pid]:`` / ``program(module)[pid]:`` / ``program:`` — the syslog
# TAG. ``module`` is loghub Linux's Format B (``sshd(pam_unix)[19939]:``);
# the optional parens around ``program`` are systemd's ``(systemd)[1]:``, and
# some syslogds write the program as a path (``/usr/sbin/sshd[1]:``). A
# socket-activated sshd is a unit instance (``sshd@7-10.0.0.5:22-….service``),
# and ``log show --style compact`` writes ``[pid:tid]`` with no colon.
_TAG = (
    r"(?P<program>\(?[A-Za-z_/][\w./-]*\)?(?:@[^\s\[\]]*)?)"
    r"(?P<module>\([^()\s]*\))?(?:\[[\w:]+\]:?|:)(?:\s+|$)"
)
# A host never ends in ``:`` — a token that does is the tag. Without that
# exclusion ``sshd[1]: error: x`` would read ``sshd[1]:`` as the host.
_HOST = r"\S*[^\s:]"

_SYSLOG_HEADER_RE = re.compile(
    # A container runner's prefix: ``docker compose logs`` (``sshd-1  | ``)
    # and ``kubectl logs --prefix`` (``[pod/sshd-0/sshd] ``).
    r"(?:\S+\s+\|\s+|\[[a-z]+/[^\]\s]+\]\s+)?"
    # RFC 3164 on the wire keeps its ``<PRI>``.
    r"(?:<\d{1,3}>)?"
    # BGL/Thunderbird prefix: flag, epoch, dotted date, node — then an
    # ordinary BSD line (same shape as ``LogsAndErrorsExtractor._BGL_LINE_RE``).
    r"(?:(?:-|[A-Z][A-Z0-9]{2,11})\s+[12]\d{9}\s+\d{4}\.\d{2}\.\d{2}\s+\S+\s+)?"
    rf"(?:{_TIME_TOKEN}\s+)*"
    # CRI container log (``/var/log/containers``): ``<ts> stderr F <msg>``.
    r"(?:(?:stdout|stderr)\s+[FP]\s+)?"
    # ``journalctl -o export`` / ``-o verbose``: one field per line.
    r"(?:MESSAGE=)?"
    rf"(?:(?:{_HOST}\s+)?{_TAG})?"
)
# RFC 5424: ``<PRI>VER TIMESTAMP HOST APP-NAME PROCID MSGID SD [MSG]``. No
# colon tag — APP-NAME is a field of its own.
_RFC5424_HEADER_RE = re.compile(
    r"<\d{1,3}>\d{1,2}\s+\S+\s+\S+\s+(?P<program>\S+)\s+\S+\s+\S+\s+"
    r'(?:-|(?:\[(?:[^\]"\\]|\\.|"(?:[^"\\]|\\.)*")*\])+)(?:\s+\ufeff?|$)'
)
# What sshd and syslogd put between the tag and the words of the message:
# Solaris' ``[ID 800047 auth.info]``, sshd's own level prefix for error,
# fatal and debug messages (log.c prefixes ``error: `` etc. on syslog; INFO
# and VERBOSE carry none), an exporter's level column, and rsyslog's
# repeated-message wrapper. None of them is client text.
_MESSAGE_LEVEL = rf"(?:(?:error|fatal|debug[1-3]?|verbose):\s+|{_LEVEL_WORD}\s+)?"
_MESSAGE_PREFIX_RE = re.compile(
    r"(?:\[ID \d+ [\w.]+\]\s+)?"
    + _MESSAGE_LEVEL
    + r"(?P<repeat>message repeated \d+ times: \[\s*"
    + _MESSAGE_LEVEL
    + r")?"
)
# JSON-lines exports carry the message as one field (journald ``MESSAGE``,
# docker json-file ``log``). Any other key is a format this module does not
# read, and the line is left to the search.
_JSON_MESSAGE_KEYS = ("MESSAGE", "log")
_JSON_PROGRAM_KEYS = ("SYSLOG_IDENTIFIER",)


@dataclass(frozen=True)
class SyslogMessage:
    """One line split at the message start: the tag's parts, then the words."""

    program: str
    module: str
    text: str


def split_syslog_line(line: str) -> SyslogMessage:
    """Split ``line`` into its syslog tag and the message after it.

    Header shapes read, each only at the START of the line so nothing a client
    wrote later can pose as one:

    * BSD syslog, with any timestamp spelling in ``_TIME_TOKEN`` and an
      optional host: ``Dec 10 06:55:46 LabSZ sshd[24200]: …``, loghub Linux's
      ``… combo sshd(pam_unix)[19939]: …``, ``2026-09-13T01:17:01+00:00 host
      sshd-session[1]: …``, every ``journalctl -o short*``/``with-unit``
      layout, and a bare ``sshd[1]: …``;
    * the BGL/Thunderbird prefix in front of a BSD line;
    * RFC 5424;
    * CRI container logs, ``docker compose logs`` and ``kubectl logs
      --prefix`` prefixes, ``journalctl -o export``/``verbose`` ``MESSAGE=``
      lines, and JSON lines (``journalctl -o json``, docker json-file);
    * no header at all (``journalctl -o cat``, ``sshd -e``), where the message
      is the line.

    After the tag, Solaris' msgid, a level prefix and rsyslog's
    ``message repeated N times: [ … ]`` wrapper are removed too. Whether the
    split is one the reader TRUSTS is decided by :func:`read_sshd_auth_line`.
    """
    # Leading indentation is a paste artefact (and ``journalctl -o verbose``'s
    # field indent), never part of what sshd wrote.
    line = line.lstrip()
    if line.startswith("{"):
        return _split_json_line(line)
    return _split_text_line(line)


def _split_text_line(line: str) -> SyslogMessage:
    rfc5424 = _RFC5424_HEADER_RE.match(line)
    if rfc5424:
        program, module, rest = rfc5424.group("program"), "", line[rfc5424.end() :]
    else:
        header = _SYSLOG_HEADER_RE.match(line)
        program = header.group("program") or ""
        module = header.group("module") or ""
        rest = line[header.end() :]
    return SyslogMessage(program, module, _strip_message_prefix(rest))


def _strip_message_prefix(rest: str) -> str:
    prefix = _MESSAGE_PREFIX_RE.match(rest)
    text = rest[prefix.end() :].rstrip()
    if prefix.group("repeat") and text.endswith("]"):
        text = text[:-1].rstrip()
    return text


def _split_json_line(line: str) -> SyslogMessage:
    try:
        record = json.loads(line)
    except ValueError:
        return SyslogMessage("", "", line)
    if not isinstance(record, dict):
        return SyslogMessage("", "", line)
    value = next(
        (record[k] for k in _JSON_MESSAGE_KEYS if isinstance(record.get(k), str)), None
    )
    if value is None:
        return SyslogMessage("", "", "")
    program = next(
        (record[k] for k in _JSON_PROGRAM_KEYS if isinstance(record.get(k), str)), ""
    )
    # The field may itself hold a whole syslog line (docker json-file of a
    # container that runs syslogd). It is read as text, never as JSON again:
    # one level, however the value is nested.
    inner = _split_text_line(value.rstrip("\n").lstrip())
    return SyslogMessage(inner.program or program, inner.module, inner.text)


def _is_sshd(program: str) -> bool:
    """``sshd``, ``sshd-session``, ``sshd-auth``, ``sshd@…``, ``/usr/sbin/sshd``."""
    return program.lower().rsplit("/", 1)[-1].lstrip("(").startswith("sshd")


# ---------------------------------------------------------------------------
# The events, matched at the message start.
# ---------------------------------------------------------------------------

# ``Failed``/``Accepted`` lines are OpenSSH ``auth_log`` (auth.c):
#   ``<verdict> <method>[/<submethod>] for [invalid user ]<user> from <ip>
#     port <n> ssh2[: <extra>]``
FAILED_PASSWORD_RE = re.compile(r"Failed password\b", re.IGNORECASE)
ACCEPTED_LOGIN_RE = re.compile(r"Accepted (?:password|publickey)\b", re.IGNORECASE)
# The attempt-OUTCOME test (fm#1627): the same line for every method, not
# only the two the categories name. Deliberately not outcomes: ``Failed
# none`` (a credential-less probe for the allowed methods) and ``Partial``/
# ``Postponed`` (one step of a login whose own outcome line follows).
AUTH_OUTCOME_RE = re.compile(
    r"(?:Failed|Accepted)\s+"
    r"(?:password|publickey|hostbased|keyboard-interactive(?:/\w+)?"
    r"|gssapi(?:-[\w-]+)?)\s+for\s",
    re.IGNORECASE,
)
# Every OpenSSH message that says ``invalid user`` says it immediately before
# the client's login name, so the phrase is genuine only where one of these
# fixed leads puts it:
#   ``Invalid user X from …``                        (auth.c getpwnamallow)
#   ``<verdict> <method> for invalid user X …``      (auth.c auth_log)
#   ``maximum authentication attempts exceeded for invalid user X …``
#   ``Too many authentication failures for invalid user X``   (pre-7.x)
#   ``input_userauth_request: invalid user X``       (``<func>: `` debug form)
#   ``Connection closed by invalid user X <ip> port …`` and the other
#     messages built on packet.c ``sshpkt_fmt_connection_id``
INVALID_USER_RE = re.compile(
    r"(?:[\w-]+:\s+)?"
    r"(?:(?:Failed|Accepted|Partial|Postponed)\s+\S+\s+for\s+"
    r"|(?:maximum authentication attempts exceeded|Too many authentication failures)"
    r"\s+for\s+"
    r"|(?:Connection (?:closed|reset) by|Connection from|Disconnected from"
    r"|Disconnecting|Received disconnect from|Timeout, client not responding from"
    r"|Unable to negotiate with)\s+"
    r")?invalid user\b",
    re.IGNORECASE,
)
# PAM's failure line in its two syslog shapes:
#   A. ``sshd[1]: pam_unix(sshd:auth): authentication failure; …`` — the
#      module name opens the message (any service: sshd, sudo, su, …);
#   B. ``sshd(pam_unix)[1]: authentication failure; …`` — loghub Linux, where
#      the module rides in the tag and the message opens with the phrase.
PAM_AUTH_FAILURE_RE = re.compile(
    r"pam_unix\([^)]*\):\s*authentication failure", re.IGNORECASE
)
PAM_TAGGED_FAILURE_RE = re.compile(r"authentication failure", re.IGNORECASE)
# ``session opened for user`` counted only from an sshd tag, so su/cron/login
# sessions stay out (logs-linux-01 q6, ISS-007).
SSH_SESSION_RE = re.compile(r"session opened for user\b", re.IGNORECASE)
# OpenSSH's reverse-DNS check (auth.c remote_hostname). The PTR name is the
# remote side's to choose, so it is read as one token and the address comes
# from its own slot, never from the name.
BREAK_IN_ATTEMPT_RE = re.compile(
    r"(?:reverse mapping checking getaddrinfo for \S+ \[(?P<reverse_addr>[^\]\s]+)\]"
    r" failed"
    r"|Address (?P<forward_addr>\S+) maps to \S+,"
    r" but this does not map back to the address)"
    r"\s+-\s+POSSIBLE BREAK-IN ATTEMPT",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# The address slot.
# ---------------------------------------------------------------------------

# sshd's own lines fit a 1 KiB buffer (log.c MSGBUFSIZ). The slot parse
# backtracks over the client field, so a pasted multi-kilobyte line is not
# parsed for an address at all rather than parsed slowly.
_MAX_ADDRESS_PARSE_CHARS = 4096

# Where sshd writes the remote address, in its two spellings: auth.c's
# ``from <ip> port <n> ssh2`` and packet.c ``sshpkt_fmt_connection_id``'s
# ``<ip> port <n>``. A leading greedy ``.*`` finds the RIGHTMOST slot the tail
# admits, a lazy ``.*?`` the LEFTMOST. The lead phrase is not part of these:
# it only selects the shape, and it holds no slot lookalike.
_FROM_SLOT = r"\sfrom\s+(?P<addr>\S+)(?:\s+port\s+\d+)?(?:\s+ssh[12])?"
_BARE_SLOT = r"\s(?P<addr>\S+)\s+port\s+\d+"
_PREAUTH_END = r"(?:\s+\[preauth\])?\s*$"
# Shapes where sshd writes nothing of the client's after the slot: the
# rightmost slot is sshd's own whatever follows it, so the tail is not
# checked and an unfamiliar suffix does not cost the line its address.
_ANY_TAIL = r"(?:\s.*)?$"
# Shapes where sshd appends client text after the slot, so a lookalike can
# sit on either side and both readings must agree on one slot.
#   auth_log's ``ssh2: <key>`` extra: a certificate's key id, hostbased's
#   ``client user``/``client host``;
_KEY_TAIL = r"(?::\s.*)?" + _PREAUTH_END
#   and a connection id followed by a reason (``Received disconnect … :11:
#   <the client's text>``).
_REASON_TAIL = r"(?:\s+timed out|:.*)?" + _PREAUTH_END
_VERDICT = r"(?:Failed|Accepted|Partial|Postponed)"


@dataclass(frozen=True)
class _SlotShape:
    """One message shape: the lead that selects it, and how its slot is read.

    ``leftmost`` is None where no client text follows the slot, and then the
    rightmost slot is the answer; otherwise the two must agree.
    """

    lead: "re.Pattern[str]"
    rightmost: "re.Pattern[str]"
    leftmost: "re.Pattern[str] | None" = None


# Selected by the FIRST lead that opens the message.
_SLOT_SHAPES: tuple[_SlotShape, ...] = (
    # auth.c ``auth_log`` for password, none and keyboard-interactive, which
    # append nothing after ``ssh2``.
    _SlotShape(
        re.compile(
            _VERDICT + r"\s+(?:password|none|keyboard-interactive(?:/\w+)?)\s",
            re.IGNORECASE,
        ),
        re.compile(r".*" + _FROM_SLOT + _ANY_TAIL, re.IGNORECASE),
    ),
    # ... and for publickey, hostbased and the rest, which append the key.
    _SlotShape(
        re.compile(_VERDICT + r"\s", re.IGNORECASE),
        re.compile(r".*" + _FROM_SLOT + _KEY_TAIL, re.IGNORECASE),
        re.compile(r".*?" + _FROM_SLOT + _KEY_TAIL, re.IGNORECASE),
    ),
    # auth.c ``auth_maxtries_exceeded``
    _SlotShape(
        re.compile(r"maximum authentication attempts exceeded\s", re.IGNORECASE),
        re.compile(r".*" + _FROM_SLOT + _ANY_TAIL, re.IGNORECASE),
    ),
    # auth.c ``getpwnamallow``: ``Invalid user X from <ip>[ port <n>]``.
    # Case-sensitive: sshd capitalises this message, and the lower-case
    # ``invalid user X`` of ``input_userauth_request: invalid user X`` carries
    # no address — headerless output can read that function name as the tag.
    _SlotShape(
        re.compile(r"Invalid user\s"),
        re.compile(
            r".*\sfrom\s+(?P<addr>\S+)(?:\s+port\s+\d+)?" + _ANY_TAIL, re.IGNORECASE
        ),
    ),
    # packet.c ``sshpkt_fmt_connection_id`` leads that end at the slot ...
    _SlotShape(
        re.compile(
            r"(?:[\w-]+:\s+)?(?:Connection (?:closed|reset) by|Disconnected from"
            r"|Timeout, client not responding from)\s",
            re.IGNORECASE,
        ),
        re.compile(r".*" + _BARE_SLOT + _ANY_TAIL, re.IGNORECASE),
    ),
    # ... and those that append a reason after it.
    _SlotShape(
        re.compile(
            r"(?:[\w-]+:\s+)?(?:Connection from|Disconnecting|Received disconnect"
            r" from|Unable to negotiate with)\s",
            re.IGNORECASE,
        ),
        re.compile(r".*" + _BARE_SLOT + _REASON_TAIL, re.IGNORECASE),
        re.compile(r".*?" + _BARE_SLOT + _REASON_TAIL, re.IGNORECASE),
    ),
)
# pam_unix: ``… ruser=<r> rhost=<host>[  user=<name>]``. Client text can sit
# on either side of the slot: vsftpd fills ``ruser`` with the client's FTP
# ``USER`` (sysdeputil.c), and with pam_unix's ``audit`` option an unknown
# login is logged as ``user=<name>`` after it. One ``rhost=`` is the slot; a
# second means one of them is a client's, and the line is credited to nobody.
_PAM_RHOST_RE = re.compile(r"(?:^|\s)rhost=(?P<addr>\S*)")


def _slot_address(message: str) -> str | None:
    if len(message) > _MAX_ADDRESS_PARSE_CHARS:
        return None
    for shape in _SLOT_SHAPES:
        if not shape.lead.match(message):
            continue
        rightmost = shape.rightmost.match(message)
        if rightmost is None:
            return None
        if shape.leftmost is not None:
            leftmost = shape.leftmost.match(message)
            if leftmost is None or leftmost.span("addr") != rightmost.span("addr"):
                return None
        return rightmost.group("addr")
    return None


def _rhost_address(message: str) -> str | None:
    slots = [m.group("addr") for m in _PAM_RHOST_RE.finditer(message)]
    return slots[0] if len(slots) == 1 else None


@dataclass(frozen=True)
class SshdAuthLine:
    """The sshd auth reading of one log line.

    ``read`` is True when this module's reading is the one to use — the line
    was positively read (module docstring), or holds no word any rule needs.
    When False, the caller reads the line exactly as before fm#1657, and the
    other fields are empty.

    ``events`` are the categories whose phrase opens the message, in the
    extractor's per-line order. ``outcome`` is True on an attempt's outcome
    line (fm#1627). ``address`` is the remote address in sshd's slot, or None
    when the line has no slot or more than one slot parses.
    """

    events: tuple[str, ...]
    outcome: bool
    address: str | None
    read: bool = True


_NO_EVENT = SshdAuthLine((), False, None)
_UNREAD = SshdAuthLine((), False, None, read=False)
# A word every event rule needs — this module's AND the search the unread
# reading uses — lower-cased: a line holding none of them cannot be an event
# under either reading, so it is decided here without parsing. Most lines of
# most logs hold none. ``test_every_rule_needs_an_event_word`` checks both
# rule sets against this list.
_EVENT_WORDS = (
    "password",
    "publickey",
    "hostbased",
    "keyboard-interactive",
    "gssapi",
    "invalid user",
    "authentication failure",
    "break-in",
    "session opened",
)


def read_sshd_auth_line(line: str) -> SshdAuthLine:
    """Read ``line`` by the words sshd opened its message with, if it can.

    The categories are those the logs extractor counts from sshd —
    ``failed_password``, ``accepted_login``, ``invalid_user``,
    ``break_in_attempt``, ``pam_auth_failure``, ``ssh_session_opened`` — plus
    the outcome test. ``connection_closed`` is not read here: sshd itself
    writes that phrase mid-message (``fatal: Write failed: Connection reset by
    peer``), as does every other network daemon, so there is no fixed
    position to anchor it to.

    Returns ``read=False`` for a line this module does not positively read.
    """
    lowered = line.lower()
    if not any(word in lowered for word in _EVENT_WORDS):
        return _NO_EVENT
    message = split_syslog_line(line)
    reading = _read_message(message)
    if reading is not None:
        return reading
    if _is_sshd(message.program):
        return _NO_EVENT
    return _UNREAD


def _read_message(message: SyslogMessage) -> SshdAuthLine | None:
    text = message.text
    if not text:
        return None
    events: list[str] = []
    if FAILED_PASSWORD_RE.match(text):
        events.append("failed_password")
    if ACCEPTED_LOGIN_RE.match(text):
        events.append("accepted_login")
    if INVALID_USER_RE.match(text):
        events.append("invalid_user")
    break_in = BREAK_IN_ATTEMPT_RE.match(text)
    if break_in:
        events.append("break_in_attempt")
    tag = (message.program + message.module).lower()
    pam_tagged = "(pam_unix)" in tag or message.program.lower() == "pam_unix"
    pam_failure = bool(PAM_AUTH_FAILURE_RE.match(text)) or (
        pam_tagged and bool(PAM_TAGGED_FAILURE_RE.match(text))
    )
    if pam_failure:
        events.append("pam_auth_failure")
    if _is_sshd(message.program) and SSH_SESSION_RE.match(text):
        events.append("ssh_session_opened")
    outcome = bool(AUTH_OUTCOME_RE.match(text)) or not {
        "failed_password",
        "accepted_login",
    }.isdisjoint(events)
    if not events and not outcome:
        return None
    if break_in:
        address = break_in.group("reverse_addr") or break_in.group("forward_addr")
    elif pam_failure:
        address = _rhost_address(text)
    else:
        address = _slot_address(text)
    return SshdAuthLine(tuple(events), outcome, _unmapped(address))


def _unmapped(address: str | None) -> str | None:
    """An IPv4-mapped IPv6 address as its IPv4 (``::ffff:203.0.113.9``).

    Dual-stack daemons write their IPv4 peers that way (vsftpd
    ``listen_ipv6``, dovecot, older sshd); the address is the same, and the
    breakdown keys on IPv4.
    """
    if address and address[:7].lower() == "::ffff:" and "." in address:
        return address[7:]
    return address
