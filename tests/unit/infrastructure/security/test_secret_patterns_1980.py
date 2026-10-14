"""#1980 — one secret-detection pattern set, and every redaction path uses it.

Invariant: ``SECRET_PATTERNS`` (the sanitizer's own secret detection) serves
every redaction path — the sanitizer's regex half and the knowledge document
preprocessor; it detects the real-world secret spellings tabled below, and it
leaves ordinary incident text unchanged except where a pattern genuinely needs
the span (``JUSTIFIED``, each with its reason).

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
5. the trigger gating in ``find_redaction_spans`` changes no result.
"""

from __future__ import annotations

import base64
import dataclasses
import random
import string

import pytest

from faultmaven.infrastructure.security.case_redaction import REDACTION_PLACEHOLDER_RE
from faultmaven.infrastructure.security.redaction import (
    NETWORK_ADDRESS_PATTERNS,
    SANITIZER_PATTERNS,
    SECRET_PATTERNS,
    DataSanitizer,
    find_redaction_spans,
    redact_spans,
)
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    redact_sensitive_content,
)

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


def test_a_word_after_key_colon_in_running_text_is_prose_by_design():
    """The trade the prose rule makes, pinned so it stays visible: in running
    text, ``key: <a word>`` is not read as a secret — that is how
    ``password: expired for the svc account`` and MySQL's ``(using password:
    YES)`` survive. A letters-only password written that way, and only that
    way, is missed (about 1 in 400,000 random 12-character passwords from
    #1993's fuzz alphabet); every other spelling of it is caught."""
    value = "Wcarjchlkakh"
    assert _spans(f"ldap bind fails with passwd: {value} for svc") == []
    assert _spans(f"ldap bind fails with passwd={value} for svc")
    assert _spans(f"ldap bind fails with passwd:{value} for svc")
    assert _spans(f'bind {{"passwd": "{value}"}} for svc')
    assert _spans(f"ldap:\n  passwd: {value}\n")


def test_one_span_never_wraps_another(sanitizer):
    """Detection reads the raw text, and overlapping matches merge into one
    span: a placeholder never holds another, so one reverse restores all."""
    text = "password=postgresql://app:pw4321@10.1.2.3:5432/app now"
    out = sanitizer.apply_regex_redaction(text, {})
    assert len(REDACTION_PLACEHOLDER_RE.findall(out)) == 1
    assert out.startswith("<PASSWORD_") and out.endswith(" now")


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
    # A line that is exactly `key: value` is how YAML and `.env` write a
    # secret; a bare word there is not told apart from `password: changeme`.
    "Token: expired": [("TOKEN", "Token: expired")],
}

NEGATIVE = P11 + LINES + ["Token: expired"]


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
