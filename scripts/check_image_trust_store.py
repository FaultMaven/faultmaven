"""Fail the image build if a TLS store its clients read trusts a banned root.

Checks OpenSSL's default cafile and capath (aiohttp, and pip through PIP_CERT) and
certifi (httpx, requests), by the SHA-256 of each certificate's DER. The positive
controls keep an empty store, or a scan that read nothing, from passing.
"""

import hashlib
import os
import re
import ssl
import sys
from pathlib import Path

import certifi

G3 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"  # pragma: allowlist secret
AAA = "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4"  # pragma: allowlist secret
ISRG_ROOT_X1 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"  # pragma: allowlist secret
BANNED = {G3: "emSign ECC Root CA - G3", AAA: "AAA Certificate Services"}
MIN_ROOTS = 100
PEM = rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----"


def pem_digests(pem: bytes) -> set[str]:
    blocks = re.findall(PEM, pem, re.DOTALL)
    ders = (ssl.PEM_cert_to_DER_cert(block.decode()) for block in blocks)
    return {hashlib.sha256(der).hexdigest() for der in ders}


def capath_digests(capath: str | None) -> set[str]:
    """Every certificate in every readable file under OpenSSL's trusted directory."""
    found: set[str] = set()
    for folder, _, names in os.walk(capath or ""):
        for name in names:
            try:
                found |= pem_digests(Path(folder, name).read_bytes())
            except OSError:  # A dangling link: OpenSSL cannot load it either.
                continue
    return found


def store_problems(store, digests, banned, min_roots, known_root=None) -> list[str]:
    problems = [f"{store} trusts {banned[sha]}" for sha in banned if sha in digests]
    if len(digests) < min_roots:
        problems.append(f"{store} holds {len(digests)} certificates, under {min_roots}")
    if known_root and known_root not in digests:
        problems.append(f"{store} lacks the known root {known_root}")
    return problems


def main(banned=BANNED, known_root=ISRG_ROOT_X1, min_roots=MIN_ROOTS) -> None:
    paths = ssl.get_default_verify_paths()
    ders = ssl.create_default_context().get_ca_certs(binary_form=True)
    cafile = {hashlib.sha256(der).hexdigest() for der in ders}
    capath = capath_digests(paths.capath)
    bundle = pem_digests(Path(certifi.where()).read_bytes())
    problems = [
        *store_problems("cafile", cafile, banned, min_roots, known_root),
        *store_problems("capath", capath, banned, min_roots, known_root),
        *store_problems("certifi", bundle, banned, min_roots),
    ]
    pip_cert = os.environ.get("PIP_CERT", "unset")
    if not paths.cafile or os.path.realpath(paths.cafile) != os.path.realpath(pip_cert):
        problems.append(f"OpenSSL's cafile {paths.cafile} is not PIP_CERT {pip_cert}")
    if problems:
        sys.exit("trust store check failed:\n" + "\n".join(problems))
    counts = f"cafile {len(cafile)}, capath {len(capath)}, certifi {len(bundle)}"
    print(f"trust store OK: {counts} certificates")


if __name__ == "__main__":
    main()
