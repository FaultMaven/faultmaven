"""Deployment-scoped key for redaction pseudonyms.

Redaction replaces each sensitive value with ``<TYPE_digest>``. That digest has
to satisfy two requirements that pull against each other:

1. It must not be derivable from the value by anyone holding redacted output.
   An unkeyed digest of an SSN, a phone number or an internal IP is a
   commitment to the plaintext — those spaces run 10^7–10^10 candidates, all
   trivially enumerable offline (#971).
2. The same value must produce the same placeholder across *separately
   sanitized* artifacts. Evidence files are redacted one at a time, each with
   its own registry, and the results are persisted; the KB is redacted at
   ingestion; prompts are redacted per turn. If one host reads as two
   placeholders across those, the investigation loses the co-reference it
   exists to find — and an LLM told about two hosts can conclude something
   false about either.

Only a *keyed* deterministic function satisfies both. Random per-registry
tokens satisfy (1) and break (2); an unkeyed hash does the reverse. So the
digest is ``HMAC-SHA256(deployment key, value)`` and this module owns that key.

The key is deployment-wide, never per-tenant: global KB content is shared
across tenants, so a per-tenant key would break co-reference between a shared
runbook and a tenant's own case. Cross-tenant correlation is bounded by RLS,
which stops one tenant from seeing another's redacted output at all.

Where the key comes from:

* ``REDACTION_PSEUDONYM_KEY`` if set — the expected configuration in cloud,
  supplied as a k8s Secret exactly like the JWT keys.
* Otherwise, in **standalone** only, a key generated once and persisted beside
  the deployment's own data. Self-hosted single-process deployments have no
  secrets manager, and demanding one to turn on redaction would just push
  operators to leave it off.
* In **cloud** an unset key is refused rather than generated. Replicas do not
  share a data volume, so each pod would mint a different key and silently
  produce a different placeholder for the same host — reintroducing the exact
  defect this design exists to prevent, in the deployment where it is hardest
  to notice.

Deliberately kept out of the stores that hold redacted data or the mapping back
from it. ``CaseRedactionContext`` persists the placeholder→plaintext registry in
**Redis** (``redaction:{case_id}``), and the redacted artifacts themselves sit
in the application database; putting the key in either would hand a single
compromise both halves.

**Every path here fails closed.** A weak or empty key is worse than no key at
all, because ``HMAC-SHA256("", value)`` is a fixed publicly-computable function
of the value — the #971 defect exactly, but silent. So the resolved key is
validated before it is ever returned, and the file is published atomically so
no reader can observe a half-written one.
"""

import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class PseudonymKeyUnavailableError(RuntimeError):
    """Raised when no usable pseudonym key is configured and none may be made.

    Covers both "cloud, nothing configured" — generating one per pod would give
    each replica a different placeholder for the same value — and "what we
    found is too weak to key an HMAC with".
    """


#: Minimum length of an operator-supplied key, after stripping. Not a strength
#: model, just a floor that rejects the mistakes that actually happen: a blank
#: or quoted-empty YAML value, a mis-templated Secret that renders one
#: character. A generated key is 64 hex chars.
MIN_KEY_LENGTH = 16

#: Resolved keys by source, so every ``DataSanitizer`` in a process shares one
#: read rather than re-hitting the filesystem per instance.
_CACHE: Dict[str, bytes] = {}


def _clean(raw: bytes) -> bytes:
    """Strip surrounding whitespace from a key.

    Applied to BOTH sources. k8s Secrets and YAML block scalars routinely
    append a newline, and `base64 <<< "key"` encodes one; without this the same
    logical secret supplied as a file and as an env var would key two different
    HMACs and silently produce different placeholders for the same value.
    """
    return raw.strip()


def _write_atomically(path: Path, key: bytes) -> bool:
    """Publish ``key`` at ``path`` iff nothing is there. True if we won.

    Written to a private temp file, flushed and fsynced, and only then linked
    into place — ``os.link`` fails if the destination exists, so the name
    appears only once and only with complete content.

    ``O_CREAT|O_EXCL`` on the real path is NOT sufficient on its own: it
    serializes the *create*, not the create-and-write. A second process that
    reads between the winner's create and its write sees zero bytes, and a
    crash in that window leaves an empty file that every later start reads as
    an empty key — which is not a broken key, it is NO key, and HMAC under it
    is publicly computable.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            # Another process published first; its key is the deployment's.
            return False
        except OSError:
            # Filesystem without hardlink support (some network mounts, some
            # Docker bind mounts). ``os.replace`` is atomic but unconditional,
            # so it would clobber a key another process just published. It is
            # serialized by an exclusive lock, and the publish decision is
            # re-made UNDER that lock.
            #
            # Claiming the real path with O_CREAT|O_EXCL and writing afterwards
            # is what this used to do, and it is the very create-then-write
            # window described above: a crash or ENOSPC between the two left a
            # permanent zero-byte key file, and a concurrent reader could
            # observe it and refuse to boot.
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
            # published, and the loser must adopt that key rather than install
            # its own. Deciding before taking the lock is what makes the naive
            # version diverge.
            try:
                if path.read_bytes().strip():
                    return False
            except FileNotFoundError:
                pass
            os.replace(tmp, path)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load_or_create(path: Path) -> bytes:
    """Return the key at ``path``, creating it on first use.

    A file that exists but is empty or too short is REFUSED, not silently
    replaced.

    Honouring it would hand every later start an unkeyed digest. Replacing it
    is worse than it looks: a corrupt file means the previous key is *gone*, so
    any replacement — however carefully synchronized between the processes
    starting right now — disagrees with every placeholder already written into
    stored evidence, KB content and transcripts. The same host then reads as
    one placeholder in an old artifact and another in the next turn, which is
    the false-conclusion hazard this module exists to prevent. No locking
    protocol can repair that, because the information needed to do so (the old
    key) no longer exists. Regenerating is only safe when nothing was ever
    redacted under the lost key, and the file's corpse cannot say whether that
    is true.

    So the failure is loud and the operator decides. The cost is real: this
    refusal stops the boot even for a deployment with redaction switched off.
    """
    try:
        existing = _clean(path.read_bytes())
    except FileNotFoundError:
        existing = None

    if existing is not None:
        if len(existing) >= MIN_KEY_LENGTH:
            return existing
        raise PseudonymKeyUnavailableError(
            f"The pseudonym key file {path} exists but holds "
            f"{len(existing)} usable bytes, which is not a usable key — "
            "redaction under it would be publicly recomputable from the value "
            "it hides, so startup stops here.\n"
            "Likely causes: an interrupted restore or backup that truncated "
            "the file, a disk-full event, or the file having been created or "
            "emptied by hand.\n"
            "To recover, either set REDACTION_PSEUDONYM_KEY to the key this "
            "deployment used before, or delete the file to have a new one "
            "generated — then restart ALL workers, not just one, or they will "
            "hold different keys.\n"
            "Deleting is not free: redaction placeholders already stored in "
            "evidence, knowledge-base content and transcripts were computed "
            "with the lost key, and a new key will not reproduce them. The "
            "same host will read as one placeholder in that older material and "
            "a different one from now on."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_hex(32).encode()
    if _write_atomically(path, candidate):
        return candidate

    # Lost the race. The winner linked complete content into place, so this
    # read cannot see a partial file.
    published = _clean(path.read_bytes())
    if len(published) < MIN_KEY_LENGTH:
        raise PseudonymKeyUnavailableError(
            f"The pseudonym key at {path} was published empty or too short. "
            "Delete it and restart to have a new one generated."
        )
    return published


def resolve_pseudonym_key(settings) -> bytes:
    """Return this deployment's redaction pseudonym key.

    Raises:
        PseudonymKeyUnavailableError: no key is configured and none may be
            generated (cloud), or the key that was found is too weak to use.
    """
    configured = settings.protection.pseudonym_key
    if configured is not None:
        # SecretStr so the key never prints in a settings dump, a repr or a
        # traceback; unwrapped only here, at the one place needing the bytes.
        raw = (
            configured.get_secret_value()
            if hasattr(configured, "get_secret_value")
            else configured
        )
        key = _clean(raw.encode() if isinstance(raw, str) else raw)
        if key:
            if len(key) < MIN_KEY_LENGTH:
                raise PseudonymKeyUnavailableError(
                    f"REDACTION_PSEUDONYM_KEY is only {len(key)} characters. A "
                    "key this short is brute-forceable, which would leave the "
                    "pseudonyms as guessable as the unkeyed digest they "
                    f"replaced. Use at least {MIN_KEY_LENGTH}."
                )
            # NOT cached. The cache exists to avoid re-reading the key FILE;
            # a configured key is already in memory and validating it is a
            # strip and a length check. Caching it would park the secret in a
            # module global — as a dict key AND as its value — for anything
            # that walks globals, a debugger or an error reporter, which is
            # the exposure SecretStr is here to prevent. Not caching also
            # means a rotated env var takes effect immediately rather than
            # being served stale.
            return key
        # An env var set to blank is "unset", not "the key is empty" —
        # otherwise every deployment that exported it blank would share one
        # publicly-computable key. Fall through to the unset handling below.

    if settings.is_cloud:
        raise PseudonymKeyUnavailableError(
            "REDACTION_PSEUDONYM_KEY is not set. Cloud replicas do not share a "
            "data volume, so a generated key would differ per pod and the same "
            "value would redact to a different placeholder on each — silently "
            "breaking evidence correlation. Set it as a deployment secret."
        )

    path = Path(settings.protection.pseudonym_key_path).expanduser()
    cache_key = f"file:{path}"
    if cache_key not in _CACHE:
        _CACHE[cache_key] = _load_or_create(path)
    return _CACHE[cache_key]


def reset_pseudonym_key_cache() -> None:
    """Drop the process-wide cache. For tests that vary the key source."""
    _CACHE.clear()


#: Redis key holding the fingerprint of the pseudonym key this deployment
#: agreed on. Deliberately NOT the key — see verify_pseudonym_key_agreement.
AGREEMENT_REDIS_KEY = "redaction:pseudonym_key_fingerprint"


class PseudonymKeyMismatchError(RuntimeError):
    """Raised when this process's key disagrees with the deployment's.

    Two processes redacting the same value to different placeholders is the
    failure this module exists to prevent, and it is invisible from inside
    either one — each looks perfectly consistent with itself.
    """


async def verify_pseudonym_key_agreement(key: bytes, redis_client) -> None:
    """Refuse to serve if another process in this deployment holds a different key.

    Resolution alone cannot establish the invariant that matters — *every*
    process redacting for this deployment uses the same key. Whether a
    generated key is shared depends on the deployment's topology: one process,
    or several sharing a durable filesystem, is fine; several pods with no
    shared volume is not. The application cannot see its own topology, and the
    predicate it used to guess with (``DEPLOYMENT_MODE=cloud``) is a proxy that
    an operator can simply not set — on-prem does not, so a multi-replica
    Deployment took the standalone path and minted a key per pod, silently.

    So this checks the invariant itself rather than a proxy for it. Every
    process publishes a fingerprint of its key to the one store all of them
    genuinely share, and any process finding a different fingerprint already
    there refuses to serve. That holds however the key was obtained, whatever
    the deployment mode says, and whatever the topology turns out to be — a
    half-rolled-out Secret change is caught by the same check.

    What is stored is ``sha256(key)``, never the key. The registry mapping
    placeholders back to plaintext lives in this same Redis, and the whole
    design turns on not putting the key beside it; a digest of a 256-bit random
    value discloses nothing.

    Degrades rather than blocks when Redis is unreachable: an unavailable check
    is not evidence of disagreement, and refusing to boot on it would make
    redaction depend on Redis being up. Standalone's in-process FakeRedis makes
    this a self-check that always agrees, which is correct — a single process
    cannot disagree with itself, and workers that share the key file converge
    anyway.

    Raises:
        PseudonymKeyMismatchError: another process holds a different key.
    """
    fingerprint = hashlib.sha256(key).hexdigest()

    try:
        # SETNX: the first process to arrive defines the deployment's key.
        claimed = await redis_client.set(AGREEMENT_REDIS_KEY, fingerprint, nx=True)
        if claimed:
            return
        published = await redis_client.get(AGREEMENT_REDIS_KEY)
    except PseudonymKeyMismatchError:
        raise
    except Exception as exc:  # noqa: BLE001 - availability, not disagreement
        logger.warning(
            "Could not verify pseudonym key agreement (%s). Continuing: an "
            "unreachable check is not evidence of a mismatch.",
            exc,
        )
        return

    if published is None:
        return
    if isinstance(published, bytes):
        published = published.decode()

    if published != fingerprint:
        raise PseudonymKeyMismatchError(
            "This process's redaction pseudonym key differs from the one "
            "another process in this deployment is already using.\n"
            "Every process must share one key: the same host has to redact to "
            "the same placeholder everywhere, or evidence stops correlating "
            "and the investigation engine can conclude something false about "
            "what look like two different machines.\n"
            "Usual cause: REDACTION_PSEUDONYM_KEY is unset, so each replica "
            "generated its own on storage the others cannot see. Set it as a "
            "deployment secret so every replica reads the same value.\n"
            "Also possible: a partially rolled-out change to that secret, or "
            "pods started against different values of it.\n"
            f"If the deployment's key was deliberately replaced, clear "
            f"'{AGREEMENT_REDIS_KEY}' in Redis and restart every process — "
            "placeholders already stored under the previous key will not "
            "reverse afterwards."
        )
