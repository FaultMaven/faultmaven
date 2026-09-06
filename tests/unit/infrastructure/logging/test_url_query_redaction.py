"""A URL's query string must not survive into a log record.

``httpx`` logs one INFO line per request carrying the full URL. That line is
wanted — it is how a provider returning 429s becomes visible — but the query
string in it is not ours to keep. The web-search tool reaches Google CSE as
``?key=<GOOGLE_API_KEY>&q=<the investigation's search text>``
(``modules/agent/tools/web_search.py``), and web search is on by default, so
every search wrote an API key and a slice of case content into a record that
ships to the log store.

The filter drops the query string and keeps method, host, path and status.

**Scope.** It is attached to named third-party loggers, and a
``logging.Filter`` on a logger sees only that logger's own records — not its
children's, and not first-party ones. That is not an oversight but the limit of
what a stdlib filter can safely do here: a first-party record's ``msg`` is
structlog's *event dict*, and rewriting it to a string would destroy the
structured event. Covering every record needs a structlog processor operating
on the event dict, which is filed as fm#1340. First-party sinks are therefore
fixed at the source; see ``test_access_log_omits_credentials.py`` and
``tests/unit/modules/agent/tools/test_web_search_tool.py``.
"""

import logging

import pytest
import structlog

from faultmaven.infrastructure.logging.config import (
    URL_LOGGING_THIRD_PARTY_LOGGERS,
    FaultMavenLogger,
    LoggingConfig,
    RedactQueryStringsFilter,
)

KEY = "AIzaSyREDACT-ME-0000000000"


@pytest.fixture()
def captured():
    """Configure logging for real, then put every global back.

    ``configure_structlog`` mutates process-wide state: structlog's own
    configuration, the root logger's level and handlers, and the filters on the
    URL-logging loggers. Leaving any of that behind makes the rest of the
    session order-dependent, which this repo already has a history of (#823).

    The levels are pinned explicitly. ``configure_structlog`` only ever
    *lowers* the root level, so it cannot guarantee the INFO record under test
    is emitted — with an ambient ``LOG_LEVEL=WARNING`` (and ``.env`` does reach
    ``os.environ`` here) the record would be dropped and the test would fail
    for an environment reason rather than a redaction one.
    """
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    config = LoggingConfig()
    config.LOG_LEVEL = "INFO"

    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved_structlog = structlog.get_config()
    saved_root_level = root.level
    saved_httpx_level = httpx_logger.level
    saved_handlers = list(root.handlers)
    saved_filters = {
        name: list(logging.getLogger(name).filters)
        for name in URL_LOGGING_THIRD_PARTY_LOGGERS
    }

    FaultMavenLogger(config).configure_structlog()
    root.setLevel(logging.INFO)
    httpx_logger.setLevel(logging.INFO)
    handler = Capture()
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root_level)
        httpx_logger.setLevel(saved_httpx_level)
        structlog.configure(**saved_structlog)
        for name, filters in saved_filters.items():
            logging.getLogger(name).filters[:] = filters


@pytest.mark.unit
@pytest.mark.security
class TestUrlQueryRedaction:
    def test_an_api_key_in_a_query_string_never_reaches_a_record(self, captured):
        """The httpx call shape: the URL arrives as a positional argument.

        Redacting only ``record.msg`` would pass a test that formatted the
        message itself and still ship the key, because httpx's ``msg`` is the
        template ``'HTTP Request: %s %s "%s %d %s"'`` and the URL is in
        ``args``.
        """
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            f"https://www.googleapis.com/customsearch/v1?key={KEY}&q=acme+outage",
            "HTTP/1.1",
            200,
            "OK",
        )

        emitted = [r.getMessage() for r in captured if r.name == "httpx"]
        assert emitted, "the httpx record was not captured at all"
        assert not any(KEY in m for m in emitted), "the API key reached a record"
        assert not any("acme" in m for m in emitted), "the query text reached a record"

    def test_the_line_is_still_useful(self, captured):
        """Method, host, path and status must survive, or the fix costs the signal."""
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            f"https://api.openai.com/v1/chat?key={KEY}",
            "HTTP/1.1",
            429,
            "Too Many Requests",
        )

        message = next(r.getMessage() for r in captured if r.name == "httpx")
        assert "GET" in message
        assert "https://api.openai.com/v1/chat" in message
        assert "429" in message

    def test_a_record_with_no_query_string_is_untouched(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            "HTTP Request: GET https://h/p",
            (),
            None,
        )
        RedactQueryStringsFilter().filter(record)
        assert record.getMessage() == "HTTP Request: GET https://h/p"

    def test_every_declared_url_logger_carries_the_filter_exactly_once(self, captured):
        """Re-configuration must not stack duplicate filters on a logger.

        The emptiness assertion is not decoration: without it, emptying
        ``URL_LOGGING_THIRD_PARTY_LOGGERS`` -- which is exactly how the fix
        gets lost -- makes the loop below iterate over nothing and this test
        pass while every URL is logged in full.

        Takes ``captured`` for its teardown, not its records: the fixture has
        already configured once, and the second call below is the point.
        """
        assert "httpx" in URL_LOGGING_THIRD_PARTY_LOGGERS

        FaultMavenLogger(LoggingConfig()).configure_structlog()

        for name in URL_LOGGING_THIRD_PARTY_LOGGERS:
            installed = [
                f
                for f in logging.getLogger(name).filters
                if isinstance(f, RedactQueryStringsFilter)
            ]
            assert len(installed) == 1, f"{name} has {len(installed)} filters"
