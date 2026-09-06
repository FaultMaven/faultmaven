"""Nothing FaultMaven writes carries a URL query string.

A URL's query string is a credential channel here: the SSO callback carries
the identity provider's authorization code in one, and the web-search tool
reaches Google CSE as ``?key=<GOOGLE_API_KEY>&q=<the search text>``.

Redaction happens on the **rendered line**. An earlier version walked the
event dict and redacted values by type, and every type it did not handle
leaked — bytes, sets, dict keys, anything the renderer ``repr``s (an exception
passed as ``error=e``), and anything nested past the walk's depth. The
``TestNothingWrittenLeaks`` cases below are one per hole that version had, and
they are the reason the design changed rather than growing a type list.
"""

import io
import logging

import pytest
import structlog

import faultmaven.infrastructure.logging.config as logging_config
from faultmaven.infrastructure.logging.config import (
    FaultMavenLogger,
    LoggingConfig,
    get_logger,
)
from faultmaven.infrastructure.logging.url_redaction import (
    RedactingFormatter,
    redact_urls,
    redact_urls_bytes,
    redacting_renderer,
)

SECRET = "AIzaSyREDACT-ME-0000000000"
URL = f"https://g.com/search?key={SECRET}&q=acme-corp"


@pytest.mark.unit
class TestRedactUrls:
    def test_a_query_string_is_stripped(self):
        assert redact_urls("see https://x.com/a?b=c now") == (
            "see https://x.com/a?<redacted> now"
        )

    def test_prose_containing_a_question_mark_is_untouched(self):
        text = "Investigation note: did the disk fill? /var/lib is at 100%"
        assert redact_urls(text) == text

    def test_prose_around_a_url_keeps_its_question_marks(self):
        """This pins the scheme anchor; the case above does not.

        A value with no "://" never reaches the pattern, so the plain-prose
        case passes with or without the anchor. Only a value carrying both a
        URL and a question mark separates them.
        """
        assert redact_urls("did https://x.com/a?b=c fail? check the pod") == (
            "did https://x.com/a?<redacted> fail? check the pod"
        )

    def test_a_second_question_mark_does_not_defeat_the_match(self):
        """‼ The regression this pattern was rewritten for.

        With "?" allowed in the pre-query class, the greedy group backtracked
        to the LAST question mark, so only the trailing one was replaced and
        the key shipped verbatim. RFC 3986 permits "?" inside a query, and a
        sentence ending in "?" right after a URL produces the same shape.
        """
        assert redact_urls(f"Failed to reach https://api.x/v1?key={SECRET}?") == (
            "Failed to reach https://api.x/v1?<redacted>"
        )
        assert redact_urls(f"GET https://api/v1?q=why+fail?&key={SECRET}") == (
            "GET https://api/v1?<redacted>"
        )

    def test_non_http_schemes_are_covered(self):
        """A DSN carries credentials in its query options."""
        assert redact_urls("connect failed: postgresql://u@h/db?password=P") == (
            "connect failed: postgresql://u@h/db?<redacted>"
        )
        assert redact_urls("redis://h:6379/0?password=P") == (
            "redis://h:6379/0?<redacted>"
        )

    def test_a_url_with_no_query_is_untouched(self):
        assert redact_urls("GET https://x.com/a/b") == "GET https://x.com/a/b"

    def test_every_url_in_the_value_is_covered(self):
        assert redact_urls("https://a/1?x=1 and https://b/2?y=2") == (
            "https://a/1?<redacted> and https://b/2?<redacted>"
        )

    def test_a_quoted_url_keeps_its_closing_quote(self):
        """httpx renders errors as ``for url '<url>'``."""
        assert redact_urls("for url 'https://g.com/s?key=K'") == (
            "for url 'https://g.com/s?<redacted>'"
        )

    def test_the_bytes_form_matches_the_str_form(self):
        text = "GET https://g.com/s?key=K and https://h/i"
        assert redact_urls_bytes(text.encode()) == redact_urls(text).encode()


@pytest.mark.unit
class TestRedactingRenderer:
    def test_a_bytes_renderer_is_redacted_too(self):
        """A renderer may emit bytes; an orjson serializer is the likely one.

        Without this the bytes branch is unexercised, and a future swap to a
        bytes renderer would silently stop redacting — a security guard that
        reports success while doing nothing.
        """
        wrapped = redacting_renderer(lambda *_: f"GET {URL}".encode())

        assert SECRET.encode() not in wrapped(None, "info", {})

    def test_a_str_renderer_is_redacted(self):
        wrapped = redacting_renderer(lambda *_: f"GET {URL}")

        assert SECRET not in wrapped(None, "info", {})


@pytest.fixture()
def written():
    """Configure logging for real and capture what the handler writes.

    Every global this touches is restored: structlog's configuration, the root
    logger's level and handlers, the httpx logger's level, and the module
    singleton. Leaving any of them behind makes the rest of the session
    order-dependent, which this repo has a history of (#823).

    Ordering matters twice over. The singleton is warmed BEFORE the handler
    list is saved, because the first ``get_logger()`` in a process constructs a
    ``FaultMavenLogger`` and installs a handler — saving the list first and
    restoring it later would strip that handler permanently, since the
    singleton stays set and nothing reinstalls it.
    """
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")

    # Warm first, then snapshot: see the docstring.
    saved_singleton = logging_config._logger_config
    get_logger("faultmaven.test.warmup")

    saved_structlog = structlog.get_config()
    saved_level = root.level
    saved_httpx_level = httpx_logger.level
    saved_handlers = list(root.handlers)

    config = LoggingConfig()
    config.LOG_FORMAT = "json"
    config.LOG_HUMAN_READABLE = False
    FaultMavenLogger(config).configure_structlog()

    # Pinned explicitly: configure_structlog only ever LOWERS the root level,
    # and httpx is deliberately not in NOISY_THIRD_PARTY_LOGGERS, so whatever
    # an earlier test left on either would decide whether these records are
    # emitted at all.
    root.setLevel(logging.INFO)
    httpx_logger.setLevel(logging.INFO)

    buffer = io.StringIO()
    _faultmaven_handler(root).setStream(buffer)
    try:
        yield buffer
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_httpx_level)
        structlog.configure(**saved_structlog)
        logging_config._logger_config = saved_singleton


def _faultmaven_handler(root):
    """Our handler, by its marker, not by position.

    pytest installs handlers of its own, so ``handlers[0]`` is whichever one
    happens to be first — under the logging plugin that is pytest's null
    handler, which has no stream at all.
    """
    marker = FaultMavenLogger._ROOT_HANDLER_MARKER
    for handler in root.handlers:
        if getattr(handler, marker, False):
            return handler
    raise AssertionError("configure_structlog installed no marked root handler")


@pytest.mark.unit
@pytest.mark.security
class TestNothingWrittenLeaks:
    """One case per hole the dict-walking version had.

    Each of these was verified leaking before the design changed to redact the
    rendered line.
    """

    def test_a_foreign_record_with_positional_args(self, written):
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"', "GET", URL, "HTTP/1.1", 400, "Bad Request"
        )
        output = written.getvalue()
        assert "HTTP Request" in output, "the record was not written at all"
        assert SECRET not in output
        assert "acme-corp" not in output
        assert "g.com/search" in output, "the path must survive"

    def test_a_first_party_message(self, written):
        get_logger("faultmaven.main").error(f"Internal server error on GET {URL}")
        assert SECRET not in written.getvalue()

    def test_bytes(self, written):
        """UnicodeDecoder turned these into a leaking str after redaction ran."""
        get_logger("p").info("body", body=URL.encode())
        assert SECRET not in written.getvalue()

    def test_a_set(self, written):
        get_logger("p").info("seen", urls={URL})
        assert SECRET not in written.getvalue()

    def test_an_exception_passed_as_a_field(self, written):
        """``logger.error("failed", error=e)`` — the renderer repr()s it."""
        get_logger("p").error("failed", error=ValueError(f"for url '{URL}'"))
        assert SECRET not in written.getvalue()

    def test_a_dictionary_key(self, written):
        get_logger("p").info("counts", per_url={URL: 1})
        assert SECRET not in written.getvalue()

    def test_a_deeply_nested_value(self, written):
        get_logger("p").info("deep", ctx={"a": {"b": {"c": {"d": {"e": URL}}}}})
        assert SECRET not in written.getvalue()

    def test_an_exception_traceback(self, written):
        try:
            raise RuntimeError(f"boom for url '{URL}'")
        except RuntimeError:
            get_logger("p").exception("failed")
        assert SECRET not in written.getvalue()

    def test_a_dsn(self, written):
        get_logger("p").error("db down", url="postgresql://u:p@h/db?password=P")
        assert "password=P" not in written.getvalue()

    def test_an_investigation_note_is_not_mangled(self, written):
        """The cost side: redaction must not damage the product's own data."""
        note = "did the disk fill? /var/lib at 100%"
        get_logger("faultmaven.engine").info(note)
        assert note in written.getvalue()

    def test_the_renderer_is_wrapped(self, written):
        """Names the cause when the end-to-end cases above go red together."""
        formatter = _faultmaven_handler(logging.getLogger()).formatter
        assert formatter.processors[-1].__module__.endswith("url_redaction")


@pytest.mark.unit
class TestRedactingFormatter:
    """The maintenance jobs configure plain stdlib logging, not structlog."""

    def test_it_redacts_its_own_output(self):
        record = logging.LogRecord(
            "job", logging.ERROR, __file__, 1, "connect failed: %s", (URL,), None
        )
        formatted = RedactingFormatter("%(message)s").format(record)
        assert SECRET not in formatted
        assert "g.com/search" in formatted

    def test_a_record_with_no_url_is_unchanged(self):
        record = logging.LogRecord(
            "job", logging.INFO, __file__, 1, "seeded %d runbooks", (91,), None
        )
        assert RedactingFormatter("%(message)s").format(record) == "seeded 91 runbooks"


@pytest.mark.unit
class TestTheFixtureLeavesNothingBehind:
    """The fixture reconfigures process-wide logging; it must undo all of it.

    Named after the failure it prevents rather than the mechanism: an earlier
    version snapshotted the root handlers BEFORE warming the module singleton,
    so teardown restored a list that predated the handler and stripped it for
    the rest of the session — while the singleton stayed set, so nothing
    reinstalled it. Every later test reading the handler then saw nothing.
    """

    def test_the_root_handler_survives(self, written):
        get_logger("faultmaven.test").info("anything")

    def test_zz_and_is_still_there_afterwards(self):
        """Ordered to run after the case above, which is the point."""
        marked = [
            handler
            for handler in logging.getLogger().handlers
            if getattr(handler, FaultMavenLogger._ROOT_HANDLER_MARKER, False)
        ]
        assert marked, "the fixture stripped the FaultMaven root handler"
