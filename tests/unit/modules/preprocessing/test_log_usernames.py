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

_USER_ROW = re.compile(r"^ {4}(\S.*?): (\d+) mentions")


def profile_usernames(content: str) -> list[str]:
    """Usernames as ``LogsAndErrorsExtractor.extract()`` renders them."""
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
            found.append(match.group(1))
    return found


def registry_usernames(content: str) -> list[str]:
    """USER rows as ``extract_entities_for_data_type`` emits them."""
    return [
        obs.entity_value
        for obs in extract_entities_for_data_type(DataType.LOGS_AND_ERRORS, content)
        if obs.entity_type == EntityType.USER
    ]


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
    """The two implementations became one; their answers must not diverge."""
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
    assert set(profile) == {"root", "cyrus", "test"}, profile
    assert set(registry) == {"root", "cyrus", "test"}, registry


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


def _repo_root() -> pathlib.Path:
    """Anchor on the imported package, not on cwd.

    An editable install can resolve ``faultmaven`` to a different checkout
    than the test file lives in; scanning the tree that was *imported* is
    the only anchor that cannot disagree with what the other tests measured.
    """
    return _PACKAGE_ROOT.parent


# The scan must see every shape this codebase actually writes a regex in,
# not just the one the rule happens to use. Measured on the tree at the time
# it was written: 205 ``re.compile`` sites, 191 inline ``re.<method>(pattern,
# ...)`` sites, 6 ``re.compile(<module-level const>)`` sites and 2
# ``regex.<method>`` sites. A scan that watched only ``re.compile`` with a
# literal would have missed 199 of them, including the inline form, which is
# the house idiom. Widening to all of them costs **zero** false positives.
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


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    """``PATTERN = r"..."`` bindings, so ``re.compile(PATTERN)`` is not a blind spot."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            text = _pattern_text(node.value)
            if text:
                found[node.targets[0].id] = text
    return found


def _has_capture_group(pattern: str) -> bool:
    stripped = re.sub(r"\(\?[:=!<#aiLmsux]", "", pattern)
    return "(" in stripped


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
    """
    root = _repo_root()
    scanned: list[pathlib.Path] = []
    offenders: list[str] = []

    for path in sorted((root / "faultmaven").rglob("*.py")):
        scanned.append(path)
        tree = _parse(path)
        if tree is None:  # pragma: no cover - defensive
            continue
        consts = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in _RE_MODULES
                and node.func.attr in _RE_FUNCTIONS
                and node.args
            ):
                continue
            pattern = _pattern_text(node.args[0], consts)
            if "user" not in pattern.lower() or not _has_capture_group(pattern):
                continue
            relative = path.relative_to(root)
            if relative == _RULE_HOME:
                continue
            offenders.append(f"{relative}:{node.lineno}  {pattern!r}")

    # The scan must have reached the places the rule can be re-introduced.
    for directory in _MUST_SCAN:
        assert any(
            str(p.relative_to(root)).startswith(str(directory)) for p in scanned
        ), f"scan never visited {directory}"

    assert not offenders, (
        "a username-capturing regex lives outside "
        f"{_RULE_HOME}; import extract_usernames instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.unit
@pytest.mark.architecture
def test_both_consumers_import_the_shared_rule() -> None:
    """The scan above only forbids a second regex; this pins the first is used."""
    root = _repo_root()
    for relative in (
        "faultmaven/modules/preprocessing/extractors/logs_extractor.py",
        "faultmaven/modules/preprocessing/entities/logs.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert (
            "from faultmaven.modules.preprocessing.log_usernames import" in source
        ), f"{relative} does not import the shared username rule"
        assert (
            "extract_usernames(" in source
        ), f"{relative} imports the rule but never calls it"
