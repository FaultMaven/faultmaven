"""A collaborator failure whose text must never reach the model (#2332).

A tool's result goes into the model's context, and the model can repeat it into
the stored answer every reader of the case is served. Each tool's catch-all is
driven with this exception: what the model reads (the result's ``error``, or a
string returned as its data) names the class and carries none of the text, and
the server log names the class.
"""

import io
import json

from faultmaven.infrastructure.logging.config import build_log_formatter

# Split so the credential pair never sits in the source tree as one token.
_DSN = "postgresql://svc:" + "hunter2" + "@db-internal.svc:5432/fm"

_FRAGMENTS = ("hunter2", "db-internal", "SELECT")


def leaky_error() -> RuntimeError:
    """A driver-style error: a credential, an internal host, and SQL."""
    return RuntimeError(f"{_DSN} SELECT * FROM cases WHERE id='c1'")


def assert_reported_by_class(model_text: str, records, event: str) -> None:
    """The model reads the class and none of the text. Each captured record,
    rendered through the chain's formatter (``build_log_formatter``), holds none
    of the text, and the line whose event holds ``event`` names the class
    (#2889)."""
    for fragment in _FRAGMENTS:
        assert fragment not in model_text
    assert "RuntimeError" in model_text
    formatter = build_log_formatter("json", False, io.StringIO())
    lines = [formatter.format(record) for record in records]
    for line in lines:
        for fragment in _FRAGMENTS:
            assert fragment not in line, line
    site = [line for line in lines if event in json.loads(line)["event"]]
    assert site, f"no line holds {event!r}: {lines}"
    assert any("RuntimeError" in line for line in site), site
