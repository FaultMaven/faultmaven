"""A collaborator failure whose text must never reach the model (#2332).

A tool's result goes into the model's context, and the model can repeat it into
the stored answer every reader of the case is served. Each tool's catch-all is
driven with this exception: what the model reads (the result's ``error``, or a
string returned as its data) names the class and carries none of the text, and
the server log carries the text.
"""

# Split so the credential pair never sits in the source tree as one token.
_DSN = "postgresql://svc:" + "hunter2" + "@db-internal.svc:5432/fm"


def leaky_error() -> RuntimeError:
    """A driver-style error: a credential, an internal host, and SQL."""
    return RuntimeError(f"{_DSN} SELECT * FROM cases WHERE id='c1'")


def assert_reported_by_class(model_text: str, log_text: str) -> None:
    """The model reads the class and none of the text; the log has the text."""
    for fragment in ("hunter2", "db-internal", "SELECT"):
        assert fragment not in model_text
    assert "RuntimeError" in model_text
    assert "hunter2" in log_text
