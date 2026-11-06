"""The image build fails if a store its TLS clients read trusts a banned root.

``scripts/check_image_trust_store.py`` runs at the Dockerfile's last root step.
Each test builds real stores from generated certificates, points OpenSSL at them
the way an image's defaults would (``SSL_CERT_FILE``, ``SSL_CERT_DIR``), and runs
the script's ``main`` over them, with the fixtures' digests standing in for the
real roots'.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_image_trust_store.py"

_spec = importlib.util.spec_from_file_location("check_image_trust_store", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_KEY = ec.generate_private_key(ec.SECP256R1())
MIN_ROOTS = 3


@dataclass(frozen=True)
class Root:
    pem: bytes
    sha256: str


def make_root(name: str) -> Root:
    """A self-signed CA, which is what `get_ca_certs` lists."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(_KEY.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(_KEY, hashes.SHA256())
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return Root(
        certificate.public_bytes(serialization.Encoding.PEM),
        hashlib.sha256(der).hexdigest(),
    )


KNOWN = make_root("Known Root")
BAD = make_root("Banned Root")
OTHERS = [make_root(f"Other Root {i}") for i in range(3)]
CLEAN = [KNOWN, *OTHERS]
BANNED = {BAD.sha256: "Banned Root"}


def bundle(roots: list[Root]) -> bytes:
    return b"".join(root.pem for root in roots)


class Image:
    """The three stores and PIP_CERT, clean until a test changes one."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cafile = tmp_path / "ca-certificates.crt"
        self.capath = tmp_path / "certs"
        self.certifi = tmp_path / "cacert.pem"
        self.capath.mkdir()
        self.cafile.write_bytes(bundle(CLEAN))
        self.certifi.write_bytes(bundle(CLEAN))
        for i, root in enumerate(CLEAN):
            (self.capath / f"root{i}.pem").write_bytes(root.pem)
        monkeypatch.setenv("SSL_CERT_FILE", str(self.cafile))
        monkeypatch.setenv("SSL_CERT_DIR", str(self.capath))
        monkeypatch.setenv("PIP_CERT", str(self.cafile))
        monkeypatch.setattr(check.certifi, "where", lambda: str(self.certifi))

    def run(self, **overrides: object) -> None:
        arguments = {
            "banned": BANNED,
            "known_root": KNOWN.sha256,
            "min_roots": MIN_ROOTS,
        }
        check.main(**{**arguments, **overrides})


@pytest.fixture
def image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Image:
    return Image(tmp_path, monkeypatch)


def failure(image: Image, **overrides: object) -> list[str]:
    """The problem lines the build fails with."""
    with pytest.raises(SystemExit) as exit_info:
        image.run(**overrides)
    assert isinstance(exit_info.value.code, str), exit_info.value.code
    header, *problems = exit_info.value.code.splitlines()
    assert header == "trust store check failed:"
    return problems


def test_clean_stores_pass_and_report_their_counts(image, capsys):
    image.run()

    assert capsys.readouterr().out.strip() == (
        "trust store OK: cafile 4, capath 4, certifi 4 certificates"
    )


# ---- A banned root in each store ------------------------------------------


def test_a_banned_root_in_the_cafile_fails(image):
    image.cafile.write_bytes(bundle([*CLEAN, BAD]))

    assert failure(image) == ["cafile trusts Banned Root"]


def test_a_banned_root_in_its_own_capath_file_fails(image):
    (image.capath / "ee64a828.0").write_bytes(BAD.pem)

    assert failure(image) == ["capath trusts Banned Root"]


def test_a_banned_root_later_in_a_multi_cert_capath_file_fails(image):
    (image.capath / "extra.pem").write_bytes(bundle([OTHERS[0], BAD]))

    assert failure(image) == ["capath trusts Banned Root"]


def test_a_banned_root_behind_a_capath_symlink_fails(image, tmp_path):
    """The shape OpenSSL actually reads: a hash-named link into another directory."""
    target = tmp_path / "elsewhere.crt"
    target.write_bytes(BAD.pem)
    (image.capath / "14bc7599.0").symlink_to(target)

    assert failure(image) == ["capath trusts Banned Root"]


def test_a_banned_root_in_a_capath_subdirectory_fails(image):
    (image.capath / "nested").mkdir()
    (image.capath / "nested" / "root.pem").write_bytes(BAD.pem)

    assert failure(image) == ["capath trusts Banned Root"]


def test_a_banned_root_in_certifi_fails(image):
    image.certifi.write_bytes(bundle([*CLEAN, BAD]))

    assert failure(image) == ["certifi trusts Banned Root"]


def test_every_problem_is_reported_not_just_the_first(image):
    image.cafile.write_bytes(bundle([*CLEAN, BAD]))
    image.certifi.write_bytes(bundle([*CLEAN, BAD]))
    (image.capath / "bad.pem").write_bytes(BAD.pem)

    assert failure(image) == [
        "cafile trusts Banned Root",
        "capath trusts Banned Root",
        "certifi trusts Banned Root",
    ]


# ---- Positive controls: an empty store or a blind scan cannot pass --------


def _empty_capath(image: Image) -> None:
    for path in image.capath.iterdir():
        path.unlink()


@pytest.mark.parametrize(
    ("empty", "expected"),
    [
        (
            lambda image: image.cafile.write_bytes(b""),
            [
                f"cafile holds 0 certificates, under {MIN_ROOTS}",
                f"cafile lacks the known root {KNOWN.sha256}",
            ],
        ),
        (
            _empty_capath,
            [
                f"capath holds 0 certificates, under {MIN_ROOTS}",
                f"capath lacks the known root {KNOWN.sha256}",
            ],
        ),
        (
            lambda image: image.certifi.write_bytes(b""),
            [f"certifi holds 0 certificates, under {MIN_ROOTS}"],
        ),
    ],
    ids=["cafile", "capath", "certifi"],
)
def test_an_empty_store_fails(image, empty, expected):
    empty(image)

    assert failure(image) == expected


@pytest.mark.parametrize("store", ["cafile", "capath", "certifi"])
def test_a_store_under_the_count_fails(image, store):
    two_roots = bundle([KNOWN, OTHERS[0]])
    if store == "capath":
        _empty_capath(image)
        (image.capath / "two.pem").write_bytes(two_roots)
    else:
        getattr(image, store).write_bytes(two_roots)

    assert failure(image) == [f"{store} holds 2 certificates, under {MIN_ROOTS}"]


def test_copies_of_one_root_do_not_meet_the_count(image):
    """In a real capath every root appears three times: `.pem`, hash link, bundle."""
    _empty_capath(image)
    for i in range(MIN_ROOTS + 1):
        (image.capath / f"copy{i}.pem").write_bytes(KNOWN.pem)

    assert failure(image) == [f"capath holds 1 certificates, under {MIN_ROOTS}"]


def test_the_default_floor_is_100_roots(image, capsys):
    roots = [KNOWN, *(make_root(f"Root {i}") for i in range(99))]
    _empty_capath(image)
    for store in (image.cafile, image.certifi, image.capath / "all.pem"):
        store.write_bytes(bundle(roots))
    check.main(banned=BANNED, known_root=KNOWN.sha256)
    assert "cafile 100, capath 100, certifi 100" in capsys.readouterr().out

    image.cafile.write_bytes(bundle(roots[:-1]))
    with pytest.raises(SystemExit) as exit_info:
        check.main(banned=BANNED, known_root=KNOWN.sha256)
    assert "cafile holds 99 certificates, under 100" in exit_info.value.code


@pytest.mark.parametrize("store", ["cafile", "capath"])
def test_the_known_root_missing_fails(image, store):
    without_known = [*OTHERS, make_root("Another Root")]
    if store == "capath":
        _empty_capath(image)
        (image.capath / "others.pem").write_bytes(bundle(without_known))
    else:
        image.cafile.write_bytes(bundle(without_known))

    assert failure(image) == [f"{store} lacks the known root {KNOWN.sha256}"]


def test_certifi_is_not_held_to_the_known_root(image):
    image.certifi.write_bytes(bundle([*OTHERS, make_root("Another Root")]))

    image.run()


def test_a_cafile_that_does_not_exist_fails(image, monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent.crt"))

    problems = failure(image)

    assert problems[0].startswith("cafile holds 0 certificates")
    assert problems[-1].startswith("OpenSSL's cafile None is not PIP_CERT")


def test_a_capath_that_does_not_exist_fails(image, monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "absent"))

    assert failure(image)[0].startswith("capath holds 0 certificates")


# ---- One file for OpenSSL and pip -----------------------------------------


def test_pip_cert_pointing_elsewhere_fails(image, monkeypatch, tmp_path):
    other = tmp_path / "other.crt"
    other.write_bytes(bundle(CLEAN))
    monkeypatch.setenv("PIP_CERT", str(other))

    assert failure(image) == [
        f"OpenSSL's cafile {image.cafile} is not PIP_CERT {other}"
    ]


def test_pip_cert_unset_fails(image, monkeypatch):
    monkeypatch.delenv("PIP_CERT")

    assert failure(image) == [f"OpenSSL's cafile {image.cafile} is not PIP_CERT unset"]


def test_pip_cert_reaching_the_cafile_through_a_link_passes(
    image, monkeypatch, tmp_path
):
    """OpenSSL's default path is itself a link to the bundle PIP_CERT names."""
    link = tmp_path / "cert.pem"
    link.symlink_to(image.cafile)
    monkeypatch.setenv("SSL_CERT_FILE", str(link))

    image.run()


# ---- What the capath scan skips -------------------------------------------


def test_the_scan_skips_what_openssl_could_not_load_either(image, tmp_path):
    (image.capath / "notes.txt").write_bytes(b"not a certificate\n")
    (image.capath / "binary.der").write_bytes(bytes(range(256)))
    (image.capath / "dangling.0").symlink_to(tmp_path / "absent")

    image.run()


# ---- The build-time failure itself ----------------------------------------


@pytest.mark.parametrize("optimize", ["", "1"], ids=["default", "PYTHONOPTIMIZE"])
def test_the_script_exits_non_zero_with_its_message(tmp_path, optimize):
    """A failure the interpreter can strip is no failure: `python -O` removes `assert`."""
    capath = tmp_path / "certs"
    capath.mkdir()
    cafile = tmp_path / "empty.crt"
    cafile.write_bytes(b"")
    env = {
        **os.environ,
        "SSL_CERT_FILE": str(cafile),
        "SSL_CERT_DIR": str(capath),
        "PIP_CERT": str(cafile),
        "PYTHONOPTIMIZE": optimize,
    }

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "trust store check failed:" in result.stderr
    assert "cafile holds 0 certificates" in result.stderr
    assert "trust store OK" not in result.stdout
