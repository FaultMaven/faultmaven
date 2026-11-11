"""The shapes the host-and-port rule no longer reads as a port (fm#2677).

Negatives first: an ISO-8601 date-time's hour, a dotted version tag and
non-ASCII digits, through the pattern, the entity registry and the logs
profile. Then what must not change (the earlier reading), the readings a
resumed scan now finds, and the false ports that remain for the owner's
ruling: every rule found for them also reads a real host or drops a real port.
"""

from __future__ import annotations

import re

import pytest

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.command_output import (
    CommandOutputEntityExtractor,
)
from faultmaven.modules.preprocessing.entities.config import ConfigEntityExtractor
from faultmaven.modules.preprocessing.entities.logs import LogsEntityExtractor
from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)
from faultmaven.utils.host_port import HOST_PORT_RE

#: The pattern before this change (``main`` at 002adf000).
PRE_2677_HOST_PORT_RE = re.compile(
    r"(?<![\w.-])(?=[\w.-]*?[A-Za-z.])[\w.-]++:(\d{1,5})\b"
)

#: Each date lookbehind has a line only it rejects: extended, ordinal, compact.
DATE_TIMES = ["2024-01-15T06:55 x", "2024-015T06:55 x", "20240115T06:55 x"]
DATE_TIMES += ["2024-01-15T06:55:45Z ERROR", '{"time":"2026-11-09T01:07:33Z"}']
DATE_TIMES += ["2024-01-15T06:55+02:00", "20240115T06:55:45"]
DATE_TIMES += ["2024-01-15t06:55:45.123z"]
VERSION_TAGS = ["image nginx:1.25 pulled", "python:3.11-slim"]
VERSION_TAGS += ["registry.k8s.io/pause:3.9"]
NOT_PORTS = DATE_TIMES + VERSION_TAGS
NON_ASCII = ["db.local:５４３２", "db.local:٥٤٣٢", "port ٣٣", "pid=٣"]

UNCHANGED = ["db.local:5432", "10.0.0.5:22", "api.example.com:443/v1"]
UNCHANGED += ["http://host:8080/path", "127.0.0.1:5432->127.0.0.1:51234"]
UNCHANGED += ["redis:6379 refused", "db.local:5432.", "db:5432.Connection reset"]
UNCHANGED += ["dial tcp db:5432: connect: refused", "x.y-z:65535 b:1"]
UNCHANGED += ["host=web-1:8080;", "[db.local:5432]", "T1:80", "web-01:80"]
UNCHANGED += ["kafka-0.kafka:9092,kafka-1.kafka:9092"]
UNCHANGED += ["web1t01:8080", "rack3t05:22", "web-7d9f8b6c5d-x4t56:8080"]
UNCHANGED += ["0.0.0.0:8000-8010->8000-8010/tcp", "listen web:8080-8090"]
UNCHANGED += ["lb:443-tls", "api.allegro.pl:443", "docs.rs:443", "get.helm.sh:443"]
UNCHANGED += ["api.go:8080", "docker run -d -p 127.0.0.1:5432:5432 postgres:16"]
UNCHANGED += ['"127.0.0.1:5432:5432"', "server.1=zk1:2888:3888"]
UNCHANGED += ["ssh -L 127.0.0.1:15432:10.0.0.5:5432", "-p 127.0.0.1:8080:80"]
UNCHANGED += ["dial tcp backup-28374650-24t56:8080: refused", "srv1234t01:22"]
UNCHANGED += ["node-12-34t56:8080", "db.2024t01:5432", "http://web/12/34t56:8080"]

#: A rejected reading no longer hides the real port behind it.
NEW_READINGS = {"peer=ipv4:10.0.0.5:51234": ["51234"], "tcp:10.0.0.5:22": ["22"]}
NEW_READINGS |= {"nginx:1.25.internal:80": ["80"]}
#: Documented false ports, pinned so the owner's ruling changes a test.
REMAINING = {"ERROR app.py:118": ["118"], "postgres:16-alpine": ["16"]}
REMAINING |= {"status:200": ["200"], "redis:7": ["7"]}
REMAINING |= {"at /app/index.js:10:5": ["10"], "src/lib.rs:88:9": ["88"]}
REMAINING |= {"config.yaml:12:3": ["12"], "00:0c:29:ab:cd:01": ["29", "01"]}
REMAINING |= {"(node:internal/modules/cjs/loader:1105:14)": ["1105"]}
REMAINING |= {"T06:55:45Z": ["55"], "2024.01.15-06:55:45": ["55"]}
REMAINING |= {"2024/01/15T06:55:45": ["55"], "2024_01_15T06:55": ["55"]}
REMAINING |= {"ts=2024-01-15T06:55:45.123-03:30": ["30"]}


def _registry(line: str, entity_type: EntityType) -> list[str]:
    return [
        o.entity_value
        for o in LogsEntityExtractor().extract(line)
        if o.entity_type == entity_type
    ]


def _profile(line: str, heading: str) -> list[str]:
    _, profile = LogsAndErrorsExtractor()._build_entity_profile(line, top_n=20)
    rows = re.search(rf"^  {heading}: \d+\n((?:    \S+: \d+ lines\n?)*)", profile, re.M)
    return re.findall(r"(\S+): \d+ lines", rows[1]) if rows else []


def _readings(line: str) -> list[list[str]]:
    """The pattern's, the registry's and the profile's ports, in that order."""
    ports = (_registry(line, EntityType.PORT), _profile(line, "Distinct Ports"))
    return [HOST_PORT_RE.findall(line), *(sorted(p) for p in ports)]


@pytest.mark.unit
class TestNotAPort:
    @pytest.mark.parametrize("line", NOT_PORTS)
    def test_no_path_reads_a_port(self, line):
        assert PRE_2677_HOST_PORT_RE.findall(
            line
        ), "the pattern before fm#2677 read a port here"
        assert _readings(line) == [[], [], []]

    @pytest.mark.parametrize("line", NON_ASCII)
    def test_non_ascii_digits_are_no_port_or_pid(self, line):
        assert HOST_PORT_RE.findall(line) == []
        for entity_type in (EntityType.PORT, EntityType.PID):
            assert _registry(line, entity_type) == []
        assert _profile(line, "Distinct Ports") == []
        assert _profile(line, "Distinct PIDs") == []

    def test_the_ascii_controls_still_read(self):
        assert _registry("port 33 pid=3", EntityType.PORT) == ["33"]
        assert _registry("port 33 pid=3", EntityType.PID) == ["3"]
        assert _profile("port 33 pid=3", "Distinct PIDs") == ["3"]

    def test_command_output_and_config_refuse_non_ascii(self):
        def ports(extractor, content):
            return [
                o.entity_value
                for o in extractor.extract(content)
                if o.entity_type == EntityType.PORT
            ]

        command = CommandOutputEntityExtractor()
        assert ports(command, "tcp 10.0.0.5:٥٤٣٢ LISTEN\nport ٣٣") == []
        assert ports(command, "tcp 10.0.0.5:5432 LISTEN") == ["5432"]
        assert ports(ConfigEntityExtractor(), "port: ٥٤٣٢") == []
        assert ports(ConfigEntityExtractor(), "port: 5432") == ["5432"]


@pytest.mark.unit
class TestWhatStillReads:
    @pytest.mark.parametrize("line", UNCHANGED)
    def test_the_earlier_reading_is_kept(self, line):
        before = PRE_2677_HOST_PORT_RE.findall(line)
        assert before
        assert _readings(line) == [before, sorted(set(before)), sorted(set(before))]

    @pytest.mark.parametrize("line, ports", NEW_READINGS.items())
    def test_a_resumed_scan_finds_the_real_port(self, line, ports):
        assert PRE_2677_HOST_PORT_RE.findall(line) != ports
        assert _readings(line) == [ports, ports, ports]

    @pytest.mark.parametrize("line, ports", REMAINING.items())
    def test_the_remaining_false_ports(self, line, ports):
        assert _readings(line) == [ports, sorted(ports), sorted(ports)]
