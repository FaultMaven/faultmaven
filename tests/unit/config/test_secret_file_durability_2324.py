"""A published secret has one name, and that name is durable before use (#2324).

Wherever the filesystem accepts ``O_TMPFILE``, ``publish_secret_once`` links
an unnamed inode, so a kill mid-publish cannot leave a second name for the
live secret; elsewhere, or after a WARNING that the unnamed publish failed, it
falls back to a named temp file. The secret's
directory and those above it on its device are fsynced after a publish and
before every read, a lost race's included.
"""

import errno
import logging
import os
import secrets
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import faultmaven.config.secret_file as publisher
import faultmaven.infrastructure.security.pseudonym_key as pseudonym
from faultmaven.config import settings as S
from faultmaven.config.secret_file import publish_secret_once
from tests.utils import delenv_every_spelling

pytestmark = pytest.mark.unit

LOGGER = "faultmaven.config.secret_file"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No ambient setting steers the JWT caller, and no cached key the other."""
    delenv_every_spelling(
        monkeypatch,
        *("DATABASE_URL", "DATABASE", "JWT_SECRET_KEY", "JWT_SECRET_FILE"),
        *("JWT_PRIVATE_KEY", "AUTH_MODE", "OAUTH_ENABLED"),
    )
    monkeypatch.setenv("JWT_SECRET_KEY", "restored-at-teardown")
    monkeypatch.delenv("JWT_SECRET_KEY")
    monkeypatch.setenv("AUTH_MODE", "local")
    pseudonym.reset_pseudonym_key_cache()
    yield
    pseudonym.reset_pseudonym_key_cache()


def _ident(path) -> tuple:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _require_unnamed_files(directory: Path) -> None:
    try:
        os.close(os.open(directory, os.O_TMPFILE | os.O_WRONLY, 0o600))
    except (AttributeError, OSError) as exc:
        pytest.skip(f"this tmp filesystem refuses O_TMPFILE ({exc!r})")


def _record(monkeypatch) -> list:
    """Log every file fsync, directory fsync, link, unlink and replace, in order."""
    events: list = []
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink
    real_replace = os.replace

    def fsync(fd):
        st = os.fstat(fd)
        kind = "dir-fsync" if stat.S_ISDIR(st.st_mode) else "file-fsync"
        events.append((kind, (st.st_dev, st.st_ino)))
        return real_fsync(fd)

    def link(*args, **kwargs):
        real_link(*args, **kwargs)
        events.append(("link", None))

    def unlink(*args, **kwargs):
        real_unlink(*args, **kwargs)
        events.append(("unlink", None))

    def replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        events.append(("replace", None))

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "replace", replace)
    return events


def _raising(code: int):
    def raises(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    return raises


def _dirs_fsynced(events) -> list:
    return [ident for kind, ident in events if kind == "dir-fsync"]


def _warnings(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _jwt(path: Path) -> bytes:
    settings = S.FaultMavenSettings()
    S.resolve_local_jwt_secret(settings)
    return settings.security.jwt_secret_key.get_secret_value().encode()


def _pseudonym(path: Path) -> bytes:
    settings = MagicMock()
    settings.protection.pseudonym_key = None
    settings.protection.pseudonym_key_path = str(path)
    settings.is_cloud = False
    return pseudonym.resolve_pseudonym_key(settings)


CALLERS = {"jwt": (S, _jwt), "pseudonym": (pseudonym, _pseudonym)}


def test_a_kill_after_the_link_leaves_no_second_name(tmp_path, monkeypatch, caplog):
    """A kill between the link and the temp file's unlink left a hard link to
    the live secret beside it. The unnamed inode has no other name to leave,
    and nothing in the directory but the directory itself is ever opened."""
    _require_unnamed_files(tmp_path)
    path = tmp_path / "secret"
    opened, real_open = [], os.open

    def spy_open(file, flags, *args, dir_fd=None, **kwargs):
        base = os.readlink(f"/proc/self/fd/{dir_fd}") if dir_fd is not None else ""
        opened.append(os.path.normpath(os.path.join(base, file)))
        return real_open(file, flags, *args, dir_fd=dir_fd, **kwargs)

    class Killed(BaseException):
        pass

    def killed(*args, **kwargs):
        raise Killed

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "unlink", killed)
    try:
        publish_secret_once(path, b"the-secret")
    except Killed:
        pass

    assert os.listdir(tmp_path) == ["secret"]
    assert path.stat().st_nlink == 1
    assert path.read_bytes() == b"the-secret"
    directory = os.path.realpath(tmp_path)
    inside = {p for p in opened if p == directory or p.startswith(directory + "/")}
    assert inside == {directory}, opened
    assert not _warnings(caplog)


@pytest.mark.parametrize("unnamed", [True, False], ids=["unnamed", "named-temp"])
def test_the_publish_fsyncs_its_directories_after_the_name_lands(
    tmp_path, monkeypatch, unnamed
):
    """The resolved parent and the ancestors ``mkdir(parents=True)`` made are
    fsynced, after the content is fsynced and after every link and unlink."""
    if unnamed:
        _require_unnamed_files(tmp_path)
    else:
        monkeypatch.delattr(os, "O_TMPFILE", raising=False)
    path = tmp_path / "a" / "b" / "secret"
    path.parent.mkdir(parents=True)
    events = _record(monkeypatch)

    assert publish_secret_once(path, b"the-secret")

    kinds = [kind for kind, _ in events]
    first_dir = kinds.index("dir-fsync")
    assert all(k == "dir-fsync" for k in kinds[first_dir:]), kinds
    assert kinds.index("link") < first_dir
    assert events.index(("file-fsync", _ident(path))) < kinds.index("link")
    assert ("unlink" in kinds) is not unnamed, kinds
    fsynced = _dirs_fsynced(events)
    for directory in (path.parent, tmp_path / "a", tmp_path):
        assert _ident(directory) in fsynced, directory


@pytest.mark.parametrize("lock_path", ["blank-file", "no-hard-links"])
def test_a_publish_under_the_lock_fsyncs_its_directories_after_the_rename(
    tmp_path, monkeypatch, lock_path
):
    path = tmp_path / "d" / "secret"
    path.parent.mkdir()
    if lock_path == "blank-file":
        path.write_bytes(b"")
    events = _record(monkeypatch)
    if lock_path == "no-hard-links":
        monkeypatch.setattr(os, "link", _raising(errno.EPERM))

    assert publish_secret_once(path, b"the-secret", replace_if=lambda raw: not raw)

    assert path.read_bytes() == b"the-secret"
    after = _dirs_fsynced(events[events.index(("replace", None)) :])
    assert _ident(path.parent) in after and _ident(tmp_path) in after, events


def test_the_walk_stops_at_the_device_boundary(tmp_path, monkeypatch):
    path = tmp_path / "a" / "b" / "secret"
    path.parent.mkdir(parents=True)
    real_stat = os.stat

    def another_device_above_b(target, *args, **kwargs):
        st = real_stat(target, *args, **kwargs)
        if os.fspath(target) != str(tmp_path / "a"):
            return st
        fields = list(st)
        fields[stat.ST_DEV] += 1
        return os.stat_result(fields)

    events = _record(monkeypatch)
    monkeypatch.setattr(os, "stat", another_device_above_b)
    assert publish_secret_once(path, b"the-secret")
    monkeypatch.setattr(os, "stat", real_stat)

    fsynced = _dirs_fsynced(events)
    assert fsynced == [_ident(path.parent)], fsynced


def test_a_symlinked_secret_fsyncs_its_targets_directory(tmp_path, monkeypatch):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    link = tmp_path / "x" / "secret"
    link.symlink_to(tmp_path / "y" / "secret")
    events = _record(monkeypatch)

    assert publish_secret_once(link, b"the-secret")

    fsynced = _dirs_fsynced(events)
    assert _ident(tmp_path / "y") in fsynced
    assert _ident(tmp_path / "x") not in fsynced


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("found", ["lost-race", "existing", "symlinked"])
def test_every_read_fsyncs_the_secrets_directory_first(
    tmp_path, monkeypatch, caller, found
):
    """A process that loses the race, or finds the file, uses a secret whose
    name a power loss could still take; its read fsyncs the directory first.
    The winner's own fsync is taken out: the publish only writes the file."""
    module, resolve = CALLERS[caller]
    target = tmp_path / "data" / "secret"
    path = tmp_path / "elsewhere" / "secret" if found == "symlinked" else target
    monkeypatch.setenv("JWT_SECRET_FILE", str(path))
    winners = secrets.token_hex(32).encode()
    events = _record(monkeypatch)

    def another_process_won(target, data, **kwargs):
        target.write_bytes(winners)
        events.append(("published", None))
        return False

    if found != "lost-race":
        target.parent.mkdir()
        target.write_bytes(winners)
        events.append(("published", None))
    if found == "symlinked":
        path.parent.mkdir()
        path.symlink_to(target)
    monkeypatch.setattr(module, "publish_secret_once", another_process_won)

    assert resolve(path) == winners
    after = _dirs_fsynced(events[events.index(("published", None)) :])
    assert _ident(target.parent) in after and _ident(tmp_path) in after, events
    if found == "symlinked":
        assert _ident(path.parent) not in after, events


@pytest.mark.parametrize(
    "fallback", ["no-O_TMPFILE", "EOPNOTSUPP", "EISDIR", "EINVAL", "no-proc"]
)
def test_a_filesystem_without_unnamed_files_publishes_silently(
    tmp_path, monkeypatch, caplog, fallback
):
    path = tmp_path / "secret"
    if fallback == "no-O_TMPFILE":
        monkeypatch.delattr(os, "O_TMPFILE", raising=False)
    elif fallback == "no-proc":
        _require_unnamed_files(tmp_path)
        monkeypatch.setattr(publisher, "_PROC_FD", str(tmp_path / "no-proc"))
    else:
        if not hasattr(os, "O_TMPFILE"):
            pytest.skip("this platform has no O_TMPFILE")
        refused, real_open = getattr(errno, fallback), os.open

        def no_tmpfile(file, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(refused, os.strerror(refused))
            return real_open(file, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", no_tmpfile)

    assert publish_secret_once(path, b"the-secret")

    assert path.read_bytes() == b"the-secret"
    assert os.listdir(tmp_path) == ["secret"]
    assert not _warnings(caplog)


@pytest.mark.parametrize("wrong", ["closed-fd", "O_EXCL", "bare-link", "EACCES"])
def test_a_broken_unnamed_publish_warns_and_still_publishes(
    tmp_path, monkeypatch, caplog, wrong
):
    """Each is a wrong way to link the inode: linking an fd already closed, an
    inode opened ``O_EXCL`` (never linkable), or a bare ``os.link`` of the
    /proc entry (EXDEV); or an open refused for a reason that is not "no
    unnamed files here". None may pass silently, nor stop a startup."""
    _require_unnamed_files(tmp_path)
    path = tmp_path / "secret"
    real_open, real_link = os.open, os.link

    def open_excl(file, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            if wrong == "EACCES":
                raise OSError(errno.EACCES, os.strerror(errno.EACCES))
            flags |= os.O_EXCL
        return real_open(file, flags, *args, **kwargs)

    def link(src, dst, **kwargs):
        if not str(src).startswith("/proc/"):
            return real_link(src, dst, **kwargs)
        if wrong == "closed-fd":
            return real_link("/proc/self/fd/999999", dst, **kwargs)
        return real_link(src, path)

    if wrong in ("O_EXCL", "EACCES"):
        monkeypatch.setattr(os, "open", open_excl)
    else:
        monkeypatch.setattr(os, "link", link)

    assert publish_secret_once(path, b"the-secret")

    assert path.read_bytes() == b"the-secret"
    warned = _warnings(caplog)
    expected = {"bare-link": "EXDEV", "EACCES": "EACCES"}.get(wrong, "ENOENT")
    assert len(warned) == 1 and expected in warned[0], warned


def test_without_hard_links_the_lock_path_renames_a_temp_file(
    tmp_path, monkeypatch, caplog
):
    """EPERM on the unnamed link is "no hard links", not a broken publish."""
    path = tmp_path / "secret"
    renamed, real_replace = [], os.replace

    def replace(src, dst):
        renamed.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "link", _raising(errno.EPERM))
    monkeypatch.setattr(os, "replace", replace)

    assert publish_secret_once(path, b"the-secret")

    assert path.read_bytes() == b"the-secret"
    assert sorted(os.listdir(tmp_path)) == ["secret", "secret.lock"]
    assert len(renamed) == 1 and renamed[0].startswith("secret.tmp."), renamed
    assert not _warnings(caplog)


def test_a_lost_race_does_not_retry_an_unnamed_publish_that_failed(
    tmp_path, monkeypatch, caplog
):
    """The blank file is gone once the lock is held. The first attempt's
    unnamed open failed, so the lock path goes straight to a named temp: one
    WARNING and one ``O_TMPFILE`` open, not two."""
    _require_unnamed_files(tmp_path)
    path = tmp_path / "secret"
    path.write_bytes(b"")
    tmpfile_opens, real_open = [], os.open

    def eio_on_tmpfile(file, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            tmpfile_opens.append(file)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_open(file, flags, *args, **kwargs)

    def blank_until_deleted(raw):
        path.unlink()
        return not raw

    monkeypatch.setattr(os, "open", eio_on_tmpfile)

    assert publish_secret_once(path, b"the-secret", replace_if=blank_until_deleted)

    assert path.read_bytes() == b"the-secret"
    assert len(tmpfile_opens) == 1 and len(_warnings(caplog)) == 1, caplog.text


def test_a_close_error_after_the_link_still_publishes(tmp_path, monkeypatch):
    _require_unnamed_files(tmp_path)
    path = tmp_path / "secret"
    events = _record(monkeypatch)
    linked_fds, raised, real_link, real_close = [], [], os.link, os.close

    def link(src, dst, **kwargs):
        real_link(src, dst, **kwargs)
        if str(src).startswith("/proc/"):
            linked_fds.append(int(str(src).rsplit("/", 1)[1]))

    def close(fd):
        real_close(fd)
        if fd in linked_fds and not raised:
            raised.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "close", close)

    assert publish_secret_once(path, b"the-secret")

    assert raised and _ident(tmp_path) in _dirs_fsynced(events)


@pytest.mark.parametrize("unnamed", [True, False], ids=["unnamed", "named-temp"])
def test_a_content_fsync_error_propagates_and_publishes_nothing(
    tmp_path, monkeypatch, caplog, unnamed
):
    if not unnamed:
        monkeypatch.delattr(os, "O_TMPFILE", raising=False)
    path = tmp_path / "secret"
    real_fsync = os.fsync

    def eio_on_files(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", eio_on_files)

    with pytest.raises(OSError) as failed:
        publish_secret_once(path, b"the-secret")

    assert failed.value.errno == errno.EIO
    assert os.listdir(tmp_path) == []
    assert not _warnings(caplog)


def test_a_temp_name_taken_is_an_error_not_a_lost_race(tmp_path, monkeypatch):
    """Only the link's EEXIST means another process published first."""
    monkeypatch.delattr(os, "O_TMPFILE", raising=False)
    path = tmp_path / "secret"
    real_open = os.open

    def temp_name_taken(file, flags, *args, **kwargs):
        if ".tmp." in os.fspath(file):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", temp_name_taken)

    with pytest.raises(FileExistsError):
        publish_secret_once(path, b"the-secret")
    assert not path.exists()


def test_an_unreadable_parent_warns_before_the_named_temp(tmp_path, caplog):
    """A directory that can be written and searched but not read refuses the
    fd the unnamed publish links through. That is no silent fallback."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    path = tmp_path / "d" / "secret"
    path.parent.mkdir(mode=0o300)
    try:
        assert publish_secret_once(path, b"the-secret")
    finally:
        path.parent.chmod(0o700)

    assert path.read_bytes() == b"the-secret"
    assert any("unnamed file (EACCES)" in w for w in _warnings(caplog)), caplog.text


def test_the_lock_paths_absent_target_opens_no_named_temp(tmp_path, monkeypatch):
    """The blank file to be replaced is gone by the time the lock is held:
    the absent target is linked as an unnamed inode, never a named temp."""
    _require_unnamed_files(tmp_path)
    path = tmp_path / "secret"
    path.write_bytes(b"")
    opened, real_open = [], os.open

    def spy_open(file, flags, *args, **kwargs):
        opened.append(os.fspath(file))
        return real_open(file, flags, *args, **kwargs)

    def blank_until_deleted(raw):
        path.unlink()
        return not raw

    monkeypatch.setattr(os, "open", spy_open)

    assert publish_secret_once(path, b"the-secret", replace_if=blank_until_deleted)

    assert path.read_bytes() == b"the-secret"
    assert not [name for name in opened if ".tmp." in name], opened
    assert sorted(os.listdir(tmp_path)) == ["secret", "secret.lock"]


def test_a_blank_jwt_file_is_replaced_by_rename_and_never_tightened(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / ".jwt_secret"
    path.write_bytes(b"  \n")
    path.chmod(0o644)
    monkeypatch.setenv("JWT_SECRET_FILE", str(path))

    secret = _jwt(path)

    assert path.read_bytes() == secret
    assert path.stat().st_nlink == 1
    assert (path.stat().st_mode & 0o777) == 0o600
    assert sorted(os.listdir(tmp_path)) == [".jwt_secret", ".jwt_secret.lock"]
    assert not [w for w in _warnings(caplog) if "readable or writable" in w]


def test_a_first_boot_with_no_data_directory_logs_no_warning(tmp_path, caplog):
    path = tmp_path / "data" / ".redaction_key"

    key = _pseudonym(path)

    assert path.read_bytes() == key
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    "where, call, code, levels",
    [
        # the unnamed publish's fallback, then the chain: two facts
        ("parent", "open", errno.EACCES, [logging.WARNING, logging.WARNING]),
        ("parent", "open", errno.ENOENT, []),
        ("parent", "fsync", errno.EIO, [logging.WARNING]),
        ("parent", "fsync", errno.EINVAL, [logging.DEBUG]),
        ("ancestor", "open", errno.EACCES, [logging.DEBUG]),
        ("ancestor", "fsync", errno.EIO, [logging.DEBUG]),
    ],
)
def test_a_directory_that_cannot_be_fsynced_is_logged_never_raised(
    tmp_path, monkeypatch, caplog, where, call, code, levels
):
    """Only a real failure on the secret's own directory warns: an ancestor
    the process cannot read, or a filesystem that does not fsync directories,
    would otherwise warn on every start."""
    path = tmp_path / "d" / "secret"
    path.parent.mkdir()
    failing = path.parent if where == "parent" else tmp_path
    real_open, real_fsync = os.open, os.fsync

    def failing_open(file, flags, *args, **kwargs):
        if os.fspath(file) == str(failing) and flags & os.O_DIRECTORY:
            raise OSError(code, os.strerror(code))
        return real_open(file, flags, *args, **kwargs)

    def failing_fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == _ident(failing):
            raise OSError(code, os.strerror(code))
        return real_fsync(fd)

    monkeypatch.setattr(os, call, failing_open if call == "open" else failing_fsync)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    assert publish_secret_once(path, b"the-secret")

    assert path.read_bytes() == b"the-secret"
    named = [
        r for r in caplog.records if r.name == LOGGER and str(failing) in r.getMessage()
    ]
    assert [r.levelno for r in named] == levels, caplog.text
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and r not in named
    ]


@pytest.mark.parametrize("caller", CALLERS)
def test_a_caller_still_gets_its_secret_when_the_directory_fsync_fails(
    tmp_path, monkeypatch, caplog, caller
):
    path = tmp_path / "data" / "secret"
    monkeypatch.setenv("JWT_SECRET_FILE", str(path))
    real_fsync = os.fsync

    def eio_on_directories(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", eio_on_directories)

    assert CALLERS[caller][1](path) == path.read_bytes()
    assert any(str(path.parent) in w for w in _warnings(caplog)), caplog.text
