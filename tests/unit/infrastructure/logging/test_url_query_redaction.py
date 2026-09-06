"""A URL's query string must not survive into anything FaultMaven writes.

Replaces a ``logging.Filter`` that was attached to three named third-party
loggers. That covered httpx printing request URLs, but not the two leaks found
next, which were first-party: ``main.py``'s 500 handler logged ``request.url``,
and ``web_search.py`` logged an exception whose message embeds the request URL.
A filter cannot cover those without listing every logger in the codebase — and
a filter on a logger does not even see its own children's records.

The processor sits in ``ProcessorFormatter.processors`` instead, where every
record converges, so the rule is one sentence: nothing FaultMaven writes
carries a URL query string.

End-to-end assertions here read the handler's **stream**, not ``caplog``.
``caplog`` installs its own handler with its own formatter, so it never runs
this processor; asserting through it would test nothing and pass.
"""

import io
import logging

import pytest
import structlog

from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    get_logger,
)
from faultmaven.infrastructure.logging.url_redaction import (
    redact_urls,
    redact_urls_processor,
)

KEY = "AIzaSyREDACT-ME-0000000000"
CODE = "authcode-Ei9kQ2xhdWRl"


@pytest.mark.unit
class TestRedactUrls:
    def test_a_query_string_is_stripped(self):
        assert (
            redact_urls("see https://x.com/a?b=c now")
            == "see https://x.com/a?<redacted> now"
        )

    def test_prose_containing_a_question_mark_is_untouched(self):
        """The reason the pattern is anchored on the scheme.

        "did the disk fill?" is an ordinary thing for this product to log, and
        an unanchored rule would eat the rest of the sentence.
        """
        text = "Investigation note: did the disk fill? /var/lib is at 100%"
        assert redact_urls(text) == text

    def test_a_url_with_no_query_is_untouched(self):
        assert redact_urls("GET https://x.com/a/b") == "GET https://x.com/a/b"

    def test_every_url_in_the_value_is_covered(self):
        assert (
            redact_urls("https://a/1?x=1 and https://b/2?y=2")
            == "https://a/1?<redacted> and https://b/2?<redacted>"
        )

    def test_a_quoted_url_keeps_its_closing_quote(self):
        """httpx renders errors as ``for url '<url>'``."""
        assert (
            redact_urls("for url 'https://g.com/s?key=K'")
            == "for url 'https://g.com/s?<redacted>'"
        )


@pytest.mark.unit
class TestProcessor:
    def test_values_at_every_level_are_covered(self):
        event = {
            "event": "failed for https://g.com/s?key=K",
            "extra": {"payload": {"url": "https://h.com/t?token=T"}},
            "urls": ["https://i.com/u?a=1"],
            "status_code": 200,
        }

        out = redact_urls_processor(None, "error", event)

        assert "key=K" not in out["event"]
        assert "token=T" not in out["extra"]["payload"]["url"]
        assert "a=1" not in out["urls"][0]
        assert out["status_code"] == 200

    def test_a_caller_owned_container_is_not_mutated(self):
        """A log call hands us objects it still owns.

        ``extra={"config": live_dict}`` must come back from logging unchanged;
        editing it in place would let a log line alter program state.
        """
        live = {"url": "https://g.com/s?key=K"}

        redact_urls_processor(None, "info", {"extra": live})

        assert live["url"] == "https://g.com/s?key=K"

    def test_a_record_with_nothing_to_redact_is_returned_as_is(self):
        event = {"event": "Request completed", "status_code": 200}
        assert redact_urls_processor(None, "info", event) == event


def _faultmaven_handler(root):
    """Our handler, by its marker, not by position.

    pytest installs handlers of its own on the root logger, so ``handlers[0]``
    is whichever one happens to be first — under ``-p logging`` that is
    pytest's null handler, which has no stream at all.
    """
    marker = FaultMavenLogger._ROOT_HANDLER_MARKER
    for handler in root.handlers:
        if getattr(handler, marker, False):
            return handler
    raise AssertionError("configure_structlog installed no marked root handler")


@pytest.fixture()
def written():
    """Configure logging for real and capture what the handler writes.

    Restores every global it touches: structlog's configuration, and the root
    logger's level and handlers. Leaving those behind makes the rest of the
    session order-dependent, which this repo has a history of (#823).
    """
    config = LoggingConfig()
    config.LOG_FORMAT = "json"
    config.LOG_HUMAN_READABLE = False

    root = logging.getLogger()
    saved_structlog = structlog.get_config()
    saved_level = root.level
    saved_handlers = list(root.handlers)

    # Warm the module singleton BEFORE capturing the stream. The first
    # get_logger() call in a process constructs a FaultMavenLogger of its own,
    # which reinstalls the root handler — and takes the captured stream with
    # it, leaving the test asserting against an empty buffer.
    get_logger("faultmaven.test.warmup")

    FaultMavenLogger(config).configure_structlog()
    root.setLevel(logging.INFO)
    buffer = io.StringIO()
    _faultmaven_handler(root).setStream(buffer)
    try:
        yield buffer
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        structlog.configure(**saved_structlog)


@pytest.mark.unit
@pytest.mark.security
class TestNothingWrittenCarriesAQueryString:
    def test_a_foreign_record_with_positional_args(self, written):
        """httpx passes the URL as a ``%s`` argument, not in the template.

        A rule that only looked at the message template would miss it
        entirely, which is why this runs after the args are interpolated.
        """
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            f"https://www.googleapis.com/customsearch/v1?key={KEY}&q=acme-corp",
            "HTTP/1.1",
            400,
            "Bad Request",
        )

        output = written.getvalue()
        assert "HTTP Request" in output, "the record was not written at all"
        assert KEY not in output
        assert "acme-corp" not in output
        assert "googleapis.com/customsearch/v1" in output, "the path must survive"

    def test_a_first_party_record(self, written):
        """The shape a ``logging.Filter`` on named loggers could never reach."""
        get_logger("faultmaven.main").error(
            f"Internal server error on GET https://app.f.ai/sso/callback?code={CODE}"
        )

        output = written.getvalue()
        assert "Internal server error" in output, "the record was not written"
        assert CODE not in output
        assert "app.f.ai/sso/callback" in output, "the path must survive"

    def test_an_exception_message_carrying_a_url(self, written):
        """~470 call sites interpolate an exception into a log message.

        httpx renders HTTPStatusError as ``... for url '<url>'``, so any of
        them leaks whatever the URL carried.
        """
        get_logger("faultmaven.web").error(
            f"Web search failed: Client error for url 'https://g.com/s?key={KEY}'"
        )

        assert KEY not in written.getvalue()

    def test_an_investigation_note_is_not_mangled(self, written):
        """The cost side: redaction must not damage the product's own data."""
        note = "did the disk fill? /var/lib at 100%"
        get_logger("faultmaven.engine").info(note)

        assert note in written.getvalue()

    def test_the_processor_is_installed_in_the_chain(self, written):
        """Fails if someone removes it from the formatter's processor list.

        The end-to-end tests above would also fail, but this one names the
        cause instead of leaving a reader to work out why a URL got through.
        """
        formatter = _faultmaven_handler(logging.getLogger()).formatter
        assert redact_urls_processor in formatter.processors
