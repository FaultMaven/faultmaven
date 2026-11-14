"""#2361: a paste is typed by its content, by the taxonomy recorded on #2361.

Each shape rule has a positive case and a negative that shares its nouns
(the same tool, the same words) but not its shape, because a rule that keys
on vocabulary instead of shape is the failure this issue measured. The
classifier tests pin the three behaviours around the rules: a paste's minted
``.txt`` name is ignored, the 0.72 ``UNSTRUCTURED_TEXT`` default and the
regex config rule do not apply to pastes, and a file is classified as before.
"""

from __future__ import annotations

import pytest

from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.paste_shapes import paste_shape
from tests.wallclock import assert_linear_growth

pytestmark = pytest.mark.unit

MINTED = "pasted-content-20261113T081500.txt"
PASTE = SourceMetadata(source_type="text_paste")


def _type(content: str):
    shape = paste_shape(content)
    return shape[0] if shape else None


# ---------------------------------------------------------------------------
# One positive and one noun-sharing negative per rule
# ---------------------------------------------------------------------------

SYSTEMD_STATUS = (
    "● billing-exporter.service - Billing metrics exporter\n"
    "     Loaded: loaded (/etc/systemd/system/billing-exporter.service; enabled)\n"
    "     Active: failed (Result: exit-code) since Fri 2026-10-16 23:05:03 UTC\n"
    "   Main PID: 4412 (code=exited, status=1/FAILURE)\n"
    "Oct 16 23:05:03 host billing-exporter[4412]: error: cannot bind :9102\n"
)
# The same unit named in prose: not a status report.
SYSTEMD_PROSE = (
    "billing-exporter.service failed after the deploy.\n"
    "Loaded and Active looked fine on the other node.\n"
    "Main PID changed twice.\n"
)

VAULT_READ = (
    "Key                 Value\n"
    "---                 -----\n"
    "token_ttl           1h\n"
    "token_max_ttl       4h\n"
    "policies            [cert-manager-pki]\n"
)
VAULT_PROSE = "Key rotation: the Value of token_ttl is 1h.\nKey owner: platform\n"

DIFF = (
    "diff --git a/pom.xml b/pom.xml\n"
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -10,7 +10,7 @@\n"
    "   <dependency>\n"
    "-    <version>1.11.0</version>\n"
    "+    <version>1.11.3</version>\n"
    "   </dependency>\n"
)
# Bullet points that start with + and -, but no hunk.
DIFF_LOOKALIKE = "- removed the old pool\n+ added a retry\n- bumped the version\n"

JSON_DOC = '{"status": "UP", "components": {"db": {"status": "UP"}}}'
JSON_LOOKALIKE = '{"status": "UP"} was returned by /health, then it went DOWN'

LOG_ISO = (
    "2026-10-23T18:44:28.377Z  WARN 1 --- [order-processing] o.a.k.c.NetworkClient : retrying\n"
    "2026-10-23T18:44:28.407Z  INFO 1 --- [order-processing] o.a.k.c.Metadata : updated\n"
    "2026-10-23T18:44:29.001Z ERROR 1 --- [order-processing] o.a.k.c.Consumer : timeout\n"
)
LOG_LOGFMT = (
    'time=2026-10-16T23:07:20.387Z level=INFO msg="request" path=/v1/users\n'
    'time=2026-10-16T23:07:20.448Z level=WARN msg="slow" path=/v1/users\n'
)
# Timestamps and the word ERROR, but status sentences, not log lines.
LOG_LOOKALIKE = (
    "05:32:23  api-gateway 503s on user-service route\n"
    "05:32:23  GET /v1/users/me through api-gateway: ERROR budget burning\n"
    "05:32:23  user-service pods Ready\n"
)

PROMETHEUS = (
    "# HELP jvm_memory_used_bytes The amount of used memory\n"
    "# TYPE jvm_memory_used_bytes gauge\n"
    'jvm_memory_used_bytes{area="heap",id="G1 Eden Space"} 1.2582912E7\n'
    'jvm_memory_used_bytes{area="nonheap",id="Metaspace"} 6.4E7\n'
)
# A whitespace metrics table: no extractor reads it, so no rule (#2791).
METRICS_TABLE = (
    "TIME      p99_ms   rps    errors\n"
    "10:00:00  120      480    2\n"
    "10:05:00  131      470    3\n"
)

KUBECTL_GET = (
    "NAME                                READY   STATUS             RESTARTS   AGE\n"
    "order-processing-7d9f8c6b5d-2xk4p   0/1     CrashLoopBackOff   7          14m\n"
    "order-processing-7d9f8c6b5d-9mzqt   1/1     Running            0          14m\n"
)
KUBECTL_GET_TWO_TABLES = (
    KUBECTL_GET + "NAME               READY   UP-TO-DATE   AVAILABLE   AGE\n"
    "order-processing   1/2     2            1           3d\n"
)
# Capitalised words in a sentence, not a header over columns.
TABLE_LOOKALIKE = (
    "NAME READY STATUS were all fine\nexcept order-processing which restarted\n"
)

CURL_V = (
    "* Host user-service.production.svc:8443 was resolved.\n"
    "* Connected to user-service.production.svc (10.96.41.207) port 8443\n"
    "> GET /v1/users/me HTTP/1.1\n"
    "> Host: user-service.production.svc:8443\n"
    "< HTTP/1.1 503 Service Unavailable\n"
    "< content-length: 19\n"
)
CURL_LOOKALIKE = (
    "* note: curl to user-service returned 503\nwe retried the GET twice\nstill 503\n"
)

KEY_VALUE = (
    "subject=CN=user-service.production.svc, OU=accounts, O=Acme Corp\n"
    "issuer=CN=Corp Internal Services CA R2, O=Acme Corp\n"
    "notAfter=Oct 29 03:00:16 2026 GMT\n"
)
# KEY=value-led lines that are status messages, not settings.
KEY_VALUE_STATUS = (
    "Approved=True cert-manager.io: Certificate request has been approved\n"
    "Ready=False Pending: Waiting on certificate issuance from order\n"
)

YAML_MANIFEST = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  name: order-processing\n"
    "spec:\n"
    "  replicas: 2\n"
    "  template:\n"
    "    spec:\n"
    "      containers:\n"
    "      - name: app\n"
    "        image: registry.internal/order-processing:2.1.3\n"
)
# ``kubectl describe``: the same nouns, aligned as a report a YAML parser
# would half-read.
DESCRIBE = (
    "Name:         order-processing-7d9f8c6b5d-2xk4p\n"
    "Namespace:    orders\n"
    "Status:       Running\n"
    "Containers:\n"
    "  app:\n"
    "    Image:          registry.internal/order-processing:2.1.3\n"
    "    State:          Waiting\n"
    "      Reason:       CrashLoopBackOff\n"
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (SYSTEMD_STATUS, DataType.COMMAND_OUTPUT),
        (DIFF, DataType.SOURCE_CODE),
        (JSON_DOC, DataType.STRUCTURED_CONFIG),
        (LOG_ISO, DataType.LOGS_AND_ERRORS),
        (LOG_LOGFMT, DataType.LOGS_AND_ERRORS),
        (KUBECTL_GET, DataType.COMMAND_OUTPUT),
        (KUBECTL_GET_TWO_TABLES, DataType.COMMAND_OUTPUT),
        (CURL_V, DataType.COMMAND_OUTPUT),
        (KEY_VALUE, DataType.STRUCTURED_CONFIG),
        (YAML_MANIFEST, DataType.STRUCTURED_CONFIG),
    ],
    ids=[
        "systemd-status",
        "diff",
        "json",
        "iso-log",
        "logfmt",
        "kubectl-get",
        "two-tables",
        "curl-v",
        "key-value",
        "yaml",
    ],
)
def test_a_shape_is_typed_by_the_taxonomy(content, expected):
    assert _type(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        SYSTEMD_PROSE,
        VAULT_PROSE,
        DIFF_LOOKALIKE,
        JSON_LOOKALIKE,
        LOG_LOOKALIKE,
        METRICS_TABLE,
        TABLE_LOOKALIKE,
        CURL_LOOKALIKE,
        KEY_VALUE_STATUS,
        DESCRIBE,
    ],
    ids=[
        "systemd-prose",
        "vault-prose",
        "plus-minus-bullets",
        "json-in-a-sentence",
        "probe-result-lines",
        "whitespace-metrics-table",
        "caps-in-a-sentence",
        "curl-in-prose",
        "status-key-value",
        "kubectl-describe",
    ],
)
def test_the_same_nouns_in_another_shape_claim_nothing(content):
    assert paste_shape(content) is None


@pytest.mark.parametrize(
    "content", [VAULT_READ, PROMETHEUS], ids=["vault-read", "prometheus"]
)
def test_a_shape_whose_rule_failed_gate_4_stays_uncertain(content):
    """Each of these rules made a held-out paste wrong and confident, so by
    #2361's gate 4 neither ships: the paste passes to the model as text."""
    assert paste_shape(content) is None


def test_a_mixed_paste_is_claimed_by_no_rule():
    """Below two thirds of the lines in one shape, a paste stays uncertain:
    a table over a YAML manifest has no single type."""
    assert paste_shape(KUBECTL_GET + YAML_MANIFEST + LOG_ISO) is None


# ---------------------------------------------------------------------------
# The classifier around the rules
# ---------------------------------------------------------------------------


class TestAPasteIsTypedByContent:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (LOG_ISO, DataType.LOGS_AND_ERRORS),
            (KUBECTL_GET, DataType.COMMAND_OUTPUT),
            (YAML_MANIFEST, DataType.STRUCTURED_CONFIG),
        ],
        ids=["spring-boot-lines", "kubectl-get-pods", "deployment-yaml"],
    )
    def test_the_2361_repro_is_typed_confidently(self, content, expected):
        """#2361's reproduction table: each of these came back
        unstructured_text at a confident 0.72 under the minted name."""
        result = DataClassifier().classify(MINTED, content, source_metadata=PASTE)

        assert result.classification_failed is False
        assert result.data_type == expected

    def test_prose_in_a_paste_is_uncertain_not_a_confident_default(self):
        """The 0.72 default is closed for pastes: no content signal supports
        it, and an uncertain paste reaches the model as unstructured text
        anyway (#2355)."""
        result = DataClassifier().classify(
            MINTED, "The pods restarted after the deploy.\n", source_metadata=PASTE
        )

        assert result.classification_failed is True

    def test_the_regex_config_rule_does_not_type_a_paste(self):
        """A paste is config only where a config parser reads it. This JVM
        log scores as config on vocabulary alone."""
        jvm = (
            "[97.004s][info][class,load] java.util.concurrent.CompletableFuture source: jrt:/java.base\n"
            "[111.385s][info][class,load] org.apache.avro.generic.GenericData source: file:/app/lib/avro.jar\n"
            "max_heap=1073741824\n"
            "used_heap=870318080\n"
        )
        result = DataClassifier().classify(MINTED, jvm, source_metadata=PASTE)

        assert not (
            result.classification_failed is False
            and result.data_type == DataType.STRUCTURED_CONFIG
        )

    def test_a_file_keeps_its_extension_rules(self):
        """Only a paste drops its extension: a real ``.yaml`` file is still
        typed by it, whatever its shape."""
        result = DataClassifier().classify(
            "values.yaml",
            "replicas: 2\nimage: app:1\n",
            source_metadata=SourceMetadata(source_type="file_upload"),
        )

        assert result.classification_failed is False
        assert result.data_type == DataType.STRUCTURED_CONFIG


# ---------------------------------------------------------------------------
# Review round: cost bounds, log shapes, JSON meaning, precedence
# ---------------------------------------------------------------------------


class TestEveryRuleIsBounded:
    """The classifier runs on the event loop: no rule may cost more than its
    sample allows, and no input may raise out of it."""

    def test_deeply_nested_json_does_not_raise(self):
        assert paste_shape("[" * 50_000 + "]" * 50_000) is None

    @pytest.mark.performance
    def test_a_padded_log_line_does_not_backtrack(self):
        # The log-line pattern itself, not paste_shape: paste_shape reads a
        # bounded sample, so its cost cannot grow. Before the whitespace run
        # was made possessive, a 19,000-space line took 13 s.
        from faultmaven.modules.preprocessing.paste_shapes import _LOG_LINE

        assert_linear_growth(
            _LOG_LINE.search,
            lambda spaces: "2025-01-01 00:00:00" + " " * spaces + "x",
            small=1024,
            label="_LOG_LINE on a timestamp padded with spaces",
        )

    def test_the_yaml_parser_reads_only_the_sample(self, monkeypatch):
        import yaml

        from faultmaven.modules.preprocessing import paste_shapes

        seen = []
        real_load = yaml.load

        def recording_load(stream, Loader):
            seen.append(len(stream))
            return real_load(stream, Loader=Loader)

        monkeypatch.setattr(yaml, "load", recording_load)
        big = "root:\n" + "".join(f"  k{i}:\n    a: {i}\n" for i in range(100_000))

        paste_shape(big)

        assert seen and max(seen) <= paste_shapes.SAMPLE_CHARS


LOGFMT_LEVEL_FIRST = (
    'level=warn ts=2025-10-15T14:00:00.123Z caller=main.go:12 msg="scrape failed"\n'
    'level=error ts=2025-10-15T14:00:01.123Z caller=main.go:40 msg="target down"\n'
)
LOGFMT_TS_FIRST = (
    'ts=2025-10-15T14:00:00Z caller=x.go:1 level=error msg="a"\n'
    'ts=2025-10-15T14:00:01Z caller=x.go:2 level=warn msg="b"\n'
)
JSON_LINES = (
    '{"time":"2026-10-16T23:07:20Z","type":"request","path":"pki/issue/svc"}\n'
    '{"time":"2026-10-16T23:07:21Z","type":"response","path":"pki/issue/svc"}\n'
)
# One JSON object per line with no time: records, not events.
JSON_LINES_NO_TIME = '{"name":"a","replicas":2}\n{"name":"b","replicas":3}\n'


@pytest.mark.parametrize(
    "content",
    [LOGFMT_LEVEL_FIRST, LOGFMT_TS_FIRST, JSON_LINES],
    ids=["logfmt-level-first", "logfmt-ts-first", "json-lines"],
)
def test_log_shapes_engineers_paste_are_logs(content):
    assert _type(content) == DataType.LOGS_AND_ERRORS


def test_json_lines_without_a_time_are_not_logs():
    assert _type(JSON_LINES_NO_TIME) != DataType.LOGS_AND_ERRORS


@pytest.mark.parametrize(
    "content",
    [
        '{"timestamp":"2025-10-15T14:00:00Z","level":"error","message":"db timeout"}',
        '[{"timestamp":"2025-10-15T14:00:00Z","level":"error","message":"db timeout"},'
        '{"timestamp":"2025-10-15T14:00:01Z","level":"warn","message":"retry"}]',
        '{"status":"success","data":{"resultType":"matrix","result":[]}}',
        '{"resourceSpans":[{"scopeSpans":[]}]}',
    ],
    ids=["log-event", "array-of-events", "metrics-query-result", "otlp-trace"],
)
def test_json_whose_meaning_is_not_configuration_is_not_config(content):
    """Owner, 2026-11-13: the taxonomy's first test, the type's meaning must
    be true of the content."""
    assert _type(content) != DataType.STRUCTURED_CONFIG


@pytest.mark.parametrize(
    "content",
    [
        "Content-Type: text/html\nDate: Mon, 15 Oct 2025 14:00:00 GMT\n"
        "Server: nginx/1.25\nX-Request-Id: abc\n",
        "error: failed to pull image\nreason: ImagePullBackOff\n"
        "message: back-off pulling image nginx:latst\n",
        "error: a failed\nerror: b failed\nwarning: c slow\nerror: d failed\n",
        "- step one\n- step two\n  more detail\n@@ -1 +1 @@\n",
    ],
    ids=["http-headers", "error-summary", "repeated-error-lines", "bullets-and-a-hunk"],
)
def test_messages_shaped_like_a_document_claim_nothing(content):
    assert paste_shape(content) is None


class TestTheOlderChecksKeepTheirShapes:
    def test_a_traceback_paste_stays_an_error_report(self):
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 3, in <module>\n'
            "    main()\n"
            "ValueError: bad\n"
        )
        result = DataClassifier().classify(MINTED, traceback, source_metadata=PASTE)

        assert result.data_type == DataType.ERROR_REPORT

    def test_a_trace_paste_stays_trace_data(self):
        trace = (
            '{"traceID":"4bf92f3577b34da6a3ce929d0e0e4736","spans":[{"spanID":'
            '"00f067aa0ba902b7","operationName":"GET /v1/users","spans": [],'
            '"process":{"serviceName":"api-gateway"}}]}'
        )
        result = DataClassifier().classify(MINTED, trace, source_metadata=PASTE)

        assert result.data_type == DataType.TRACE_DATA

    def test_a_long_paste_no_rule_claims_is_uncertain(self):
        """Owner, 2026-11-13: at 200+ lines the type picks the extractor, so
        an unclaimed paste is put to the user rather than guessed (#2355)."""
        prose = "".join(f"Step {i}: we checked the pods again.\n" for i in range(250))
        result = DataClassifier().classify(MINTED, prose, source_metadata=PASTE)

        assert result.classification_failed is True


# ---------------------------------------------------------------------------
# Second review round
# ---------------------------------------------------------------------------


def test_a_large_json_config_is_parsed_whole():
    """The JSON rule reads past the 20 KB sample: ``kubectl get -o json``
    lists run to hundreds of KB."""
    import json

    items = [{"kind": "Deployment", "metadata": {"name": f"d{i}"}} for i in range(3000)]
    doc = json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2)
    assert len(doc) > 100_000

    assert _type(doc) == DataType.STRUCTURED_CONFIG


def test_json_lines_of_metrics_are_not_logs():
    dump = "".join(
        f'{{"timestamp":"2025-01-01T00:0{i}:00Z","metric":"cpu_usage","value":0.{i}}}\n'
        for i in range(5)
    )
    assert _type(dump) != DataType.LOGS_AND_ERRORS


def test_a_flat_env_style_yaml_is_config():
    env = "".join(f"DATABASE_{i}: value{i}\n" for i in range(10))
    assert _type(env) == DataType.STRUCTURED_CONFIG


def test_many_http_headers_are_still_not_config():
    headers = (
        "Content-Type: text/html\nContent-Length: 19\nDate: Mon, 15 Oct 2025 14:00:00 GMT\n"
        "Server: nginx/1.25\nX-Request-Id: abc\nCache-Control: no-store\n"
    )
    assert paste_shape(headers) is None


def test_prose_naming_logfmt_keys_is_not_a_log():
    assert _type("we set time=5 level=3 there\nwe set time=6 level=4 here\n") != (
        DataType.LOGS_AND_ERRORS
    )
