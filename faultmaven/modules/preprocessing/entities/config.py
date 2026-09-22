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

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.line_tally import (
    EntityRule,
    is_port,
    tally_entity_lines,
)
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

# Scanning is per line, like every other entity extractor — one line is
# one mention (fm#1587). It used to run ``findall`` over the whole file,
# which counted a key repeated on ONE line twice; the regexes below never
# crossed a newline anyway (see the next paragraph), so per-line matching
# finds exactly the same values.
#
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

    #: Rule order is the emission order. Configs are declarative and
    #: carry no error lines, so no rule records ``error_context``.
    _RULES = (
        EntityRule(EntityType.HOSTNAME, patterns=(_HOST_RE,)),
        EntityRule(EntityType.PORT, patterns=(_PORT_RE,), keep=is_port),
        EntityRule(EntityType.SERVICE, patterns=(_SERVICE_RE,)),
        EntityRule(EntityType.PATH, patterns=(_PATH_RE,)),
        EntityRule(EntityType.IP, patterns=(_IPV4_RE,)),
    )

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
        return tally_entity_lines(content, self._RULES)
