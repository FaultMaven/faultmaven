"""#2791: an extractor that cannot parse a shape keeps the content, not a fragment.

One rule, in one place. An extractor that meets a shape it does not parse says
so, setting ``file_meta["unparsed"]`` to a reason; the dispatch
(``_keep_unparsed_content``, on the way back from every extractor's worker)
then stores the content as uploaded, under a marker naming the reason, in
place of the fragment. Like any stored extract it is held to
``MAX_STORED_EXTRACT_CHARS`` (#2789): past it, head and tail, with
``search_file`` reading the rest.

The five shapes the issue measured, each with a test that the content is
kept, plus the two other extractors whose unknown-format fallback kept a
preview: ``kubectl describe`` under the config extractor, a repeated YAML
key, a long table from a command no parser reads, a whitespace metrics table
(which the metrics extractor now parses), and a unified diff.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.container.providers.infrastructure import create_extractors
from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.extractors import (
    CommandOutputExtractor,
    MetricsAndPerformanceExtractor,
    ProfilingDataExtractor,
    SourceCodeExtractor,
    StructuredConfigExtractor,
    TraceDataExtractor,
)
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    MAX_STORED_EXTRACT_CHARS,
    UNPARSED_META_KEY,
    unparsed_preview,
)
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
)

pytestmark = pytest.mark.unit

DESCRIBE = """\
Name:             api-7d9f8b6c5-x2k4p
Namespace:        production
Node:             ip-10-0-3-41.ec2.internal/10.0.3.41
Labels:           app=api
                  pod-template-hash=7d9f8b6c5
Status:           Running
Containers:
  api:
    Image:          registry.example.com/api:2.14.1
    State:          Running
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
    Ready:          False
    Restart Count:  4
    Limits:
      memory:  512Mi
    Liveness:     http-get http://:8080/healthz delay=10s timeout=1s period=10s
Conditions:
  Type              Status
  Initialized       True
  Ready             False
Events:
  Type     Reason     Age                From     Message
  ----     ------     ----               ----     -------
  Warning  BackOff    2m (x14 over 9m)   kubelet  Back-off restarting failed container api
  Warning  Unhealthy  3m (x9 over 9m)    kubelet  Readiness probe failed: statuscode: 503
"""

REPEATED_YAML_KEY = """\
status:
  conditions:
  - lastTransitionTime: "2026-11-13T22:10:04Z"
    type: Ready
    status: "False"
    type: ContainersReady
    status: "True"
  phase: Running
"""


def _service_table(rows: int) -> str:
    """``kubectl get svc``: a table from a command no parser here reads."""
    lines = [
        "NAME                    TYPE           CLUSTER-IP     EXTERNAL-IP   PORT(S)"
    ]
    lines += [
        f"svc-{i:03d}-backend         ClusterIP      10.96.0.{i % 250:<3}    <none>"
        f"        {8000 + i}/TCP"
        for i in range(rows - 1)
    ]
    lines.append(
        "svc-payments-gateway    LoadBalancer   10.96.1.49     <pending>     443/TCP"
    )
    return "\n".join(lines) + "\n"


WHITESPACE_METRICS = "timestamp            cpu_pct  mem_mb  p99_ms\n" + "".join(
    f"2026-11-13T22:{i:02d}:00  {30 + i * 1.5:5.1f}  {400 + i * 3:5d}  {120 + i * 9:5d}\n"
    for i in range(40)
)

JAVA_DIFF = """\
diff --git a/src/main/java/com/example/pool/ConnectionPool.java b/src/main/java/com/example/pool/ConnectionPool.java
index 3f2a1b0..9c4e7d2 100644
--- a/src/main/java/com/example/pool/ConnectionPool.java
+++ b/src/main/java/com/example/pool/ConnectionPool.java
@@ -40,7 +40,8 @@ public class ConnectionPool {
     private final DataSource dataSource;
-    private static final int MAX_POOL_SIZE = 50;
+    private static final int MAX_POOL_SIZE = 200;
+    private static final Duration ACQUIRE_TIMEOUT = Duration.ofSeconds(30);

     public ConnectionPool(DataSource dataSource) {
         this.dataSource = dataSource;
@@ -88,6 +89,6 @@ public class ConnectionPool {
     public Connection acquire() throws SQLException {
-        return pool.take();
+        return pool.poll(ACQUIRE_TIMEOUT.toMillis(), TimeUnit.MILLISECONDS);
     }
 }
"""

#: Jaeger's UI copied as text: a trace that is not JSON. Its failing span is
#: in the middle, where the extractor's own preview (first and last ten
#: lines) does not reach.
TRACE_TEXT = (
    "Trace 4bf92f3577b34da6 checkout 2.31s 41 spans\n"
    + "".join(
        f"  inventory StockClient.reserve[{i}]   0.0{i % 10}s\n" for i in range(20)
    )
    + "      payments PaymentClient.charge     2.21s  status=DEADLINE_EXCEEDED\n"
    + "".join(
        f"  pricing PriceClient.quote[{i}]       0.0{i % 10}s\n" for i in range(20)
    )
)

#: ``py-spy dump``: a profile in no format read here. The thread holding the
#: pool is past the 30 lines the extractor's own preview keeps.
PROFILE_TEXT = (
    "Process 4242: python app.py\nPython v3.11.9 (/usr/bin/python3.11)\n\n"
    + "".join(
        f'Thread {4300 + i} (idle): "worker-{i}"\n    wait (threading.py:331)\n'
        for i in range(20)
    )
    + 'Thread 4242 (active): "MainThread"\n    acquire (pool.py:88)\n'
)


def _service() -> PreprocessingService:
    return PreprocessingService(classifier=DataClassifier(), **create_extractors())


def _extract_of(structural_index: str) -> ExtractResult:
    return ExtractResult.from_json(structural_index)


async def _dispatch(extractor, content: str):
    """The extraction as the dispatch returns it, through a real worker."""
    return await _service()._extract_with_timeout(
        extractor, content, "upload.txt", principal="u1"
    )


class TestTheDispatchKeepsWhatWasNotParsed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("extractor", "content", "needles"),
        [
            (
                StructuredConfigExtractor(),
                DESCRIBE,
                ["OOMKilled", "Restart Count:  4", "BackOff", "statuscode: 503"],
            ),
            (
                StructuredConfigExtractor(),
                REPEATED_YAML_KEY,
                ["type: Ready", 'status: "False"', "type: ContainersReady"],
            ),
            (
                CommandOutputExtractor(),
                _service_table(60),
                ["svc-000-backend", "svc-030-backend", "svc-payments-gateway"],
            ),
            (
                SourceCodeExtractor(),
                JAVA_DIFF,
                [
                    "-    private static final int MAX_POOL_SIZE = 50;",
                    "+    private static final int MAX_POOL_SIZE = 200;",
                    "TimeUnit.MILLISECONDS",
                ],
            ),
            (TraceDataExtractor(), TRACE_TEXT, ["DEADLINE_EXCEEDED"]),
            (ProfilingDataExtractor(), PROFILE_TEXT, ["pool.py:88", "MainThread"]),
        ],
        ids=["describe", "repeated-key", "unknown-table", "diff", "trace", "profile"],
    )
    async def test_the_content_is_kept_whole(self, extractor, content, needles):
        on_its_own = extractor.extract(content)
        assert UNPARSED_META_KEY in on_its_own.file_meta
        assert not all(
            n in on_its_own.file_extract for n in needles
        ), "premise: the extractor alone drops some of the content"

        result = await _dispatch(extractor, content)

        kept = _extract_of(result.metadata["extract_result_json"])
        assert kept.file_extract.endswith(content)
        assert kept.file_extract.startswith("[Not parsed: ")
        assert all(n in kept.file_extract for n in needles)
        assert (
            kept.file_meta[UNPARSED_META_KEY] == on_its_own.file_meta[UNPARSED_META_KEY]
        )
        assert result.content == kept.file_extract

    @pytest.mark.asyncio
    async def test_a_long_unknown_table_keeps_its_last_rows(self):
        """Past the stored bound the content is cut head and tail, never to
        its first rows: the table's last line, its one load balancer, is
        kept."""
        content = _service_table(1200)
        assert len(content) > MAX_STORED_EXTRACT_CHARS

        result = await _service().classify_and_extract(
            content=content,
            filename="services.txt",
            principal="u1",
            user_override=DataType.COMMAND_OUTPUT,
        )

        kept = _extract_of(result.structural_index).file_extract
        assert len(kept) <= MAX_STORED_EXTRACT_CHARS
        assert kept.startswith("[Not parsed: ")
        assert "svc-000-backend" in kept
        assert kept.rstrip().endswith(content.rstrip().splitlines()[-1])
        assert "search_file reads the whole upload" in kept

    @pytest.mark.asyncio
    async def test_a_parsed_shape_is_left_as_the_extractor_made_it(self):
        """The control: an extract with no report is not touched."""
        content = "server:\n  port: 8080\n  host: 0.0.0.0\nlogging:\n  level: info\n"

        result = await _dispatch(StructuredConfigExtractor(), content)

        kept = _extract_of(result.metadata["extract_result_json"])
        assert UNPARSED_META_KEY not in kept.file_meta
        assert (
            kept.file_extract
            == StructuredConfigExtractor().extract(content).file_extract
        )

    @pytest.mark.asyncio
    async def test_an_upload_past_the_line_cutoff_is_kept_end_to_end(self):
        """Through ``classify_and_extract``: a 300-row table typed as command
        output reaches the extractor and is stored with its tail."""
        content = _service_table(300)
        assert content.count("\n") >= MIN_EXTRACTION_LINES

        result = await _service().classify_and_extract(
            content=content,
            filename="services.txt",
            principal="u1",
            user_override=DataType.COMMAND_OUTPUT,
        )

        kept = _extract_of(result.structural_index)
        assert "svc-payments-gateway" in kept.file_extract
        assert kept.file_meta[UNPARSED_META_KEY]


class TestUnparsedPreview:
    def test_the_content_follows_the_marker_whole(self):
        content = "row\n" * 50_000
        preview = unparsed_preview(content, "why")
        assert preview.startswith("[Not parsed: why.")
        assert preview.endswith(content)


class TestTheConfigExtractorReports:
    def test_a_repeated_yaml_key_is_reported(self):
        result = StructuredConfigExtractor().extract(REPEATED_YAML_KEY)
        assert "'type'" in result.file_meta[UNPARSED_META_KEY]

    def test_a_repeated_json_key_is_reported(self):
        content = json.dumps({"a": 1})[:-1] + ', "a": 2, "b": 3}'
        result = StructuredConfigExtractor().extract(content)
        assert "'a'" in result.file_meta[UNPARSED_META_KEY]

    def test_a_merge_key_override_is_yaml_not_a_repeat(self):
        content = (
            "defaults: &defaults\n  timeout: 30\n  retries: 3\n"
            "service:\n  <<: *defaults\n  timeout: 60\n"
        )
        result = StructuredConfigExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta
        assert result.file_meta["format"] == "yaml"
        assert "timeout: 60" in result.file_extract

    def test_a_chained_merge_override_is_not_a_repeat(self):
        """A mapping merged into another is flattened in place before its own
        keys could be checked; the check reads the tree before that."""
        content = (
            "base: &z {x: 0, y: 0}\n"
            "defaults: {inner: &a {<<: *z, x: 1}}\n"
            "c: {<<: *a, y: 5}\n"
        )
        result = StructuredConfigExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta
        assert result.file_meta["format"] == "yaml"

    def test_keys_that_construct_equal_are_a_repeat(self):
        """``yes`` and ``true`` are both True in YAML 1.1: one value is lost."""
        result = StructuredConfigExtractor().extract("yes: 1\ntrue: 2\n")
        assert "True" in result.file_meta[UNPARSED_META_KEY]

    def test_a_scalar_yaml_refuses_falls_through_instead_of_raising(self):
        """``2026-13-45`` is a date its constructor refuses (ValueError); the
        extractor must not raise, or the upload gets the error preview."""
        result = StructuredConfigExtractor().extract("a: 2026-13-45\nb: 1\n")
        assert UNPARSED_META_KEY in result.file_meta

    @pytest.mark.parametrize(
        ("content", "fmt"),
        [
            ('{"server": {"port": 8080}}', "json"),
            ("server:\n  port: 8080\n", "yaml"),
            ('[server]\nport = 8080\nhost = "0.0.0.0"\n', "toml"),
        ],
    )
    def test_each_format_still_parses_and_names_itself(self, content, fmt):
        result = StructuredConfigExtractor().extract(content)
        assert result.file_meta["format"] == fmt
        assert UNPARSED_META_KEY not in result.file_meta

    def test_an_env_file_is_read_by_the_key_value_fallback(self):
        content = (
            "# app settings\nDB_HOST=db.internal\nDB_POOL_SIZE=50\n"
            "LOG_LEVEL=info\nexport FEATURE_X=on\n"
        )
        result = StructuredConfigExtractor().extract(content)
        assert result.file_meta["format"] == "key-value"
        assert UNPARSED_META_KEY not in result.file_meta

    def test_an_ini_file_with_a_stray_line_is_still_read(self):
        """The fallback reads the content where most of it is pairs."""
        content = (
            "[mysqld]\nport=3306\nbind-address=0.0.0.0\nskip-name-resolve\n"
            "max_connections=500\n[client]\nport=3306\n"
        )
        result = StructuredConfigExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta

    def test_describe_output_is_reported(self):
        result = StructuredConfigExtractor().extract(DESCRIBE)
        assert "key=value" in result.file_meta[UNPARSED_META_KEY]


class TestTheMetricsExtractorReadsWhitespaceTables:
    def test_a_whitespace_table_is_parsed(self):
        result = MetricsAndPerformanceExtractor().extract(WHITESPACE_METRICS)
        assert result.file_meta["format"] == "whitespace table"
        assert result.file_meta["total_data_points"] == 3 * 40
        assert result.file_meta["time_span"] == (
            "2026-11-13T22:00:00 to 2026-11-13T22:39:00"
        )
        assert UNPARSED_META_KEY not in result.file_meta

    def test_a_timestamp_written_with_a_space_stays_one_field(self):
        content = "time  rps  errors\n" + "".join(
            f"2026-11-13 22:{i:02d}:00  {900 - i}  {i % 3}\n" for i in range(10)
        )
        result = MetricsAndPerformanceExtractor().extract(content)
        assert result.file_meta["format"] == "whitespace table"
        assert result.file_meta["time_span"].startswith("2026-11-13 22:00:00")

    def test_a_table_with_text_columns_is_not_metrics(self):
        """``kubectl get pods``: reading only RESTARTS would drop READY and
        STATUS without a word, so the table is reported, not read."""
        content = (
            "NAME     READY   STATUS             RESTARTS   AGE\n"
            "api-1    0/1     CrashLoopBackOff   12         34m\n"
            "api-2    1/1     Running            0          2d\n"
            "api-3    1/1     Running            1          2d\n"
        )
        result = MetricsAndPerformanceExtractor().extract(content)
        assert UNPARSED_META_KEY in result.file_meta

    def test_a_host_keyed_table_keeps_every_column(self):
        """Read as a table, not as Prometheus lines (``web1 50 60`` would be a
        sample of ``web1`` with only its first number kept)."""
        content = "host  cpu  mem\nweb1  50   60\nweb2  70   80\nweb3  95   90\n"
        result = MetricsAndPerformanceExtractor().extract(content)
        assert result.file_meta["format"] == "whitespace table"
        assert result.file_meta["metric_names"] == "cpu, mem"

    def test_prometheus_text_is_still_read_as_prometheus(self):
        content = (
            "# HELP http_requests_total Requests.\n"
            "# TYPE http_requests_total counter\n"
            'http_requests_total{code="200"} 1027\n'
            'http_requests_total{code="503"} 88\n'
        )
        result = MetricsAndPerformanceExtractor().extract(content)
        assert result.file_meta["format"] == "prometheus"

    def test_content_no_format_reads_is_reported(self):
        result = MetricsAndPerformanceExtractor().extract(
            "latency went up after the deploy and stayed high for an hour\n" * 3
        )
        assert UNPARSED_META_KEY in result.file_meta


class TestTheOtherExtractorsReport:
    def test_a_diff_is_reported_with_its_files(self):
        result = SourceCodeExtractor().extract(JAVA_DIFF)
        assert "unified diff" in result.file_meta[UNPARSED_META_KEY]
        assert result.file_meta["files_changed"] == 1

    def test_a_diff_names_its_files_deleted_ones_included(self):
        content = (
            "--- a/old.conf\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a=1\n-b=2\n"
            "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x = 1\n+++ y\n"
        )
        result = SourceCodeExtractor().extract(content)
        assert result.file_meta["files"] == "old.conf, app.py"
        assert result.file_meta["files_changed"] == 2

    def test_code_is_not_taken_for_a_diff(self):
        content = (
            "public class ConnectionPool {\n"
            "    private static final int MAX_POOL_SIZE = 200;\n"
            "    public Connection acquire() { return pool.take(); }\n"
            "}\n"
        )
        result = SourceCodeExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta

    def test_a_known_command_is_not_reported(self):
        content = (
            "Filesystem     1K-blocks     Used Available Use% Mounted on\n"
            "/dev/nvme0n1p1  51474912 48967424   2507488  96% /\n"
        )
        result = CommandOutputExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta

    def test_json_trace_data_is_not_reported(self):
        content = json.dumps(
            {
                "traceId": "4bf92f3577b34da6",
                "spans": [
                    {"spanId": "a1", "operationName": "checkout", "duration": 2310}
                ],
            }
        )
        result = TraceDataExtractor().extract(content)
        assert UNPARSED_META_KEY not in result.file_meta
