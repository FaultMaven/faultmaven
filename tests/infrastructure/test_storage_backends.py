"""Tests for storage backends.

Verifies:
1. Filesystem backend: returns deterministic local URLs
2. S3 backend: returns presigned URL shape (mock boto client)
3. Integration smoke test: evidence upload flow uses backend interface only
4. Factory correctly selects backend based on STORAGE_BACKEND
"""

import importlib.util
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# boto3 ships only in the cloud extra: `requirements/test.txt` (the Standalone
# CI install) deliberately omits it, and that job asserts its absence. Tests
# that patch `boto3.client` must therefore skip there — `mock.patch` imports
# the target module, so an unguarded patch is a hard error, not a skip.
#
# `spec is not None` would not deliver that: pip and uv leave a package's
# directories behind on uninstall, and PEP 420 resolves an empty `boto3/` tree
# to a namespace package with `origin is None` — so the guard would read
# "available", the patch would run, and it would fail as the hard error this
# comment exists to prevent. Same discriminator as
# faultmaven/infrastructure/model_cache.py.
_boto3_spec = importlib.util.find_spec("boto3")
_BOTO3_AVAILABLE = _boto3_spec is not None and _boto3_spec.origin is not None
_REQUIRES_BOTO3 = pytest.mark.skipif(
    not _BOTO3_AVAILABLE, reason="boto3 is a cloud-only dependency"
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """Ensure clean environment for each test."""
    env_vars = ["STORAGE_BACKEND", "S3_BUCKET_NAME", "S3_REGION", "S3_KEY_PREFIX"]
    original = {k: os.environ.get(k) for k in env_vars}

    yield

    # Restore original values
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def temp_storage_dir():
    """Create temporary directory for filesystem storage tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def filesystem_backend(temp_storage_dir):
    """Create filesystem storage backend for testing."""
    from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend

    return FilesystemStorageBackend(
        storage_root=temp_storage_dir,
        base_url="http://localhost:8090",
    )


@pytest.fixture
def mock_boto3_client():
    """Create mock boto3 S3 client."""
    mock_client = MagicMock()

    # Mock presigned URL generation
    mock_client.generate_presigned_url.return_value = (
        "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc123"
    )

    # Mock head_object (file exists check)
    mock_client.head_object.return_value = {
        "ContentLength": 1024,
        "ContentType": "text/plain",
        "LastModified": "2025-01-02T12:00:00Z",
        "Metadata": {"custom": "value"},
    }

    # Mock get_object
    mock_client.get_object.return_value = {
        "Body": MagicMock(read=lambda: b"file content"),
    }

    return mock_client


# =============================================================================
# Filesystem Backend Tests
# =============================================================================


class TestFilesystemStorageBackend:
    """Tests for filesystem storage backend."""

    @pytest.mark.asyncio
    async def test_generate_upload_url_format(self, filesystem_backend):
        """Test upload URL has correct format for filesystem backend."""
        url = await filesystem_backend.generate_upload_url(
            key="org123/case456/error.log",
            content_type="text/plain",
        )

        assert url.url.startswith("http://localhost:8090/api/v1/storage/upload/")
        assert url.method == "POST"
        assert "Content-Type" in url.headers
        assert url.headers["Content-Type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_generate_download_url_format(
        self, filesystem_backend, temp_storage_dir
    ):
        """Test download URL has correct format for filesystem backend."""
        # First store a file
        key = "org123/case456/error.log"
        await filesystem_backend.store_file(key, b"test content")

        url = await filesystem_backend.generate_download_url(
            key=key,
            filename="error.log",
        )

        assert url.url.startswith("http://localhost:8090/api/v1/storage/download/")
        assert url.method == "GET"
        assert "filename=error.log" in url.url

    @pytest.mark.asyncio
    async def test_generate_download_url_file_not_found(self, filesystem_backend):
        """Test download URL raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            await filesystem_backend.generate_download_url(key="nonexistent/file.log")

    @pytest.mark.asyncio
    async def test_store_and_retrieve_file(self, filesystem_backend):
        """Test storing and retrieving file content."""
        key = "test/data.txt"
        content = b"Hello, World!"

        # Store file
        stored = await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type="text/plain",
        )

        assert stored.key == key
        assert stored.size_bytes == len(content)
        assert stored.content_type == "text/plain"

        # Retrieve file
        retrieved = await filesystem_backend.retrieve_file(key)
        assert retrieved == content

    @pytest.mark.asyncio
    async def test_delete_file(self, filesystem_backend):
        """Test deleting a file."""
        key = "test/delete-me.txt"

        # Store file
        await filesystem_backend.store_file(key, b"delete me")

        # Verify exists
        assert await filesystem_backend.file_exists(key)

        # Delete file
        deleted = await filesystem_backend.delete_file(key)
        assert deleted is True

        # Verify gone
        assert not await filesystem_backend.file_exists(key)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file(self, filesystem_backend):
        """Test deleting a nonexistent file returns False."""
        deleted = await filesystem_backend.delete_file("nonexistent/file.txt")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_file_exists(self, filesystem_backend):
        """Test file existence check."""
        key = "test/exists.txt"

        assert not await filesystem_backend.file_exists(key)

        await filesystem_backend.store_file(key, b"I exist!")

        assert await filesystem_backend.file_exists(key)

    @pytest.mark.asyncio
    async def test_get_file_info(self, filesystem_backend):
        """Test getting file metadata."""
        key = "test/metadata.txt"
        content = b"File with metadata"
        metadata = {"author": "test", "version": "1.0"}

        await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type="text/plain",
            metadata=metadata,
        )

        info = await filesystem_backend.get_file_info(key)

        assert info is not None
        assert info.key == key
        assert info.size_bytes == len(content)
        assert info.content_type == "text/plain"

    @pytest.mark.asyncio
    async def test_path_traversal_prevention(self, filesystem_backend):
        """The two shapes the old denylist caught are still caught.

        Unchanged from before #1235, assertion included. The guard underneath
        is now root-anchored containment rather than a substring check, but the
        backend still raises a ``ValueError`` whose message begins
        ``Invalid storage key`` — deliberately, because that string reaches a
        client and the LLM context (see ``_get_full_path``), so the detailed
        message stays in the server log.
        """
        with pytest.raises(ValueError, match="Invalid storage key"):
            await filesystem_backend.store_file("../../../etc/passwd", b"malicious")

        with pytest.raises(ValueError, match="Invalid storage key"):
            await filesystem_backend.store_file("/absolute/path", b"malicious")

    def test_storage_type(self, filesystem_backend):
        """Test storage type is FILESYSTEM."""
        from faultmaven.infrastructure.storage.base import StorageType

        assert filesystem_backend.get_storage_type() == StorageType.FILESYSTEM


# =============================================================================
# Filesystem Backend Containment (#1235)
# =============================================================================


class TestFilesystemPathContainment:
    """The backend refuses any key that RESOLVES outside the storage root.

    Until #1235 the guard was a denylist — ``".." in key or
    key.startswith("/")``. These tests are written against the property that
    denylist did not have: a key can contain neither marker and still land
    outside the root, because a substring check does not resolve symlinks.

    Severity note, so nobody reads more into this than is there: no known key
    reaches the backend from user input (``storage_key`` is minted by
    ``FileStorageService._generate_storage_key``). This is defense in depth and
    one containment discipline across subsystems, not a live traversal.
    """

    @pytest.fixture
    def outside_dir(self, tmp_path):
        """A directory that is definitively NOT under the storage root."""
        outside = tmp_path / "outside_the_root"
        outside.mkdir()
        return outside

    @staticmethod
    def _backend(storage_root):
        from faultmaven.infrastructure.storage.filesystem import (
            FilesystemStorageBackend,
        )

        return FilesystemStorageBackend(
            storage_root=str(storage_root),
            base_url="http://localhost:8090",
        )

    # -- The decisive case: a key the old denylist admitted ------------------

    @pytest.mark.asyncio
    async def test_store_through_directory_symlink_is_refused(
        self, temp_storage_dir, outside_dir
    ):
        """A key with no ``..`` and no leading ``/`` that still escapes.

        ``linked -> <outside>`` inside the root. The key ``linked/evidence.log``
        passes every denylist predicate and resolves to
        ``<outside>/evidence.log``. This is the test that fails on the denylist
        and passes on the allowlist.
        """
        (Path(temp_storage_dir) / "linked").symlink_to(
            outside_dir, target_is_directory=True
        )
        key = "linked/evidence.log"

        # Precondition: the OLD guard would have admitted this key.
        assert ".." not in key
        assert not key.startswith("/")

        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.store_file(key, b"escaped payload")

        # And nothing was written through the link.
        assert not (outside_dir / "evidence.log").exists()
        assert list(outside_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_read_through_file_symlink_is_refused(
        self, temp_storage_dir, outside_dir
    ):
        """The read side escapes the same way — exfiltration, not just write."""
        secret = outside_dir / "secret.txt"
        secret.write_text("private")
        (Path(temp_storage_dir) / "peek.txt").symlink_to(secret)

        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.retrieve_file("peek.txt")

    @pytest.mark.asyncio
    async def test_delete_through_file_symlink_is_refused(
        self, temp_storage_dir, outside_dir
    ):
        """And the destructive side: unlink must not follow a link out."""
        victim = outside_dir / "victim.txt"
        victim.write_text("do not delete me")
        (Path(temp_storage_dir) / "victim-link.txt").symlink_to(victim)

        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.delete_file("victim-link.txt")

        assert victim.exists()

    # -- Validation precedes creation ----------------------------------------

    @pytest.mark.asyncio
    async def test_refused_key_creates_no_directories(self, temp_storage_dir):
        """A refused write leaves nothing on disk — not even a directory.

        ``mkdir(parents=True)`` on an escaped path materialises
        attacker-chosen directories outside the tree whatever the write then
        does; that was the second half of #1215's round-1 defect. Containment
        must therefore be checked BEFORE ``makedirs``, which is why
        ``_get_full_path`` is the first statement in ``store_file``.
        """
        root = Path(temp_storage_dir) / "root"
        root.mkdir()
        backend = self._backend(root)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.store_file("../sibling/deep/nested/f.txt", b"x")

        assert not (Path(temp_storage_dir) / "sibling").exists()
        assert list(root.iterdir()) == []

    # -- The shapes the denylist already caught, still caught -----------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key",
        [
            "../escape.txt",
            "../../../etc/passwd",
            "a/../../escape.txt",
            "/etc/passwd",
        ],
    )
    async def test_textual_traversal_still_refused(self, temp_storage_dir, key):
        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.store_file(key, b"malicious")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["", ".", "./"])
    async def test_key_resolving_to_the_root_itself_is_refused(
        self, temp_storage_dir, key
    ):
        """Strictly inside, so the root is refused too.

        Deliberate behaviour change: an empty key used to return the root and
        fail later with ``IsADirectoryError``. It now fails at the guard, where
        the message names the key.
        """
        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.store_file(key, b"x")

    @pytest.mark.asyncio
    async def test_unresolvable_key_is_refused_not_raised_raw(self, temp_storage_dir):
        """An embedded NUL makes ``resolve()`` raise; it must arrive typed."""
        from faultmaven.utils.path_containment import PathEscape

        backend = self._backend(temp_storage_dir)

        with pytest.raises(PathEscape, match="Invalid storage key"):
            await backend.store_file("evidence\x00.log", b"x")

    # -- No regression on ordinary keys ---------------------------------------

    @pytest.mark.asyncio
    async def test_ordinary_nested_key_round_trips(self, temp_storage_dir):
        """The shape ``_generate_storage_key`` actually mints."""
        backend = self._backend(temp_storage_dir)
        key = "org_abc/case_123/2026-08-29/deadbeef1234_app.log"

        stored = await backend.store_file(key, b"log line", content_type="text/plain")

        assert stored.key == key
        assert await backend.retrieve_file(key) == b"log line"
        assert await backend.file_exists(key)
        assert key in await backend.list_keys()
        assert (Path(temp_storage_dir) / key).is_file()
        assert await backend.delete_file(key)

    @pytest.mark.asyncio
    async def test_sidecar_suffix_key_round_trips(self, temp_storage_dir):
        """``{storage_key}.sidecar.json`` — the orphan-tracking companion."""
        backend = self._backend(temp_storage_dir)
        key = "org_abc/case_123/blob.bin.sidecar.json"

        await backend.store_file(key, b"{}")

        assert await backend.retrieve_file(key) == b"{}"

    @pytest.mark.asyncio
    async def test_relative_storage_root_still_contains(self, tmp_path, monkeypatch):
        """The default root (``./data/storage``) is relative.

        Both root and candidate resolve against the same cwd, so containment
        holds — but a guard that resolved only one of them would not, and the
        shipped default takes this path.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "outside").mkdir()
        (tmp_path / "data" / "storage").mkdir(parents=True)
        (tmp_path / "data" / "storage" / "linked").symlink_to(
            tmp_path / "outside", target_is_directory=True
        )

        backend = self._backend("./data/storage")

        await backend.store_file("ok/file.txt", b"fine")
        assert (tmp_path / "data" / "storage" / "ok" / "file.txt").is_file()

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.store_file("linked/escaped.txt", b"escaped")
        assert list((tmp_path / "outside").iterdir()) == []

    # -- What the refusal is, and what it says --------------------------------

    @pytest.mark.asyncio
    async def test_a_storage_refusal_is_never_a_runbook_refusal(self, temp_storage_dir):
        """The type must distinguish this subsystem from the runbook tree.

        ``RunbookPathEscape`` exists so the knowledge scan and the draft listing
        can catch it and SKIP one bad row rather than fail. If the storage
        backend raised that type, an evidence refusal surfacing anywhere near
        those callers would be silently swallowed as "one bad runbook".

        This asserts against ``FilesystemStorageBackend`` actually raising. An
        earlier version of this pin exercised the shared primitive with its
        default error type instead, where the property held by construction and
        said nothing about the backend — passing ``error=RunbookPathEscape`` in
        ``_get_full_path`` left the whole suite green.
        """
        from faultmaven.utils.path_containment import PathEscape
        from faultmaven.utils.runbook_id import RunbookPathEscape

        backend = self._backend(temp_storage_dir)

        with pytest.raises(PathEscape) as exc:
            await backend.store_file("../escape.txt", b"x")

        assert type(exc.value) is PathEscape, (
            f"the backend raised {type(exc.value).__name__}; a storage refusal "
            f"must be a plain PathEscape"
        )
        assert not isinstance(exc.value, RunbookPathEscape)

    @pytest.mark.asyncio
    async def test_the_refusal_message_carries_no_server_paths(
        self, temp_storage_dir, outside_dir, caplog
    ):
        """``PathEscape``'s message names resolved absolute paths, and its own
        docstring says that must never reach a client. This backend's exception
        does reach one: ``FileStorageService.retrieve_file`` re-wraps it into a
        ``ServiceError`` and ``read_file_tool`` puts that string into a
        ``ToolResult``, which enters the LLM context and the case transcript.

        So the message names only the key, and the detail goes to the log.
        """
        (Path(temp_storage_dir) / "linked").symlink_to(
            outside_dir, target_is_directory=True
        )
        backend = self._backend(temp_storage_dir)

        with caplog.at_level("WARNING"):
            with pytest.raises(ValueError) as exc:
                await backend.store_file("linked/evidence.log", b"x")

        message = str(exc.value)
        assert str(outside_dir) not in message
        assert str(temp_storage_dir) not in message
        assert "linked/evidence.log" in message

        # ...but the operator still gets the whole picture, server-side.
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert str(outside_dir) in logged
        assert "outside the storage tree" in logged

    # -- An in-root symlink is legal, and deleting it deletes the LINK --------

    @pytest.mark.asyncio
    async def test_delete_unlinks_the_key_not_the_symlink_target(
        self, temp_storage_dir
    ):
        """Containment is decided on the resolved path; the unlink is not.

        In-root symlinks pass the guard (they land inside the root). If the
        unlink followed the link, deleting key ``a.log`` would destroy
        ``real.bin`` and leave ``a.log`` dangling — after which ``list_keys``
        drops it (``is_file()`` is False on a broken link) and the object is
        unreachable by the orphan sweep and by ``fm-wipe-deployment``. Silent
        data loss, so it is pinned.
        """
        root = Path(temp_storage_dir)
        (root / "real.bin").write_bytes(b"the actual object")
        (root / "a.log").symlink_to(root / "real.bin")

        backend = self._backend(temp_storage_dir)

        assert await backend.delete_file("a.log") is True

        assert not (root / "a.log").is_symlink(), "the key itself should be gone"
        assert (root / "real.bin").read_bytes() == b"the actual object"

    @pytest.mark.asyncio
    async def test_reads_still_follow_an_in_root_symlink(self, temp_storage_dir):
        """The read side is unchanged by that: ``open`` follows the link."""
        root = Path(temp_storage_dir)
        (root / "real.bin").write_bytes(b"payload")
        (root / "a.log").symlink_to(root / "real.bin")

        backend = self._backend(temp_storage_dir)

        assert await backend.retrieve_file("a.log") == b"payload"
        assert await backend.file_exists("a.log")

    # -- Every entry point that touches the filesystem ------------------------

    @pytest.mark.asyncio
    async def test_file_exists_is_guarded(self, temp_storage_dir, outside_dir):
        (outside_dir / "secret.txt").write_text("private")
        (Path(temp_storage_dir) / "peek.txt").symlink_to(outside_dir / "secret.txt")
        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.file_exists("peek.txt")

    @pytest.mark.asyncio
    async def test_get_file_info_is_guarded(self, temp_storage_dir, outside_dir):
        (outside_dir / "secret.txt").write_text("private")
        (Path(temp_storage_dir) / "peek.txt").symlink_to(outside_dir / "secret.txt")
        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.get_file_info("peek.txt")

    @pytest.mark.asyncio
    async def test_generate_download_url_is_guarded(
        self, temp_storage_dir, outside_dir
    ):
        (outside_dir / "secret.txt").write_text("private")
        (Path(temp_storage_dir) / "peek.txt").symlink_to(outside_dir / "secret.txt")
        backend = self._backend(temp_storage_dir)

        with pytest.raises(ValueError, match="Invalid storage key"):
            await backend.generate_download_url("peek.txt")


# =============================================================================
# S3 Backend Tests
# =============================================================================


@pytest.mark.skipif(
    condition=True, reason="boto3 not installed"  # Skip S3 tests - requires boto3
)
class TestS3StorageBackend:
    """Tests for S3 storage backend (with mocked boto3)."""

    def test_s3_backend_requires_boto3(self):
        """Test S3 backend raises ImportError if boto3 not installed."""
        with patch.dict("sys.modules", {"boto3": None}):
            # Force reimport
            import importlib

            from faultmaven.infrastructure.storage import s3

            # Check BOTO3_AVAILABLE flag
            # (actual test would need more complex import mocking)

    @pytest.mark.asyncio
    async def test_generate_upload_url_shape(self, mock_boto3_client):
        """Test S3 upload URL has presigned URL shape."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                url = await backend.generate_upload_url(
                    key="evidence/file.log",
                    content_type="text/plain",
                )

                # Verify presigned URL shape
                assert "s3.amazonaws.com" in url.url or "X-Amz" in url.url
                assert url.method == "PUT"
                assert url.headers.get("Content-Type") == "text/plain"

                # Verify boto3 was called correctly
                mock_boto3_client.generate_presigned_url.assert_called_once()
                call_args = mock_boto3_client.generate_presigned_url.call_args
                assert call_args[1]["ClientMethod"] == "put_object"

            except ImportError:
                pytest.skip("boto3 not installed")

    @pytest.mark.asyncio
    async def test_generate_download_url_shape(self, mock_boto3_client):
        """Test S3 download URL has presigned URL shape."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                url = await backend.generate_download_url(
                    key="evidence/file.log",
                    filename="file.log",
                )

                # Verify presigned URL shape
                assert "s3.amazonaws.com" in url.url or "X-Amz" in url.url
                assert url.method == "GET"

            except ImportError:
                pytest.skip("boto3 not installed")

    @pytest.mark.asyncio
    async def test_s3_store_and_retrieve(self, mock_boto3_client):
        """Test S3 store and retrieve operations."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                # Store file
                stored = await backend.store_file(
                    key="test/file.txt",
                    data=b"test content",
                    content_type="text/plain",
                )

                assert stored.key == "test/file.txt"
                mock_boto3_client.put_object.assert_called_once()

            except ImportError:
                pytest.skip("boto3 not installed")

    def test_s3_storage_type(self, mock_boto3_client):
        """Test S3 storage type."""
        with patch("boto3.client", return_value=mock_boto3_client):
            try:
                from faultmaven.infrastructure.storage.base import StorageType
                from faultmaven.infrastructure.storage.s3 import S3StorageBackend

                backend = S3StorageBackend(
                    bucket_name="test-bucket",
                    region="us-east-1",
                )

                assert backend.get_storage_type() == StorageType.S3

            except ImportError:
                pytest.skip("boto3 not installed")


# =============================================================================
# Factory Tests
# =============================================================================


class TestStorageFactory:
    """Tests for storage backend factory."""

    def test_factory_creates_filesystem_by_default(self, clean_env, temp_storage_dir):
        """Test factory creates filesystem backend by default."""
        os.environ["STORAGE_BACKEND"] = "filesystem"

        from faultmaven.infrastructure.storage import (
            StorageType,
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(storage_backend=MagicMock(value="filesystem")),
                evidence_storage=MagicMock(evidence_storage_root=temp_storage_dir),
                server=MagicMock(host="localhost", port=8000),
            )

            backend = get_storage_backend(reset=True)

            assert backend.get_storage_type() == StorageType.FILESYSTEM

    def test_factory_explicit_override(self, clean_env, temp_storage_dir):
        """Test factory accepts explicit storage type."""
        from faultmaven.infrastructure.storage import (
            StorageType,
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(
                    storage_backend=MagicMock(value="s3")  # Settings say S3
                ),
                evidence_storage=MagicMock(evidence_storage_root=temp_storage_dir),
                server=MagicMock(host="localhost", port=8000),
            )

            # But we explicitly request filesystem
            backend = get_storage_backend(storage_type="filesystem", reset=True)

            assert backend.get_storage_type() == StorageType.FILESYSTEM

    def test_factory_s3_requires_bucket_name(self, clean_env):
        """Test factory raises error if S3 requested without bucket name."""
        os.environ.pop("S3_BUCKET_NAME", None)

        from faultmaven.infrastructure.storage import (
            get_storage_backend,
            reset_storage_backend,
        )

        reset_storage_backend()

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(storage_backend=MagicMock(value="s3")),
                evidence_storage=MagicMock(
                    s3_bucket_name=None,  # Missing bucket name
                    s3_region="us-east-1",
                    s3_key_prefix="evidence/",
                    s3_endpoint_url=None,
                ),
            )

            with pytest.raises(ValueError, match="S3_BUCKET_NAME"):
                get_storage_backend(reset=True)


# =============================================================================
# Integration Tests
# =============================================================================


class TestStorageIntegration:
    """Integration tests for storage backends."""

    @_REQUIRES_BOTO3
    @pytest.mark.asyncio
    async def test_evidence_storage_honours_s3_backend_selection(
        self, clean_env, mock_boto3_client
    ):
        """STORAGE_BACKEND=s3 must actually route evidence blobs to S3.

        This is the #689 regression. The evidence path used to write straight
        to the local filesystem, so the whole storage-backend abstraction —
        interface, S3 implementation, factory, setting — was inert: flipping
        STORAGE_BACKEND changed nothing. Asserting on the backend alone cannot
        catch that, so this drives the real FileStorageService and checks that
        the bytes reached the S3 client.
        """
        from faultmaven.infrastructure.storage import (
            get_storage_backend,
            reset_storage_backend,
        )
        from faultmaven.modules.evidence.domain.services.file_storage_service import (
            FileStorageService,
        )

        try:
            with patch("faultmaven.config.settings.get_settings") as mock_settings:
                mock_settings.return_value = MagicMock(
                    providers=MagicMock(storage_backend=MagicMock(value="s3")),
                    evidence_storage=MagicMock(
                        s3_bucket_name="evidence-bucket",
                        s3_region="us-east-1",
                        s3_key_prefix="",
                        s3_endpoint_url=None,
                    ),
                )
                with patch("boto3.client", return_value=mock_boto3_client):
                    # Resolved exactly as production does: the service takes
                    # whatever STORAGE_BACKEND selects.
                    service = FileStorageService(
                        backend=get_storage_backend(reset=True)
                    )

                    result = await service.store_file(
                        file_data=b"kernel panic at 03:14",
                        original_filename="error.log",
                        organization_id="org123",
                        case_id="case456",
                        mime_type="text/plain",
                    )
        finally:
            reset_storage_backend()

        # The blob went to S3, not to any local directory.
        stored = {
            call.kwargs["Key"]: call.kwargs["Body"]
            for call in mock_boto3_client.put_object.call_args_list
        }
        assert stored[result["storage_key"]] == b"kernel panic at 03:14"
        # ...and so did its orphan-tracking sidecar, or cleanup would never
        # see the file on an S3 deployment.
        assert f"{result['storage_key']}.meta.json" in stored

    @_REQUIRES_BOTO3
    @pytest.mark.asyncio
    async def test_s3_calls_do_not_run_on_the_event_loop(self, mock_boto3_client):
        """boto3 is synchronous — its calls must not block the event loop.

        A blocking S3 round-trip stalls every other request in the process,
        including /health, which a Kubernetes liveness probe escalates into a
        pod kill. Asserting on the thread identity is the mechanical way to
        prove the call was offloaded.
        """
        import threading

        from faultmaven.infrastructure.storage.s3 import S3StorageBackend

        loop_thread = threading.get_ident()
        call_threads = []

        def _record(**kwargs):
            call_threads.append(threading.get_ident())
            return {}

        mock_boto3_client.put_object.side_effect = _record

        with patch("boto3.client", return_value=mock_boto3_client):
            backend = S3StorageBackend(bucket_name="test-bucket")

        await backend.store_file(key="k", data=b"payload")

        assert call_threads, "put_object was never called"
        assert loop_thread not in call_threads

    @pytest.mark.asyncio
    async def test_evidence_upload_flow_uses_interface(self, filesystem_backend):
        """Test that evidence upload flow works through interface only."""
        from faultmaven.infrastructure.storage.base import IFileStorageBackend

        # Verify backend is an IFileStorageBackend
        assert isinstance(filesystem_backend, IFileStorageBackend)

        # Simulate evidence upload flow
        key = "evidence/org123/case456/error.log"
        content = b"Error log content here"
        content_type = "text/plain"

        # Step 1: Generate upload URL (client would use this)
        upload_url = await filesystem_backend.generate_upload_url(
            key=key,
            content_type=content_type,
        )
        assert upload_url.url is not None
        assert not upload_url.is_expired

        # Step 2: Store file (server-side or via presigned URL)
        stored = await filesystem_backend.store_file(
            key=key,
            data=content,
            content_type=content_type,
        )
        assert stored.size_bytes == len(content)

        # Step 3: Generate download URL
        download_url = await filesystem_backend.generate_download_url(
            key=key,
            filename="error.log",
        )
        assert download_url.url is not None

        # Step 4: Verify file info
        info = await filesystem_backend.get_file_info(key)
        assert info is not None
        # Note: Filesystem backend doesn't persist content_type metadata,
        # returns default application/octet-stream
        assert info.content_type == "application/octet-stream"

        # Step 5: Retrieve content
        retrieved = await filesystem_backend.retrieve_file(key)
        assert retrieved == content

        # Step 6: Cleanup
        deleted = await filesystem_backend.delete_file(key)
        assert deleted is True

    @pytest.mark.asyncio
    async def test_url_expiration(self, filesystem_backend):
        """Test presigned URL expiration tracking."""
        key = "test/expiry.txt"
        await filesystem_backend.store_file(key, b"test")

        # Short expiry
        url = await filesystem_backend.generate_download_url(
            key=key,
            expires_in=timedelta(seconds=10),
        )

        assert not url.is_expired
        assert url.seconds_until_expiry > 0
        assert url.seconds_until_expiry <= 10

        # Long expiry
        url_long = await filesystem_backend.generate_download_url(
            key=key,
            expires_in=timedelta(hours=24),
        )

        assert url_long.seconds_until_expiry > 3600  # More than 1 hour
