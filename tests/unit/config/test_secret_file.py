"""The one publisher and the one read rule for a local secret file (#1788).

``publish_secret_once`` publishes the standalone JWT secret and the redaction
pseudonym key; ``tighten_secret_file_mode`` runs before either is used.
"""

import errno
import logging
import os

import pytest

import faultmaven.config.secret_file as publisher
from faultmaven.config.secret_file import publish_secret_once
from faultmaven.infrastructure.security.pseudonym_key import _load_or_create

pytestmark = pytest.mark.unit


def test_a_published_secret_is_kept_and_the_loser_is_told(tmp_path):
    path = tmp_path / "secret"
    path.write_bytes(b"the-winners-secret\n")

    assert publish_secret_once(path, b"a-losing-candidate") is False
    assert path.read_bytes() == b"the-winners-secret\n"
    assert [p.name for p in tmp_path.iterdir()] == ["secret"]


def test_an_existing_file_is_replaced_only_when_the_caller_says_it_holds_none(
    tmp_path,
):
    """Replacing a blank file is the JWT secret's rule, not the publisher's:
    without ``replace_if`` even an empty file is kept (#1788 review, 2/6)."""
    path = tmp_path / "secret"
    path.write_bytes(b"")
    path.chmod(0o644)

    assert publish_secret_once(path, b"a-new-secret") is False
    assert path.read_bytes() == b""

    assert publish_secret_once(path, b"a-new-secret", replace_if=lambda raw: not raw)
    assert path.read_bytes() == b"a-new-secret"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_a_link_that_fails_for_another_reason_is_never_a_win(tmp_path, monkeypatch):
    """Only "no hard links here" takes the locked path. An EIO took it too, and
    its unconditional replace landed over the secret a peer linked meanwhile:
    both reported a win (#1788 review, 7). The peer links at the moment that
    replace would land; ours must raise and never land over it."""
    path = tmp_path / "secret"
    real_link, real_replace = os.link, os.replace

    def failing_link(src, dst):
        raise OSError(errno.EIO, "Input/output error")

    def peer_links_first(src, dst):
        peer = tmp_path / "peer"
        peer.write_bytes(b"the-peers-secret")
        real_link(peer, dst)
        peer.unlink()
        real_replace(src, dst)

    monkeypatch.setattr(publisher.os, "link", failing_link)
    monkeypatch.setattr(publisher.os, "replace", peer_links_first)

    with pytest.raises(OSError) as failed:
        publish_secret_once(path, b"our-secret")

    assert failed.value.errno == errno.EIO
    assert not path.exists() or path.read_bytes() == b"the-peers-secret"


def test_a_directory_at_the_key_path_keeps_its_mode(tmp_path):
    """Only a regular file is tightened: a directory went to 0600 before the
    read refused it (#1788 review, 1)."""
    path = tmp_path / ".redaction_key"
    path.mkdir()
    path.chmod(0o755)

    with pytest.raises(IsADirectoryError):
        _load_or_create(path)

    assert (path.stat().st_mode & 0o777) == 0o755


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
    assert "readable or writable" in warned[0], warned[0]
    assert "REDACTION_PSEUDONYM_KEY" in warned[0], warned[0]
    assert "redaction:pseudonym_key_fingerprint" in warned[0], warned[0]
    assert key.decode() not in caplog.text
