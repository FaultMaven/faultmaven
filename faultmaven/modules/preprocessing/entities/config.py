"""EntityExtractor for ``DataType.STRUCTURED_CONFIG``.

Configs carry hostnames, listen ports, service names, and absolute
paths. They rarely carry PIDs or IPs in error context (no "error
context" at all — configs are declarative). This extractor focuses on
the entities that answer "what is this config pointing at?"

Recognition is key/value based rather than line-based. Common keys:
``host=``, ``hostname:``, ``server=``, ``listen``, ``port``,
``bind_address``, ``path=``, ``data_dir:``. YAML and TOML both follow
the same ``key: value`` shape after ``find`` normalisation, so a
single regex set handles both.
"""

from __future__ import annotations

import re
from collections import Counter

from faultmaven.modules.case.domain.models import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

# Separator regexes use ``[ \t]*`` instead of ``\s*`` on purpose: a
# key/value pair like ``host:\n  port: 5432`` must *not* be interpreted
# as host=port — which is what ``\s*`` allowed, because the value
# regex would then greedily eat across the newline into the next key.
# Restricting to space+tab keeps each key bound to the value on its
# own line (or the same JSON object).
_HOST_RE = re.compile(
    r"\b(?:hostname|host|server|bind_host|bind_address|target_host|remote_host)"
    r"\b[ \t]*[:=][ \t]*[\"']?"
    r"([A-Za-z][\w.\-]{1,253})"
    r"[\"']?",
    re.IGNORECASE,
)
_PORT_RE = re.compile(
    r"\b(?:port|listen|bind_port|target_port)\b[ \t]*[:=][ \t]*[\"']?(\d{1,5})[\"']?",
    re.IGNORECASE,
)
_SERVICE_RE = re.compile(
    r"\b(?:service_name|service|program|application|app_name|daemon)"
    r"\b[ \t]*[:=][ \t]*[\"']?([A-Za-z][\w\-]{1,63})[\"']?",
    re.IGNORECASE,
)
_PATH_RE = re.compile(
    r"\b(?:path|dir|directory|data_dir|log_dir|config_path|pid_file|socket)"
    r"\b[ \t]*[:=][ \t]*[\"']?"
    r"(/(?:[A-Za-z0-9_\-.]+/){0,10}[A-Za-z0-9_\-.]+)"
    r"[\"']?",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class ConfigEntityExtractor:
    """Extractor for STRUCTURED_CONFIG content."""

    @property
    def data_type_name(self) -> str:
        return "structured_config"

    def extract(
        self,
        content: str,
        error_line_indices: set[int] | None = None,
    ) -> list[EntityObservation]:
        if not content:
            return []

        host_total: Counter = Counter()
        port_total: Counter = Counter()
        service_total: Counter = Counter()
        path_total: Counter = Counter()
        ip_total: Counter = Counter()

        for match in _HOST_RE.findall(content):
            host_total[match] += 1
        for match in _PORT_RE.findall(content):
            if match.isdigit() and 0 < int(match) <= 65535:
                port_total[match] += 1
        for match in _SERVICE_RE.findall(content):
            service_total[match] += 1
        for match in _PATH_RE.findall(content):
            path_total[match] += 1
        for ip in _IPV4_RE.findall(content):
            ip_total[ip] += 1

        observations: list[EntityObservation] = []
        for value, count in host_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.HOSTNAME,
                    entity_value=value,
                    mention_count=count,
                )
            )
        for value, count in port_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PORT,
                    entity_value=value,
                    mention_count=count,
                )
            )
        for value, count in service_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.SERVICE,
                    entity_value=value,
                    mention_count=count,
                )
            )
        for value, count in path_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PATH,
                    entity_value=value,
                    mention_count=count,
                )
            )
        for value, count in ip_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.IP,
                    entity_value=value,
                    mention_count=count,
                )
            )
        return observations
