"""Publish and read a local secret file: whole, once, private to its owner.

Two secrets can live in local files: the standalone HS256 JWT secret
(``settings.resolve_local_jwt_secret``) and the redaction pseudonym key
(``infrastructure.security.pseudonym_key``). This module is the one way such a
file is published, and the one rule applied before one is read (#1788).

Two rules hold (#2324):

- A live secret is never given a name it does not keep. Wherever the
  filesystem accepts ``O_TMPFILE`` (Linux), it is written to an unnamed inode
  and linked into place once, so no other directory entry ever names it; a
  file that holds no secret is replaced by renaming a temp file, which moves
  its name. Elsewhere (other platforms, NFS, 9p, FUSE without tmpfiles) a
  named temp file is linked and then unlinked, and a kill between the two can
  still leave a second name for the secret.
- No process uses a secret before the directories that name it are durable:
  the publish, and every reader (the losers of a race included), fsyncs the
  secret's directory and each one above it on the same device, so those
  ``mkdir(parents=True)`` made too. That holds only where directories can be
  fsynced (not Windows).
"""

import errno
import logging
import os
import secrets
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

#: ``os.link`` failing with one of these means the filesystem has no hard
#: links. Only then is the secret published by ``os.replace`` under the lock;
#: any other failure of a named temp's link (EIO, ENOSPC, EDQUOT, ...)
#: propagates, and of the unnamed link falls back to a named temp.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS})

#: An ``O_TMPFILE`` open failing with one of these means this filesystem or
#: kernel has no unnamed files: the named temp file is used, silently.
_NO_TMPFILE = frozenset({errno.EOPNOTSUPP, errno.EISDIR, errno.EINVAL})

#: Where an open unnamed inode has a name to link from.
_PROC_FD = "/proc/self/fd"

#: A directory fsync failing with one of these means the filesystem does not
#: fsync directories, which is no news to an operator.
_NO_DIRECTORY_FSYNC = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


def publish_secret_once(
    path: Path,
    data: bytes,
    *,
    replace_if: Optional[Callable[[bytes], bool]] = None,
) -> bool:
    """Publish ``data`` at ``path`` iff nothing is there. True if we won.

    Written in full and fsynced before it has the name, which it gets by
    ``os.link`` (that fails if the destination exists) or, under the lock, by
    ``os.replace``: the name appears only once and only with complete content.
    The directories that name it are fsynced before this returns True.

    ``O_CREAT|O_EXCL`` on the real path is NOT sufficient on its own: it
    serializes the *create*, not the create-and-write. A second process that
    reads between the winner's create and its write sees zero bytes, and a
    crash in that window leaves an empty file that every later start reads as
    an empty secret — which is not a broken secret, it is NO secret, and an
    HMAC under it is publicly computable.

    An existing file is never replaced, unless the caller passes ``replace_if``
    and it returns True for the file's bytes: the caller's own rule for a file
    that holds no secret. A file published here never holds none, so that
    replacement cannot overwrite a racer's secret.

    A symlink is published through: the secret lands on its resolved target,
    so a link to a persistent volume keeps pointing at it.
    """
    # realpath, not Path.resolve(): a symlink loop then fails as an OSError.
    resolved = Path(os.path.realpath(path))
    # No name before resolving ('' is the cwd) or after ('/..', a link to '/').
    if not path.name or not resolved.name:
        raise IsADirectoryError(errno.EISDIR, "no file name", str(path))
    path = resolved
    parent_fd: Optional[int] = None
    if hasattr(os, "O_TMPFILE"):
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            if exc.errno != errno.ENOENT:  # a missing one fails the publish below
                _unnamed_failed(path, exc)
    try:
        linked, unnamed = _link_new(path, data, parent_fd)
        # A lost race's lock path skips an unnamed publish that already failed.
        lock_fd = parent_fd if unnamed else None
        if linked:
            published = True
        elif linked is None:
            # Filesystem without hardlink support (some network mounts, some
            # Docker bind mounts). ``os.replace`` is atomic but unconditional,
            # so it would clobber a secret another process just published. It
            # is serialized by an exclusive lock, and the publish decision is
            # re-made UNDER that lock.
            #
            # Claiming the real path with O_CREAT|O_EXCL and writing afterwards
            # is what this used to do, and it is the very create-then-write
            # window described above: a crash or ENOSPC between the two left a
            # permanent zero-byte file, and a concurrent reader could observe
            # it and refuse to boot.
            published = _publish_under_lock(path, data, replace_if, lock_fd)
        elif replace_if is None or not replace_if(path.read_bytes()):
            # Another process published first; its secret is the deployment's.
            return False
        else:
            published = _publish_under_lock(path, data, replace_if, lock_fd)
        if published:
            _fsync_directory_chain(path, parent_fd)
        return published
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _link_new(
    path: Path, data: bytes, parent_fd: Optional[int]
) -> Tuple[Optional[bool], bool]:
    """Link a new file holding ``data`` at ``path``: ``(linked, unnamed)``.

    ``linked`` is True if it landed, False if a file is already there, and None
    if the filesystem has no hard links. It is an unnamed inode, linked exactly
    once (a second link would be a second name), when ``parent_fd`` is given
    and the ``O_TMPFILE`` open and the ``/proc`` link work. Otherwise it is a
    named temp file, and ``unnamed`` is False: silently where this platform or
    filesystem has no unnamed files, after a WARNING on any other failure of
    that open or link, so a startup that worked before still does. A failure
    to write or fsync the content propagates.
    """
    fd = _unnamed_inode(path, data, parent_fd) if parent_fd is not None else None
    if fd is not None:
        try:
            # dst_dir_fd makes CPython call linkat(AT_SYMLINK_FOLLOW), which
            # links the inode; a bare os.link() links the /proc entry: EXDEV.
            source = f"{_PROC_FD}/{fd}"
            follow = {"dst_dir_fd": parent_fd, "follow_symlinks": True}
            return _link(source, path.name, **follow), True
        except OSError as exc:
            if exc.errno != errno.ENOENT or os.path.isdir(_PROC_FD):
                _unnamed_failed(path, exc)
        finally:
            with suppress(OSError):  # its content is fsynced; a link stands
                os.close(fd)
    with _named_temp(path, data) as tmp:
        return _link(tmp, path), False


def _link(src: str | Path, dst: str | Path, **kwargs) -> Optional[bool]:
    """``os.link``: True if linked, False if ``dst`` exists, None if no hard links."""
    try:
        os.link(src, dst, **kwargs)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in _NO_HARD_LINKS:
            return None
        raise


def _unnamed_inode(path: Path, data: bytes, parent_fd: int) -> Optional[int]:
    """An fd of a new unnamed inode holding ``data``, fsynced; None if none."""
    try:
        fd = os.open(".", os.O_TMPFILE | os.O_WRONLY, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno not in _NO_TMPFILE:
            _unnamed_failed(path, exc)
        return None
    try:
        _write_synced(fd, data)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unnamed_failed(path: Path, exc: OSError) -> None:
    logger.warning(
        "Could not publish the secret file %s as an unnamed file (%s); using a "
        "named temp file, which a kill mid-publish can leave beside it.",
        path,
        errno.errorcode.get(exc.errno or 0, exc),
    )


def _write_synced(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


@contextmanager
def _named_temp(path: Path, data: bytes) -> Iterator[Path]:
    """A private temp file beside ``path`` holding ``data``, fsynced; removed after."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            _write_synced(fd, data)
        finally:
            os.close(fd)
        yield tmp
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _publish_under_lock(
    path: Path,
    data: bytes,
    replace_if: Optional[Callable[[bytes], bool]],
    parent_fd: Optional[int],
) -> bool:
    """Publish ``data`` at ``path`` iff nothing is published yet. True if we won.

    An absent target is linked (``_link_new``: an unnamed inode unless
    ``parent_fd`` is None), so a racer linking without the lock is never
    overwritten; ``os.replace`` of a named temp file lands only on a
    filesystem without hard links, or over a file ``replace_if`` says holds no
    secret. A kill before that rename leaves a temp file that was never
    published, not a second name of a live secret.

    The lock file is never unlinked: removing it would let a later opener lock
    a fresh inode while a current holder still holds the old one, so the two
    would not exclude each other.

    Without ``fcntl`` (Windows) the same decision is made with no lock.
    Without flock, two processes publishing at once over a blank file, or over
    an absent one on a filesystem without hard links, can each install their
    own; a secret published before either process read the file is still never
    overwritten.
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]

    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # released on close
        # Re-read under the lock: whoever got here first has already
        # published, and the loser must adopt that secret rather than install
        # its own. Deciding before taking the lock is what makes the naive
        # version diverge.
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            linked, _ = _link_new(path, data, parent_fd)
            if linked is not None:
                return linked
        else:
            if replace_if is None or not replace_if(existing):
                return False
        with _named_temp(path, data) as tmp:
            os.replace(tmp, path)
        return True


def _fsync_directory_chain(path: Path, parent_fd: Optional[int] = None) -> None:
    """Fsync the directory holding ``path``, then each above it on its device.

    ``path`` is resolved. Stops at the device's root, or at the first
    failure. Nothing at ``path``, or no parent directory, has nothing to make
    durable. A failure on an ancestor, or a filesystem that does not fsync
    directories, is logged at DEBUG; any other failure on the parent itself
    logs one WARNING. Never raises.
    """
    if not hasattr(os, "O_DIRECTORY") or not os.path.exists(path):
        return
    device = None
    for directory in path.parents:
        fd = parent_fd if device is None else None
        try:
            st = os.stat(directory)
            if device is not None and st.st_dev != device:
                return
            if fd is None:
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                if fd != parent_fd:
                    os.close(fd)
        except OSError as exc:
            if device is not None or exc.errno in _NO_DIRECTORY_FSYNC:
                logger.debug("Did not fsync directory %s: %s", directory, exc)
            elif exc.errno != errno.ENOENT:
                logger.warning(
                    "Could not fsync directory %s (%s), so the name of the "
                    "secret file %s may not survive a power loss.",
                    directory,
                    errno.errorcode.get(exc.errno or 0, exc),
                    path.name,
                )
            return
        device = st.st_dev


def prepare_secret_file_for_read(
    path: Path, *, advice: str, logger: logging.Logger
) -> None:
    """Make an existing secret file durable and private before it is read.

    First the directories that name it are fsynced (``_fsync_directory_chain``
    on its resolved path), so no process uses a secret whose name a power loss
    could still take away. Then a regular file that group or others can read
    or write, which may already have been read or replaced, is chmodded to
    0600 and a WARNING names the file, the mode it had, the mode it has now,
    and ``advice``: the secret it holds and how to rotate it. Anything else at
    the path (nothing, a directory, a FIFO) is left alone for the read that
    follows to report, and so is every file where POSIX modes do not apply
    (Windows). Never raises: a secret that may have leaked is still this
    deployment's secret until the operator rotates it.
    """
    _fsync_directory_chain(Path(os.path.realpath(path)))
    if os.name == "nt":
        return
    try:
        st = path.stat()
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISREG(st.st_mode) or not mode & 0o077:
        return
    try:
        path.chmod(0o600)
        now = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        outcome = f"it could not be made 0600 ({exc})"
    else:
        outcome = (
            "it is now 0600"
            if now == 0o600
            else f"chmod reported success but the mode is still {now:04o}, so "
            "this filesystem does not keep file modes"
        )
    logger.warning(
        "Secret file %s was readable or writable by group or others (mode %04o); "
        "%s. Anyone who could read it may hold %s",
        path,
        mode,
        outcome,
        advice,
    )
