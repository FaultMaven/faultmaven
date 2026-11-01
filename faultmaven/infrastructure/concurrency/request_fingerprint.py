"""The fingerprint of a keyed multipart request: what a receipt matches a retry by.

One function for every receipt (the turn's, #1888; the KB upload's and
conversion's, #2433). The form fields verbatim, and each file by name, size
and the sha256 of its CONTENT, never by ``UploadFile`` metadata alone: two
different files of one name and size are two different requests. Canonical
JSON (sorted keys), so the fingerprint is a function of the inputs and nothing
else, and a receipt stored under one release matches its retry under the next.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple, Union

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FileDigest:
    """A file as the fingerprint sees it, for a file read from disk in chunks
    rather than held in memory (:func:`digest_file`)."""

    name: str
    size: int
    sha256: str


def digest_file(name: str, path: Path) -> FileDigest:
    """``path``'s digest, read in chunks. Blocking: call it off the event loop."""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return FileDigest(name=name, size=size, sha256=digest.hexdigest())


def request_fingerprint(
    fields: Mapping[str, Optional[str]],
    files: Sequence[Union[Tuple[str, bytes], FileDigest]],
) -> str:
    """sha256 over a request's semantic inputs, as the client sent them."""
    if "files" in fields:
        raise ValueError("'files' is the fingerprint's own key, not a field name")
    digests = [
        (
            f
            if isinstance(f, FileDigest)
            else FileDigest(f[0], len(f[1]), hashlib.sha256(f[1]).hexdigest())
        )
        for f in files
    ]
    canonical = json.dumps(
        {
            **fields,
            "files": [
                {"name": d.name, "size": d.size, "sha256": d.sha256} for d in digests
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
