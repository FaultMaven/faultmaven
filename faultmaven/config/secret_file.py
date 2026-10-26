"""Publish and read a local secret file: whole, once, private to its owner.

Two secrets can live in local files: the standalone HS256 JWT secret
(``settings.resolve_local_jwt_secret``) and the redaction pseudonym key
(``infrastructure.security.pseudonym_key``). This module is the one way such a
file is published, and the one rule applied before one is read (#1788).
"""

import logging
import os
import secrets
import stat
from pathlib import Path


def publish_secret_once(path: Path, data: bytes) -> bool:
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

    An existing file that is empty or whitespace-only is not a published
    secret: a file published here never appears empty. It is replaced through
    the locked path, which re-decides under the lock, so racers that all found
    it empty still converge on one secret.
    """
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
            if path.read_bytes().strip():
                return False
            return _publish_under_lock(path, tmp)
        except OSError:
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
            return _publish_under_lock(path, tmp)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _publish_under_lock(path: Path, tmp: Path) -> bool:
    """Move ``tmp`` onto ``path`` iff nothing is published yet. True if we won.

    The lock file is never unlinked: removing it would let a later opener lock
    a fresh inode while a current holder still holds the old one, so the two
    would not exclude each other.
    """
    import fcntl

    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Re-read under the lock: whoever got here first has already
            # published, and the loser must adopt that secret rather than
            # install its own. Deciding before taking the lock is what makes
            # the naive version diverge.
            try:
                if path.read_bytes().strip():
                    return False
            except FileNotFoundError:
                pass
            os.replace(tmp, path)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def tighten_secret_file_mode(path: Path, *, what: str, logger: logging.Logger) -> None:
    """Make an existing secret file private to its owner before it is read.

    A file that group or others can read may already have been read, so it is
    chmodded to 0600 and a WARNING names the file, the mode it had, and
    ``what``: the secret it holds and how to rotate it. A failed chmod is a
    WARNING too. Never raises: the read that follows reports a missing or
    unreadable file, and a secret that may have leaked is still this
    deployment's secret until the operator rotates it.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if not mode & 0o077:
        return
    try:
        path.chmod(0o600)
        outcome = "it is now 0600"
    except OSError as exc:
        outcome = f"it could not be made 0600 ({exc})"
    logger.warning(
        "Secret file %s was readable by group or others (mode %04o); %s. Anyone "
        "who could read it may hold %s",
        path,
        mode,
        outcome,
        what,
    )
