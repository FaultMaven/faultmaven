"""The one publisher and the one read rule for a local secret file (#1788).

``publish_secret_once`` publishes the standalone JWT secret and the redaction
pseudonym key; ``tighten_secret_file_mode`` runs before either is read.
"""

import logging

import pytest

from faultmaven.config.secret_file import publish_secret_once
from faultmaven.infrastructure.security.pseudonym_key import _load_or_create

pytestmark = pytest.mark.unit


def test_a_published_secret_is_kept_and_the_loser_is_told(tmp_path):
    path = tmp_path / "secret"
    path.write_bytes(b"the-winners-secret\n")

    assert publish_secret_once(path, b"a-losing-candidate") is False
    assert path.read_bytes() == b"the-winners-secret\n"
    assert [p.name for p in tmp_path.iterdir()] == ["secret"]


@pytest.mark.parametrize("blank", [b"", b"\n  "], ids=["empty", "whitespace"])
def test_an_empty_file_is_not_a_published_secret(tmp_path, blank):
    """A file published by link or replace is never empty, so an empty one is
    a leftover, replaced whole at 0600 (the JWT secret's existing rule)."""
    path = tmp_path / "secret"
    path.write_bytes(blank)
    path.chmod(0o644)

    assert publish_secret_once(path, b"a-new-secret") is True
    assert path.read_bytes() == b"a-new-secret"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_the_pseudonym_key_reader_tightens_a_key_file_others_can_read(tmp_path, caplog):
    """The same rule for the second secret: tightened, with a WARNING naming
    the file and how to rotate the key, and the key is still the one used."""
    path = tmp_path / ".redaction_key"
    key = b"k" * 64
    path.write_bytes(key)
    path.chmod(0o644)
    caplog.set_level(logging.WARNING)

    assert _load_or_create(path) == key
    assert (path.stat().st_mode & 0o777) == 0o600
    warned = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and str(path) in r.getMessage()
    ]
    assert len(warned) == 1, caplog.text
    assert "REDACTION_PSEUDONYM_KEY" in warned[0], warned[0]
    assert key.decode() not in caplog.text
