"""Lifecycle of the redaction pseudonym key (#971).

The key is what makes a placeholder unguessable while still being the SAME
placeholder for the same value across separately-sanitized artifacts. Both
properties depend on the key being stable AND non-trivial for the whole
deployment, so what these tests pin is every way it can silently become
neither.

The failure that matters most is an EMPTY key: `HMAC-SHA256("", value)` is a
fixed, publicly-computable function of the value — the exact #971 defect,
reintroduced silently and with no error to notice.
"""

import hashlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from faultmaven.infrastructure.security.pseudonym_key import (
    AGREEMENT_REDIS_KEY,
    MIN_KEY_LENGTH,
    PseudonymKeyMismatchError,
    PseudonymKeyUnavailableError,
    _load_or_create,
    reset_pseudonym_key_cache,
    resolve_pseudonym_key,
    verify_pseudonym_key_agreement,
)

GOOD_KEY = "a-perfectly-adequate-deployment-key"


def _settings(tmp_path: Path, *, key=None, cloud=False):
    settings = MagicMock()
    settings.protection.pseudonym_key = key
    settings.protection.pseudonym_key_path = str(tmp_path / ".redaction_key")
    settings.is_cloud = cloud
    return settings


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_pseudonym_key_cache()
    yield
    reset_pseudonym_key_cache()


class TestConfiguredKey:
    def test_the_configured_key_is_used_verbatim(self, tmp_path):
        assert resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY)) == (
            GOOD_KEY.encode()
        )

    def test_a_configured_key_is_not_written_to_disk(self, tmp_path):
        """Persisting a copy would be a second place for the secret to leak."""
        resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY))

        assert list(tmp_path.iterdir()) == []

    def test_the_configured_key_wins_over_an_existing_file(self, tmp_path):
        (tmp_path / ".redaction_key").write_bytes(b"a-key-that-is-on-the-disk-here")

        assert resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY)) == (
            GOOD_KEY.encode()
        )

    def test_a_secretstr_key_is_unwrapped(self, tmp_path):
        settings = _settings(tmp_path, key=SecretStr(GOOD_KEY))

        assert resolve_pseudonym_key(settings) == GOOD_KEY.encode()

    def test_a_configured_key_is_never_published_to_the_key_path(self, tmp_path):
        """Asserting the directory is empty only covers the configured path; a
        write anywhere else would slip past. Assert the publish never runs."""
        import faultmaven.infrastructure.security.pseudonym_key as mod

        with patch.object(mod, "_write_atomically") as publish:
            resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY))

        publish.assert_not_called()

    def test_a_rotated_key_is_not_served_from_the_cache(self, tmp_path):
        """No reset between the calls — that is the point. The cache is keyed
        by the key's value, so changing the env var in a long-lived process
        takes effect. A cache keyed by a literal would serve the old one, and
        every test that resets around a key change would still pass."""
        first = resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY))
        second = resolve_pseudonym_key(_settings(tmp_path, key=GOOD_KEY + "-rotated"))

        assert first == GOOD_KEY.encode()
        assert second == (GOOD_KEY + "-rotated").encode()


class TestTheRealSettingsField:
    """Exercise the actual env var and the actual settings type.

    Everything else here injects a mock, so nothing would notice if the
    `validation_alias` were misspelled or the field stopped being a SecretStr —
    and the alias is cloud's one REQUIRED knob.
    """

    def test_the_env_var_name_populates_the_field(self, monkeypatch, tmp_path):
        monkeypatch.setenv("REDACTION_PSEUDONYM_KEY", GOOD_KEY)
        from faultmaven.config.settings import FaultMavenSettings

        settings = FaultMavenSettings()

        assert resolve_pseudonym_key(settings) == GOOD_KEY.encode()

    def test_the_key_is_masked_in_a_settings_dump(self, monkeypatch):
        monkeypatch.setenv("REDACTION_PSEUDONYM_KEY", GOOD_KEY)
        from faultmaven.config.settings import FaultMavenSettings

        settings = FaultMavenSettings()

        assert GOOD_KEY not in repr(settings.protection)
        assert GOOD_KEY not in str(settings.protection.model_dump())


class TestWeakKeysAreRefused:
    """A weak key is worse than no key: it looks configured and is not."""

    @pytest.mark.parametrize("weak", ["x", "short", "0123456789abcde"])
    def test_a_too_short_key_raises(self, tmp_path, weak):
        with pytest.raises(PseudonymKeyUnavailableError):
            resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(weak)))

    def test_a_key_exactly_at_the_floor_is_accepted(self, tmp_path):
        exact = "x" * MIN_KEY_LENGTH

        assert (
            resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(exact)))
            == exact.encode()
        )

    @pytest.mark.parametrize("blank", ["", "   ", "\n", " \t\n "])
    def test_a_blank_key_is_treated_as_unset_not_as_an_empty_key(self, tmp_path, blank):
        """An env var exported blank — or whitespace-only, which is the same
        thing once stripped — must fall through to generation. Taking it
        literally would give every such deployment the same empty key, and
        `HMAC-SHA256("", value)` is publicly recomputable."""
        key = resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(blank)))

        assert len(key) == 64
        assert (tmp_path / ".redaction_key").exists()

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_a_blank_key_in_cloud_still_refuses(self, tmp_path, blank):
        """Falling through to generation is only safe where generation is."""
        with pytest.raises(PseudonymKeyUnavailableError):
            resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(blank), cloud=True))


class TestWhitespaceIsNormalisedOnBothSources:
    """k8s Secrets and `base64 <<< "key"` routinely append a newline. If only
    one source stripped, the same logical secret supplied as a file and as an
    env var would key two different HMACs."""

    def test_a_trailing_newline_on_the_env_key_is_ignored(self, tmp_path):
        plain = resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(GOOD_KEY)))
        reset_pseudonym_key_cache()
        newline = resolve_pseudonym_key(
            _settings(tmp_path, key=SecretStr(GOOD_KEY + "\n"))
        )

        assert plain == newline

    def test_env_and_file_sources_agree_on_the_same_secret(self, tmp_path):
        (tmp_path / ".redaction_key").write_bytes(GOOD_KEY.encode() + b"\n")
        from_file = resolve_pseudonym_key(_settings(tmp_path))
        reset_pseudonym_key_cache()
        from_env = resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(GOOD_KEY)))

        assert from_file == from_env


class TestStandaloneGeneratesAndPersists:
    def test_a_key_is_generated_on_first_use(self, tmp_path):
        key = resolve_pseudonym_key(_settings(tmp_path))

        assert len(key) == 64  # token_hex(32)
        assert (tmp_path / ".redaction_key").read_bytes() == key

    def test_the_same_key_is_returned_across_processes(self, tmp_path):
        """A restart must not renumber every placeholder against stored
        evidence. Cache cleared between calls to model a fresh process."""
        first = resolve_pseudonym_key(_settings(tmp_path))
        reset_pseudonym_key_cache()
        second = resolve_pseudonym_key(_settings(tmp_path))

        assert first == second

    def test_the_key_file_is_not_world_readable(self, tmp_path):
        resolve_pseudonym_key(_settings(tmp_path))

        assert (tmp_path / ".redaction_key").stat().st_mode & 0o077 == 0

    def test_no_temp_files_are_left_behind(self, tmp_path):
        resolve_pseudonym_key(_settings(tmp_path))

        assert [p.name for p in tmp_path.iterdir()] == [".redaction_key"]


class TestAnEmptyKeyFileIsNeverHonoured:
    """An empty or near-empty key file is refused, not honoured and not
    silently replaced.

    Honouring it would hand every later start an unkeyed digest —
    `HMAC-SHA256("", value)` is publicly recomputable. Replacing it is worse
    than it looks: a corrupt file means the previous key is GONE, so any
    replacement disagrees with every placeholder already written into stored
    evidence, KB content and transcripts. No locking protocol repairs that —
    the information needed is destroyed — so the failure is loud and the
    operator decides.
    """

    @pytest.mark.parametrize("corrupt", [b"", b"\n", b"   ", b"tooshort"])
    def test_a_corrupt_key_file_is_refused(self, tmp_path, corrupt):
        (tmp_path / ".redaction_key").write_bytes(corrupt)

        with pytest.raises(PseudonymKeyUnavailableError, match="not a usable key"):
            resolve_pseudonym_key(_settings(tmp_path))

    def test_a_corrupt_key_file_is_not_overwritten(self, tmp_path):
        """The refusal must not have installed a key on its way out — that
        would be the silent-divergence path wearing an exception."""
        path = tmp_path / ".redaction_key"
        path.write_bytes(b"")

        with pytest.raises(PseudonymKeyUnavailableError):
            resolve_pseudonym_key(_settings(tmp_path))

        assert path.read_bytes() == b""
        assert [p.name for p in tmp_path.iterdir()] == [".redaction_key"]

    def test_resolve_never_returns_a_weak_key(self, tmp_path):
        """The backstop: what comes out is always usable, or nothing does."""
        (tmp_path / ".redaction_key").write_bytes(b"")

        try:
            key = resolve_pseudonym_key(_settings(tmp_path))
        except PseudonymKeyUnavailableError:
            return
        assert len(key) >= MIN_KEY_LENGTH


class TestConcurrentFirstStart:
    """Standalone supports WORKERS>1, so several processes run the startup
    resolve against one fresh path simultaneously.

    `O_CREAT|O_EXCL` alone is NOT enough: it serializes the create, not the
    create-and-write, so a loser reading between the winner's create and its
    write gets zero bytes. The publish is therefore write-to-temp then link,
    and the name only ever appears with complete content.
    """

    @staticmethod
    def _race(path, workers):
        """Run ``workers`` threads through _load_or_create on one path.

        Returns (keys, errors). Errors are captured rather than left to die in
        the thread: pytest downgrades an unhandled thread exception to a
        warning, so a test that only inspected the successful results would
        pass while most racers crashed — which is exactly how the first version
        of this test failed to catch a non-atomic publish.
        """
        keys, errors = [], []
        barrier = threading.Barrier(workers)

        def worker():
            try:
                barrier.wait()
                keys.append(_load_or_create(path))
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return keys, errors

    def test_every_racer_gets_the_same_non_empty_key(self, tmp_path):
        path = tmp_path / ".redaction_key"

        keys, errors = self._race(path, 16)

        assert errors == [], f"racers failed: {errors}"
        assert len(keys) == 16, "every racer must return a key"
        assert len(set(keys)) == 1, f"racers diverged: {set(keys)}"
        assert len(keys[0]) == 64
        assert keys[0] == path.read_bytes()

    def test_repeated_races_never_produce_an_empty_or_divergent_key(self, tmp_path):
        for i in range(10):
            path = tmp_path / f".redaction_key_{i}"

            keys, errors = self._race(path, 8)

            assert errors == [], f"round {i} racers failed: {errors}"
            assert len(keys) == 8
            assert b"" not in keys
            assert len(set(keys)) == 1


class TestCloudRefusesToGenerate:
    def test_cloud_without_a_key_raises(self, tmp_path):
        """Replicas share no data volume, so a generated key would differ per
        pod — the same value redacting differently on each, silently."""
        with pytest.raises(PseudonymKeyUnavailableError):
            resolve_pseudonym_key(_settings(tmp_path, cloud=True))

    def test_cloud_writes_no_key_file(self, tmp_path):
        with pytest.raises(PseudonymKeyUnavailableError):
            resolve_pseudonym_key(_settings(tmp_path, cloud=True))

        assert list(tmp_path.iterdir()) == []

    def test_cloud_with_a_configured_key_is_fine(self, tmp_path):
        assert (
            resolve_pseudonym_key(
                _settings(tmp_path, key=SecretStr(GOOD_KEY), cloud=True)
            )
            == GOOD_KEY.encode()
        )


class TestTheStartupGate:
    """The boot must refuse when no usable key can be resolved.

    Without this, the gate in `main.py` is deletable with every other test
    still green — and the failure it converts is not cosmetic: the regex pass
    mints pseudonyms on every sanitize call regardless of
    PROTECTION_SANITIZE_PII, so a keyless deployment would surface as scattered
    500s mid-investigation rather than a refusal to start.
    """

    # A companion test asserting a SUCCESSFUL boot reaches the resolver was
    # dropped: it needs the whole app to come up, and other tests in this
    # directory leave LLM provider settings that make a full boot fail for
    # unrelated reasons, so it was order-dependent. The refusal below covers
    # the same wiring without that fragility — the gate sits above LLM
    # registration, so it raises before anything env-sensitive runs, and
    # deleting it from main.py fails this test either way (the patched
    # resolver is never called, so the expected exception never arrives).

    def test_an_unresolvable_key_stops_the_boot(self, monkeypatch):
        """The gate must be inside the config block that re-raises, not in a
        handler that logs and continues."""
        import faultmaven.infrastructure.security.pseudonym_key as mod

        def refuse(_settings):
            raise PseudonymKeyUnavailableError("no key for you")

        monkeypatch.setattr(mod, "resolve_pseudonym_key", refuse)

        from fastapi.testclient import TestClient

        from faultmaven.main import app

        with pytest.raises(PseudonymKeyUnavailableError):
            with TestClient(app):
                pass


class TestTheNoHardlinkFallback:
    """Filesystems where `os.link` fails take a second publish path.

    Nothing exercised it, which is how it kept the create-then-write window the
    main path exists to close: it claimed the real path with `O_CREAT|O_EXCL`
    and wrote afterwards, so a crash or ENOSPC in between left a permanent
    zero-byte key file — and a concurrent reader could observe those zero bytes
    and refuse to boot. It now re-decides under an exclusive lock and lands the
    content with an atomic replace.
    """

    @staticmethod
    def _no_hardlinks(monkeypatch):
        import faultmaven.infrastructure.security.pseudonym_key as mod

        def unsupported(_src, _dst):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(mod.os, "link", unsupported)

    def test_the_fallback_publishes_a_complete_key(self, tmp_path, monkeypatch):
        self._no_hardlinks(monkeypatch)
        path = tmp_path / ".redaction_key"

        key = _load_or_create(path)

        assert len(key) == 64
        assert path.read_bytes() == key

    def test_the_fallback_never_leaves_an_empty_file(self, tmp_path, monkeypatch):
        """The state that used to be permanent: the real path existing with
        zero bytes. Whatever happens, the name must never appear empty."""
        self._no_hardlinks(monkeypatch)
        path = tmp_path / ".redaction_key"

        _load_or_create(path)

        assert path.read_bytes().strip() != b""

    def test_the_fallback_converges_under_concurrency(self, tmp_path, monkeypatch):
        """Losers must adopt the winner's key, not install their own. The
        replace is unconditional, so without the re-read under the lock the
        last writer would win and the racers would diverge."""
        self._no_hardlinks(monkeypatch)
        path = tmp_path / ".redaction_key"

        keys, errors = TestConcurrentFirstStart._race(path, 16)

        assert errors == [], f"racers failed: {errors}"
        assert len(keys) == 16
        assert len(set(keys)) == 1, f"racers diverged: {set(keys)}"
        assert keys[0] == path.read_bytes()

    def test_the_fallback_leaves_no_temp_file(self, tmp_path, monkeypatch):
        self._no_hardlinks(monkeypatch)
        path = tmp_path / ".redaction_key"

        _load_or_create(path)

        leftovers = {p.name for p in tmp_path.iterdir()}
        assert not any(".tmp." in name for name in leftovers), leftovers


class TestTheConfiguredKeyStaysOutOfModuleGlobals:
    """A configured key must not be parked in the process-wide cache.

    The cache exists to avoid re-reading the key file; a configured key needs
    no such saving. Caching it would leave the secret reachable from module
    globals — as a dict key and as its value — for anything that walks them,
    which is the exposure SecretStr is meant to prevent.
    """

    def test_the_key_appears_nowhere_in_the_cache(self, tmp_path):
        import faultmaven.infrastructure.security.pseudonym_key as mod

        resolve_pseudonym_key(_settings(tmp_path, key=SecretStr(GOOD_KEY)))

        blob = repr(mod._CACHE)
        assert GOOD_KEY not in blob, "configured key leaked into the cache"
        assert mod._CACHE == {}, f"configured key should not be cached: {blob}"

    def test_a_file_key_is_still_cached(self, tmp_path):
        """The cache keeps doing its actual job."""
        import faultmaven.infrastructure.security.pseudonym_key as mod

        resolve_pseudonym_key(_settings(tmp_path))

        assert mod._CACHE, "the file-backed key should still be cached"


@pytest.mark.asyncio
class TestKeyAgreementAcrossProcesses:
    """The invariant is that EVERY process redacting for this deployment uses
    the same key — and resolution alone cannot establish it.

    Whether a generated key is shared depends on topology the app cannot see.
    The predicate it used to infer from (`DEPLOYMENT_MODE=cloud`) is a proxy an
    operator can simply not set: on-prem does not, so a multi-replica
    Deployment took the standalone path and minted a key per pod, silently,
    with no crashloop and no failed rollout. This checks the invariant in the
    one store all replicas genuinely share instead of guessing at it.
    """

    class _FakeRedis:
        def __init__(self):
            self.store = {}

        async def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = value.encode() if isinstance(value, str) else value
            return True

        async def get(self, key):
            return self.store.get(key)

    class _BrokenRedis:
        async def set(self, *a, **k):
            raise ConnectionError("redis unreachable")

        async def get(self, *a, **k):
            raise ConnectionError("redis unreachable")

    KEY_A = b"a" * 64
    KEY_B = b"b" * 64

    async def test_the_first_process_defines_the_deployment_key(self):
        redis = self._FakeRedis()

        await verify_pseudonym_key_agreement(self.KEY_A, redis)

        assert AGREEMENT_REDIS_KEY in redis.store

    async def test_a_process_with_the_same_key_agrees(self):
        redis = self._FakeRedis()
        await verify_pseudonym_key_agreement(self.KEY_A, redis)

        await verify_pseudonym_key_agreement(self.KEY_A, redis)

    async def test_a_process_with_a_different_key_refuses(self):
        """The case that was silent: replica 2 generated its own key because
        no shared volume carried replica 1's."""
        redis = self._FakeRedis()
        await verify_pseudonym_key_agreement(self.KEY_A, redis)

        with pytest.raises(PseudonymKeyMismatchError):
            await verify_pseudonym_key_agreement(self.KEY_B, redis)

    async def test_the_key_itself_is_never_published(self):
        """This Redis also holds the placeholder→plaintext registry; a key
        beside it would hand one compromise both halves. A digest does not."""
        redis = self._FakeRedis()
        secret = b"s3cret-deployment-key-not-a-digest"

        await verify_pseudonym_key_agreement(secret, redis)

        assert secret not in redis.store[AGREEMENT_REDIS_KEY]
        assert redis.store[AGREEMENT_REDIS_KEY] == (
            hashlib.sha256(secret).hexdigest().encode()
        )

    async def test_an_unreachable_redis_does_not_block_startup(self):
        """An unavailable check is not evidence of disagreement, and blocking
        on it would make redaction depend on Redis being up."""
        await verify_pseudonym_key_agreement(self.KEY_A, self._BrokenRedis())

    async def test_detection_does_not_depend_on_deployment_mode(self):
        """The whole point: the mode is never consulted. A deployment that
        never set DEPLOYMENT_MODE — which is how the hole got in — is caught
        exactly the same way."""
        redis = self._FakeRedis()
        await verify_pseudonym_key_agreement(self.KEY_A, redis)

        with pytest.raises(PseudonymKeyMismatchError):
            await verify_pseudonym_key_agreement(self.KEY_B, redis)
