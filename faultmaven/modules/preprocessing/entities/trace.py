"""EntityExtractor for ``DataType.TRACE_DATA``.

Distributed traces (OpenTelemetry / Jaeger / Zipkin) expose:

- ``service.name`` — the span-owning service
- ``peer.service`` / ``net.peer.name`` — the RPC target
- ``http.url`` / ``http.target`` — the request path
- IPs for upstream/downstream hops

Traces are JSON-ish in practice, so value-extraction runs off the
serialised text. This stays cheap (no JSON parse) and tolerates the
wire formats Jaeger, OpenTelemetry JSON, and Zipkin V2 all share.

Error context: ``error=true`` / ``status.code: ERROR`` is the trace
world's severity signal. Lines containing those substrings mark
entities as ``in_error_context`` — a rough heuristic, but it matches
how operators eyeball trace dumps.
"""

from __future__ import annotations

import re
from collections import Counter

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

# Keys in trace dumps come both as JSON (``"key":"value"``) and as
# OTLP attribute pairs (``key=value``). The separator block allows an
# optional closing quote on the key, the ``:`` or ``=``, and an
# optional opening quote on the value — covering both wire formats.
_KV_SEP = r"[\"']?[ \t]*[:=][ \t]*[\"']?"
_SERVICE_RE = re.compile(
    r"\b(?:service\.name|peer\.service|net\.peer\.name|serviceName)"
    + _KV_SEP
    + r"([A-Za-z][\w\-.]{1,63})[\"']?",
    re.IGNORECASE,
)
_HOSTNAME_RE = re.compile(
    r"\b(?:host\.name|net\.host\.name|host)"
    + _KV_SEP
    + r"([A-Za-z][\w.\-]{1,253})[\"']?",
    re.IGNORECASE,
)
_URL_PATH_RE = re.compile(
    r"\b(?:http\.url|http\.target|http\.route|url\.path)"
    + _KV_SEP
    + r"(?:https?://[^/\s\"']+)?(/[^\s\"'?]*)[\"']?",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_ERROR_LINE_RE = re.compile(
    r'(?:"error"\s*:\s*true|status\.code[\s:=]+"?ERROR)', re.IGNORECASE
)


class TraceEntityExtractor:
    """Extractor for TRACE_DATA content."""

    @property
    def data_type_name(self) -> str:
        return "trace_data"

    def extract(
        self,
        content: str,
        error_line_indices: set[int] | None = None,
    ) -> list[EntityObservation]:
        if not content:
            return []

        # Per-line scan so we can tag error-context hits.
        service_total: Counter = Counter()
        service_error: Counter = Counter()
        hostname_total: Counter = Counter()
        hostname_error: Counter = Counter()
        path_total: Counter = Counter()
        path_error: Counter = Counter()
        ip_total: Counter = Counter()
        ip_error: Counter = Counter()

        for line in content.split("\n"):
            is_err = bool(_ERROR_LINE_RE.search(line))

            for value in _SERVICE_RE.findall(line):
                service_total[value] += 1
                if is_err:
                    service_error[value] += 1
            for value in _HOSTNAME_RE.findall(line):
                hostname_total[value] += 1
                if is_err:
                    hostname_error[value] += 1
            for value in _URL_PATH_RE.findall(line):
                path_total[value] += 1
                if is_err:
                    path_error[value] += 1
            for ip in _IPV4_RE.findall(line):
                ip_total[ip] += 1
                if is_err:
                    ip_error[ip] += 1

        observations: list[EntityObservation] = []
        for value, count in service_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.SERVICE,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=service_error.get(value, 0) > 0,
                )
            )
        for value, count in hostname_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.HOSTNAME,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=hostname_error.get(value, 0) > 0,
                )
            )
        for value, count in path_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.PATH,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=path_error.get(value, 0) > 0,
                )
            )
        for value, count in ip_total.items():
            observations.append(
                EntityObservation(
                    entity_type=EntityType.IP,
                    entity_value=value,
                    mention_count=count,
                    in_error_context=ip_error.get(value, 0) > 0,
                )
            )
        return observations
