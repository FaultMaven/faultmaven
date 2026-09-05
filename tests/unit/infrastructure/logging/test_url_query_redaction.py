"""A URL's query string must not survive into a log record.

``httpx`` logs one INFO line per request carrying the full URL. That line is
wanted — it is how a provider returning 429s becomes visible — but the query
string in it is not ours to keep. The web-search tool reaches Google CSE as
``?key=<GOOGLE_API_KEY>&q=<the investigation's search text>``
(``modules/agent/tools/web_search.py``), and web search is on by default, so
every search wrote an API key and a slice of case content into a record that
ships to the log store.

The filter drops the query string and keeps method, host, path and status.
"""

import logging

import pytest

from faultmaven.infrastructure.logging.config import (
    URL_LOGGING_THIRD_PARTY_LOGGERS,
    FaultMavenLogger,
    LoggingConfig,
    RedactQueryStringsFilter,
)

KEY = "AIzaSyREDACT-ME-0000000000"


@pytest.fixture()
def captured():
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    FaultMavenLogger(LoggingConfig()).configure_structlog()
    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        yield records
    finally:
        logging.getLogger().removeHandler(handler)


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

    def test_every_declared_url_logger_carries_the_filter_exactly_once(self):
        """Re-configuration must not stack duplicate filters on a logger.

        The emptiness assertion is not decoration: without it, emptying
        ``URL_LOGGING_THIRD_PARTY_LOGGERS`` -- which is exactly how the fix
        gets lost -- makes the loop below iterate over nothing and this test
        pass while every URL is logged in full.
        """
        assert "httpx" in URL_LOGGING_THIRD_PARTY_LOGGERS

        FaultMavenLogger(LoggingConfig()).configure_structlog()
        FaultMavenLogger(LoggingConfig()).configure_structlog()

        for name in URL_LOGGING_THIRD_PARTY_LOGGERS:
            installed = [
                f
                for f in logging.getLogger(name).filters
                if isinstance(f, RedactQueryStringsFilter)
            ]
            assert len(installed) == 1, f"{name} has {len(installed)} filters"
