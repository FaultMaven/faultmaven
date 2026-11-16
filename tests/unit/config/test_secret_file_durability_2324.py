"""A published secret's name is durable, and no publisher-named copy outlives it (#2324).

``publish_secret_once`` fsynced the temp file but never the directory, so a
power loss soon after a first start could lose the name and the next start
minted a new secret. A crash between its link and its temp unlink left
``<name>.tmp.<pid>.<hex>`` as a second name for the live secret, which
"delete the file to rotate" left on disk. ``prepare_secret_file_for_read``
removes that name before each read, and keeps any publisher-named file with
its own inode for the operator.
"""

import errno
import logging
import os
import stat

import pytest

import faultmaven.config.secret_file as publisher
from faultmaven.config import settings as S
from faultmaven.config.secret_file import publish_secret_once
from faultmaven.infrastructure.security.pseudonym_key import (
    reset_pseudonym_key_cache,
    resolve_pseudonym_key,
)
from tests.utils import delenv_every_spelling

pytestmark = pytest.mark.unit

#: The shape a crash between the publisher's link and its unlink leaves.
LEFTOVER = ".tmp.123.abcd1234"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Local auth with no ambient secret or database, and no cached key."""
    delenv_every_spelling(
        monkeypatch,
        "DATABASE_URL",
        "DATABASE",
        "JWT_SECRET_KEY",
        "JWT_SECRET_FILE",
        "JWT_PRIVATE_KEY",
        "AUTH_MODE",
        "OAUTH_ENABLED",
    )
    # Recorded as absent, so the value the resolver exports is removed again.
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")
    monkeypatch.setenv("AUTH_MODE", "local")
    reset_pseudonym_key_cache()
    yield
    reset_pseudonym_key_cache()


class _Settings:
    """What ``resolve_pseudonym_key`` reads: no key configured, standalone."""

    is_cloud = False

    def __init__(self, path):
        self.protection = type(
            "P", (), {"pseudonym_key": None, "pseudonym_key_path": str(path)}
        )()


def _read_pseudonym(path) -> bytes:
    reset_pseudonym_key_cache()  # a new start
    return resolve_pseudonym_key(_Settings(path))


def _read_jwt(path, monkeypatch) -> bytes:
    monkeypatch.setenv("JWT_SECRET_FILE", str(path))
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)  # a new start
    settings = S.FaultMavenSettings()
    S.resolve_local_jwt_secret(settings)
    return settings.security.jwt_secret_key.get_secret_value().encode()


READERS = ("pseudonym-key", "jwt-secret")


def _read(reader, path, monkeypatch) -> bytes:
    if reader == "jwt-secret":
        return _read_jwt(path, monkeypatch)
    return _read_pseudonym(path)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _identity(path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


# ---------------------------------------------------------------------------
# 1. The publish ends with a directory fsync of the resolved parent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hard_links", [True, False], ids=["link", "no-hard-links"])
@pytest.mark.parametrize("symlinked", [False, True], ids=["plain", "symlinked"])
def test_the_resolved_parent_is_fsynced_after_the_temp_name_is_gone(
    tmp_path, monkeypatch, hard_links, symlinked
):
    target = tmp_path / "volume" / "secret"
    target.parent.mkdir()
    path = target
    if symlinked:
        path = tmp_path / "secret"
        path.symlink_to(target)
    events: list[tuple] = []
    real_link, real_replace = os.link, os.replace
    real_unlink, real_fsync = os.unlink, os.fsync

    def link(src, dst, **kw):
        if not hard_links:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_link(src, dst, **kw)
        events.append(("publish", str(dst)))

    def replace(src, dst, **kw):
        real_replace(src, dst, **kw)
        events.append(("publish", str(dst)))

    def unlink(p, **kw):
        try:
            real_unlink(p, **kw)
        finally:  # after the replace path's unlink, the name is already gone
            events.append(("unlink", str(p)))

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", stat.S_ISDIR(st.st_mode), (st.st_dev, st.st_ino)))
        real_fsync(fd)

    monkeypatch.setattr(publisher.os, "link", link)
    monkeypatch.setattr(publisher.os, "replace", replace)
    monkeypatch.setattr(publisher.os, "unlink", unlink)
    monkeypatch.setattr(publisher.os, "fsync", fsync)

    assert publish_secret_once(path, b"the-secret") is True

    published = events.index(("publish", str(target)))
    tmp_unlinked = next(
        i
        for i, e in enumerate(events)
        if e[0] == "unlink" and ".tmp." in e[1] and e[1].startswith(str(target))
    )
    directory_fsyncs = [
        i for i, e in enumerate(events) if e[0] == "fsync" and e[1] is True
    ]
    assert directory_fsyncs, events
    assert events[directory_fsyncs[-1]][2] == _identity(target.parent), events
    assert directory_fsyncs[-1] > published, events
    assert directory_fsyncs[-1] > tmp_unlinked, events
    assert target.read_bytes() == b"the-secret"


# ---------------------------------------------------------------------------
# 2. A directory fsync that fails is a WARNING, never a lost publish
# ---------------------------------------------------------------------------


def _fail_directory_sync(monkeypatch, stage: str, code: int) -> None:
    """Fail the directory's open or fsync with ``code``; never the file's."""
    real_open, real_fsync = os.open, os.fsync

    def failing_open(p, flags, *args, **kw):
        if stage == "open" and flags & getattr(os, "O_DIRECTORY", 0):
            raise OSError(code, os.strerror(code))
        return real_open(p, flags, *args, **kw)

    def failing_fsync(fd):
        if stage == "fsync" and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(code, os.strerror(code))
        real_fsync(fd)

    monkeypatch.setattr(publisher.os, "open", failing_open)
    monkeypatch.setattr(publisher.os, "fsync", failing_fsync)


@pytest.mark.parametrize("stage", ["open", "fsync"])
@pytest.mark.parametrize("code", [errno.EINVAL, errno.EIO, errno.EACCES])
def test_a_directory_fsync_error_warns_and_the_publish_still_wins(
    tmp_path, monkeypatch, caplog, stage, code
):
    path = tmp_path / "secret"
    _fail_directory_sync(monkeypatch, stage, code)
    caplog.set_level(logging.WARNING)

    assert publish_secret_once(path, b"the-secret") is True

    assert path.read_bytes() == b"the-secret"
    warned = [w for w in _warnings(caplog) if str(tmp_path) in w]
    assert len(warned) == 1, caplog.text
    assert errno.errorcode[code] in warned[0], warned[0]
    assert "the-secret" not in caplog.text


def test_the_jwt_caller_keeps_its_secret_when_the_directory_fsync_fails(
    tmp_path, monkeypatch, caplog
):
    """Raising after the link would land in the JWT caller's ``except OSError``
    and set no secret, although the file it published is live."""
    path = tmp_path / ".jwt_secret"
    _fail_directory_sync(monkeypatch, "fsync", errno.EIO)
    caplog.set_level(logging.WARNING)

    secret = _read_jwt(path, monkeypatch)

    assert secret and secret == path.read_bytes()
    assert any("EIO" in w and str(tmp_path) in w for w in _warnings(caplog))


# ---------------------------------------------------------------------------
# 3. A crash's second name for the live secret is removed before the read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("symlinked", [False, True], ids=["plain", "symlinked"])
def test_a_leftover_link_to_the_live_secret_is_removed_before_the_read(
    tmp_path, monkeypatch, caplog, reader, symlinked
):
    target = tmp_path / "volume" / "secret"
    target.parent.mkdir()
    path = target
    if symlinked:
        path = tmp_path / "secret"
        path.symlink_to(target)
    secret = _read(reader, path, monkeypatch)
    leftover = target.with_name(target.name + LEFTOVER)
    os.link(target, leftover)  # the crash shape: linked, never unlinked
    caplog.set_level(logging.WARNING)

    assert _read(reader, path, monkeypatch) == secret

    assert not leftover.exists()
    assert target.read_bytes().strip() == secret
    assert os.stat(target).st_nlink == 1
    removed = [w for w in _warnings(caplog) if str(leftover) in w]
    assert len(removed) == 1 and "Removed" in removed[0], caplog.text
    assert secret.decode() not in caplog.text


# ---------------------------------------------------------------------------
# 4. Rotate before restart: the old secret's name is kept and named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader", READERS)
def test_a_leftover_from_before_a_rotation_is_kept_and_named_at_every_read(
    tmp_path, monkeypatch, caplog, reader
):
    """Crash leaves L on the old inode A; the operator deletes the secret to
    rotate it; a start publishes B (one name). L may be the only copy of A, so
    it is never deleted, and every later read names it."""
    path = tmp_path / "secret"
    old = _read(reader, path, monkeypatch)
    leftover = path.with_name(path.name + LEFTOVER)
    os.link(path, leftover)
    path.unlink()  # rotate
    new = _read(reader, path, monkeypatch)
    assert new != old and os.stat(path).st_nlink == 1
    caplog.set_level(logging.WARNING)

    for _ in range(2):  # restarts
        caplog.clear()
        assert _read(reader, path, monkeypatch) == new
        assert leftover.read_bytes().strip() == old
        kept = [w for w in _warnings(caplog) if str(leftover) in w]
        assert len(kept) == 1 and "Kept" in kept[0], caplog.text
        assert old.decode() not in caplog.text and new.decode() not in caplog.text


# ---------------------------------------------------------------------------
# 5. Must-not-change pins
# ---------------------------------------------------------------------------


def test_names_outside_the_publishers_shape_are_never_touched(tmp_path, caplog):
    path = tmp_path / "secret"
    path.write_bytes(b"the-secret")
    backup = path.with_name("secret.tmp.bak")
    os.link(path, backup)
    lock = path.with_name("secret.lock")
    lock.write_bytes(b"")
    caplog.set_level(logging.WARNING)

    publisher.prepare_secret_file_for_read(
        path, advice="advice", logger=logging.getLogger("t")
    )

    assert backup.read_bytes() == b"the-secret" and lock.exists()
    assert os.stat(path).st_nlink == 2
    warned = [w for w in _warnings(caplog) if "2 names" in w]
    assert len(warned) == 1 and str(path) in warned[0], caplog.text


def test_a_publisher_whose_temp_name_is_removed_after_its_link_still_wins(
    tmp_path, monkeypatch, caplog
):
    """A peer's read can land between our link and our temp unlink."""
    path = tmp_path / "secret"
    real_link = os.link

    def link_then_a_peer_reads(src, dst, **kw):
        real_link(src, dst, **kw)
        publisher.prepare_secret_file_for_read(
            dst, advice="advice", logger=logging.getLogger("peer")
        )

    monkeypatch.setattr(publisher.os, "link", link_then_a_peer_reads)
    caplog.set_level(logging.WARNING)

    assert publish_secret_once(path, b"the-secret") is True

    assert any("Removed" in w for w in _warnings(caplog)), caplog.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secret"]
    assert path.read_bytes() == b"the-secret"
