"""Phase 4b — per-data-type ``EntityExtractor`` coverage.

Tests the four concrete extractors individually (logs, command output,
config, trace) plus the registry dispatcher. Each test focuses on
content shapes the extractor must handle and, crucially, shapes it
must NOT misinterpret — false-positive suppression is more important
than recall for a case-level index.
"""

from __future__ import annotations

import re

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


@pytest.mark.asyncio
async def test_preprocessor_increments_overflow_counter_on_cap_trip(monkeypatch):
    """Phase 4 hygiene — ``faultmaven_case_entities_overflow_total`` must
    increment once per (evidence, entity_type) overflow event. The
    counter is labeled by entity_type; we read the ``ip`` sample before
    and after to assert a single-step increment even though the
    underlying cap truncated 15 rows (20 distinct IPs → 5 retained)."""
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY", "true")
    monkeypatch.setenv("FAULTMAVEN_ENTITY_REGISTRY_CAP", "5")
    from faultmaven.config import settings as settings_module

    settings_module._settings_instance = None

    from faultmaven.infrastructure.observability.evidence_metrics import (
        CASE_ENTITIES_OVERFLOW_TOTAL,
        PROMETHEUS_AVAILABLE,
    )
    from faultmaven.modules.preprocessing.classifier import DataClassifier
    from faultmaven.modules.preprocessing.extractors.logs_extractor import (
        LogsAndErrorsExtractor,
    )
    from faultmaven.modules.preprocessing.preprocessing_service import (
        PreprocessingService,
    )

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed; counter is a no-op")

    def _ip_sample() -> float:
        # prometheus_client ``Counter.labels(...)._value.get()`` exposes
        # the raw float — the public API for introspection.
        return CASE_ENTITIES_OVERFLOW_TOTAL.labels(entity_type="ip")._value.get()

    before = _ip_sample()

    lines = [
        f"2024-01-01 12:00:{i:02d} sshd[1234]: ERROR Failed password "
        f"for root from 10.0.0.{i} port 22"
        for i in range(20)
    ]
    svc = PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=LogsAndErrorsExtractor(),
    )
    await svc.classify_and_extract(content="\n".join(lines), filename="auth.log")

    after = _ip_sample()
    assert after == pytest.approx(before + 1.0), (
        f"Expected ip overflow counter to step by exactly 1; "
        f"before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# fm#1600 — config.py's "``[ \t]*`` cannot cross a line ending" invariant.
#
# ``ConfigEntityExtractor`` matches at DOCUMENT scope: there is no line split
# in it at all, and what makes that safe is that its key/value separators
# admit space and tab only. Widen one back to ``\s*`` and the value regex
# eats across the newline into the next key, so ``host:\n  port: 5432``
# reports the hostname ``port``.
#
# Before this block the invariant was exercised for ONE of the four
# separator-bearing regexes (``_HOST_RE``) and ONE of the three line endings
# (LF). Measured on the merge base by widening each regex to ``\s*`` in turn
# and running ``tests/unit/modules/preprocessing/``:
#
#   _HOST_RE     -> 1 failed  (test_yaml_nested_keys_do_not_leak_across_lines)
#   _PORT_RE     -> 805 passed, 11 skipped   (unguarded)
#   _SERVICE_RE  -> 805 passed, 11 skipped   (unguarded)
#   _PATH_RE     -> 805 passed, 11 skipped   (unguarded)
# ---------------------------------------------------------------------------

_LINE_ENDINGS = (
    pytest.param("\n", id="LF"),
    pytest.param("\r\n", id="CRLF"),
    pytest.param("\r", id="CR"),
)

#: One leak fixture per separator-bearing regex in ``config.py``.
#:
#: ``lines`` is a key with nothing after it, then a line whose text the
#: widened separator would swallow as that key's value, then a correctly
#: bound pair. ``leaked`` must never be extracted; ``bound`` always must —
#: it is the positive control, without which an extractor that returned
#: nothing at all would read as a pass.
_CONFIG_SEPARATOR_LEAKS = (
    pytest.param(
        EntityType.HOSTNAME,
        ("host:", "  port: 5432", "hostname: real-host.internal"),
        "port",
        "real-host.internal",
        id="_HOST_RE",
    ),
    pytest.param(
        EntityType.PORT,
        ("port:", "  5432", "listen: 8080"),
        "5432",
        "8080",
        id="_PORT_RE",
    ),
    pytest.param(
        EntityType.SERVICE,
        ("service:", "  name: api-gateway", "program: real-service"),
        "name",
        "real-service",
        id="_SERVICE_RE",
    ),
    pytest.param(
        EntityType.PATH,
        ("path:", "  /var/lib/leaked", "data_dir: /srv/real"),
        "/var/lib/leaked",
        "/srv/real",
        id="_PATH_RE",
    ),
)


@pytest.mark.unit
class TestConfigSeparatorCannotCrossALineEnding:
    """fm#1600 — the invariant config.py's document-scope matching rests on."""

    @pytest.mark.parametrize("ending", _LINE_ENDINGS)
    @pytest.mark.parametrize("entity_type,lines,leaked,bound", _CONFIG_SEPARATOR_LEAKS)
    def test_a_key_cannot_bind_to_the_next_line(
        self,
        entity_type: EntityType,
        lines: tuple[str, ...],
        leaked: str,
        bound: str,
        ending: str,
    ) -> None:
        content = ending.join(lines) + ending
        values = _values(ConfigEntityExtractor().extract(content), entity_type)
        assert bound in values, (
            f"positive control failed: {bound!r} is bound on its own line and "
            f"must extract — got {values!r}"
        )
        assert leaked not in values, (
            f"{leaked!r} sits on the line AFTER its key, so a separator that "
            f"stops at space and tab cannot reach it. Got {values!r} — a "
            f"separator has been widened to \\s* (fm#1600)."
        )

    def test_no_config_pattern_admits_a_line_ending(self) -> None:
        """States the rule where it can be violated, not only where it was.

        The cases above name four regexes because those are the four that
        carry a key/value separator today. A fifth added tomorrow with
        ``\\s*`` would leak with no case to catch it, and ``_IPV4_RE`` — the
        one existing pattern with no separator — has no leak fixture at all.
        So the rule is also stated over every compiled pattern in the
        module: none may contain a construct that matches CR or LF.
        """
        offenders = {
            name: pattern.pattern
            for name, pattern in _config_patterns().items()
            if _admits_a_line_ending(pattern)
        }
        assert offenders == {}, (
            "config.py matches at document scope, so a pattern that can "
            f"match across a line ending can bind a key to the next line: {offenders}"
        )

    def test_the_pattern_scan_would_see_a_widened_separator(self) -> None:
        """Positive control for the scan above.

        A scan that recognises nothing reports no offenders and looks like a
        clean bill of health, so it is shown finding the exact regression
        fm#1600 is about.
        """
        widened = re.compile(
            r"\b(?:host)\b\s*[:=]\s*([A-Za-z][\w.\-]{1,253})", re.IGNORECASE
        )
        assert _admits_a_line_ending(widened)
        # A bare ``.`` matches CR with no DOTALL, and CR alone is a line
        # ending here — so a value class loosened to ``.`` leaks too.
        assert _admits_a_line_ending(re.compile(r"\bhost\b[ \t]*[:=][ \t]*(.+)"))
        assert not _admits_a_line_ending(
            re.compile(r"\b(?:host)\b[ \t]*[:=][ \t]*([A-Za-z][\w.\-]{1,253})")
        )

    @pytest.mark.parametrize(
        "value_class,admits",
        [
            # Excluding one spelling of a line ending leaves the other: both
            # of these capture across a bare CR (or LF) into the next key.
            pytest.param(r"[^\n]+", True, id="negated-LF-only"),
            pytest.param(r"[^\r]+", True, id="negated-CR-only"),
            pytest.param(r"[^,]+", True, id="negated-neither"),
            pytest.param(r"[^\S]+", True, id="negated-non-space"),
            # Excluding both is the only safe negated class.
            pytest.param(r"[^\r\n]+", False, id="negated-CR-and-LF"),
            pytest.param(r"[^\s]+", False, id="negated-whitespace"),
            pytest.param(r"[^\s,]+", False, id="negated-whitespace-and-comma"),
        ],
    )
    def test_the_scan_judges_a_negated_class_per_line_ending(
        self, value_class: str, admits: bool
    ) -> None:
        """A negated class is safe only when it excludes CR AND LF.

        ``[^\\n]+`` excludes the newline and therefore reads as safe to a
        check that asks "does it mention a line ending", yet it captures
        ``'\\rport: 1'`` from ``'host:\\rport: 1'`` — the fm#1600 leak on a
        bare-CR file, passed by the guard written to catch it. Each verdict
        is checked against what the pattern actually captures, so the scan
        cannot be right on paper and wrong on bytes.
        """
        pattern = re.compile(r"\bhost\b[ \t]*[:=]" + value_class)
        assert _admits_a_line_ending(pattern) is admits, value_class

        # The scan's claim is "this pattern can match a line ending", so that
        # is what is checked on bytes — whether the match SPANS a CR or LF,
        # not whether it happens to reach the next key (``[^\\S]+`` eats the
        # CR and stops, which is still a match the scan must report).
        crosses = any(
            any(ch in m.group(0) for ch in "\r\n")
            for m in (pattern.search(f"host:{ending}port: 1") for ending in "\r\n")
            if m
        )
        assert crosses is admits, (
            f"{value_class}: the scan says admits={admits}, but a match "
            f"spanning a line ending {'did' if crosses else 'did not'} happen"
        )


def _config_patterns() -> dict[str, re.Pattern[str]]:
    """Every module-level compiled pattern in ``entities/config.py``."""
    from faultmaven.modules.preprocessing.entities import config as _config

    found = {
        name: value
        for name, value in vars(_config).items()
        if isinstance(value, re.Pattern)
    }
    assert found, "no compiled patterns found — the scan is looking in the wrong place"
    return found


def _admits_a_line_ending(pattern: re.Pattern[str]) -> bool:
    """True when ``pattern`` contains a construct that can match CR or LF.

    A source scan rather than a probe, because "can this regex match a
    newline anywhere" is not answerable by sampling inputs. It walks the
    pattern so an escaped literal is never confused with the class that
    spells it: ``\\.`` is a dot, ``\\s`` is whitespace, ``[.\\-]`` is two
    literals.

    An unescaped ``.`` counts whether or not ``DOTALL`` is set: without it
    ``.`` still matches CR, and CR alone is a line ending here.
    """
    source = pattern.pattern
    i = 0
    while i < len(source):
        char = source[i]
        if char == "\\":
            escaped = source[i + 1 : i + 2]
            # \s, \W and \D all match CR and LF; \S, \w and \d do not.
            if escaped in {"s", "W", "D", "n", "r"}:
                return True
            i += 2
            continue
        if char in "\r\n":
            return True
        if char == ".":
            return True
        if char == "[":
            end = i + 1
            if source[end : end + 1] == "^":
                end += 1
            if source[end : end + 1] == "]":  # a literal ] as the first member
                end += 1
            while end < len(source) and source[end] != "]":
                end += 2 if source[end] == "\\" else 1
            body = source[i + 1 : end]
            # A line ending is spelled three ways and CR alone is one of them,
            # so the question is per character: does this class match CR,
            # and does it match LF? ``\\s`` covers both; ``\\n``/``\\r`` (or
            # the raw characters, from a non-raw pattern string) cover one.
            spells_lf = any(t in body for t in ("\\s", "\\n", "\n"))
            spells_cr = any(t in body for t in ("\\s", "\\r", "\r"))
            if body.startswith("^"):
                # A negated class admits every character it does not name, so
                # it is safe only if it names BOTH: ``[^\\n]+`` still eats a
                # bare CR and binds a key to the next line of a CR-only file.
                if not (spells_lf and spells_cr):
                    return True
            elif spells_lf or spells_cr or "\\W" in body or "\\D" in body:
                return True
            i = end + 1
            continue
        i += 1
    return False


# ---------------------------------------------------------------------------
# fm#1601 — a bare-CR fixture for the per-line entity extractors.
#
# The three per-line extractors count the LINES a value appears on, so the
# count is only as right as what the code calls a line. ``content.split("\n")``
# reads a bare-``\r`` file — classic-Mac endings, and what some Windows
# exporters still emit — as ONE line and floors every count at 1. fm#1574
# introduced ``split_log_lines`` for that and fm#1597 routed these three
# through it, and nothing pinned it here: measured on the merge base,
# reverting ``trace.py`` and ``command_output.py`` to the pre-fm#1597
# per-module ``content.split("\n")`` left ``tests/unit/modules/preprocessing/``
# at 805 passed / 11 skipped — identical to an unmutated run.
#
# The one existing bare-CR fixture is ``test_log_usernames.py``'s
# ``test_a_line_is_a_line_under_every_line_ending``, and it reaches only
# ``logs.py``'s USERNAME rule. It catches a revert of the SHARED site in
# ``line_tally`` — which is where the split lives today — and nothing at all
# if a module re-acquires a split of its own, which is the shape fm#1597
# removed and so the shape most likely to come back.
# ---------------------------------------------------------------------------

#: One record shape per per-line extractor, plus the entity whose count is
#: asserted. ``{i}`` varies the parts that must NOT be de-duplicated so the
#: fixture cannot pass by accident on a single repeated line.
_PER_LINE_EXTRACTORS = (
    pytest.param(
        LogsEntityExtractor,
        "2024-01-01 12:00:0{i} ERROR Failed password for alice "
        "from 10.0.0.7 port 22",
        EntityType.IP,
        "10.0.0.7",
        id="logs",
    ),
    pytest.param(
        CommandOutputEntityExtractor,
        "tcp  0  {i}  10.0.0.7:5432  ESTABLISHED  /var/log/app-{i}.log",
        EntityType.IP,
        "10.0.0.7",
        id="command_output",
    ),
    pytest.param(
        TraceEntityExtractor,
        '{{"span":{i},"service.name":"checkout","http.url":"/v1/pay",'
        '"peer.ip":"10.0.0.7"}}',
        EntityType.SERVICE,
        "checkout",
        id="trace",
    ),
)

_RECORD_COUNT = 5


def _counts(obs: list[EntityObservation]) -> dict[tuple[EntityType, str], int]:
    return {(o.entity_type, o.entity_value): o.mention_count for o in obs}


@pytest.mark.unit
@pytest.mark.parametrize("extractor_cls,record,entity_type,value", _PER_LINE_EXTRACTORS)
@pytest.mark.parametrize("ending", _LINE_ENDINGS)
def test_a_line_is_a_line_under_every_line_ending(
    extractor_cls,
    record: str,
    entity_type: EntityType,
    value: str,
    ending: str,
) -> None:
    """fm#1601 — the same bytes under LF, CRLF and CR must count the same.

    Asserted over the WHOLE observation set, not just one value: a splitter
    that loses a line ending collapses every per-line count in the file, and
    a guard on one entity type would pass a partial regression.

    The named entity's count is asserted explicitly as the positive control.
    It is what tells CR-collapse (every count floors at 1) apart from an
    extractor that matched nothing, which would make "the three endings
    agree" vacuously true.
    """
    records = [record.format(i=i) for i in range(_RECORD_COUNT)]
    reference = _counts(extractor_cls().extract("\n".join(records) + "\n"))
    assert reference[(entity_type, value)] == _RECORD_COUNT, (
        f"positive control failed under LF: {value!r} appears on "
        f"{_RECORD_COUNT} lines — got {reference!r}"
    )

    under_test = _counts(extractor_cls().extract(ending.join(records) + ending))
    assert under_test == reference, (
        f"{ending!r} changed the per-line counts: {under_test!r} != "
        f"{reference!r}. The extractor is splitting on something narrower "
        f"than CRLF/CR/LF (fm#1601)."
    )


@pytest.mark.unit
@pytest.mark.parametrize("extractor_cls,record,entity_type,value", _PER_LINE_EXTRACTORS)
def test_an_in_line_control_character_is_not_a_line_break(
    extractor_cls,
    record: str,
    entity_type: EntityType,
    value: str,
) -> None:
    """``str.splitlines()`` is the near-miss the three endings cannot see.

    It agrees with ``split_log_lines`` on CRLF, CR and LF, so the cases
    above pass under it — measured: swapping the shared split for
    ``content.splitlines()`` left the whole directory green. It disagrees on
    ``\\x0b \\x0c \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029``, none of which
    ends a line in any log format, and ``extractors/utils`` rejects it for
    exactly that reason — a form feed inside a Windows CBS line would be
    reported as two lines the file does not have.

    So each record is doubled around a form feed. That is still ONE line and
    its values are per-line distinct, so the counts must not move; a
    splitter that breaks on ``\\x0c`` doubles every one of them.
    """
    records = [record.format(i=i) for i in range(_RECORD_COUNT)]
    reference = _counts(extractor_cls().extract("\n".join(records) + "\n"))
    assert reference[(entity_type, value)] == _RECORD_COUNT, reference

    folded = "\n".join(f"{line}\x0c{line}" for line in records) + "\n"
    assert _counts(extractor_cls().extract(folded)) == reference, (
        "a form feed was treated as a line break — str.splitlines() or "
        "another over-splitter has replaced split_log_lines (fm#1601)"
    )
