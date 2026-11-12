"""Fail the image build if a root store its TLS clients read trusts a banned root,
matched by SHA-256 over what OpenSSL parses: OpenSSL's default cafile and capath,
certifi, every grpcio roots.pem, and the bundle of every pip and pip wheel the
image holds (#2665).
Root counts and a known root keep an empty store from passing.

Takes one optional argument, the directory searched for pips (default ``/``)."""

import hashlib
import os
import re
import ssl
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import certifi
except ImportError:  # a bare base: the other stores are still read
    certifi = None

G3 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"  # pragma: allowlist secret
AAA = "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4"  # pragma: allowlist secret
ISRG_ROOT_X1 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"  # pragma: allowlist secret
BANNED = {G3: "emSign ECC Root CA - G3", AAA: "AAA Certificate Services"}
MIN_ROOTS = 100
#: From this version pip's truststore context also loads its vendored bundle
#: (``PIP_CERT`` only adds to it), unless ``PIP_USE_DEPRECATED`` holds
#: ``legacy-certs``. Below it, ``PIP_CERT`` replaces the bundle.
PIP_ADDS_ITS_BUNDLE = (24, 2)
WHEEL_BUNDLE = "pip/_vendor/certifi/cacert.pem"
_NOT_SEARCHED = ("proc", "sys", "dev")


def loaded_digests(files) -> set[str]:
    """What OpenSSL itself parses from these files: no PEM spelling can escape."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for file in files:
        try:
            context.load_verify_locations(cafile=file)
        except ssl.SSLError as error:  # A refused block can follow a trusted one.
            if error.reason != "NO_CERTIFICATE_OR_CRL_FOUND":
                raise
        except OSError:  # A dangling link: OpenSSL cannot load it either.
            continue
    ders = context.get_ca_certs(binary_form=True)
    return {hashlib.sha256(der).hexdigest() for der in ders}


def store_problems(store, digests, banned, min_roots, known_root=None) -> list[str]:
    problems = [f"{store} trusts {banned[sha]}" for sha in banned if sha in digests]
    if len(digests) < min_roots:
        problems.append(f"{store} holds {len(digests)} certificates, under {min_roots}")
    if known_root and known_root not in digests:
        problems.append(f"{store} lacks the known root {known_root}")
    return problems


def stores(root: str) -> list[tuple[str, str, Path]]:
    """(kind, version, file) for each pip's vendored bundle, each pip wheel and
    each grpcio ``roots.pem`` under ``root``. Versions are read as text: no pip
    is imported."""
    skipped = {os.path.join(root, name) for name in _NOT_SEARCHED}
    skipped.add(os.environ.get("HF_HOME", ""))  # the model cache: large, no pip
    found = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if os.path.join(directory, name) not in skipped
        )
        here = Path(directory)
        if here.parts[-3:] == ("pip", "_vendor", "certifi") and "cacert.pem" in names:
            try:
                text = (here.parents[1] / "__init__.py").read_text(errors="replace")
            except OSError:
                text = ""
            version = re.search(r"__version__ = [\"']([^\"']+)", text)
            found.append(("pip", version[1] if version else "", here / "cacert.pem"))
        if (
            here.parts[-3:] == ("grpc", "_cython", "_credentials")
            and "roots.pem" in names
        ):
            found.append(("grpcio", "", here / "roots.pem"))
        for name in sorted(names):
            if wheel := re.fullmatch(r"pip-([^-]+)-.+\.whl", name):
                found.append(("pip wheel", wheel[1], here / name))
    return found


def wheel_digests(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as scratch:
        if WHEEL_BUNDLE not in archive.namelist():
            return set()
        bundle = Path(scratch, "cacert.pem")
        bundle.write_bytes(archive.read(WHEEL_BUNDLE))
        return loaded_digests([bundle])


def found_problems(root, banned, cafile) -> tuple[list[str], list[str]]:
    """(problems, report lines) for every store the walk finds. Each grpcio
    ``roots.pem`` is gRPC's default in a process apply_root_set did not start:
    the image links it to the OS bundle, so it holds exactly the cafile's roots.
    A pip that adds its bundle to PIP_CERT is judged; one that does not is
    reported."""
    legacy = "legacy-certs" in os.environ.get("PIP_USE_DEPRECATED", "").split()
    problems, reports = [], []
    for kind, version, file in stores(root):
        wheel = kind == "pip wheel"
        digests = wheel_digests(file) if wheel else loaded_digests([file])
        if kind == "grpcio":
            problems += store_problems(f"grpcio's {file}", digests, banned, 0)
            if digests != cafile:
                problems.append(
                    f"grpcio's {file} is not the cafile: link it to the OS bundle"
                )
            reports.append(f"grpcio {file}: {len(digests)} certificates")
            continue
        parsed = tuple(int(part) for part in re.findall(r"\d+", version)[:3])
        store = f"{kind} {version or '(version unread)'} {file}"
        if legacy or (parsed and parsed < PIP_ADDS_ITS_BUNDLE):
            why = "legacy-certs" if legacy else "below 24.2, PIP_CERT replaces it"
            reports.append(f"{store}: {len(digests)} certificates, not judged ({why})")
            continue
        remedy = (
            "remove it or repack it with the OS bundle"
            if wheel
            else "link it to the OS bundle"
        )
        problems += [
            f"{line}: {remedy}" for line in store_problems(store, digests, banned, 0)
        ]
        reports.append(f"{store}: {len(digests)} certificates")
    return problems, reports


def main(banned=BANNED, known_root=ISRG_ROOT_X1, min_roots=MIN_ROOTS, root="/") -> None:
    paths = ssl.get_default_verify_paths()
    ders = ssl.create_default_context().get_ca_certs(binary_form=True)
    cafile = {hashlib.sha256(der).hexdigest() for der in ders}
    walk = os.walk(paths.capath or "")
    capath = loaded_digests(Path(d, name) for d, _, names in walk for name in names)
    problems = [
        *store_problems("cafile", cafile, banned, min_roots, known_root),
        *store_problems("capath", capath, banned, min_roots, known_root),
    ]
    counts = [f"cafile {len(cafile)}", f"capath {len(capath)}"]
    if certifi is not None:
        bundle = loaded_digests([certifi.where()])
        problems += store_problems("certifi", bundle, banned, min_roots)
        counts.append(f"certifi {len(bundle)}")
    pip_cert = os.environ.get("PIP_CERT", "unset")
    if not paths.cafile or os.path.realpath(paths.cafile) != os.path.realpath(pip_cert):
        problems.append(f"OpenSSL's cafile {paths.cafile} is not PIP_CERT {pip_cert}")
    found, reports = found_problems(root, banned, cafile)
    problems += found
    if notes := ["certifi not installed"] * (certifi is None) + reports:
        print("\n".join(notes))
    if problems:
        sys.exit("trust store check failed:\n" + "\n".join(problems))
    print(f"trust store OK: {', '.join(counts)} certificates")


if __name__ == "__main__":
    main(root=sys.argv[1] if len(sys.argv) > 1 else "/")
