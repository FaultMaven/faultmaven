"""EntityExtractor for ``DataType.COMMAND_OUTPUT``.

Command output (top, ps, netstat, iostat, etc.) leans heavily on PIDs,
ports, IPs, and path-like tokens. This extractor captures them with
shapes tight enough to avoid the common false positives:

- PIDs must sit in a column with integer context (leading whitespace +
  trailing whitespace) — rejecting ``:`` and bracketed noise found in
  syslog fragments embedded in command output.
- IPs use the same IPv4 shape as the logs extractor.
- Ports only count when paired with an IP (netstat/ss output) or with
  a ``port`` keyword.
- Paths are absolute-Unix ``/...`` tokens; this is what ``ls``, ``ps``
  argv columns, and ``lsof`` paths look like.

No severity discrimination — command output rarely has stable "error
line" semantics; flags like ``Z`` (zombie) in ps belong to the
extractor's structural index, not to the registry.
"""

from __future__ import annotations

import re
from collections import Counter

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# A bare column-style PID: at least one whitespace before and after.
# Requires 1-7 digits; caps range check at the PID_MAX below.
_PID_COLUMN_RE = re.compile(r"(?:^|\s)(\d{1,7})(?=\s)")
_PID_MAX = 4_194_304
_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)
_IP_PORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})\b")
_ABS_PATH_RE = re.compile(r"(?<![/\w])(/(?:[a-zA-Z0-9_\-.]+/){0,10}[a-zA-Z0-9_\-.]+)")


class CommandOutputEntityExtractor:
    """Extractor for COMMAND_OUTPUT content."""

    @property
    def data_type_name(self) -> str:
        return "command_output"

    def extract(
        self,
        content: str,
        error_line_indices: set[int] | None = None,
    ) -> list[EntityObservation]:
        if not content:
            return []

        ip_total: Counter = Counter()
        pid_total: Counter = Counter()
        port_total: Counter = Counter()
        path_total: Counter = Counter()

        for line in content.split("\n"):
            for ip in _IPV4_RE.findall(line):
                ip_total[ip] += 1
            for pid_str in _PID_COLUMN_RE.findall(line):
                if pid_str.isdigit() and 0 < int(pid_str) <= _PID_MAX:
                    # Drop obvious non-PIDs: 0 and oversized values.
                    # Keep small numbers — kthreads live in low PIDs.
                    pid_total[pid_str] += 1
            for port in _PORT_KEYWORD_RE.findall(line) + _IP_PORT_RE.findall(line):
                if port.isdigit() and 0 < int(port) <= 65535:
                    port_total[port] += 1
            for path in _ABS_PATH_RE.findall(line):
                # Exclude /usr/bin-style binary paths from high-noise
                # headers — they inflate the registry without adding
                # investigative value. Keep /var, /etc, /tmp, /home,
                # /opt paths which are the usual suspects for state.
                if path.startswith(("/var", "/etc", "/tmp", "/home", "/opt", "/data")):
                    path_total[path] += 1

        observations: list[EntityObservation] = []
        for value, count in ip_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.IP,
                    entity_value=value,
                    mention_count=count,
                )
            )
        for value, count in pid_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PID,
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
        for value, count in path_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PATH,
                    entity_value=value,
                    mention_count=count,
                )
            )
        return observations
