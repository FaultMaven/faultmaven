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

Usernames come from ``preprocessing/log_usernames.py`` rather than a
local pattern. The local copy carried none of the guards the logs
extractor grew, so on verbatim OpenSSH input it emitted reverse-mapping
PTR records, PAM structural words and kernel ``for <thing>`` phrases as
login accounts (fm#522).
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
from faultmaven.modules.preprocessing.log_usernames import extract_usernames

# Regexes mirror ``logs_extractor.py``. Kept local so this module can
# evolve independently if the logs extractor's formatting changes
# (e.g. if it dropped the entity profile). The cost is a second compile
# — negligible. What is NOT mirrored is anything that decides a count:
# the username rule, because a second copy of it cost fm#522; the line
# split, because a per-line count is only as right as what it calls a line
# — ``split("\n")`` read a bare-``\r`` file as one line and floored every
# count at 1 (fm#1574 review); and the mention unit itself, because one
# copy per extractor is exactly how USER ended up counting something
# different from IP (fm#1587). Those live in ``line_tally`` /
# ``extractors.utils``.
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
_PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"(?<![\w.-])[\w-]*[A-Za-z.][\w.-]*:(\d{1,5})\b")
_PID_KEYWORD_RE = re.compile(r"\bpid[= ]+(\d{1,7})\b", re.IGNORECASE)
_PID_BRACKET_RE = re.compile(r"\[(\d{1,7})\]")
_HTTP_PATH_RE = re.compile(r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[^\s\?]*)\b")


class LogsEntityExtractor:
    """Extractor for LOGS_AND_ERRORS content."""

    #: Rule order is the order observations are emitted in, which is the
    #: order this extractor emitted before ``tally_entity_lines`` owned the
    #: loop. ``in_error_context`` is recorded for IP and USER only — the
    #: information exists for the other three, but reporting it would be a
    #: separate change from fm#1587's unit fix.
    _RULES = (
        EntityRule(EntityType.IP, patterns=(_IPV4_RE, _IPV6_RE), error_context=True),
        EntityRule(EntityType.USER, matcher=extract_usernames, error_context=True),
        EntityRule(
            EntityType.PORT, patterns=(_PORT_KEYWORD_RE, _HOST_PORT_RE), keep=is_port
        ),
        EntityRule(
            EntityType.PID, patterns=(_PID_KEYWORD_RE, _PID_BRACKET_RE), keep=is_pid
        ),
        EntityRule(EntityType.PATH, patterns=(_HTTP_PATH_RE,)),
    )

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
        return tally_entity_lines(
            content,
            self._RULES,
            is_error=lambda index, _line: index in error_lines,
        )
