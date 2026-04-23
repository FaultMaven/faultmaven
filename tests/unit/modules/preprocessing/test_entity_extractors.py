"""Phase 4b — per-data-type ``EntityExtractor`` coverage.

Tests the four concrete extractors individually (logs, command output,
config, trace) plus the registry dispatcher. Each test focuses on
content shapes the extractor must handle and, crucially, shapes it
must NOT misinterpret — false-positive suppression is more important
than recall for a case-level index.
"""

from __future__ import annotations

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.case.domain.models import EntityType
from faultmaven.modules.preprocessing.entities import (
    EntityObservation,
    extract_entities_for_data_type,
)
from faultmaven.modules.preprocessing.entities.command_output import (
    CommandOutputEntityExtractor,
)
from faultmaven.modules.preprocessing.entities.config import ConfigEntityExtractor
from faultmaven.modules.preprocessing.entities.logs import LogsEntityExtractor
from faultmaven.modules.preprocessing.entities.trace import TraceEntityExtractor


def _by_type(
    obs: list[EntityObservation], entity_type: EntityType
) -> list[EntityObservation]:
    return [o for o in obs if o.entity_type == entity_type]


def _values(obs: list[EntityObservation], entity_type: EntityType) -> set[str]:
    return {o.entity_value for o in _by_type(obs, entity_type)}


class TestLogsEntityExtractor:
    def setup_method(self):
        self.extractor = LogsEntityExtractor()

    def test_empty_content_returns_empty(self):
        assert self.extractor.extract("") == []

    def test_ipv4_counted_and_error_flagged(self):
        content = (
            "2024-01-01 12:00:00 ERROR Failed password for root from 192.168.1.1 port 22\n"
            "2024-01-01 12:00:01 INFO Accepted from 10.0.0.5 port 22\n"
            "2024-01-01 12:00:02 ERROR Failed password from 192.168.1.1 port 22\n"
        )
        obs = self.extractor.extract(content, error_line_indices={0, 2})
        ips = {o.entity_value: o for o in _by_type(obs, EntityType.IP)}
        assert ips["192.168.1.1"].mention_count == 2
        assert ips["192.168.1.1"].in_error_context is True
        assert ips["10.0.0.5"].mention_count == 1
        assert ips["10.0.0.5"].in_error_context is False

    def test_user_extracted_from_syslog(self):
        content = (
            "sshd[123]: Failed password for root from 10.0.0.1\n"
            "sshd[124]: Accepted publickey for alice user=alice\n"
        )
        obs = self.extractor.extract(content, error_line_indices={0})
        users = _values(obs, EntityType.USER)
        assert "alice" in users
        # ``for root`` is followed by ``from`` — the pattern intentionally
        # only catches the explicit ``user=``/``invalid user`` forms.

    def test_port_requires_structural_context(self):
        # A bare timestamp fragment ``04:47`` must not be captured as port 47.
        content = "2024-01-01 04:47:22 something happened\nport 8080 opened"
        obs = self.extractor.extract(content)
        ports = _values(obs, EntityType.PORT)
        assert "8080" in ports
        assert "47" not in ports

    def test_pid_bracket_notation(self):
        content = "sshd[1234]: starting\nunrelated 12:34:56 noise"
        obs = self.extractor.extract(content)
        pids = _values(obs, EntityType.PID)
        assert "1234" in pids
        # Time fragments should NOT capture PIDs 12/34/56 via brackets.
        assert "12" not in pids
        assert "34" not in pids

    def test_http_path_captured(self):
        content = "GET /api/users/42 HTTP/1.1\nPOST /checkout HTTP/1.1"
        obs = self.extractor.extract(content)
        paths = _values(obs, EntityType.PATH)
        assert paths == {"/api/users/42", "/checkout"}

    def test_ipv6_captured(self):
        content = "event from 2001:db8::1 and also fe80::1"
        obs = self.extractor.extract(content)
        ips = _values(obs, EntityType.IP)
        assert "2001:db8::1" in ips


class TestCommandOutputEntityExtractor:
    def setup_method(self):
        self.extractor = CommandOutputEntityExtractor()

    def test_ps_style_pids(self):
        content = (
            "  PID USER      %CPU COMMAND\n"
            " 1234 www-data  45.2 /var/lib/app/server\n"
            " 5678 mysql      2.3 /var/lib/mysql/mysqld\n"
        )
        obs = self.extractor.extract(content)
        pids = _values(obs, EntityType.PID)
        assert pids == {"1234", "5678"}

    def test_netstat_ip_port_pairs(self):
        content = "tcp    0  0 10.0.0.5:5432        1.2.3.4:54321 ESTABLISHED\n"
        obs = self.extractor.extract(content)
        ips = _values(obs, EntityType.IP)
        ports = _values(obs, EntityType.PORT)
        assert ips == {"10.0.0.5", "1.2.3.4"}
        assert ports == {"5432", "54321"}

    def test_path_whitelist_excludes_binaries(self):
        content = "/usr/bin/true\n/var/log/app.log\n/etc/nginx/nginx.conf"
        obs = self.extractor.extract(content)
        paths = _values(obs, EntityType.PATH)
        # /usr/bin paths are intentionally excluded — they're noise.
        assert "/var/log/app.log" in paths
        assert "/etc/nginx/nginx.conf" in paths
        assert "/usr/bin/true" not in paths

    def test_empty_content_returns_empty(self):
        assert self.extractor.extract("") == []


class TestConfigEntityExtractor:
    def setup_method(self):
        self.extractor = ConfigEntityExtractor()

    def test_yaml_nested_keys_do_not_leak_across_lines(self):
        """Key/value separator is space+tab only — not newlines —
        so a nested YAML block can't be misread as ``host=port``."""
        content = (
            "server:\n"
            "  host: db-master.prod.internal\n"
            "  port: 5432\n"
            "path: /var/lib/postgres\n"
        )
        obs = self.extractor.extract(content)
        hosts = _values(obs, EntityType.HOSTNAME)
        ports = _values(obs, EntityType.PORT)
        paths = _values(obs, EntityType.PATH)
        assert "db-master.prod.internal" in hosts
        # Must NOT be "host" or "port" (the keyword leaking into the
        # value capture from the next line).
        assert "host" not in hosts
        assert "port" not in hosts
        assert "5432" in ports
        assert "/var/lib/postgres" in paths

    def test_toml_ini_style_equals(self):
        content = 'hostname="node-1"\nlisten=8080\nservice_name=web'
        obs = self.extractor.extract(content)
        assert "node-1" in _values(obs, EntityType.HOSTNAME)
        assert "8080" in _values(obs, EntityType.PORT)
        assert "web" in _values(obs, EntityType.SERVICE)

    def test_ipv4_surfaces_from_config(self):
        content = "upstream = 192.168.1.50\nbackup = 192.168.1.51"
        obs = self.extractor.extract(content)
        assert _values(obs, EntityType.IP) == {"192.168.1.50", "192.168.1.51"}

    def test_empty_content_returns_empty(self):
        assert self.extractor.extract("") == []


class TestTraceEntityExtractor:
    def setup_method(self):
        self.extractor = TraceEntityExtractor()

    def test_otlp_json_and_error_flag(self):
        content = (
            '{"service.name":"checkout","peer.service":"payment",'
            '"http.url":"https://api.internal/pay","error":true}'
        )
        obs = self.extractor.extract(content)
        services = _by_type(obs, EntityType.SERVICE)
        assert {s.entity_value for s in services} == {"checkout", "payment"}
        assert all(s.in_error_context for s in services)
        assert "/pay" in _values(obs, EntityType.PATH)

    def test_otlp_attribute_kv(self):
        content = "service.name=auth net.peer.name=db-01 host.name=node-1"
        obs = self.extractor.extract(content)
        assert "auth" in _values(obs, EntityType.SERVICE)
        assert "db-01" in _values(obs, EntityType.SERVICE)
        assert "node-1" in _values(obs, EntityType.HOSTNAME)

    def test_error_flag_from_status_code(self):
        content = 'span: service.name="auth" status.code: "ERROR"'
        obs = self.extractor.extract(content)
        services = _by_type(obs, EntityType.SERVICE)
        assert any(s.in_error_context for s in services)

    def test_empty_content_returns_empty(self):
        assert self.extractor.extract("") == []


class TestRegistryDispatch:
    """Confirms each data type routes to the correct implementation,
    and that types without a registered extractor return empty."""

    def test_logs_routes_to_logs_extractor(self):
        obs = extract_entities_for_data_type(
            DataType.LOGS_AND_ERRORS, "from 10.0.0.1 port 22"
        )
        assert "10.0.0.1" in _values(obs, EntityType.IP)

    def test_config_routes_to_config_extractor(self):
        obs = extract_entities_for_data_type(
            DataType.STRUCTURED_CONFIG, "host: node-1\nport: 8080"
        )
        assert "node-1" in _values(obs, EntityType.HOSTNAME)

    def test_trace_routes_to_trace_extractor(self):
        obs = extract_entities_for_data_type(
            DataType.TRACE_DATA, 'service.name="checkout"'
        )
        assert "checkout" in _values(obs, EntityType.SERVICE)

    def test_command_routes_to_command_extractor(self):
        obs = extract_entities_for_data_type(
            DataType.COMMAND_OUTPUT, " 1234 user cmd /var/log/foo"
        )
        assert "1234" in _values(obs, EntityType.PID)

    def test_unregistered_type_returns_empty(self):
        # METRICS, SOURCE_CODE, UNSTRUCTURED_TEXT, etc. all take the
        # "no extractor" path until we add coverage.
        for dt in (
            DataType.METRICS_AND_PERFORMANCE,
            DataType.SOURCE_CODE,
            DataType.UNSTRUCTURED_TEXT,
            DataType.VISUAL_EVIDENCE,
            DataType.UNANALYZABLE,
        ):
            assert extract_entities_for_data_type(dt, "anything") == []


# ============================================================
# Preprocessor integration — feature flag, cap, overflow marker
# ============================================================


@pytest.mark.asyncio
async def test_preprocessor_skips_entities_when_flag_off(monkeypatch):
    """With the flag OFF, extraction runs but no entities are surfaced
    on PreprocessingResult. The Phase 4c lookups see an empty registry,
    identical to pre-Phase-4 behaviour."""
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY", "false")
    # Re-read settings so the service picks up the env var.
    from faultmaven.config import settings as settings_module

    # Reset the module-level singleton so the new env var is picked up.
    settings_module._settings_instance = None

    from faultmaven.modules.preprocessing.classifier import DataClassifier
    from faultmaven.modules.preprocessing.extractors.logs_extractor import (
        LogsAndErrorsExtractor,
    )
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    svc = PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=LogsAndErrorsExtractor(),
    )
    content = "ERROR Failed password for root from 192.168.1.1 port 22"
    result = await svc.classify_and_extract(content=content, filename="auth.log")
    assert result.entities == []
    assert result.entity_overflow_types == []


@pytest.mark.asyncio
async def test_preprocessor_emits_entities_when_flag_on(monkeypatch):
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY", "true")
    from faultmaven.config import settings as settings_module

    # Reset the module-level singleton so the new env var is picked up.
    settings_module._settings_instance = None

    from faultmaven.modules.preprocessing.classifier import DataClassifier
    from faultmaven.modules.preprocessing.extractors.logs_extractor import (
        LogsAndErrorsExtractor,
    )
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    svc = PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=LogsAndErrorsExtractor(),
    )
    content = "ERROR Failed password for root from 192.168.1.1 port 22"
    result = await svc.classify_and_extract(content=content, filename="auth.log")
    # At minimum we expect the IP + port to surface as entities.
    values_by_type = {(e["entity_type"], e["entity_value"]) for e in result.entities}
    assert ("ip", "192.168.1.1") in values_by_type
    assert ("port", "22") in values_by_type
    assert result.entity_overflow_types == []


@pytest.mark.asyncio
async def test_preprocessor_caps_per_type_and_records_overflow(monkeypatch):
    """The per-(evidence, type) cap is bound to
    ``entity_registry_cap_per_type``. Anything over the cap must be
    trimmed — retaining the highest-mention entries — and the type
    must appear in ``entity_overflow_types`` and in
    ``metadata.evidence_metadata.entities.overflow_types``."""
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY", "true")
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY_CAP", "5")
    from faultmaven.config import settings as settings_module

    # Reset the module-level singleton so the new env var is picked up.
    settings_module._settings_instance = None

    from faultmaven.modules.preprocessing.classifier import DataClassifier
    from faultmaven.modules.preprocessing.extractors.logs_extractor import (
        LogsAndErrorsExtractor,
    )
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    # Generate >5 distinct IPs so the cap trips. Shape the lines like
    # real syslog so the classifier routes to LOGS_AND_ERRORS rather
    # than the classification-failed placeholder path.
    lines = [
        f"2024-01-01 12:00:{i:02d} sshd[1234]: ERROR Failed password "
        f"for root from 10.0.0.{i} port 22"
        for i in range(20)
    ]
    content = "\n".join(lines)

    svc = PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=LogsAndErrorsExtractor(),
    )
    result = await svc.classify_and_extract(content=content, filename="auth.log")

    ip_entries = [e for e in result.entities if e["entity_type"] == "ip"]
    assert len(ip_entries) == 5  # capped
    assert "ip" in result.entity_overflow_types
    evidence_meta = result.extraction_metadata.get("evidence_metadata", {})
    overflow = evidence_meta.get("entities", {}).get("overflow_types")
    assert overflow == ["ip"]
