"""EntityExtractor for ``DataType.LOGS_AND_ERRORS``.

Reuses the regex shapes from
``LogsAndErrorsExtractor._build_entity_profile`` — the logs extractor
has always scanned these entities for the structural index, but the
counts were string-formatted into prose. This extractor emits them as
structured ``EntityObservation`` rows so the Phase 4 registry can
index them.

No LLM call. Purely regex-driven; same severity discrimination that
the logs extractor uses to separate "IP showed up in an error line"
from "IP showed up in ambient traffic".
"""

from __future__ import annotations

import re
from collections import Counter

from faultmaven.modules.case.domain.models import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

# Regexes mirror ``logs_extractor.py``. Kept local so this module can
# evolve independently if the logs extractor's formatting changes
# (e.g. if it dropped the entity profile). The cost is a second compile
# — negligible.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Private-network detection is noisy in practice; we index every IP we
# see and let the agent/context-builder decide relevance.
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:.])"
    r"("
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
    r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
    r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}"
    r")"
    r"(?![0-9A-Fa-f:.])"
)
_USER_RE = re.compile(
    r"(?:\buser[= ]+|\bfor (?:invalid user )?\b|\buser=)"
    r"([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
    re.IGNORECASE,
)
_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"(?<![\w.-])[\w-]*[A-Za-z.][\w.-]*:(\d{1,5})\b")
_PID_KEYWORD_RE = re.compile(r"\bpid[= ]+(\d{1,7})\b", re.IGNORECASE)
_PID_BRACKET_RE = re.compile(r"\[(\d{1,7})\]")
_PID_MAX = 4_194_304
_HTTP_PATH_RE = re.compile(r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[^\s\?]*)\b")


class LogsEntityExtractor:
    """Extractor for LOGS_AND_ERRORS content."""

    @property
    def data_type_name(self) -> str:
        return "logs_and_errors"

    def extract(
        self,
        content: str,
        error_line_indices: set[int] | None = None,
    ) -> list[EntityObservation]:
        if not content:
            return []
        error_lines = error_line_indices or set()
        lines = content.split("\n")

        # Total mention counts, plus a parallel tally restricted to
        # lines the logs extractor flagged as errors. Merging them
        # later preserves "appeared in an error" signal as a boolean
        # per unique value.
        ip_total: Counter = Counter()
        ip_error: Counter = Counter()
        user_total: Counter = Counter()
        user_error: Counter = Counter()
        port_total: Counter = Counter()
        pid_total: Counter = Counter()
        path_total: Counter = Counter()

        for i, line in enumerate(lines):
            is_err = i in error_lines

            for ip in _IPV4_RE.findall(line):
                ip_total[ip] += 1
                if is_err:
                    ip_error[ip] += 1
            for ip in _IPV6_RE.findall(line):
                ip_total[ip] += 1
                if is_err:
                    ip_error[ip] += 1

            for user in _USER_RE.findall(line):
                if not user:
                    continue
                user_total[user] += 1
                if is_err:
                    user_error[user] += 1

            for port_str in _PORT_KEYWORD_RE.findall(line) + _HOST_PORT_RE.findall(
                line
            ):
                if port_str.isdigit() and 0 < int(port_str) <= 65535:
                    port_total[port_str] += 1

            for pid_str in _PID_KEYWORD_RE.findall(line) + _PID_BRACKET_RE.findall(
                line
            ):
                if pid_str.isdigit() and 0 < int(pid_str) <= _PID_MAX:
                    pid_total[pid_str] += 1

            for path in _HTTP_PATH_RE.findall(line):
                path_total[path] += 1

        observations: list[EntityObservation] = []

        for value, count in ip_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.IP,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=ip_error.get(value, 0) > 0,
                )
            )
        for value, count in user_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.USER,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=user_error.get(value, 0) > 0,
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
        for value, count in pid_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PID,
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
