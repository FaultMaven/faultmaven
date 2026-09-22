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

from faultmaven.modules.case.contracts import EntityType
from faultmaven.modules.preprocessing.entities.line_tally import (
    EntityRule,
    is_pid,
    is_port,
    tally_entity_lines,
)
from faultmaven.modules.preprocessing.entities.protocol import EntityObservation

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# A bare column-style PID: at least one whitespace before and after.
# Requires 1-7 digits; the range check is ``is_pid``.
_PID_COLUMN_RE = re.compile(r"(?:^|\s)(\d{1,7})(?=\s)")
_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)
_IP_PORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})\b")
_ABS_PATH_RE = re.compile(r"(?<![/\w])(/(?:[a-zA-Z0-9_\-.]+/){0,10}[a-zA-Z0-9_\-.]+)")
# Exclude /usr/bin-style binary paths from high-noise headers — they
# inflate the registry without adding investigative value. Keep /var,
# /etc, /tmp, /home, /opt paths which are the usual suspects for state.
_PATH_PREFIXES = ("/var", "/etc", "/tmp", "/home", "/opt", "/data")


class CommandOutputEntityExtractor:
    """Extractor for COMMAND_OUTPUT content."""

    #: Rule order is the emission order. No ``error_context`` anywhere —
    #: command output has no stable error-line semantics (see the module
    #: docstring).
    _RULES = (
        EntityRule(EntityType.IP, patterns=(_IPV4_RE,)),
        # Drop obvious non-PIDs: 0 and oversized values. Keep small
        # numbers — kthreads live in low PIDs.
        EntityRule(EntityType.PID, patterns=(_PID_COLUMN_RE,), keep=is_pid),
        EntityRule(
            EntityType.PORT, patterns=(_PORT_KEYWORD_RE, _IP_PORT_RE), keep=is_port
        ),
        EntityRule(
            EntityType.PATH,
            patterns=(_ABS_PATH_RE,),
            keep=lambda path: path.startswith(_PATH_PREFIXES),
        ),
    )

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
        return tally_entity_lines(content, self._RULES)
