"""tiktoken's ``cl100k_base`` encoding, read from the repository, not the network.

``faultmaven/utils/token_estimation.py`` counts tokens with
``tiktoken.get_encoding("cl100k_base")``. tiktoken loads that encoding from a
file it downloads from ``openaipublic.blob.core.windows.net`` the first time a
process asks, and keeps it in a cache directory (``$TIKTOKEN_CACHE_DIR``, else
``<tmp>/data-gym-cache``). A warm machine never notices. A cold one -- every CI
runner -- makes a live HTTPS request to Azure storage from inside the test
suite, once per process, and the suite passes only because that request
happens to succeed. The network guard (``tests/network_guard.py``) refuses it,
and the char-based fallback the code takes when it fails is not a substitute:
45 tests assert on real token counts.

So the file ships with the tests, and ``tiktoken.load.read_file_cached`` is
told to serve it for that one URL. Replacing the loader rather than seeding a
cache directory is deliberate: it holds when a test empties or repoints
``os.environ`` (``TIKTOKEN_CACHE_DIR`` would then be lost, and the first
``get_encoding`` in the process would go to the network), and it writes
nothing. Every other URL is passed to tiktoken's own loader, so an encoding
this module does not carry still behaves as it always did (and is refused by
the guard when it needs the network).

``resources/tiktoken/cl100k_base.tiktoken.gz`` is the file at ``BLOB_URL``
compressed (776 kB, under the repository's 1000 kB large-file hook), and
decompresses to exactly the bytes tiktoken's own check expects: SHA-256
``BLOB_SHA256``. To regenerate it, download ``BLOB_URL``, check the hash, and
``gzip -9 -n`` it. The hash is checked again every time it is read.
"""

from __future__ import annotations

import gzip
import hashlib
import pathlib
from typing import Optional

#: Where tiktoken's ``cl100k_base`` loader asks for the encoding.
BLOB_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"

#: The SHA-256 tiktoken itself expects of that file (``tiktoken_ext/openai_public.py``).
BLOB_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

ARCHIVE = (
    pathlib.Path(__file__).resolve().parent
    / "resources"
    / "tiktoken"
    / "cl100k_base.tiktoken.gz"
)

_installed = False


def _blob() -> bytes:
    data = gzip.decompress(ARCHIVE.read_bytes())
    if hashlib.sha256(data).hexdigest() != BLOB_SHA256:
        raise ValueError(
            f"{ARCHIVE} does not decompress to the file at {BLOB_URL} "
            f"(expected SHA-256 {BLOB_SHA256})"
        )
    return data


def install() -> None:
    """Serve ``BLOB_URL`` from ``ARCHIVE``. Idempotent (the conftest is imported twice)."""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        import tiktoken.load as tiktoken_load
    except ImportError:
        return  # tiktoken is optional; the code under test then takes its fallback

    real = tiktoken_load.read_file_cached
    blob: Optional[bytes] = None

    def read_file_cached(blobpath: str, expected_hash: Optional[str] = None) -> bytes:
        nonlocal blob
        if blobpath != BLOB_URL:
            return real(blobpath, expected_hash)
        if blob is None:
            blob = _blob()
        return blob

    tiktoken_load.read_file_cached = read_file_cached
