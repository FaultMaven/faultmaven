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
    tally_document_matches,
)
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

# ``mention_count`` here counts MATCHES over the whole document, not
# lines — the one entity extractor that does, and the exception is
# deliberate (fm#1587). The per-line unit's argument is "a line is one
# event"; a config has no events, it is a structure, and flow-style YAML,
# minified JSON and single-line ``key=v key=v`` blocks put a whole config
# on one physical line. Counting lines there flattens every value to 1
# and the ``SUM(mention_count) DESC`` ranking that picks the top five
# entities for the prompt degenerates into an insertion-ordered tie.
# Measured on the same bytes:
#
#   ONE physical line   db1.internal 1, 5432 1, pgbouncer 1, db2.internal 1
#   newline-separated   db1.internal 2, 5432 2, pgbouncer 1, db2.internal 1
#
# — strictly worse than the match count it would have replaced, which
# ranks both forms identically. So configs keep match counting until the
# owner rules otherwise; see ``entity-registry.md`` §*The mention unit*.
# ‼ There is no line split here on purpose: the separators below cannot
# cross a line ending in any of its three spellings, so document-scope
# matching needs no notion of a line at all.
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
        return tally_document_matches(content, self._RULES)
