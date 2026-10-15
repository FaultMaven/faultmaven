"""#1980 — one secret-detection pattern set, and every redaction path uses it.

Invariant (as restated by the #2021 review): every free-text redaction path
detects with one pattern set, ``SECRET_PATTERNS`` (the sanitizer's regex half
adds the network-address patterns); one pass is idempotent on both paths, the
sanitizer's and the document preprocessor's; no secret class or spelling main
detected is missed, except the trades listed (``test_the_listed_trades_hold``);
and every pattern runs in time linear in the text.

Pinned on each path:

1. every spelling in ``POSITIVE`` is one span of its entity type, and the
   secret does not survive ``sanitize_text_with_registry``,
   ``apply_regex_redaction`` or ``redact_sensitive_content``;
2. the negative corpus — #1993's fifty secret-free incident sentences and
   #1980's lines that share a noun or a shape with a secret — comes back
   unchanged, except ``JUSTIFIED``;
3. the document preprocessor's output is ``SECRET_PATTERNS``' output, row for
   row (a second list there would diverge);
4. seeded random secrets per class leave no six-character fragment in clear;
5. the trigger gating in ``find_redaction_spans`` changes no result;
6. one pass is idempotent, and secrets glued to one another are gone after it;
7. the review's rulings: a keyed value is a secret unless it is, whole, a
   word from a closed list each of whose entries a negative line needs (the
   113 common passwords all are secrets); the value alone is replaced (the
   key and delimiting quotes stay, one secret has one placeholder), and an
   unquoted value is its whole run, so no random printable password leaves a
   character in clear; a value on the next line is read after `=`, and after
   `:` only as a deeper-indented YAML continuation; an escaped newline or a
   percent byte is a boundary; binds/flags/references/markers are not
   secrets, as whole values only; the full review generator agrees with an
   independent reference and is idempotent on both paths and through the
   title scrub; and every hostile shape found in review costs linear time
   (``assert_linear_growth``).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import pathlib
import random
import re
import string

import pytest

from faultmaven.infrastructure.security import redaction
from faultmaven.infrastructure.security.case_redaction import (
    REDACTION_PLACEHOLDER_RE,
    CaseRedactionContext,
)
from faultmaven.infrastructure.security.redaction import (
    NETWORK_ADDRESS_PATTERNS,
    SANITIZER_PATTERNS,
    SECRET_PATTERNS,
    DataSanitizer,
    RedactionPattern,
    find_redaction_spans,
    redact_spans,
)
from faultmaven.modules.case.api.title_generation import _scrub_title_source
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    redact_sensitive_content,
)
from tests.wallclock import assert_linear_growth

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture(scope="module")
def sanitizer() -> DataSanitizer:
    return DataSanitizer()


def _spans(text: str, patterns=SANITIZER_PATTERNS) -> list[tuple[str, str]]:
    return [
        (s.entity_type, text[s.start : s.end])
        for s in find_redaction_spans(text, patterns)
    ]


def _marker(span, _value: str) -> str:
    return f"[REDACTED:{span.entity_type.lower()}]"


# ---------------------------------------------------------------------------
# 1. The spellings that must be detected
# ---------------------------------------------------------------------------

_GH = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"[:36]
#: A GitLab token's 20 characters after `glpat-` (#2012 review F10).
_GL = "Zq7Wm2Kx9Lp4Rt6Yv3Bn"
_PEM_BODY = (
    "MIIEowIBAAKCAQEA7bq98EXAMPLEb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9u\n"
    "ZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQAAACDc0Bn5dXN2bVt7Lfw1yZ8ql3pq"
)
_PEM_LINES = _PEM_BODY.replace("\\n", "\n")

#: Slack-shaped test tokens, assembled here so no token literal sits in the
#: source: push protection reads the shape, not whether a token is real.
_SLACK_BOT = "-".join(
    ("xoxb", "123456789012", "1234567890123", "AbCdEfGhIjKlMnOpQrStUvWx")
)
_SLACK_USER = "-".join(("xoxp", "123456789012", "1234567890123", "abcdef0123456789"))
_SLACK_SHORT = "-".join(("xoxb", "123456789012", "1234567890123", "AbCdEfGhIjKl"))

#: (id, text, entity type, the secret that must not survive). The entity type
#: names the placeholder (``<PASSWORD_digest>``) and the knowledge marker
#: (``[REDACTED:password]``).
POSITIVE = [
    # the keyed spellings the issue lists
    (
        "password-spaced-equals",
        "login fails with password = hunter2 on replica",
        "PASSWORD",
        "hunter2",
    ),
    (
        "password-spaced-colon",
        "dsn has password : hunter2 for app",
        "PASSWORD",
        "hunter2",
    ),
    (
        "password-equals",
        "db login fails with password=hunter2 on replica",
        "PASSWORD",
        "hunter2",
    ),
    ("password-colon-tight", "auth password:hunter2 rejected", "PASSWORD", "hunter2"),
    (
        "password-colon-credential",
        "password: MySecret123 on the replica",
        "PASSWORD",
        "MySecret123",
    ),
    (
        "password-yaml-line",
        "db:\n  host: primary\n  password: changeme\n  port: 5432",
        "PASSWORD",
        "changeme",
    ),
    (
        "password-json",
        'payload {"user": "app", "password": "s3cr3t pass"} rejected',
        "PASSWORD",
        "s3cr3t pass",
    ),
    ("password-quoted", "password = 'supersecret123'", "PASSWORD", "supersecret123"),
    (
        "password-starts-with-equals",
        "password==@9nhvy9zfRb on replica",
        "PASSWORD",
        "=@9nhvy9zfRb",
    ),
    (
        "password-yaml-line-punctuated",
        "db:\n  host: primary\n  password: o/ofz.\n  port: 5432",
        "PASSWORD",
        "o/ofz.",
    ),
    (
        "passwd-slashes",
        "ldap bind fails with passwd: w/ddrf/afdff for svc",
        "PASSWORD",
        "w/ddrf/afdff",
    ),
    (
        "passwd",
        "ldap bind fails with passwd: Tr0ub4dor&3 for svc",
        "PASSWORD",
        "Tr0ub4dor&3",
    ),
    ("pwd", "pwd=hunter2 in the dsn", "PASSWORD", "hunter2"),
    (
        "pwd-odbc",
        "Server=db;Uid=sa;Pwd=Sup3rS3cret;Database=orders",
        "PASSWORD",
        "Sup3rS3cret",
    ),
    (
        "db-password-env",
        "container env DB_PASSWORD=hunter2 rejected",
        "PASSWORD",
        "hunter2",
    ),
    ("pgpassword", "PGPASSWORD=hunter2 psql -h db -U app", "PASSWORD", "hunter2"),
    (
        "password-flag",
        "docker login -u ci --password Hunter2pw registry.example.com",
        "PASSWORD",
        "Hunter2pw",
    ),
    ("secret", "webhook fails with secret=hunter2 configured", "SECRET", "hunter2"),
    (
        "client-secret",
        "oauth exchange client_secret=Zx9-q.w~E1 invalid_client",
        "SECRET",
        "Zx9-q.w~E1",
    ),
    (
        "django-secret-key",
        "SECRET_KEY = 'django-insecure-0a9s8d7f'",
        "SECRET",
        "django-insecure-0a9s8d7f",
    ),
    (
        "client-secret-camel",
        '{"clientSecret": "Zx9qw7E1rT"} rejected',
        "SECRET",
        "Zx9qw7E1rT",
    ),
    ("token", "cli call with token=abc123def returns 403", "TOKEN", "abc123def"),
    (
        "access-token-json",
        '{"access_token":"AbC123xyz789","expires_in":3600}',
        "TOKEN",
        "AbC123xyz789",
    ),
    ("x-auth-token-header", "X-Auth-Token: 9f8e7d6c5b4a", "TOKEN", "9f8e7d6c5b4a"),
    ("token-flag", "kubectl --token Zk91mQ2x get pods", "TOKEN", "Zk91mQ2x"),
    (
        "api-key-any-value",
        "set api_key=my-secret-value-not-hex in the client",
        "API_KEY",
        "my-secret-value-not-hex",
    ),
    ("apikey-colon-tight", "apikey:my-secret-value", "API_KEY", "my-secret-value"),
    (
        "apikey-colon-credential",
        "header apikey: k3y-value-1 was rejected",
        "API_KEY",
        "k3y-value-1",
    ),
    (
        "apikey-yaml-line",
        "client:\n  apikey: mysecretvalue\n",
        "API_KEY",
        "mysecretvalue",
    ),
    (
        "api-key-hex",
        "gateway rejects api_key0123456789abcdef0123456789abcdef",
        "API_KEY",
        "0123456789abcdef0123456789abcdef",
    ),
    # header credentials
    ("bearer", "Authorization: Bearer abc.def.ghi", "BEARER_TOKEN", "abc.def.ghi"),
    (
        "bearer-opaque",
        "Authorization: Bearer DCpacLmdnMzGQWyGZraHRFoRuXXgLxXX",
        "BEARER_TOKEN",
        "DCpacLmdnMzGQWyGZraHRFoRuXXgLxXX",
    ),
    (
        "bearer-jwt",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "JWT_TOKEN",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    ),
    ("basic", "> Authorization: Basic dXNlcjpwYXNz", "BASIC_AUTH", "dXNlcjpwYXNz"),
    # connection URLs with credentials
    (
        "postgres-url",
        "postgres://app:Winter2024!x@db.internal:5432/app",
        "DATABASE_URL",
        "Winter2024!x",
    ),
    (
        "postgresql-url",
        "replica fails for postgresql://app:pw4321@db:5432/app",
        "DATABASE_URL",
        "pw4321",
    ),
    (
        "postgresql-driver-url",
        "DATABASE_URL=postgresql+asyncpg://app:pw4321@db/app",
        "DATABASE_URL",
        "pw4321",
    ),
    (
        "password-with-at-and-slash",
        "postgresql://app:p@ss/w0rd@db:5432 fails",
        "DATABASE_URL",
        "p@ss/w0rd",
    ),
    ("mysql-url", "mysql://root:toor99@mysql:3306/app", "DATABASE_URL", "toor99"),
    ("mongodb-url", "mongodb://admin:s3cret@mongo:27017", "DATABASE_URL", "s3cret"),
    (
        "mongodb-srv-url",
        "mongodb+srv://admin:s3cret@cluster0.abc.mongodb.net/db",
        "DATABASE_URL",
        "s3cret",
    ),
    ("redis-url", "redis://user:pass99@cache:6379/0", "DATABASE_URL", "pass99"),
    (
        "rediss-url",
        "rediss://:t0psecret@cache.example.com:6380",
        "DATABASE_URL",
        "t0psecret",
    ),
    ("amqp-url", "amqp://guest:guest1@rabbitmq:5672/", "DATABASE_URL", "guest1"),
    ("amqps-url", "amqps://app:mq-pass-7@mq:5671/vhost", "DATABASE_URL", "mq-pass-7"),
    (
        "https-credentials",
        "git clone https://oauth2:glpat-AbCd1234EfGh@gitlab.example.com/acme/app.git",
        "CREDENTIAL_URL",
        "glpat-AbCd1234EfGh",
    ),
    # provider tokens
    ("gitlab-pat", f"clone failed with glpat-{_GL}", "GITLAB_TOKEN", f"glpat-{_GL}"),
    (
        "gitlab-pat-as-url-user",
        f"git clone https://glpat-{_GL}@gitlab.com/acme/app.git",
        "GITLAB_TOKEN",
        f"glpat-{_GL}",
    ),
    (
        "gitlab-pat-routable",
        f"push uses glpat-{_GL}{_GL}.01abcdefg now",
        "GITLAB_TOKEN",
        f"glpat-{_GL}{_GL}",
    ),
    ("github-ghp", f"push rejected for ghp_{_GH}", "GITHUB_TOKEN", f"ghp_{_GH}"),
    ("github-gho", f"oauth token gho_{_GH} expired", "GITHUB_TOKEN", f"gho_{_GH}"),
    ("github-ghu", f"ghu_{_GH}", "GITHUB_TOKEN", f"ghu_{_GH}"),
    (
        "github-ghs",
        f"https://x-access-token:ghs_{_GH}@github.com/acme/app",
        "CREDENTIAL_URL",
        f"ghs_{_GH}",
    ),
    ("github-ghr", f"refresh ghr_{_GH}", "GITHUB_TOKEN", f"ghr_{_GH}"),
    (
        "github-pat",
        "GITHUB_PAT github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123",
        "GITHUB_TOKEN",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123",
    ),
    (
        "slack-bot",
        f"slack bot {_SLACK_BOT} revoked",
        "SLACK_TOKEN",
        _SLACK_BOT,
    ),
    (
        "slack-user",
        _SLACK_USER,
        "SLACK_TOKEN",
        _SLACK_USER,
    ),
    (
        "openai-classic",
        "key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd fails",
        "API_KEY",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd",
    ),
    (
        "openai-project",
        "OPENAI_API_KEY=sk-proj-Ab3dEf_gh1JkLmn0PqRsTuvWxYz-1234567890abcdefGHIJ",
        "API_KEY",
        "sk-proj-Ab3dEf_gh1JkLmn0PqRsTuvWxYz-1234567890abcdefGHIJ",
    ),
    (
        "anthropic",
        "key sk-ant-api03-AbCdEf0123456789_GhIjKl-MnOpQr0123 rate limited",
        "API_KEY",
        "sk-ant-api03-AbCdEf0123456789_GhIjKl-MnOpQr0123",
    ),
    (
        "aws-akia",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "AWS_ACCESS_KEY",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "aws-asia",
        "sts creds ASIAIOSFODNN7EXAMPLE expired",
        "AWS_ACCESS_KEY",
        "ASIAIOSFODNN7EXAMPLE",
    ),
    (
        "aws-secret",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AWS_SECRET_KEY",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ),
    (
        "jwt",
        "auth rejects token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl on every request",
        "JWT_TOKEN",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    ),
    (
        "k8s-secret",
        "pods crash reading k8s_secret_0123456789abcdef0123456789abcdef",
        "K8S_SECRET",
        "0123456789abcdef0123456789abcdef",
    ),
    # private keys
    (
        "pem-openssh",
        f"key:\n-----BEGIN OPENSSH PRIVATE KEY-----\n{_PEM_LINES}\n-----END OPENSSH PRIVATE KEY-----\nok",
        "PRIVATE_KEY",
        _PEM_LINES,
    ),
    (
        "pem-ec",
        f"-----BEGIN EC PRIVATE KEY-----\n{_PEM_LINES}\n-----END EC PRIVATE KEY-----",
        "PRIVATE_KEY",
        _PEM_LINES,
    ),
    (
        "pem-generic",
        f"-----BEGIN PRIVATE KEY-----{_PEM_LINES.replace(chr(10), '')}-----END PRIVATE KEY-----",
        "PRIVATE_KEY",
        _PEM_LINES.replace(chr(10), ""),
    ),
    (
        "pem-rsa",
        f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LINES}\n-----END RSA PRIVATE KEY-----",
        "PRIVATE_KEY",
        _PEM_LINES,
    ),
    (
        "pem-no-end",
        f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LINES}\n2024-01-01 next log line",
        "PRIVATE_KEY",
        _PEM_LINES,
    ),
    (
        "pem-no-end-json",
        '{"private_key": "-----BEGIN PRIVATE KEY-----\\n' + _PEM_BODY + '\\n"}',
        "PRIVATE_KEY",
        _PEM_BODY.replace("\\n", "")[:40],
    ),
    (
        "pem-no-end-flattened",
        f"-----BEGIN PRIVATE KEY----- {_PEM_LINES.replace(chr(10), ' ')}",
        "PRIVATE_KEY",
        _PEM_LINES.split(chr(10))[1],
    ),
]


@pytest.mark.parametrize(
    "text,entity_type,secret", [r[1:] for r in POSITIVE], ids=[r[0] for r in POSITIVE]
)
def test_each_spelling_is_one_span_of_its_type(text, entity_type, secret):
    spans = find_redaction_spans(text, SANITIZER_PATTERNS)
    assert [s.entity_type for s in spans] == [entity_type], _spans(text)
    (span,) = spans
    for fragment in secret.split("\n"):
        position = text.index(fragment)
        assert span.start <= position and position + len(fragment) <= span.end, (
            fragment,
            _spans(text),
        )


@pytest.mark.parametrize(
    "text,entity_type,secret", [r[1:] for r in POSITIVE], ids=[r[0] for r in POSITIVE]
)
def test_no_path_lets_a_spelling_through(sanitizer, text, entity_type, secret):
    registry: dict = {}
    full = sanitizer.sanitize_text_with_registry(text, registry)
    regex_half = sanitizer.apply_regex_redaction(text, {})
    document, report = redact_sensitive_content(text)
    for fragment in secret.split("\n"):
        assert fragment not in full
        assert fragment not in regex_half
        assert fragment not in document
    assert f"<{entity_type}_" in regex_half
    assert list(registry) == [entity_type]
    assert f"[REDACTED:{entity_type.lower()}]" in document
    assert report.total_redacted == 1
    assert [entry.type for entry in report.redactions] == [entity_type.lower()]


def test_a_url_password_is_cut_at_the_last_at_sign():
    """A raw `@` and `/` in the password: the user part runs to the LAST `@`
    in the URL, so no tail of the password survives (the former `[^@]+`
    stopped at the first and left `/w0rd@db:5432` in clear)."""
    assert _spans("postgresql://app:p@ss/w0rd@db:5432 fails") == [
        ("DATABASE_URL", "postgresql://app:p@ss/w0rd@db:5432")
    ]


#: The closed list a keyed value is never read as (#1980 review rulings):
#: states, not secrets, each one needed by a negative line
#: (``test_every_closed_list_entry_is_needed_by_a_negative_line``). Every
#: other value is one.
_STATUS_WORDS = [
    "YES", "no", "true", "False", "null", "required", "expired", "n/a",
    "incorrect", "invalid_grant", "changed", "rotated", "missing", "not",
    "str", "Optional[str]", "********", '""',
]  # fmt: skip
#: Words the list held before the review measured them on rockyou (R3): no
#: negative line needs them, so each is a secret like any other.
_FORMER_STATUS_WORDS = [
    "on", "off", "nil", "none", "empty", "optional", "set", "unset",
    "redacted", "hidden", "masked", "na", "invalid", "invalid_request",
    "bearer", "basic", "digest", "string", "bytes", "secretstr", "''",
]  # fmt: skip


@pytest.mark.parametrize("word", _STATUS_WORDS)
@pytest.mark.parametrize(
    "template",
    [
        "users see password: {w} after the cutover",
        "  token: {w}",
        '{{"password": "{w}"}}',
        "secret={w}",
    ],
)
def test_a_status_word_after_a_key_is_not_a_secret(template, word):
    text = template.format(w=word.strip('"') if '"' in template else word)
    assert _spans(text) == []


@pytest.mark.parametrize("word", _FORMER_STATUS_WORDS)
def test_a_word_no_negative_line_needs_is_a_secret(word):
    assert _spans(f"login with password: {word} failed") == [("PASSWORD", word)]
    assert _spans(f"secret={word}") == [("SECRET", word)]


#: The 113 common passwords the review drew from (rockyou's top list, vendor
#: defaults). Under the word-shape rule this replaced, 79 of them were missed
#: written mid-line; main missed none.
_COMMON_PASSWORDS = """123456 password 12345678 qwerty 123456789 12345 1234
111111 1234567 dragon 123123 baseball abc123 football monkey letmein 696969
shadow master 666666 qwertyuiop 123321 mustang 1234567890 michael 654321
superman 1qaz2wsx 7777777 121212 000000 qazwsx 123qwe killer trustno1 jordan
jennifer zxcvbnm asdfgh hunter buster soccer harley batman andrew tigger
sunshine iloveyou 2000 charlie robert thomas hockey ranger daniel starwars
klaster 112233 george computer michelle jessica pepper 1111 zxcvbn 555555
11111111 131313 freedom 777777 pass maggie 159753 aaaaaa ginger princess
joshua cheese amanda summer love ashley nicole chelsea biteme matthew access
yankees 987654321 dallas austin thunder taylor matrix admin changeme changeit
root toor guest default secret welcome Welcome Password P@ssw0rd Passw0rd
Summer2024 Welcome1 CHANGEME ADMIN letmein1 correct-horse-battery-staple""".split()


@pytest.mark.parametrize(
    "template",
    [
        "login with password: {w} failed",
        "2026-10-14 DEBUG db password: {w}",
        "user admin password: {w} on the box",
    ],
)
def test_every_common_password_after_a_key_is_redacted(sanitizer, template):
    assert len(_COMMON_PASSWORDS) == 113
    head, tail = template.split("{w}")
    missed = []
    for word in _COMMON_PASSWORDS:
        out = sanitizer.apply_regex_redaction(template.format(w=word), {})
        if not re.fullmatch(
            re.escape(head) + r"<PASSWORD_[0-9a-f]{16}>" + re.escape(tail), out
        ):
            missed.append((word, out))
    assert missed == [], missed[:5]


def test_one_span_never_wraps_another(sanitizer):
    """Detection reads the raw text, and overlapping matches merge into one
    span: a placeholder never holds another, so one reverse restores all."""
    text = "password=postgresql://app:pw4321@10.1.2.3:5432/app now"
    out = sanitizer.apply_regex_redaction(text, {})
    assert len(REDACTION_PLACEHOLDER_RE.findall(out)) == 1
    assert re.fullmatch(r"password=<PASSWORD_[0-9a-f]{16}> now", out), out


# ---------------------------------------------------------------------------
# 2. Ordinary incident text comes back unchanged
# ---------------------------------------------------------------------------

P11 = [
    "checkout api returns 502 for a third of requests since the v2.1.3 deploy",
    "orders service times out against postgresql://orders-db:5432/orders since 14:00",
    "mysql://replica-2:3306 lag climbs past 900s during the nightly batch",
    "mongodb://mongo-0.mongo:27017,mongo-1.mongo:27017/?replicaSet=rs0 elections every few minutes",
    "pods in 10.42.0.0/16 cannot reach 10.43.0.10 the cluster dns service",
    "node 192.168.1.20 flaps between ready and notready every 5 minutes",
    "upstream 172.20.3.4:8080 returns connection reset under load",
    "public resolver 8.8.8.8 times out from the eu-west nat gateway",
    "alerts to oncall@example.com stopped after the pagerduty migration",
    "ldap login fails: password: expired for the svc-backup account",
    "users see password: incorrect after the sso cutover",
    "kafka consumer lag grows on topic orders.v2 partition 7",
    "redis cluster failover leaves slots 0-5460 unassigned",
    "nginx returns 413 on uploads larger than 1m since config change",
    "disk /var/lib/docker fills to 100% on build agents nightly",
    "cron job at /etc/cron.d/backup/ silently stops writing archives",
    "grafana dashboard https://grafana.internal/d/abc123/api shows no data since 09:00",
    "image registry.example.com/app@sha256:4f2a9c1b2d3e fails to pull on new nodes",
    "deploy of git@github.com:acme/payments.git main stalls at the migrate step",
    "spring app throws on @Transactional methods after the hibernate upgrade",
    "postgresql://analytics-db:5432 slow queries; contact dba@example.com before restarts",
    "the orders db at postgresql://orders-db:5432 is slow and @dba-oncall was paged",
    "elasticsearch @timestamp field mapping conflict breaks the logs index",
    "dhcp leases for 00:1a:2b:3c:4d:5e expire immediately on vlan 12",
    "jvm heap at 95% on payments-7f9c8d-x2k4z after the 3.2 release",
    "istio sidecar rejects mtls from task-0123456789abcdef0123456789abcdef worker",
    "s3 bucket acme-logs-prod returns access denied for the uploader role",
    "terraform apply hangs on aws_rds_cluster.primary modification",
    "certificate for api.example.com expires in 3 days and renewal fails",
    "smtp relay rejects mail from noreply@acme.io with 550 errors",
    "load balancer health checks fail on /healthz after the port change",
    "prometheus scrape of 10.0.5.12:9100 times out every other minute",
    "the k8s secret rotation job keeps restarting in the batch namespace",
    "api key rotation broke the mobile client login flow",
    "vpn users on 192.168.50.0/24 cannot reach the internal wiki",
    "dns lookups for db.internal.example.com return nxdomain intermittently",
    "call 555-123-4567 bridge shows the payments outage started at 13:05",
    "version 10.4.2.1 of the agent crashes on startup",
    "replica set mongodb://mongo:27017 primary steps down when backups run",
    "mysql://primary:3306 deadlocks on the orders table, see jira OPS-1234 by alice@acme.io",
    "rabbitmq queue payments.retry grows unbounded after the consumer deploy",
    "service mesh retries amplify 503s from the inventory service",
    "the hpa scales payments to 50 pods and cpu stays at 90%",
    "jwt validation fails with clock skew errors on the auth service",
    "oauth callback to https://app.example.com/callback returns 400 invalid_grant",
    "pgbouncer pool for postgresql://pgbouncer:6432/app is exhausted at peak",
    "memory leak in node worker after upgrading to node 20.11",
    "ntp drift on 10.1.1.5 causes kerberos auth failures",
    "github actions runner cannot reach artifact store at 172.31.0.20",
    "the mongodb://mongo:27017 connection storm follows each deploy of user@service",
]


LINES = [
    # secret vocabulary in prose
    "password reset emails failing since the smtp relay change",
    "Password: field empty in the login form",
    "the password reset link expires after 15 minutes",
    "users report the password: incorrect banner after sso cutover",
    "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: YES)",
    "ERROR 1045 (28000): Access denied for user 'app'@'10.0.0.0/255.0.0.0' (using password: NO)",
    "sshd[2211]: Failed password for invalid user admin from 203.0.113.9 port 50122 ssh2",
    "sshd[2211]: Accepted password for deploy from 203.0.113.10 port 50123 ssh2",
    'FATAL:  password authentication failed for user "postgres"',
    "Event types:\n    failed_password: 520\n    accepted_login: 3",
    "user_passwd: changed_ok",
    "password policy requires 12 characters and one symbol",
    "the password manager integration with vault is down",
    "token bucket rate limiter drops 5% of requests at peak",
    "refresh token expired and the client did not retry",
    "invalid token: signature verification failed on the gateway",
    "401 Unauthorized: token: expired for the mobile client",
    "the token: invalid_grant error comes back from the idp",
    "token expiry is 3600 seconds but sessions die after 900",
    "secret rotation runbook failed at step 3",
    "the k8s secret: db-creds is missing in the payments namespace",
    "secrets manager returns AccessDeniedException for the lambda role",
    'Error from server (NotFound): secrets "db-creds" not found',
    "api_key rotation failed for the partner integration",
    "API key: missing from the request header on the mobile client",
    "apikey: not provided in the webhook call",
    "the x-api-key header is stripped by the cdn",
    "bearer of bad news: the backup never ran",
    "the bearer token expired mid-request",
    "missing Authorization: Bearer header on calls from the cron job",
    "Authorization header missing on the internal health checks",
    "Authorization: Basic authentication failed for the legacy endpoint",
    'WWW-Authenticate: Bearer realm="api", error="invalid_token"',
    "the passphrase prompt blocks the unattended ssh-agent start",
    "pwd shows /home/runner/work instead of the workspace",
    "PWD=/home/runner/work/payments/payments",
    "OLDPWD=/home/runner",
    # LLM / usage logs
    "prompt_tokens=1532 completion_tokens=211 total_tokens=1743",
    "max_tokens=4096 exceeded on the summarizer call",
    "token_count: 5123 over the 4096 budget",
    "input_token: 120 output_token: 30",
    "tokenizer bge-m3 loaded in 2.1s",
    # compound keys that are counts or flags
    "reset_password=true for 14 accounts in the batch",
    "failed_password=520 in the last hour from one ASN",
    "password_expiry_days=90 is enforced by the domain policy",
    "token_ttl=3600 on the gateway config",
    "secret_name=db-creds mounted at /etc/creds",
    "next_token=null at the end of the listing",
    # versions, dates, ids, hashes
    "release 24-01-02-03-04-05 failed canary",
    "build 2024-01-02-03-04-05 is the last green one",
    "deploy v1.2.3-rc.4 rolled back at 14:02:33",
    "trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span_id=00f067aa0ba902b7",
    "request 550e8400-e29b-41d4-a716-446655440000 timed out after 30s",
    "commit 356a192b7913b04c54574d18c28d46e6395428ab broke the build",
    "image sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "ssh host key fingerprint SHA256:nThbg6kXUpJWGl7E1IGOCspRomTxdCARLviKw6E5SY8",
    "kernel 6.8.0-31-generic panics on boot",
    "time 12:34:56.789 drift exceeds 500ms",
    # urls, emails, mentions
    "@dba-oncall please look at the replication lag",
    "paged @sre-primary and @sre-secondary at 02:14",
    "notify user@service when the queue drains",
    "the webhook to https://hooks.example.com/services/T000/B000 returns 404",
    "package @acme/payments-sdk@2.3.1 fails to install",
    "see https://medium.com/@acme-eng/postmortem for the timeline",
    "ssh://git@github.com/acme/infra.git clone hangs",
    "redis://cache:6379/0 connection refused after the failover",
    "postgres://localhost:5432/app is used by the migration job",
    "amqp://rabbitmq:5672 consumers disconnect every 60s",
    "http://localhost:8080/@admin returns 403",
    "jdbc:postgresql://db:5432/app?sslmode=require times out",
    "contact security@acme.io and ops@acme.io before rotating anything",
    # shell / code / config with references
    "export DB_PASSWORD=${DB_PASSWORD} in the entrypoint",
    'curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/health',
    "curl -H 'Authorization: Bearer <token>' https://api.example.com/v1/me",
    "password: {{ .Values.db.password }}",
    "  password: ${POSTGRES_PASSWORD}",
    "token=$(vault read -field=token secret/ci)",
    "set password=<your-password> in the dsn",
    "def login(user: str, password: str) -> Session:",
    "password: Optional[str] = None",
    'if password == "": raise ValueError',
    "if token != expected: return 401",
    "  secretKeyRef:\n    name: db-creds\n    key: password",
    "kind: Secret\nmetadata:\n  name: db-creds",
    "    secret:\n      secretName: tls-certs",
    "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db",
    "spring.datasource.url=jdbc:mysql://db:3306/app",
    "the password field in values.yaml is left empty on purpose",
    # mac-like / ip-like
    "interface eth0 up, mtu 9000, speed 25000",
    "dmesg: usb 1-1.2: new high-speed USB device number 4",
    "port 8080:8080 mapping lost after compose restart",
    "range 10-20-30-40-50-60 of the sweep timed out",
    "public 203.0.113.42 is the nat egress",
    "ipv6 fe80::1ff:fe23:4567:890a on eth1 has dad failure",
    # generic incident sentences
    "checkout latency p99 jumped from 300ms to 4s after the 14:00 deploy",
    "the cronjob backup-nightly has not completed since tuesday",
    "OOMKilled on payments-api pods with limit 512Mi",
    "connection pool exhausted: 100/100 in use, 37 waiting",
    "CrashLoopBackOff on worker-7d9f8c after the config change",
    "Readiness probe failed: HTTP probe failed with statuscode: 503",
    "upstream connect error or disconnect/reset before headers. reset reason: overflow",
    "kafka broker 3 under-replicated partitions: 12",
    "disk usage 97% on /var/lib/postgresql/data",
    "TLS handshake error from 203.0.113.7:51234: remote error: tls: bad certificate",
    "x509: certificate has expired or is not yet valid",
    "the deploy pipeline step 'migrate' exited 137",
    "slow query: SELECT * FROM orders WHERE id = 42 took 9.3s",
    "rate limit 429 from the payment provider since 09:12",
    "elasticsearch cluster health is yellow with 4 unassigned shards",
    "nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)",
    "java.lang.OutOfMemoryError: GC overhead limit exceeded",
    "panic: runtime error: invalid memory address or nil pointer dereference",
    "Traceback (most recent call last): KeyError: 'session'",
    "the on-call handoff note says the secret santa bot is noisy again",
    "a key rotation is scheduled for friday; no customer impact expected",
    "user reports login works but the api returns 403 for /admin",
    # near the spellings #1980 added
    "docker login --password-stdin < ci-token.txt keeps failing",
    "the --password flag is deprecated, use --password-stdin instead",
    "PGPASSWORD is not set in the cron environment",
    "kubectl --token flag overrides the kubeconfig user",
    "--token expired, re-run aws sso login",
    "the clientSecret field is empty in the helm values",
    "ghp_ prefixed tokens are rejected by the new org policy",
    "xoxb- tokens need the chat:write scope for the alert bot",
    "-----BEGIN CERTIFICATE----- block in ca.pem is malformed",
    "the BEGIN PRIVATE KEY header is missing from tls.key",
    "https://user@example.com/acme/repo has no password in it",
    "Basic auth is disabled on the ingress",
    "Authorization: Basic realm required by the proxy",
    "token: 401 from the idp on every refresh",
    "password: n/a for the service accounts",
    "secret: rotated last week, still failing",
    "api_key: missing.",
    "sk-learn style estimators in the ml service crash",
    "eyJ prefixed blobs in the logs are base64 json",
    "the task-runner sk- prefix check rejects nothing",
    "asiapacificsoutheast1 bucket replication lags",
    "AKIA prefixed keys must be rotated every 90 days",
]

#: The review's negative corpus (#2021, round 3), the lines not above.
REVIEW_LINES = [
    "2026-10-14T10:00:01.123Z ERROR [auth] token validation failed: signature expired",
    'FATAL:  password authentication failed for user "app"',
    'DETAIL:  Password does not match for user "app".',
    "Failed password for invalid user admin from 203.0.113.7 port 22 ssh2",
    "Access denied for user 'root'@'localhost' (using password: YES)",
    "UPDATE accounts SET password_hash = $1, token = $2 WHERE id = $3",
    "SELECT id FROM users WHERE email = ? AND password = ?",
    "INSERT INTO sessions (user_id, token) VALUES (%s, %s)",
    'cursor.execute("UPDATE users SET password = :pwd WHERE id = :id")',
    "  at com.example.auth.TokenService.validate(TokenService.java:42)",
    '  File "/app/auth/token.py", line 42, in verify',
    "KeyError: 'password'",
    "jwt.exceptions.ExpiredSignatureError: Signature has expired",
    'WWW-Authenticate: Basic realm="Restricted Area"',
    "  automountServiceAccountToken: false",
    "  secretName: tls-cert",
    '  existingSecret: ""',
    "  passwordKey: postgres-password",
    "  token: false",
    '  password: ""',
    "  secret: null",
    "  apiKey: true",
    "Password: required",
    "Password: ********",
    "Secret: monitoring/grafana-admin not found",
    "Token: kube-system/default-token-x7k2p",
    'level=warn msg="token refresh failed" token_ttl=3600 retry=3',
    '{"level":"error","msg":"login failed","reason":"invalid password","attempts":3}',
    '{"event": "token_rotated", "token_id": "tk_2024_10_14", "ok": true}',
    "request_id=550e8400-e29b-41d4-a716-446655440000 status=401",
    "commit 3f9a2b1c4d5e6f708192a3b4c5d6e7f8091a2b3c merged",
    "image sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 pulled",
    'etag "33a64df551425fcc55e4d42a148795d9f25f89d4" stale',
    "uptime 12:00:00:00:00:00 since boot",
    "build 2026-10-14-10-00-00 promoted",
    "pod payments-7f9c8d6b5-x2k4z restarted 4 times",
    "kube-apiserver --token-auth-file=/etc/k8s/tokens.csv --secure-port=6443",
    "helm upgrade app ./chart --set image.tag=v1.2.3",
    "the secret: rotate it every 90 days",
    "see docs for password: requirements",
    "the bearer token was rejected by the gateway",
    '"token_type": "Bearer", "expires_in": 3600',
    "api_key rotation is scheduled for friday",
    "Set PWD=/srv/app and restart",
    "PASSWORD_MIN_LENGTH=12",
    "password_reset_requests: 42 in the last hour",
    "failed_login_password_attempts=7",
    "redis-cli INFO keyspace shows db0:keys=1024,expires=10",
    "mongodb://mongo-0:27017,mongo-1:27017/?replicaSet=rs0&readPreference=primary",
    "amqp://rabbitmq:5672/vhost connection refused",
    "https://grafana.internal/d/abc123/api?orgId=1&from=now-6h",
    "git@github.com:acme/payments.git",
    "smtp://mail.internal:587 rejected relay for ops@example.com",
    "ldap://ldap.internal:389 bind as cn=admin,dc=example,dc=com",
    "connection string: Server=db;Database=app;Integrated Security=true;",
    "Authorization header missing; expected: Bearer <token>",
    'curl -H "Authorization: Bearer $TOKEN" https://api.example.com',
    "export GITHUB_TOKEN=$(gh auth token)",
    "password: ${{ secrets.DB_PASSWORD }}",
    "  password:",
    "    secretKeyRef:",
    "      name: db",
    "      key: password",
    "env: [{name: TOKEN, valueFrom: {secretKeyRef: {name: t, key: token}}}]",
    "--password-stdin < /run/secrets/registry",
    "docker login -u ci --password-stdin registry.example.com",
    "sk-learn version 1.3.2 installed",
    "pk-12 sensor reading 0.42",
    "ASIAPACIFICSOUTHEAST region list",
    "xoxo the team",
    "ghp_ prefix tokens are classic PATs",
    'eyJ is the base64 of {" which starts every JWT header',
    "-----BEGIN CERTIFICATE-----",
    "MIIDdzCCAl+gAwIBAgIEAgAAuTANBgkqhkiG9w0BAQUFADBaMQswCQYDVQQGEwJJ",
    "-----END CERTIFICATE-----",
    "-----BEGIN PUBLIC KEY-----",
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEexample",
    "-----END PUBLIC KEY-----",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@host",
    "the token: refresh succeeded at 10:02",
    "token: 3 retries left",
    "password: changed 2 days ago by alice",
    "Token expires in 3600 seconds",
    "secret_key_base is set in credentials.yml.enc",
]


#: The only negative lines any pattern changes, each with the spans it
#: takes and why it should. Internal addresses and MACs are the sanitizer's
#: network identifiers (`NETWORK_ADDRESS_PATTERNS`), redacted by design and
#: unchanged by #1980; the document preprocessor does not apply them.
JUSTIFIED = {
    "pods in 10.42.0.0/16 cannot reach 10.43.0.10 the cluster dns service": [
        ("IP_ADDRESS", "10.42.0.0"),
        ("IP_ADDRESS", "10.43.0.10"),
    ],
    "node 192.168.1.20 flaps between ready and notready every 5 minutes": [
        ("IP_ADDRESS", "192.168.1.20")
    ],
    "upstream 172.20.3.4:8080 returns connection reset under load": [
        ("IP_ADDRESS", "172.20.3.4")
    ],
    "dhcp leases for 00:1a:2b:3c:4d:5e expire immediately on vlan 12": [
        ("MAC_ADDRESS", "00:1a:2b:3c:4d:5e")
    ],
    "prometheus scrape of 10.0.5.12:9100 times out every other minute": [
        ("IP_ADDRESS", "10.0.5.12")
    ],
    "vpn users on 192.168.50.0/24 cannot reach the internal wiki": [
        ("IP_ADDRESS", "192.168.50.0")
    ],
    # A four-part version inside 10/8 is indistinguishable from an address.
    "version 10.4.2.1 of the agent crashes on startup": [("IP_ADDRESS", "10.4.2.1")],
    "ntp drift on 10.1.1.5 causes kerberos auth failures": [("IP_ADDRESS", "10.1.1.5")],
    "github actions runner cannot reach artifact store at 172.31.0.20": [
        ("IP_ADDRESS", "172.31.0.20")
    ],
    "ERROR 1045 (28000): Access denied for user 'app'@'10.0.0.0/255.0.0.0'"
    " (using password: NO)": [("IP_ADDRESS", "10.0.0.0")],
    # The closed-list ruling (#1980 review): a keyed value is a secret unless
    # it is a status word (`_STATUS_WORDS`). Detection of real secrets wins
    # over these false positives — the word-shape rule that spared them missed
    # 79 of 113 common passwords written the same way.
    "Password: field empty in the login form": [("PASSWORD", "field")],
    "user_passwd: changed_ok": [("PASSWORD", "changed_ok")],
    "invalid token: signature verification failed on the gateway": [
        ("TOKEN", "signature")
    ],
    "the k8s secret: db-creds is missing in the payments namespace": [
        ("SECRET", "db-creds")
    ],
    # A number cannot be a status: `password: 123` is a password.
    "token: 401 from the idp on every refresh": [("TOKEN", "401")],
    "token: 3 retries left": [("TOKEN", "3")],
    "the secret: rotate it every 90 days": [("SECRET", "rotate")],
    "see docs for password: requirements": [("PASSWORD", "requirements")],
    "the token: refresh succeeded at 10:02": [("TOKEN", "refresh")],
    # Six pairs of hex digits are a MAC address, as on main.
    "uptime 12:00:00:00:00:00 since boot": [("MAC_ADDRESS", "12:00:00:00:00:00")],
    # `ASIA` and sixteen capitals is an access key id's shape exactly.
    "ASIAPACIFICSOUTHEAST region list": [("AWS_ACCESS_KEY", "ASIAPACIFICSOUTHEAST")],
}

NEGATIVE = P11 + LINES + REVIEW_LINES + ["Token: expired"]


def test_the_corpus_is_the_size_the_issue_asks_for():
    assert len(P11) == 50
    assert len(LINES) >= 100
    assert set(JUSTIFIED) <= set(NEGATIVE)


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_text_is_unchanged_but_for_the_justified_spans(sanitizer, text):
    expected = JUSTIFIED.get(text, [])
    assert _spans(text) == expected
    out = sanitizer.apply_regex_redaction(text, {})
    if not expected:
        assert out == text
        assert sanitizer.sanitize_text_with_registry(text, {}) == text
    secrets_only = [
        span for span in expected if span[0] not in {"IP_ADDRESS", "MAC_ADDRESS"}
    ]
    document, report = redact_sensitive_content(text)
    assert report.total_redacted == len(secrets_only)
    if not secrets_only:
        assert document == text


def _without(listed: str, entry: str) -> tuple:
    """The sanitizer set with ``entry`` cut from the closed list ``listed``
    (the regex source of ``_NON_SECRET_WORDS`` or of the count guard). A
    YAML block's body has one exemption, a whole reference (#2025), so the
    block patterns hold no closed list to cut."""
    entries = listed.split("|")
    assert entry in entries
    rest = "|".join(e for e in entries if e != entry) or "(?!)"
    variant = []
    for pattern in SANITIZER_PATTERNS:
        source = pattern.regex.pattern
        if pattern.names_a_value and pattern.scope != "block":
            assert listed in source, pattern.entity_type
            source = source.replace(listed, rest)
        variant.append(
            dataclasses.replace(pattern, regex=re.compile(source, pattern.regex.flags))
        )
    return tuple(variant)


_CLOSED_LISTS = [
    (redaction._NON_SECRET_WORDS, entry)
    for entry in redaction._NON_SECRET_WORDS.split("|")
] + [(r"\d{1,5}", r"\d{1,5}")]


@pytest.mark.parametrize("listed,entry", _CLOSED_LISTS)
def test_every_closed_list_entry_is_needed_by_a_negative_line(listed, entry):
    """R3 of the #2021 review: the closed lists stay minimal, every entry
    cited by a negative line it spares. Cut the entry, and a line of the
    corpus (ours or the review's) is redacted."""
    variant = _without(listed, entry)
    needs = [
        text
        for text in NEGATIVE
        if find_redaction_spans(text, variant)
        != find_redaction_spans(text, SANITIZER_PATTERNS)
    ]
    assert needs, entry


def test_a_masked_or_empty_value_is_needed_by_a_negative_line():
    """The two exemptions outside the word list: a masked value and an empty
    quoted one."""
    assert _spans("Password: ********") == []
    assert _spans('  password: ""') == []
    assert _spans("Password: ***") == []
    assert _spans("Password: **") == [("PASSWORD", "**")]
    assert _spans("password: ''") == [("PASSWORD", "''")]


def test_a_url_without_credentials_does_not_reach_a_later_at_sign(sanitizer):
    """The case-joined substance the #1993 review measured: the statement's
    DB URL and an email in an evidence summary, a mention and `user@service`
    in chat. The former `[^@]+` ran across whitespace to the `@` and redacted
    everything between."""
    text = (
        "orders api times out against postgresql://orders-db:5432 for every region\n"
        "dba-oncall@example.com confirmed no locks on the primary\n"
        "paged @sre-primary; the mongodb://mongo:27017 storm follows user@service"
    )
    assert sanitizer.apply_regex_redaction(text, {}) == text


# ---------------------------------------------------------------------------
# 3. One set: the document preprocessor detects with SECRET_PATTERNS
# ---------------------------------------------------------------------------


def test_the_sanitizer_set_is_the_secret_set_then_the_network_identifiers(sanitizer):
    assert sanitizer.regex_patterns is SANITIZER_PATTERNS
    assert SANITIZER_PATTERNS == SECRET_PATTERNS + NETWORK_ADDRESS_PATTERNS
    assert {p.entity_type for p in NETWORK_ADDRESS_PATTERNS} == {
        "IP_ADDRESS",
        "MAC_ADDRESS",
    }


@pytest.mark.parametrize(
    "text",
    [r[1] for r in POSITIVE] + NEGATIVE,
    ids=[r[0] for r in POSITIVE] + [f"negative-{i}" for i in range(len(NEGATIVE))],
)
def test_the_document_preprocessor_redacts_what_the_secret_set_detects(text):
    document, report = redact_sensitive_content(text)
    assert document == redact_spans(text, SECRET_PATTERNS, _marker)
    assert report.total_redacted == len(find_redaction_spans(text, SECRET_PATTERNS))


def test_the_report_counts_by_type_and_says_where_the_first_was_found():
    text = (
        "Setup:\n```\nexport DB_PASSWORD=hunter2\npassword=hunter3\n```\n"
        "Once the service has restarted cleanly after all of that, connect with "
        "token=abc123def."
    )
    document, report = redact_sensitive_content(text)
    assert [(e.type, e.count, e.context) for e in report.redactions] == [
        ("password", 2, "Found in code blocks"),
        ("token", 1, "Found in text content"),
    ]
    assert report.total_redacted == 3
    assert "hunter2" not in document and "hunter3" not in document
    assert "abc123def" not in document


# ---------------------------------------------------------------------------
# 4. Random secrets: no fragment survives
# ---------------------------------------------------------------------------

_AN = string.ascii_letters + string.digits
_PW = _AN + "!#$%&*?=^~/@."


def _random(rng: random.Random, alphabet: str, size: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(size))


_CLASSES = {
    "password-equals": lambda r: (
        pw := _random(r, _PW, 12),
        f"db login fails with password={pw} on replica",
    ),
    "password-colon": lambda r: (
        pw := _random(r, _PW, 12),
        f"ldap bind fails with passwd: {pw} for svc",
    ),
    "password-yaml": lambda r: (
        pw := _random(r, _PW.replace("#", ""), 10),
        f"db:\n  password: {pw}\n  port: 5432",
    ),
    "db-url": lambda r: (
        pw := _random(r, _PW, 12),
        f"replica fails for {r.choice(['postgresql', 'mysql', 'mongodb', 'redis', 'amqp'])}://app:{pw}@db:5432/app from pods",
    ),
    "compound-env": lambda r: (
        pw := _random(r, _PW, 12),
        f"container env DB_PASSWORD={pw} rejected",
    ),
    "bearer": lambda r: (tok := _random(r, _AN, 32), f"Authorization: Bearer {tok}"),
    "basic": lambda r: (
        cred := base64.b64encode(
            f"{_random(r, string.ascii_lowercase, 6)}:{_random(r, _PW, 10)}".encode()
        ).decode(),
        f"> Authorization: Basic {cred}",
    ),
    "flag": lambda r: (
        pw := _random(r, _PW.replace("-", ""), 12),
        f"docker login --password {pw} registry",
    ),
}


def _leaks(secret: str, text: str, k: int = 6) -> bool:
    lowered, value = text.lower(), secret.lower()
    return any(value[i : i + k] in lowered for i in range(len(value) - k + 1))


@pytest.mark.parametrize("name", sorted(_CLASSES))
def test_no_fragment_of_a_random_secret_survives(sanitizer, name):
    rng = random.Random(f"1980-{name}")
    leaked = []
    for _ in range(300):
        secret, text = _CLASSES[name](rng)
        if _leaks(secret, sanitizer.apply_regex_redaction(text, {})):
            leaked.append(text)
    assert not leaked, leaked[:3]


# ---------------------------------------------------------------------------
# 5. The trigger gating changes no result
# ---------------------------------------------------------------------------

_UNGATED = tuple(
    dataclasses.replace(p, triggers=(), scope="text", hint=None)
    for p in SANITIZER_PATTERNS
)

_UNICODE = [
    "pa\u017fsword=hunter2 on replica",  # long s: re.IGNORECASE reads it as `s`
    "api_key=abc and pr\u0131vate notes",  # dotless i
    "\u0130stanbul dc: password=hunter2",  # lower() lengthens it: no index maps
    "\u212a8s_secret_0123456789abcdef0123456789abcdef",  # Kelvin sign
    # No index maps, and a masked round finds a key whose value is on the
    # line after the one it touched: its window must reach that line.
    "\u0130\n10.0.0.5password:\n    520",
    "\u0130\n10.0.0.5password=\n520",
]


@pytest.mark.parametrize(
    "text",
    [r[1] for r in POSITIVE] + NEGATIVE + _UNICODE,
)
def test_gating_finds_what_an_ungated_scan_finds(text):
    assert find_redaction_spans(text, SANITIZER_PATTERNS) == find_redaction_spans(
        text, _UNGATED
    )


def test_the_gating_controls_detect_something():
    """Positive control for the test above: the folded spellings ARE matched,
    so agreeing with the ungated scan is not agreeing on nothing."""
    assert _spans(_UNICODE[0]) and _spans(_UNICODE[2])


def test_every_keyed_trigger_starts_its_keyword():
    """`keyed` tries only line starts and trigger positions: each keyword and
    cased spelling must start with a trigger."""
    for pattern in SECRET_PATTERNS:
        if pattern.scope == "keyed":
            assert pattern.triggers, pattern.entity_type
            for start in (
                "password",
                "passwd",
                "passphrase",
                "pwd",
                "secret",
                "token",
                "api_key",
                "apikey",
                "api-key",
            ):
                spelled = f"{start}=Zx9qw7E1rT"
                if pattern.regex.search(spelled):
                    assert any(spelled.startswith(t) for t in pattern.triggers), (
                        pattern.entity_type,
                        start,
                    )


# ---------------------------------------------------------------------------
# 6. One pass is idempotent (from the review of #2011)
# ---------------------------------------------------------------------------

_ATOMS = [
    "10.0.0.5",
    "192.168.1.20",
    "172.16.0.9",
    "00:1A:2B:3C:4D:5E",
    "aa-bb-cc-dd-ee-ff",
    "password=hunter2",
    "password: hunter2",
    "password = hunter2",
    "passwd: Tr0ub4dor&3",
    "pwd=hunter2",
    "secret=hunter2",
    "token=abc123def",
    "api_key=my-secret-value",
    "apikey: k3y-value-1",
    "DB_PASSWORD=hunter2",
    "PGPASSWORD=hunter2",
    "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd",
    "sk-proj-Ab3dEf_gh1JkLmn0PqRsTuvWxYz-1234",
    "pk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    "Bearer abc.def.ghi",
    "Authorization: Basic dXNlcjpwYXNz",
    "ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "glpat-" + _GL,
    _SLACK_SHORT,
    "postgresql://app:pw4321@db:5432",
    "redis://user:pass99@cache:6379",
    "https://oauth2:glpatAbCd1234@gitlab.example.com",
    "k8s_secret_0123456789abcdef0123456789abcdef",
    "api_key0123456789abcdef0123456789abcdef",
    "-----BEGIN PRIVATE KEY-----MIIEowIBAAKCAQEA7bq98EXAMPLE-----END PRIVATE KEY-----",
    "--password Hunter2pw",
    "X-Auth-Token: 9f8e7d6c5b4a",
]
#: Glue between atoms: nothing, separators, and a letter, a digit, a hex
#: letter — what makes one span's edge another's context.
_GLUE = ["", "", "", " ", "_", "-", ".", ":", "=", "/", "@", ",", "x", "5", "E"]


def _glued(rng: random.Random, atoms=_ATOMS) -> str:
    parts = [rng.choice(atoms) for _ in range(rng.choice([2, 2, 3]))]
    text = parts[0]
    for part in parts[1:]:
        text += rng.choice(_GLUE) + part
    return text


def test_one_pass_of_the_sanitizer_is_idempotent(sanitizer):
    """``apply(apply(x)) == apply(x)``. Main failed 133 of 30,000 of these
    (sequential passes: `10.0.0.5password=hunter2` kept `password=hunter2`
    until a second pass); spans on the raw text without the masked rounds
    failed 1,397 — every lookbehind reads a neighbour that a placeholder
    then replaces."""
    rng = random.Random("1980-idempotent")
    changed = []
    for _ in range(4000):
        text = _glued(rng)
        once = sanitizer.apply_regex_redaction(text, {})
        if sanitizer.apply_regex_redaction(once, {}) != once:
            changed.append(text)
    assert changed == [], changed[:3]


def test_one_pass_of_the_document_preprocessor_is_idempotent():
    rng = random.Random("1980-idempotent-markers")
    changed = []
    for _ in range(2000):
        text = _glued(rng)
        once, _ = redact_sensitive_content(text)
        if redact_sensitive_content(once)[0] != once:
            changed.append(text)
    assert changed == [], changed[:3]


@pytest.mark.parametrize(
    "text",
    [
        "10.0.0.5password=hunter2",
        "00:1A:2B:3C:4D:5Epassword=hunter2",
        "10.0.0.5password: hunter2",
        "00:1A:2B:3C:4D:5Epassword: hunter2",
        "xpassword=hunter2",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdpassword=hunter2",
    ],
)
def test_a_secret_glued_to_another_is_gone_after_one_pass(sanitizer, text):
    assert "hunter2" not in sanitizer.apply_regex_redaction(text, {})


#: `=` spellings with the value that must not survive, glued after anything.
_ASSIGNED = [
    ("password=hunter2", "hunter2"),
    ("passwd=Tr0ub4dor", "Tr0ub4dor"),
    ("pwd=pw0rd99", "pw0rd99"),
    ("secret=s3cr3tval", "s3cr3tval"),
    ("token=abc123def", "abc123def"),
    ("api_key=my-secret-value", "my-secret-value"),
    ('"password": "hunter3x"', "hunter3x"),
]


#: The atoms that end in a fixed shape, not an unquoted ``key: value`` (a
#: value runs to whitespace, so one glued to a following key takes the key
#: with it — the limitation the PR lists).
_FIXED_ATOMS = [
    atom
    for atom in _ATOMS
    if not any(
        key in atom.lower()
        for key in (
            "password",
            "passwd",
            "pwd=",
            "secret=",
            "token",
            "apikey:",
            "api_key=",
        )
    )
]


def test_an_assignment_glued_after_anything_is_gone_after_one_pass(sanitizer):
    """A keyword glued to what precedes it (`xpassword=`, `5password=`,
    `…abcdpassword=` inside an `sk-` run) is still a key with `=`: the
    compound boundary is "after any letter, digit or underscore"."""
    rng = random.Random("1980-one-pass")
    leaked = []
    for _ in range(3000):
        assignment, value = rng.choice(_ASSIGNED)
        text = rng.choice(_FIXED_ATOMS) + rng.choice(_GLUE) + assignment
        if value in sanitizer.apply_regex_redaction(text, {}):
            leaked.append(text)
    assert leaked == [], leaked[:3]


# ---------------------------------------------------------------------------
# 7. The #2021 review rulings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,value",
    [
        ("db:\n  password:\n    Tr0ub4dor&3xyz\n  host: x", "Tr0ub4dor&3xyz"),
        ("password=\nTr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
        ("password:\r\n  Tr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
        ("  - name: app\n    password:\n      Tr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
        ('"password":\n    "Tr0ub4dor&3xyz"', "Tr0ub4dor&3xyz"),
        ("password:\n  john3:16", "john3:16"),
        ("password:\n  music=life", "music=life"),
        ("\tpassword:\n\t\tTr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
        ("Password: \r\n  Tr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
        ("PASSWORD =\nTr0ub4dor&3xyz", "Tr0ub4dor&3xyz"),
    ],
)
def test_a_value_on_the_next_line_is_read(sanitizer, text, value):
    """`password:` then the value on its own, deeper-indented line — valid
    YAML, and main read it on both paths; and `password=` then the next
    line's first token, as main read it."""
    assert _spans(text) == [("PASSWORD", value)]
    assert value not in sanitizer.apply_regex_redaction(text, {})
    assert value not in redact_sensitive_content(text)[0]


@pytest.mark.parametrize(
    "text",
    [
        "  password:\n    secretKeyRef:\n      name: db",
        "PASSWORD=\nUSER=bob",
        "password:\n  - item",
    ],
)
def test_a_next_line_that_starts_a_key_is_not_the_value(text):
    assert [t for t, _ in _spans(text)] in ([], ["DATABASE_URL"])
    assert ("PASSWORD", "secretKeyRef:") not in _spans(text)


def test_a_url_on_the_next_line_is_the_value():
    """`scheme://` is not a key: the next line is the value, typed by the URL
    pattern that also reads it."""
    assert _spans("password:\n  redis://u:p4ss@cache:6379") == [
        ("DATABASE_URL", "redis://u:p4ss@cache:6379")
    ]
    assert _spans("password:\n  postgresql://db:5432/app") == [
        ("PASSWORD", "postgresql://db:5432/app")
    ]
    assert _spans("password=\npostgresql://db:5432/app") == [
        ("PASSWORD", "postgresql://db:5432/app")
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Enter password:\n2026-10-14 10:00:01 ERROR db refused",
        "Enter password: \nERROR 1045 (28000): Access denied",
        "Password: \r\nLogin incorrect",
        "token:\n  # filled in by CI",
        "  password:\n  hunter2x9",
        "\tpassword:\n\thunter2x9",
        "db:\n    password:\n  host: x",
    ],
)
def test_after_a_colon_only_a_deeper_line_holds_the_value(sanitizer, text):
    """R4 of the #2021 review: after an empty `key:`, the next line is its
    value only as a YAML continuation — indented deeper than the key's line,
    and not a comment. A prompt's next log line, a status line, a comment, a
    sibling key are not. (Main read the first four's next token: the date,
    `ERROR`, `Login` — and the head before this, `#`.)"""
    assert _spans(text) == []
    assert sanitizer.apply_regex_redaction(text, {}) == text
    assert redact_sensitive_content(text)[0] == text


def test_after_equals_the_next_line_is_read_as_main_read_it():
    """A listed trade, kept from main: `password=` then nothing reads the
    next line's first token, whatever it is."""
    text = "password=\n2026-10-14 10:00:01 ERROR db refused"
    assert _spans(text) == [("PASSWORD", "2026-10-14")]


_SK = "sk-" + "Ab3dEfGh1JkLmn0PqRsTuvWxYz12345678"


@pytest.mark.parametrize(
    "text,secret,keeps_json",
    [
        ('{"msg":"line one\\n' + _SK + '\\nline two"}', _SK, True),
        ('{"msg":"col\\t' + _SK + '"}', _SK, True),
        ("payload=%22" + _SK + "%22", _SK, False),
        (
            '{"cfg": "user: app\\npassword: Tr0ub4dor&3xyz\\nport: 5432"}',
            "Tr0ub4dor",
            False,
        ),
        ('{"s": "a\\ntoken: 9f8e7d6c5b4a\\n"}', "9f8e7d6c5b4a", False),
        (
            '{"req": "GET /\\nAuthorization: Basic dXNlcjpwYXNzd29yZDEyMw==\\n"}',
            "dXNlc",
            True,
        ),
        ('{"req": "GET /\\nBearer Zk91aXJlbGVzc3Rva2VuMTIz\\n"}', "Zk91aXJl", True),
    ],
)
def test_an_escaped_newline_or_percent_byte_is_a_boundary(
    sanitizer, text, secret, keeps_json
):
    """R5: `\\n`, `\\t` or `%XX` before a token or a key reads as the character
    it encodes would. Main caught these; the first #2021 head did not. A
    provider token ends where its alphabet does, so the JSON around it
    survives; a keyed value is its whole run, escapes and closers included
    (R2 of round 3), so the JSON string it ends does not."""
    out = sanitizer.apply_regex_redaction(text, {})
    assert secret not in out
    assert secret not in redact_sensitive_content(text)[0]
    if keeps_json:
        json.loads(out)  # the escape and the next key survive


@pytest.mark.parametrize(
    "text",
    [
        "UPDATE accounts SET token = $2 WHERE id = $3",
        "SELECT id FROM users WHERE email = ? AND password = ?",
        'cursor.execute("UPDATE users SET password = :pwd WHERE id = :id")',
        "INSERT INTO t (user, password) VALUES (%s, %s) -- password = %s",
        "  token: false",
        "  secret: null",
        "  apiKey: true",
        '  password: ""',
        "Password: ********",
        "Secret: monitoring/grafana-admin not found",
        "Token: kube-system/default-token-x7k2p",
        "password: ${{ secrets.DB_PASSWORD }}",
        "pwd => ${VAR}",
        "password=[REDACTED:password]",
        "the db password=[password] is the one being rejected",
    ],
)
def test_a_bind_flag_reference_or_marker_is_not_a_secret(text):
    """The review's unlisted over-redaction, fixed: SQL bind parameters, YAML
    booleans and nulls, Kubernetes `namespace/name` references, GitHub Actions
    expressions, `=>` that cannot give back its `>` (R4), and markers a pass
    wrote (R1)."""
    assert _spans(text) == []


@pytest.mark.parametrize(
    "text,value",
    [
        # R2(i): a reference shape exempts only the WHOLE value.
        ("password=${{xQ9!", "${{xQ9!"),
        ("password=${VAR}x9", "${VAR}x9"),
        ("password=$(x9!", "$(x9!"),
        ("password={{x9", "{{x9"),
        ("password=<a>bQ9", "<a>bQ9"),
        ("password=[a]Q9", "[a]Q9"),
        ("password=?Q9", "?Q9"),
        ("password=$1abc", "$1abc"),
        ("password=:pwd;x9", ":pwd;x9"),
        ("password=%sQ9", "%sQ9"),
        ("PWD=/home/app$Q9!", "/home/app$Q9!"),
        ("password=truex9", "truex9"),
        ("password=n/a/x9", "n/a/x9"),
        ("password=*****x", "*****x"),
        # R2(ii): markers and escapes are case-sensitive.
        ("password=[REDACTED:PASSWORD]", "[REDACTED:PASSWORD]"),
        ("password=[Password]", "[Password]"),
        ("password=:Pwd9", ":Pwd9"),
        # R2(iii): trailing closers are the value's.
        ("password=Tr0ub4dor&3)]}", "Tr0ub4dor&3)]}"),
        ('password=Tr0ub4dor&3"}', 'Tr0ub4dor&3"}'),
        ('{"msg": "password: Tr0ub4dor&3"}', 'Tr0ub4dor&3"}'),
        ("password=Tr0ub4dor&3,,,,,,,,,,", "Tr0ub4dor&3,,,,,,,,,,"),
        # R2(iv): an escape inside a value does not end it.
        ("password=Tr0ub\\n4dor", "Tr0ub\\n4dor"),
        ("password=Tr0ub\\N4dor", "Tr0ub\\N4dor"),
        ('{"cfg": "password: Tr0ub4dor\\nport: 5432"}', "Tr0ub4dor\\nport:"),
        # A quote that does not end the value is the value's too.
        ('password="ab"cd9', '"ab"cd9'),
        ("password='x y'=z9 next", "'x y'=z9"),
        ("password='it's9'", "'it's9'"),
        ("password=>x9Q", ">x9Q"),
    ],
)
def test_an_unquoted_value_is_its_whole_run(sanitizer, text, value):
    """R2 of the #2021 review (round 3): after `key=` or `key: `, unquoted,
    the value is the maximal run of non-whitespace, as on main — and a
    shape exempts it only when it is the whole run."""
    assert _spans(text) == [(_spans(text)[0][0], value)]
    assert value not in sanitizer.apply_regex_redaction(text, {})
    assert value not in redact_sensitive_content(text)[0]


@pytest.mark.parametrize(
    "text,value",
    [
        ("x\\Npassword=hunter2x", "hunter2x"),
        ("x\\Tsk-" + "Ab3dEfGh1JkLmn0PqRsTuvWxYz12345678", None),
    ],
)
def test_an_upper_case_escape_is_not_a_boundary(text, value):
    """`\\N` escapes nothing: `Npassword` is a compound key (read), and a
    letter before `sk-` is the listed `task-…` trade (not read)."""
    expected = [] if value is None else [("PASSWORD", value)]
    assert _spans(text) == expected


_PRINTABLE = string.ascii_letters + string.digits + string.punctuation


@pytest.mark.parametrize(
    "template",
    [
        "connect password={} host=db",
        "DB_PASSWORD={}",
        "login with password: {} failed",
    ],
)
def test_no_character_of_a_random_printable_password_is_left(sanitizer, template):
    """The review's full-printable fuzz (R2): 14 random printable characters
    after `password=`, `DB_PASSWORD=` and a mid-line `password: ` are replaced
    whole, every time — none missed, no character in clear. (A value that
    opens with a quote is a quoted value, delimited by its quotes.)"""
    rng = random.Random("1980-printable")
    head, tail = template.split("{}")
    left = []
    for _ in range(3000):
        password = "".join(rng.choice(_PRINTABLE) for _ in range(14))
        if password[0] in "\"'":
            continue
        out = sanitizer.apply_regex_redaction(template.format(password), {})
        if not re.fullmatch(
            re.escape(head) + r"<PASSWORD_[0-9a-f]{16}>" + re.escape(tail), out
        ):
            left.append((password, out))
    assert left == [], left[:3]


@pytest.mark.parametrize(
    "text",
    [
        "password_=Tr0ub4dor&3xyz",
        "password-=Tr0ub4dor&3xyz",
        "password_: Tr0ub4dor&3xyz",
    ],
)
def test_mains_suffixed_spellings_are_read(text):
    assert _spans(text) == [("PASSWORD", "Tr0ub4dor&3xyz")]


_JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    + base64.urlsafe_b64encode(b'{"sub":"42"}').decode().rstrip("=")
    + ".c2lnbmF0dXJlMTIzNDU2"
)


@pytest.mark.parametrize(
    "secret,spellings",
    [
        (
            _JWT,
            [
                f"auth failed for {_JWT} at gateway",
                f"Authorization: Bearer {_JWT}",
                f"callback id_token={_JWT}",
                f'{{"access_token": "{_JWT}"}}',
            ],
        ),
        (
            _SK,
            [f"client used {_SK} today", f"OPENAI_API_KEY={_SK}", f"  api_key: {_SK}"],
        ),
    ],
)
def test_one_secret_has_one_placeholder_however_it_is_spelled(
    sanitizer, secret, spellings
):
    """Value-only substitution, and a provider token typed by its own pattern
    under any key (#1980 review, co-reference): one placeholder for the
    secret, the key in clear, JSON still JSON, and the reverse exact."""
    context = CaseRedactionContext("case-1980", sanitizer, enabled=True)
    placeholders = set()
    for text in spellings:
        out = context.sanitize(text)
        assert secret not in out
        placeholders.update(REDACTION_PLACEHOLDER_RE.findall(out))
        assert context.reverse(out) == text
        if text.startswith("{"):
            assert json.loads(out)["access_token"] in placeholders
    assert len(placeholders) == 1, placeholders


def test_a_provider_token_keeps_its_type_with_punctuation_after_it():
    gh = "".join(("gh", "p_")) + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    assert _spans(f"GITHUB_TOKEN={gh},") == [("GITHUB_TOKEN", gh)]


def test_the_key_stays_in_clear(sanitizer):
    out = sanitizer.apply_regex_redaction('{"password": "Tr0ub4dor&3"}', {})
    assert re.fullmatch(r'\{"password": "<PASSWORD_[0-9a-f]{16}>"\}', out), out


def test_spans_that_only_touch_stay_two(sanitizer):
    """Mutation B: merging touching spans would hide a secret under its
    neighbour's type (`10.0.0.5` + an AWS key id glued to it)."""
    assert _spans("10.0.0.5AKIAIOSFODNN7EXAMPLE") == [
        ("IP_ADDRESS", "10.0.0.5"),
        ("AWS_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE"),
    ]
    out = sanitizer.apply_regex_redaction("10.0.0.5password=hunter2", {})
    assert re.fullmatch(
        r"<IP_ADDRESS_[0-9a-f]{16}>password=<PASSWORD_[0-9a-f]{16}>", out
    ), out


@pytest.mark.parametrize(
    "text,why",
    [
        ("xsk-" + _SK[3:], "a letter before `sk-`: `task-0123…` ids are common"),
        ("xpk-" + "Cad9D4uMs8YIfCj5cIpMyCLNMbyt6aFhDKuztIbZ", "as `sk-`"),
        ("key id akiaiosfodnn7examplx leaked", "AWS key ids are upper case"),
    ],
)
def test_the_listed_trades_hold(text, why):
    """Spellings main read that this set deliberately does not (PR, Out of
    scope): pinned, so a change to one is a decision, not a drift."""
    assert _spans(text) == [], why


def test_a_glued_jwt_or_aws_key_is_still_read():
    """Restored main behaviour: a JWT or an AWS key id after a letter or a
    digit is still one."""
    assert _spans("x" + _JWT) == [("JWT_TOKEN", _JWT)]
    assert _spans("7AKIAIOSFODNN7EXAMPLE") == [
        ("AWS_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE")
    ]


# --- the review's full generator ---------------------------------------------

_SL = "-".join(("xoxb", "123456789012", "1234567890123", "AbCdEfGhIjKl"))
_GH = "".join(("gh", "p_")) + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
_KEYS = ["password", "Password", "PASSWORD", "passwd", "passphrase", "pwd", "PWD", "secret",
         "secret_key", "client_secret", "token", "access_token", "Token", "api_key", "apikey",
         "api-key", "API_KEY", "DB_PASSWORD", "x-auth-token", "Authorization", "authorization"]  # fmt: skip
_SEPS = ["=", " = ", ": ", ":", " : ", '": ', '":"', "=>", " => ", "==", " ", "\t", ": \n",
         "=\n"]  # fmt: skip
_VALS = ["hunter2", "changeme", "Welcome", "ADMIN", "n/a", "expired", "123", "1234",
         "Tr0ub4dor&3", "'x y'", '"q v"', "${VAR}", "$(cmd)", "{{ v }}", "<your-password>",
         "/home/u", "~/.ssh", "true", "520", "Optional[str]", "(using", "abc-def",
         "Basic dXNlcjpwYXNzd29yZA==", "Bearer abc.def.ghi", "Bearer Zk91aXJlbGVzc3Rva2Vu1",
         "<PASSWORD_0123456789abcdef>", "[REDACTED:password]", "#comment", ""]  # fmt: skip
_GEN_ATOMS = [
    "10.0.0.5", "192.168.1.20", "172.16.0.9", "10.1.2.3.", "00:1A:2B:3C:4D:5E",
    "aa-bb-cc-dd-ee-ff", "24-01-02-03-04-05", "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd",
    "sk-proj-Ab3dEf_gh1JkLmn0PqRsTuvWxYz-1234", "pk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd",
    "AKIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", _GH, _SL, "xapp-1-A0123456789-abcdef",
    "glpat-" + _GL,
    "postgresql://app:pw4321@db:5432", "redis://user:pass99@cache:6379",
    "postgresql://db:5432/app", "mongodb+srv://u:p@ss@c0.x.net",
    "https://oauth2:glpatAbCd1234@gitlab.example.com", "me@example.com", "@oncall",
    "k8s_secret_0123456789abcdef0123456789abcdef", "api_key0123456789abcdef0123456789abcdef",
    "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----",
    "MIIEowIBAAKCAQEA7bq98EXAMPLEabcdef", "Proc-Type: 4,ENCRYPTED", "<XXXX>", "<X>",
    "<IP_ADDRESS_0123456789abcdef>", "[REDACTED:token]", "X", ">", "<", "paſsword=hunter2",
    "İ", "ı", "K8s_secret_0123456789abcdef0123456789abcdef", "ошибка", "失败", "é",
    "the bearer of bad news", "Token: expired", "users see password: incorrect",
    "--password Hunter2pw", "--token Zk91abc", "def f(password: str)", "if password == '':",
    "PWD=/home/runner", "failed_password: 520",
]  # fmt: skip
_GEN_GLUE = ["", "", " ", " ", "\n", "\r\n", "\t", "_", "-", ".", ":", "=", "/", "@", ",", ";",
             "x", "5", "E", '"', "'", "(", ")", "\\n", "%3D", "#"]  # fmt: skip


def _generated(rng: random.Random) -> str:
    """The #2021 review's fuzz generator: keyed spellings and atoms of every
    pattern, glued every way."""
    parts = []
    for _ in range(rng.randint(1, 5)):
        if rng.random() < 0.45:
            parts.append(rng.choice(_KEYS) + rng.choice(_SEPS) + rng.choice(_VALS))
        else:
            parts.append(rng.choice(_GEN_ATOMS))
    text = parts[0]
    for part in parts[1:]:
        text += rng.choice(_GEN_GLUE) + part
    if rng.random() < 0.3:
        text = rng.choice(["  ", "- ", "\n", "x ", '{"a": "']) + text
    if rng.random() < 0.3:
        text += rng.choice(["\n", ".", " now", "\r\n", '"}'])
    return text


def _reference_matches(pattern, view: str) -> list:
    """Every match of ``pattern`` on ``view``, as ``finditer`` reads them,
    except a command-line flag match (its ``flag`` group took part) whose
    word does not start with ``-``: dropped, and the scan goes on one past
    its start (#2025). The word is the run of ``[A-Za-z0-9_.-]`` ending at the
    key, found from scratch each time, nothing cached."""
    if "flag" not in pattern.regex.groupindex:
        return list(pattern.regex.finditer(view))
    matches = []
    pos = 0
    while pos <= len(view):
        match = pattern.regex.search(view, pos)
        if match is None:
            break
        word = re.search(r"[A-Za-z0-9_.-]*\Z", view[: match.start()]).group()
        long_option = view[max(match.start() - 2, 0) : match.start()] == "--"
        if match.start("flag") != -1 and not (long_option or word.startswith("-")):
            pos = match.start() + 1
            continue
        matches.append(match)
        pos = max(match.end(), match.start() + 1)
    return matches


def _reference_spans(text: str, patterns) -> list:
    """An independent finder: every pattern on the whole text, every round,
    no triggers, no scopes, no windows."""
    candidates: list = []

    def scan(view: str) -> None:
        for priority, pattern in enumerate(patterns):
            for match in _reference_matches(pattern, view):
                start, end = match.span(pattern.span_group)
                if start == -1:
                    start, end = match.span()
                if end > start:
                    candidates.append(
                        (
                            start,
                            -end,
                            priority,
                            pattern.entity_type,
                            pattern.names_a_value,
                        )
                    )

    scan(text)
    spans = redaction._merge(candidates, text)
    while spans:
        scan(redaction._masked(text, spans))
        grown = redaction._merge(candidates, text)
        if [(x.start, x.end) for x in grown] == [(x.start, x.end) for x in spans]:
            return grown
        spans = grown
    return spans


def test_the_finder_agrees_with_an_independent_reference():
    rng = random.Random("1980-reference")
    differ = []
    for _ in range(2000):
        text = _generated(rng)
        for patterns in (SANITIZER_PATTERNS, SECRET_PATTERNS):
            if find_redaction_spans(text, patterns) != _reference_spans(text, patterns):
                differ.append(text)
    assert differ == [], differ[:3]


@pytest.mark.parametrize(
    "text",
    [
        # A provider token with a full stop after it, as a keyed value on the
        # next line: the token keeps its type and the `.` stays out, so the
        # next pass must read `<K8S_SECRET_…>.` as a whole reference.
        "x)PWD: \n k8s_secret_0123456789abcdef0123456789abcdef.",
        "token=" + _JWT + ".",
        "password='x y';[REDACTED:token]_DB_PASSWORD:Optional[str]",
    ],
)
def test_a_shape_the_round_three_fuzz_found_is_idempotent(sanitizer, text):
    once = sanitizer.apply_regex_redaction(text, {})
    assert once != text
    assert sanitizer.apply_regex_redaction(once, {}) == once
    document = redact_sensitive_content(text)[0]
    assert redact_sensitive_content(document)[0] == document


def test_both_paths_are_idempotent_over_the_full_generator(sanitizer):
    rng = random.Random("1980-full-generator")
    changed = []
    for _ in range(3000):
        text = _generated(rng)
        once = sanitizer.apply_regex_redaction(text, {})
        if sanitizer.apply_regex_redaction(once, {}) != once:
            changed.append(("sanitizer", text))
        document = redact_sensitive_content(text)[0]
        if redact_sensitive_content(document)[0] != document:
            changed.append(("preprocessor", text))
    assert changed == [], changed[:3]


@pytest.mark.parametrize(
    "text",
    [
        "PWD=/home/runner/app is set",
        "pwd: ~/src/app/ on the box",
        "password=$1234567890 in the dump",
        "PWD=/home/runner_me@example.com",
        "PWD=/srv/x10.0.0.5 on the box",
        "me@example.com--token Zk91abc",
    ],
)
def test_a_value_the_title_scrub_rewrites_stays_scrubbed(sanitizer, text):
    # (``PWD=[path][email]``, ``PWD=[path]x[ip_address]`` after one scrub.)
    """The title's last step marks paths and phone numbers (`[path]app`,
    `$[phone]`). A path the sanitizer spares stays spared once marked, and a
    bind is at most four digits, so a ten-digit `$…` is a secret before the
    title marks it a phone number."""
    once = _scrub_title_source(text, sanitizer)
    assert _scrub_title_source(once, sanitizer) == once


def test_the_title_scrub_is_idempotent(sanitizer):
    """#1993's title scrub over the sanitizer: main changed 20 of 20,000 on a
    second pass, the first #2021 head 776 (`_REFERENCE` did not know a
    `[type]` marker, R1)."""
    rng = random.Random("1980-title")
    changed = []
    for _ in range(3000):
        once = _scrub_title_source(_generated(rng), sanitizer)
        if _scrub_title_source(once, sanitizer) != once:
            changed.append(once)
    assert changed == [], changed[:3]


# --- cost: every hostile shape the review found grows linearly ----------------


def _cost(sanitizer):
    return lambda text: (
        sanitizer.apply_regex_redaction(text, {}),
        redact_sensitive_content(text),
    )


#: Each shape, the smallest size tried, and how many times the helper may move
#: its window up (it reads 1x, 8x and 64x the size, then 8x more per
#: escalation, if those cannot decide). Every payload stays under ~256 KB.
_HOSTILE = {
    # R2: a BEGIN line, then one long line of spaces: the old body split the
    # spaces between two `[ \t]*`.
    "pem-spaces-line": (
        lambda n: "-----BEGIN PRIVATE KEY-----\n" + " " * n + "!",
        512,
        1,
    ),
    # BEGIN after BEGIN with no END: a lazy body ran to the end every time.
    "pem-begins-without-end": (
        lambda n: "-----BEGIN PRIVATE KEY-----\nAAAA\n" * n,
        16,
        1,
    ),
    # A run of schemes with no `@`: `\S+@` rescanned the run from each one.
    "dburl-run-without-at": (lambda n: "postgresql://h/" * n, 32, 1),
    # `eyJ` after `eyJ` with no `.`: each was a start that read the run.
    "jwt-run-without-dot": (lambda n: "eyJ" * n, 128, 1),
    # `sk-` after `sk-` with no digit: the digit lookahead read the run.
    "sk-run-without-digit": (lambda n: "sk-" * n, 128, 1),
    # One line holding every span: a newline search per span read the line
    # back to its start. That search is C-speed, so it shows only at ~250 KB.
    "one-line-of-spans": (lambda n: "password: x " * n, 320, 0),
    # Escaped newlines in one JSON string.
    "json-escaped-keys": (lambda n: '{"m":"' + "\\npassword: x" * n + '"}', 32, 1),
    # R4: `key:` lines whose next line is no continuation, each a line start
    # the line patterns try; then real continuations, each one a value.
    "colon-prompts": (lambda n: "Enter password:\nERROR 1045 x\n" * n, 64, 1),
    "yaml-continuations": (lambda n: "  password:\n    Tr0ub4dor&3\n" * n, 64, 1),
    # One line of `key: ` with the newline only at its end: the line start's
    # lazy prefix reads the line once.
    "one-line-of-colon-keys": (lambda n: "token: " * n + "\n  x", 128, 1),
    # Glued detections across masked rounds.
    "ip-akia-chain": (
        lambda n: "".join(f"10.0.{i % 250}.1AKIAABCDEFGHIJKLMNO1" for i in range(n)),
        16,
        1,
    ),
    # #2025: YAML blocks, each a key line and a body the match consumes; one
    # body of n lines; indicators with no body (each line a try that fails
    # at once); one line of commented indicators, which the atomic key line
    # commits on at the first (a lazy prefix re-read the comment per key);
    # blank runs a body does not take.
    "block-bodies": (lambda n: "password: |\n  Tr0ub4dor&3\n  line2\n" * n, 64, 1),
    "block-one-body": (lambda n: "token: |\n" + "  line of the body\n" * n, 64, 1),
    "block-empty-indicators": (lambda n: "secret: >-\n" * n, 64, 1),
    "block-comment-keys": (lambda n: "password: | # " * n + "\nnext: 1\n", 128, 1),
    "block-blank-runs": (
        lambda n: ("password: |\n  x" + "\n" * 8 + "y: 1\n") * n,
        32,
        1,
    ),
    # #2025 (A4): command names read as flags and dropped, each a scan that
    # resumes one past the dropped match; with an `İ` there is no index-mapped
    # view, so the whole-text fallback does the dropping.
    "flag-command-names": (
        lambda n: "aws ecr get-login-password --region us-east-1 " * n,
        64,
        1,
    ),
    "flag-command-names-unmapped": (
        lambda n: "İ " + "x-password --password " * n,
        64,
        1,
    ),
    # #2012 review: a flag glued after punctuation, each word walked back once;
    # tagged and anchored blocks; `: !` glued again and again, where a node
    # property read to the next blank would re-read the run per `:`; GitLab
    # prefixes glued into one run, and short ones.
    "flag-glued-punctuation": (
        lambda n: 'x="--password a" ;--token b (--secret c) ' * n,
        64,
        1,
    ),
    "block-tagged-bodies": (
        lambda n: "password: !!binary &a |\n  Tr0ub4dor&3\n  line2\n" * n,
        64,
        1,
    ),
    "block-tag-colons": (lambda n: "password " + ":!" * n + "\n  v\n", 256, 1),
    "gitlab-run": (lambda n: "glpat-" * n, 128, 1),
    "gitlab-short-tokens": (lambda n: "glpat-0123456789abcdefghi " * n, 64, 1),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_a_hostile_shape_costs_linear_time(sanitizer, name):
    build, small, escalations = _HOSTILE[name]
    assert_linear_growth(
        _cost(sanitizer), build, small=small, label=name, max_escalations=escalations
    )


def test_a_chain_of_rounds_is_cut_and_fails_closed():
    """R3: a chain whose every link needs the round before it — one new span
    per masked round — is cut at `_MAX_MASKED_ROUNDS`, and the line still
    growing is redacted whole. Uncapped it costs a round per link: quadratic.
    No pattern in the set chains on real text (the fuzz settles within three
    rounds), so the chain is built from a pattern of its own."""
    link = (RedactionPattern("LINK", re.compile(r"(?<![A-Za-z])ab"), ("ab",), "token"),)
    spans = find_redaction_spans("x ab" + "ab" * 40, link)
    assert [(s.start, s.end) for s in spans] == [(0, 84)]  # the whole line
    assert_linear_growth(
        lambda text: find_redaction_spans(text, link),
        lambda n: "ab" * n,
        small=256,
        label="masked-round chain",
        max_escalations=1,
    )


class _CountingText(str):
    """A ``str`` that counts the characters its ``find``/``rfind`` searches
    cover: the cost of a line-bound helper, without a clock."""

    covered = 0

    def find(self, sub, start=0, end=None):  # type: ignore[override]
        end = len(self) if end is None else end
        type(self).covered += max(0, end - start)
        return str.find(self, sub, start, end)

    def rfind(self, sub, start=0, end=None):  # type: ignore[override]
        end = len(self) if end is None else end
        type(self).covered += max(0, end - start)
        return str.rfind(self, sub, start, end)


def test_the_line_bounds_read_the_text_once():
    """``_lines_of`` over many spans on one line searches the line about once.
    A newline search back to the text's start per span is quadratic, but in
    C, too cheap to show as growth under 256 KB (it read 13-17x against the
    bound of 22.6), so it is pinned by what it reads instead."""
    text = "password: x " * 4000
    spans = find_redaction_spans(text, SANITIZER_PATTERNS)
    assert len(spans) == 4000
    _CountingText.covered = 0
    lines = redaction._lines_of(_CountingText(text), spans)
    assert lines == [(0, len(text))]
    assert _CountingText.covered <= 2 * len(text), _CountingText.covered


# ---------------------------------------------------------------------------
# 8. YAML block scalars and command names (#2025)
# ---------------------------------------------------------------------------
#
# Invariant: a secret written as a YAML block scalar under a keyed family key
# is redacted whole on the sanitizer and preprocessor paths; one pass is
# idempotent; every pattern stays linear-time; no case #2021's corpora detect
# is lost (sections 1-7 above, unchanged). Main redacted only the indicator:
# `password: |\n  hunter2\n  line2\nnext: 1\n` was the one span `|`.

#: ``(id, text, [(type, span)])``: the block body is the span, from the first
#: body line's first non-blank character to the last body line's end.
_BLOCK_ROWS = [
    ("literal", "password: |\n  hunter2\n  line2\nnext: 1\n", [("PASSWORD", "hunter2\n  line2")]),
    ("literal-strip", "password: |-\n  hunter2\nnext: 1", [("PASSWORD", "hunter2")]),
    ("folded", "password: >\n  hunter2\n  more\n", [("PASSWORD", "hunter2\n  more")]),
    ("folded-strip", "secret: >-\n  s3cr3t-value\nnext: 1\n", [("SECRET", "s3cr3t-value")]),
    ("indent-digit", "password: |2\n    hunter2\n", [("PASSWORD", "hunter2")]),
    ("keep", "password: |+\n  hunter2\n\nnext: 1", [("PASSWORD", "hunter2")]),
    ("strip-digit", "password: |-2\n   hunter2\n", [("PASSWORD", "hunter2")]),
    ("digit-strip", "token: >2-\n   abc123def\n", [("TOKEN", "abc123def")]),
    ("then-sibling", "  db:\n    password: |\n      hunter2\n    user: app\n", [("PASSWORD", "hunter2")]),
    ("end-of-text", "password: |\n  hunter2", [("PASSWORD", "hunter2")]),
    ("blank-inside", "password: |\n  a\n\n\n  b", [("PASSWORD", "a\n\n\n  b")]),
    ("blank-then-sibling", "password: |\n  a\n\n\nnext: b", [("PASSWORD", "a")]),
    ("blank-first", "password: |\n\n  hunter2\n", [("PASSWORD", "hunter2")]),
    ("blank-of-spaces", "password: |\n  a\n   \n  b\nnext: 1", [("PASSWORD", "a\n   \n  b")]),
    ("list-item", "- password: |\n    hunter2\n- name: x\n", [("PASSWORD", "hunter2")]),
    ("crlf", "password: |\r\n  hunter2\r\n  line2\r\nnext: 1\r\n", [("PASSWORD", "hunter2\r\n  line2")]),
    ("comment", "password: | # the db password\n  hunter2\n", [("PASSWORD", "hunter2")]),
    ("quoted-key", '"password": |\n  hunter2\n', [("PASSWORD", "hunter2")]),
    ("compound-key", "  db_password: >-\n    Tr0ub4dor&3\n", [("PASSWORD", "Tr0ub4dor&3")]),
    ("tab-indented", "password: |\n\thunter2\n\tline2\n", [("PASSWORD", "hunter2\n\tline2")]),
    ("provider-token", "token: >-\n  " + _GH + "\nother: x\n", [("GITHUB_TOKEN", _GH)]),
    ("k8s-string-data", "stringData:\n  api_key: |\n    k3y-value-1\n", [("API_KEY", "k3y-value-1")]),
    # A list item's body is deeper than its KEY, so the item's sibling keys are
    # not its body (#2012 review F7); a body at the dash's own depth (not
    # YAML) is still read, as the dash's.
    ("list-item-sibling", "- password: |\n    hunter2\n  user: bob\n", [("PASSWORD", "hunter2")]),
    ("nested-list-sibling", "users:\n  - name: a\n    password: |\n      hunter2\n    user: bob\n", [("PASSWORD", "hunter2")]),
    ("list-item-shallow-body", "- password: |\n  hunter2\n", [("PASSWORD", "hunter2")]),
    # Node properties before the indicator (#2012 review F5).
    ("tag-binary", "password: !!binary |\n  aHVudGVyMg==\n  line2\n", [("PASSWORD", "aHVudGVyMg==\n  line2")]),
    ("anchor", "password: &pw |\n  hunter2\n  line2\n", [("PASSWORD", "hunter2\n  line2")]),
    ("local-tag", "db_password: !vault |\n  hunter2\n  line2\n", [("PASSWORD", "hunter2\n  line2")]),
    ("tag-folded", "token: !!str >-\n  abc123def\n  line2\n", [("TOKEN", "abc123def\n  line2")]),
    ("tag-and-anchor", "secret: !!str &s |\n  s3cr3t-value\n", [("SECRET", "s3cr3t-value")]),
]  # fmt: skip

#: An indicator with no body below it reads nothing (main took the indicator).
_EMPTY_BLOCKS = [
    "password: |\nnext: 1\n",
    "password: |",
    "password: |\n",
    "password: |\n\n\nnext: 1",
    "password: >-\n   \n",
    "  password: |\n  next: 1\n",
]
#: A body that, whole, refers to a secret: what a pass writes, so the second
#: pass changes nothing.
_REFERENCE_BODIES = [
    "password: |\n  <PASSWORD_0123456789abcdef>\n",
    "password: |\n  [REDACTED:password]\nnext: 1",
    "token: >-\n  ${DB_TOKEN}\n",
    "password: |\n\n  <GITHUB_TOKEN_0123456789abcdef>\r\n",
]
#: A3: only after `:` is an indicator a block; after `=`, `=>` or a flag it
#: is the value as before, and so is one with more on its line.
_NOT_BLOCKS = [
    ("--password | tee out", [("PASSWORD", "|")]),
    ("token=>", [("TOKEN", ">")]),
    ("password= |\n  hunter2", [("PASSWORD", "|")]),
    ("password: > 8 chars", [("PASSWORD", ">")]),
]


@pytest.mark.parametrize(
    "text,expected", [r[1:] for r in _BLOCK_ROWS], ids=[r[0] for r in _BLOCK_ROWS]
)
def test_a_yaml_block_is_one_span_of_its_body(text, expected):
    assert _spans(text, SECRET_PATTERNS) == expected
    assert _spans(text) == expected


@pytest.mark.parametrize(
    "text,expected", [r[1:] for r in _BLOCK_ROWS], ids=[r[0] for r in _BLOCK_ROWS]
)
def test_no_line_of_a_block_survives_either_path(sanitizer, text, expected):
    """The body is gone on the sanitizer and the preprocessor paths, the key
    and its indicator stay (still YAML), and one pass is a fixed point."""
    body_lines = {
        line.strip()
        for _, value in expected
        for line in value.split("\n")
        if line.strip()
    }
    key_line = next(
        line
        for line in text.splitlines()
        if re.search(r":\s*(?:[!&]\S*\s+)*[|>]", line)
    )
    once_s = sanitizer.apply_regex_redaction(text, {})
    once_p = redact_sensitive_content(text)[0]
    for once in (once_s, once_p):
        assert key_line in once.splitlines(), once
        assert not body_lines & {line.strip() for line in once.splitlines()}, once
    assert sanitizer.apply_regex_redaction(once_s, {}) == once_s
    assert redact_sensitive_content(once_p)[0] == once_p


@pytest.mark.parametrize("text", _EMPTY_BLOCKS)
def test_an_empty_block_reads_nothing(text):
    assert _spans(text) == []


@pytest.mark.parametrize("text", _REFERENCE_BODIES)
def test_a_block_whose_body_is_a_reference_is_not_a_secret(text):
    assert _spans(text) == []


def test_a_block_of_two_references_is_a_secret():
    """Only a WHOLE body that is one reference is exempt."""
    text = "password: |\n  ${A}\n  ${B}\n"
    assert _spans(text) == [("PASSWORD", "${A}\n  ${B}")]


@pytest.mark.parametrize("text,expected", _NOT_BLOCKS)
def test_after_equals_or_a_flag_an_indicator_is_the_value_as_before(text, expected):
    assert _spans(text) == expected


def test_a_key_line_inside_a_block_is_not_read_again():
    """The block's match consumes its body, and a key line inside it (whose own
    block lies inside the outer one) is skipped: a ladder of ever-deeper key
    lines is one try per round, not one per line, so its cost stays linear in
    the text (each line re-reading the rest of the ladder is ~n^1.5)."""
    text = (
        "".join(" " * i + "password: |\n" for i in range(40)) + " " * 40 + "hunter2\n"
    )
    block = next(
        p for p in SECRET_PATTERNS if p.scope == "block" and p.entity_type == "PASSWORD"
    )

    class _Counting:
        def __init__(self, regex):
            self.regex, self.tries = regex, 0

        def match(self, *args):
            self.tries += 1
            return self.regex.match(*args)

        def __getattr__(self, name):
            return getattr(self.regex, name)

    counting = _Counting(block.regex)
    (span,) = find_redaction_spans(text, (dataclasses.replace(block, regex=counting),))
    assert text[span.start : span.end].endswith("hunter2")
    assert text[: span.start] == "password: |\n "
    # One try in the first round, one in the masked round that confirms it.
    assert counting.tries <= 2, counting.tries


_INDICATORS = [
    "|",
    "|-",
    "|+",
    ">",
    ">-",
    ">+",
    "|2",
    "|-2",
    "|2-",
    ">1",
    "| # note",
    ">- #x",
]


def _block_generated(rng: random.Random) -> str:
    """YAML-ish documents: keyed and plain keys with block indicators, bodies
    of values and atoms at random depths, blank lines, siblings, CRLF."""
    lines = []
    for _ in range(rng.randint(1, 4)):
        indent = rng.choice(["", "  ", "    ", "\t", "- ", "  - "])
        key = rng.choice(_KEYS + ["name", "config", "data"])
        quote = rng.choice(["", "", '"'])
        sep = rng.choice([" ", "", "  "])
        props = rng.choice(["", "", "", "!!binary ", "&a ", "!vault ", "!!str &b "])
        lines.append(
            f"{indent}{quote}{key}{quote}:{sep}{props}{rng.choice(_INDICATORS)}"
        )
        deeper = indent.replace("-", " ") + rng.choice([" ", "  ", "    ", "\t"])
        for _ in range(rng.randint(0, 3)):
            roll = rng.random()
            if roll < 0.15:
                lines.append("")
            elif roll < 0.25:
                lines.append(rng.choice(["  ", " \t"]))
            elif roll < 0.35:
                lines.append(indent + rng.choice(_KEYS) + ": " + rng.choice(_VALS))
            else:
                lines.append(deeper + rng.choice(_VALS + _GEN_ATOMS))
    newline = rng.choice(["\n", "\n", "\r\n"])
    text = newline.join(lines)
    return text + newline if rng.random() < 0.5 else text


def test_blocks_are_idempotent_on_both_paths():
    sanitizer = DataSanitizer()
    rng = random.Random("2025-blocks-idempotent")
    changed = []
    for _ in range(3000):
        text = _block_generated(rng)
        once = sanitizer.apply_regex_redaction(text, {})
        if sanitizer.apply_regex_redaction(once, {}) != once:
            changed.append(("sanitizer", text))
        document = redact_sensitive_content(text)[0]
        if redact_sensitive_content(document)[0] != document:
            changed.append(("preprocessor", text))
    assert changed == [], changed[:3]


def test_blocks_agree_with_the_reference_and_the_ungated_scan():
    """The gated finder (hits, line prechecks, resumes, every hit retried in a
    masked round) finds what the independent reference and the ungated
    whole-text scan find, an `İ` prefix included (no index-mapped view)."""
    rng = random.Random("2025-blocks-reference")
    differ = []
    for i in range(1500):
        text = _block_generated(rng)
        if i % 5 == 0:
            text = "İ\n" + text
        gated = find_redaction_spans(text, SANITIZER_PATTERNS)
        if gated != _reference_spans(text, SANITIZER_PATTERNS):
            differ.append(("reference", text))
        if gated != find_redaction_spans(text, _UNGATED):
            differ.append(("ungated", text))
    assert differ == [], differ[:3]


#: A4: a keyword inside a hyphenated command name is no flag. The word
#: holding the key (`[A-Za-z0-9_.-]`) must start with `-`.
_COMMAND_NAMES = [
    "aws ecr get-login-password --region us-east-1",
    "aws eks get-token --cluster-name my-cluster",
    "aws secretsmanager rotate-secret \\",
    "aws sts get-session-token --serial-number arn:aws:iam::1:mfa/u",
    "gcloud auth application-default print-access-token > /dev/null",
    "İ aws ecr get-login-password --region us-east-1",
]
_FLAGS = [
    ("--db-password hunter2", "hunter2"),
    ("--password hunter2", "hunter2"),
    ("--api-key sk_live_9x", "sk_live_9x"),
    ("mysql --password=hunter2", "hunter2"),
    ("x-password --password Tr0ub4dor&3", "Tr0ub4dor&3"),
    ("pg_dump --db-password Tr0ub4dor&3 -h db", "Tr0ub4dor&3"),
    ("İ --password Tr0ub4dor&3", "Tr0ub4dor&3"),
]


#: A flag glued after punctuation is a flag: its WORD starts with `-`, though
#: its run of non-whitespace does not (#2012 review F1). Main read each of
#: these; the first walk-back over all non-whitespace lost them.
_GLUED_FLAGS = [
    'ARGS="--password hunter2Zq"',
    "'--password hunter2Zq'",
    '["--password hunter2Zq"]',
    "cat f |--password hunter2Zq",
    "true;--password hunter2Zq",
    "a&&--password hunter2Zq",
    "(--password hunter2Zq)",
    "`--password hunter2Zq`",
    "ARGS=--password hunter2Zq",
    "x=$(--password hunter2Zq)",
    # A long option glued after a word: `--` makes it one, so the title
    # scrub's `[email]--token` reads it as the raw text did.
    "me@example.com--token hunter2Zq",
    # A single-dash flag after punctuation: only the word rule reads these.
    'ARGS="-password hunter2Zq"',
    "(-password hunter2Zq)",
    "x=$(-password hunter2Zq)",
]


@pytest.mark.parametrize("text", _GLUED_FLAGS)
def test_a_flag_glued_after_punctuation_is_read(sanitizer, text):
    assert _spans(text), text
    assert find_redaction_spans(text, _UNGATED) == find_redaction_spans(
        text, SANITIZER_PATTERNS
    )
    assert "hunter2Zq" not in sanitizer.apply_regex_redaction(text, {})
    assert "hunter2Zq" not in redact_sensitive_content(text)[0]


@pytest.mark.parametrize("text", _COMMAND_NAMES)
def test_a_command_name_is_not_a_flag(text):
    assert _spans(text) == []
    assert find_redaction_spans(text, _UNGATED) == []


@pytest.mark.parametrize("text,value", _FLAGS)
def test_a_flag_still_takes_its_value(sanitizer, text, value):
    assert [v for _, v in _spans(text)] == [value]
    assert find_redaction_spans(text, _UNGATED) == find_redaction_spans(
        text, SANITIZER_PATTERNS
    )
    assert value not in sanitizer.apply_regex_redaction(text, {})


_PACK = (
    pathlib.Path(__file__).resolve().parents[4] / "resources/knowledge/pack/runbooks"
)
#: What the keyed families read in the 91 shipped bodies: main read 17 lines,
#: 16 of them false positives; the 8 command names (A4) are gone. The keyed
#: families are not run on runbook bodies (#2012); this pins their precision
#: on real procedure text, which the conversion preprocessor and logs see.
_KEYED_ON_THE_PACK = [
    ("aws-iam-role-assumption-failure.md", 417, "TOKEN", "file://token.jwt"),
    ("aws-secrets-manager-throttling.md", 197, "TOKEN", 'os.environ["AWS_SESSION_TOKEN"]}'),
    ("azure-vm-boot-failure.md", 58, "PASSWORD", "P@ssw0rd1234!"),
    ("datadog-agent-no-metrics.md", 102, "API_KEY", ".*/api_key:"),
    ("datadog-agent-no-metrics.md", 145, "API_KEY", '$DD_API_KEY"'),
    ("efs-mount-failure.md", 51, "TOKEN", '$TOKEN"'),
    ("eks-cluster-authentication.md", 26, "TOKEN", "AccessDenied:"),
    ("k8s-rbac-forbidden.md", 369, "TOKEN", "false`"),
    ("prometheus-high-cardinality.md", 141, "API_KEY", "..."),
]  # fmt: skip


def test_the_keyed_families_read_nine_spans_of_the_shipped_bodies():
    keyed = tuple(p for p in SECRET_PATTERNS if p.names_a_value)
    bodies = sorted(_PACK.rglob("*.md"))
    assert len(bodies) == 91
    found = sorted(
        (
            path.name,
            text.count("\n", 0, span.start) + 1,
            span.entity_type,
            text[span.start : span.end],
        )
        for path in bodies
        for text in [path.read_text(encoding="utf-8")]
        for span in find_redaction_spans(text, keyed)
    )
    assert found == _KEYED_ON_THE_PACK
