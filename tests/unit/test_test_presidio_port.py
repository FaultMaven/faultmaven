"""No test process names a Presidio it does not own (#2232, ``tests/conftest.py``).

The shipped Presidio URLs name ``presidio-{analyzer,anonymizer}.faultmaven.local``.
Every ``DataSanitizer`` built with service checks on under ``SANITIZE_PII``
health-checks both, so a test that composed the app passed in CI because the
names did not resolve there, and failed where they do. The conftest points both
URLs, by variable and by field default, at a loopback port the process holds
without listening, unless the network guard is off.

Not in ``test_network_guard.py``: that file forgets the ports earlier tests
bound, and the conftest's bind is one of them.
"""

from __future__ import annotations

import _socket
import errno
import os
import pathlib
import socket
import textwrap
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from faultmaven.config.settings import FaultMavenSettings, ProtectionSettings
from faultmaven.infrastructure.security.redaction import DataSanitizer
from tests import network_guard
from tests.conftest import PRESIDIO_URL_FIELDS

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _session_url() -> str:
    (url,) = {os.environ[variable] for variable in PRESIDIO_URL_FIELDS}
    return url


def test_presidio_is_a_loopback_port_this_process_holds_and_nothing_answers():
    """A health check finds no server because the kernel refuses the connect,
    not because a name failed to resolve."""
    url = _session_url()
    settings = ProtectionSettings()
    assert settings.presidio_analyzer_url == settings.presidio_anonymizer_url == url
    host, port = urlsplit(url).hostname, urlsplit(url).port
    assert host == "127.0.0.1"
    assert port in network_guard.GUARD.bound_ports
    with socket.socket() as other:
        with pytest.raises(OSError) as taken:
            other.bind((host, port))
    assert taken.value.errno == errno.EADDRINUSE  # held, so no listener can take it
    # The C socket's connect, below the guard: the kernel's own answer.
    probe = _socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            probe.connect((host, port))
    finally:
        probe.close()


def test_a_test_that_empties_the_environment_keeps_the_port():
    """The port is the fields' default too, as the data root's paths are, so a
    sanitizer built in an emptied environment checks it, with checks on."""
    url = _session_url()
    with patch.dict(os.environ, {"SANITIZE_PII": "true"}, clear=True):
        settings = FaultMavenSettings()
        assert settings.server.skip_service_checks is False
        sanitizer = DataSanitizer(settings=settings)
    assert sanitizer.presidio_probed is True
    assert (sanitizer.analyzer_url, sanitizer.anonymizer_url) == (url, url)
    assert (sanitizer.analyzer_available, sanitizer.anonymizer_available) == (
        False,
        False,
    )
    assert network_guard.drain_refusals() == []


_CHILD_GUARD_OFF = """
import os

from faultmaven.config.settings import ProtectionSettings

CALLER = "http://presidio.example.invalid:3000"


def test_the_callers_urls_stand():
    assert os.environ["PRESIDIO_ANALYZER_URL"] == CALLER
    assert os.environ["PRESIDIO_ANONYMIZER_URL"] == CALLER
    settings = ProtectionSettings()
    assert settings.presidio_analyzer_url == CALLER
    assert ProtectionSettings.model_fields["presidio_analyzer_url"].default == (
        "http://presidio-analyzer.faultmaven.local:30080"
    )
"""


def test_with_the_guard_off_the_callers_urls_stand(pytester, monkeypatch):
    """The reason for replacing them, that the guard would refuse whatever they
    name, holds only while the guard is on. The child reads, and reaches for
    nothing."""
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    monkeypatch.setenv(network_guard.ALLOW_LIVE_NETWORK_ENV, "1")
    for variable in PRESIDIO_URL_FIELDS:
        monkeypatch.setenv(variable, "http://presidio.example.invalid:3000")
    pytester.makepyfile(textwrap.dedent(_CHILD_GUARD_OFF))
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "tests.conftest", timeout=600
    )
    result.assert_outcomes(passed=1)
