"""Publish and read a local secret file: whole, once, private to its owner.

Two secrets can live in local files: the standalone HS256 JWT secret
(``settings.resolve_local_jwt_secret``) and the redaction pseudonym key
(``infrastructure.security.pseudonym_key``). This module is the one way such a
file is published, and the one rule applied before one is read (#1788).
"""

import errno
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Callable, Optional

#: ``os.link`` failing with one of these means the filesystem has no hard
#: links. Only then is the secret published by ``os.replace`` under the lock;
#: any other failure (EIO, ENOSPC, EDQUOT, ...) propagates.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS})


def publish_secret_once(
    path: Path,
    data: bytes,
    *,
    replace_if: Optional[Callable[[bytes], bool]] = None,
) -> bool:
    """Publish ``data`` at ``path`` iff nothing is there. True if we won.

    Written to a private temp file, flushed and fsynced, and only then linked
    into place — ``os.link`` fails if the destination exists, so the name
    appears only once and only with complete content.

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
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            # Another process published first; its secret is the deployment's.
            if replace_if is None or not replace_if(path.read_bytes()):
                return False
            return _publish_under_lock(path, tmp, replace_if)
        except OSError as exc:
            if exc.errno not in _NO_HARD_LINKS:
                raise
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
            return _publish_under_lock(path, tmp, replace_if)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _publish_under_lock(
    path: Path, tmp: Path, replace_if: Optional[Callable[[bytes], bool]]
) -> bool:
    """Publish ``tmp`` at ``path`` iff nothing is published yet. True if we won.

    An absent target is published with ``os.link`` where it works, so a racer
    linking without the lock is never overwritten; ``os.replace`` lands only on
    a filesystem without hard links, or over a file ``replace_if`` says holds
    no secret.

    The lock file is never unlinked: removing it would let a later opener lock
    a fresh inode while a current holder still holds the old one, so the two
    would not exclude each other.

    Without ``fcntl`` (Windows) the same decision is made with no lock. Two
    processes publishing over a BLANK file at once can then each install their
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
            try:
                os.link(tmp, path)
                return True
            except FileExistsError:
                return False
            except OSError as exc:
                if exc.errno not in _NO_HARD_LINKS:
                    raise
        else:
            if replace_if is None or not replace_if(existing):
                return False
        os.replace(tmp, path)
        return True


def tighten_secret_file_mode(path: Path, *, what: str, logger: logging.Logger) -> None:
    """Make an existing secret file private to its owner before it is read.

    A regular file that group or others can read or write may already have
    been read or replaced, so it is chmodded to 0600 and a WARNING names the
    file, the mode it had, the mode it has now, and ``what``: the secret it
    holds and how to rotate it. Anything else at the path (nothing, a
    directory, a FIFO) is left alone for the read that follows to report, and
    so is every file where POSIX modes do not apply (Windows). Never raises: a
    secret that may have leaked is still this deployment's secret until the
    operator rotates it.
    """
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
        what,
    )
