"""The image build fails if a store its TLS clients read trusts a banned root.

``scripts/check_image_trust_store.py`` runs at the Dockerfile's last root step.
Each test builds real stores from generated certificates, points OpenSSL at them
the way an image's defaults would (``SSL_CERT_FILE``, ``SSL_CERT_DIR``), and runs
the script's ``main`` over them, with the fixtures' digests standing in for the
real roots'. One test runs the script on its own defaults, over the real roots.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import importlib.util
import os
import ssl
import subprocess
import sys
import zipfile
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

_BAD_DER = base64.b64decode(b"".join(BAD.pem.splitlines()[1:-1]))
# What OpenSSL loads as a certificate besides the plain block.
SPELLINGS = {
    "trusted-label": BAD.pem.replace(b"CERTIFICATE", b"TRUSTED CERTIFICATE"),
    "x509-label": BAD.pem.replace(b"CERTIFICATE", b"X509 CERTIFICATE"),
    "der-plus-aux": b"-----BEGIN CERTIFICATE-----\n"
    + base64.encodebytes(_BAD_DER + b"\x30\x00")
    + b"-----END CERTIFICATE-----\n",
    "commented-begin": b"# -----BEGIN CERTIFICATE-----\n" + BAD.pem,
}
REFUSED_BLOCKS = {
    "bad-base64": b"-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n",
    "non-utf8": b"-----BEGIN CERTIFICATE-----\n\xff\n-----END CERTIFICATE-----\n",
    "unterminated": b"-----BEGIN CERTIFICATE-----\nAAAA\n",
}

# The real roots the script bans and the one it requires: public CA certificates, here
# so that no test needs the network or a bundle that still carries them. G3 and AAA are
# from the certifi pip 24.0 vendors, ISRG from the pinned certifi. Loading a certificate
# does not check its dates.
REAL_G3 = b"""\
-----BEGIN CERTIFICATE-----
MIICTjCCAdOgAwIBAgIKPPYHqWhwDtqLhDAKBggqhkjOPQQDAzBrMQswCQYDVQQG
EwJJTjETMBEGA1UECxMKZW1TaWduIFBLSTElMCMGA1UEChMcZU11ZGhyYSBUZWNo
bm9sb2dpZXMgTGltaXRlZDEgMB4GA1UEAxMXZW1TaWduIEVDQyBSb290IENBIC0g
RzMwHhcNMTgwMjE4MTgzMDAwWhcNNDMwMjE4MTgzMDAwWjBrMQswCQYDVQQGEwJJ
TjETMBEGA1UECxMKZW1TaWduIFBLSTElMCMGA1UEChMcZU11ZGhyYSBUZWNobm9s
b2dpZXMgTGltaXRlZDEgMB4GA1UEAxMXZW1TaWduIEVDQyBSb290IENBIC0gRzMw
djAQBgcqhkjOPQIBBgUrgQQAIgNiAAQjpQy4LRL1KPOxst3iAhKAnjlfSU2fySU0
WXTsuwYc58Byr+iuL+FBVIcUqEqy6HyC5ltqtdyzdc6LBtCGI79G1Y4PPwT01xyS
fvalY8L1X44uT6EYGQIrMgqCZH0Wk9GjQjBAMB0GA1UdDgQWBBR8XQKEE9TMipuB
zhccLikenEhjQjAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAKBggq
hkjOPQQDAwNpADBmAjEAvvNhzwIQHWSVB7gYboiFBS+DCBeQyh+KTOgNG3qxrdWB
CUfvO6wIBHxcmbHtRwfSAjEAnbpV/KlK6O3t5nYBQnvI+GDZjVGLVTv7jHvrZQnD
+JbNR6iC8hZVdyR+EhCVBCyj
-----END CERTIFICATE-----
"""
REAL_AAA = b"""\
-----BEGIN CERTIFICATE-----
MIIEMjCCAxqgAwIBAgIBATANBgkqhkiG9w0BAQUFADB7MQswCQYDVQQGEwJHQjEb
MBkGA1UECAwSR3JlYXRlciBNYW5jaGVzdGVyMRAwDgYDVQQHDAdTYWxmb3JkMRow
GAYDVQQKDBFDb21vZG8gQ0EgTGltaXRlZDEhMB8GA1UEAwwYQUFBIENlcnRpZmlj
YXRlIFNlcnZpY2VzMB4XDTA0MDEwMTAwMDAwMFoXDTI4MTIzMTIzNTk1OVowezEL
MAkGA1UEBhMCR0IxGzAZBgNVBAgMEkdyZWF0ZXIgTWFuY2hlc3RlcjEQMA4GA1UE
BwwHU2FsZm9yZDEaMBgGA1UECgwRQ29tb2RvIENBIExpbWl0ZWQxITAfBgNVBAMM
GEFBQSBDZXJ0aWZpY2F0ZSBTZXJ2aWNlczCCASIwDQYJKoZIhvcNAQEBBQADggEP
ADCCAQoCggEBAL5AnfRu4ep2hxxNRUSOvkbIgwadwSr+GB+O5AL686tdUIoWMQua
BtDFcCLNSS1UY8y2bmhGC1Pqy0wkwLxyTurxFa70VJoSCsN6sjNg4tqJVfMiWPPe
3M/vg4aijJRPn2jymJBGhCfHdr/jzDUsi14HZGWCwEiwqJH5YZ92IFCokcdmtet4
YgNW8IoaE+oxox6gmf049vYnMlhvB/VruPsUK6+3qszWY19zjNoFmag4qMsXeDZR
rOme9Hg6jc8P2ULimAyrL58OAd7vn5lJ8S3frHRNG5i1R8XlKdH5kBjHYpy+g8cm
ez6KJcfA3Z3mNWgQIJ2P2N7Sw4ScDV7oL8kCAwEAAaOBwDCBvTAdBgNVHQ4EFgQU
oBEKIz6W8Qfs4q8p74Klf9AwpLQwDgYDVR0PAQH/BAQDAgEGMA8GA1UdEwEB/wQF
MAMBAf8wewYDVR0fBHQwcjA4oDagNIYyaHR0cDovL2NybC5jb21vZG9jYS5jb20v
QUFBQ2VydGlmaWNhdGVTZXJ2aWNlcy5jcmwwNqA0oDKGMGh0dHA6Ly9jcmwuY29t
b2RvLm5ldC9BQUFDZXJ0aWZpY2F0ZVNlcnZpY2VzLmNybDANBgkqhkiG9w0BAQUF
AAOCAQEACFb8AvCb6P+k+tZ7xkSAzk/ExfYAWMymtrwUSWgEdujm7l3sAg9g1o1Q
GE8mTgHj5rCl7r+8dFRBv/38ErjHT1r0iWAFf2C3BUrz9vHCv8S5dIa2LX1rzNLz
Rt0vxuBqw8M0Ayx9lt1awg6nCpnBBYurDC/zXDrPbDdVCYfeU0BsWO/8tqtlbgT2
G9w84FoVxp7Z8VlIMCFlA2zs6SFz7JsDoeA3raAVGI/6ugLOpyypEBMs1OUIJqsi
l2D4kF501KKaU73yqWjgom7C12yxow+ev+to51byrvLjKzg6CYG1a4XXvi3tPxq3
smPi9WIsgtRqAEFQ8TmDn5XpNpaYbg==
-----END CERTIFICATE-----
"""
REAL_ISRG = b"""\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
"""
ISRG_ROOT_X1 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"  # pragma: allowlist secret


def bundle(roots: list[Root]) -> bytes:
    return b"".join(root.pem for root in roots)


class Image:
    """The stores and PIP_CERT, clean until a test changes one. ``fs`` stands in
    for the image's root, searched for pips and grpcio, absent until a test adds one."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fs = tmp_path / "fs"
        self.fs.mkdir()
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
        monkeypatch.delenv("PIP_USE_DEPRECATED", raising=False)

    def run(self, **overrides: object) -> None:
        arguments = {
            "banned": BANNED,
            "known_root": KNOWN.sha256,
            "min_roots": MIN_ROOTS,
            "root": str(self.fs),
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


# ---- Every spelling OpenSSL loads, and blocks it refuses ------------------


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS)
def test_a_banned_root_in_any_spelling_in_the_capath_fails(image, spelling):
    (image.capath / "ee64a828.0").write_bytes(spelling)

    assert failure(image) == ["capath trusts Banned Root"]


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS)
def test_a_banned_root_in_any_spelling_in_certifi_fails(image, spelling):
    image.certifi.write_bytes(bundle(CLEAN) + spelling)

    assert failure(image) == ["certifi trusts Banned Root"]


@pytest.mark.parametrize("block", REFUSED_BLOCKS.values(), ids=REFUSED_BLOCKS)
def test_a_refused_block_after_a_root_in_the_capath_does_not_pass(image, block):
    """OpenSSL's capath lookup keeps the certificates it read before a refused block,
    while `load_verify_locations` drops the whole file. Skipping the refusal would
    report a clean store with the root still trusted, so it has to fail the build."""
    (image.capath / "ee64a828.0").write_bytes(BAD.pem + block)

    with pytest.raises(ssl.SSLError):
        image.run()


@pytest.mark.parametrize("block", REFUSED_BLOCKS.values(), ids=REFUSED_BLOCKS)
def test_a_refused_block_after_a_root_in_certifi_does_not_pass(image, block):
    image.certifi.write_bytes(bundle(CLEAN) + BAD.pem + block)

    with pytest.raises(ssl.SSLError):
        image.run()


@pytest.mark.parametrize("block", REFUSED_BLOCKS.values(), ids=REFUSED_BLOCKS)
def test_a_refused_block_in_the_cafile_empties_it_and_does_not_pass(image, block):
    image.cafile.write_bytes(bundle(CLEAN) + BAD.pem + block)

    assert failure(image) == [
        f"cafile holds 0 certificates, under {MIN_ROOTS}",
        f"cafile lacks the known root {KNOWN.sha256}",
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
    check.main(banned=BANNED, known_root=KNOWN.sha256, root=str(image.fs))
    assert "cafile 100, capath 100, certifi 100" in capsys.readouterr().out

    image.cafile.write_bytes(bundle(roots[:-1]))
    with pytest.raises(SystemExit) as exit_info:
        check.main(banned=BANNED, known_root=KNOWN.sha256, root=str(image.fs))
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


def test_a_certifi_bundle_that_does_not_exist_fails(image, tmp_path):
    image.certifi.unlink()

    assert failure(image) == [f"certifi holds 0 certificates, under {MIN_ROOTS}"]


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


# ---- The script's own constants -------------------------------------------


def test_the_scripts_defaults_ban_the_real_roots_and_know_the_real_root(tmp_path):
    """Every other test overrides the constants that do the banning. Nothing else
    fails on an empty BANNED, a mistyped digest or a known root that is never checked.
    """
    fillers = [make_root(f"Filler {i}").pem for i in range(100)]
    cafile = tmp_path / "ca.crt"
    cafile.write_bytes(b"".join(fillers) + REAL_G3 + REAL_AAA)
    # The real known root in the capath: the constant has to be its real digest.
    capath = tmp_path / "certs"
    capath.mkdir()
    (capath / "isrg.pem").write_bytes(REAL_ISRG)
    env = {
        **os.environ,
        "SSL_CERT_FILE": str(cafile),
        "SSL_CERT_DIR": str(capath),
        "PIP_CERT": str(cafile),
    }

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert _problems(result) == [
        "cafile trusts emSign ECC Root CA - G3",
        "cafile trusts AAA Certificate Services",
        f"cafile lacks the known root {ISRG_ROOT_X1}",
        "capath holds 1 certificates, under 100",
    ]
    assert "cafile trusts emSign ECC Root CA - G3" in result.stderr
    assert "cafile trusts AAA Certificate Services" in result.stderr
    assert f"cafile lacks the known root {ISRG_ROOT_X1}" in result.stderr
    assert "capath lacks the known root" not in result.stderr
    assert "cafile holds" not in result.stderr


def _problems(result: subprocess.CompletedProcess) -> list[str]:
    """The problem lines a script run failed with."""
    return result.stderr.split("trust store check failed:\n", 1)[1].splitlines()


def test_the_script_passes_a_clean_image_end_to_end(tmp_path):
    """Exit 0 over clean stores, a new pip linked to the OS bundle and grpcio's
    roots.pem linked to the cafile: the stores the image ships."""
    cafile = tmp_path / "ca.crt"
    cafile.write_bytes(b"".join(make_root(f"Filler {i}").pem for i in range(99)))
    cafile.write_bytes(cafile.read_bytes() + REAL_ISRG)
    capath = tmp_path / "certs"
    capath.mkdir()
    (capath / "all.pem").symlink_to(cafile)
    site = tmp_path / "fs" / "usr/lib/python3.12/site-packages"
    (site / "pip/_vendor/certifi").mkdir(parents=True)
    (site / "pip/__init__.py").write_text('__version__ = "25.0.1"\n')
    (site / "pip/_vendor/certifi/cacert.pem").symlink_to(cafile)
    (site / "grpc/_cython/_credentials").mkdir(parents=True)
    (site / "grpc/_cython/_credentials/roots.pem").symlink_to(cafile)
    env = {
        **os.environ,
        "SSL_CERT_FILE": str(cafile),
        "SSL_CERT_DIR": str(capath),
        "PIP_CERT": str(cafile),
    }

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "fs")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1].startswith(
        "trust store OK: cafile 100, capath 100, certifi "
    )


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
        [sys.executable, str(SCRIPT), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "trust store check failed:" in result.stderr
    assert "cafile holds 0 certificates" in result.stderr
    assert all(line.startswith(("cafile", "capath")) for line in _problems(result))
    assert "trust store OK" not in result.stdout


# ---- grpcio's roots.pem and each pip's bundle (#2665, #2692) ----------------


def vendored_pip(image: Image, version: str, roots: list[Root], prefix: str) -> Path:
    """A pip under ``prefix`` in the image: its version as pip writes it, and its
    vendored certifi bundle. Returns the bundle."""
    pip = image.fs / prefix / "pip"
    (pip / "_vendor" / "certifi").mkdir(parents=True)
    (pip / "__init__.py").write_text(f'__version__ = "{version}"\n')
    bundle_file = pip / "_vendor" / "certifi" / "cacert.pem"
    bundle_file.write_bytes(bundle(roots))
    return bundle_file


def pip_wheel(image: Image, version: str, roots: list[Root], prefix: str) -> Path:
    directory = image.fs / prefix
    directory.mkdir(parents=True)
    wheel = directory / f"pip-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pip/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr("pip/_vendor/certifi/cacert.pem", bundle(roots))
    return wheel


def grpc_roots(image: Image, prefix: str) -> Path:
    directory = image.fs / prefix / "grpc" / "_cython" / "_credentials"
    directory.mkdir(parents=True)
    return directory / "roots.pem"


ENSUREPIP = "usr/local/lib/python3.12/ensurepip/_bundled"
SITE = "usr/local/lib/python3.12/site-packages"


@pytest.mark.parametrize("version", ["24.0", "24.2", "25.3"])
def test_a_pip_whose_bundle_holds_a_banned_root_fails_at_any_version(image, version):
    """``pip --isolated``, or a pip without PIP_CERT, verifies against its vendored
    bundle on every version (#2756): the build trusts it."""
    found = vendored_pip(image, version, [*CLEAN, BAD], SITE)

    assert failure(image) == [
        f"pip {version} {found} trusts Banned Root: link it to the OS bundle",
        f"pip {version} {found} is not the cafile: link it to the OS bundle",
    ]


def test_a_pip_24_0_bundle_that_is_not_the_cafile_fails(image):
    """No banned root is not enough: the bundle must be the cafile, core's base too."""
    found = vendored_pip(image, "24.0", OTHERS, SITE)

    assert failure(image) == [
        f"pip 24.0 {found} is not the cafile: link it to the OS bundle"
    ]


def test_a_venv_pip_anywhere_in_the_image_is_read(image):
    found = vendored_pip(
        image, "25.0.1", [BAD], "srv/app/.venv/lib/python3.12/site-packages"
    )

    assert failure(image) == [
        f"pip 25.0.1 {found} trusts Banned Root: link it to the OS bundle",
        f"pip 25.0.1 {found} is not the cafile: link it to the OS bundle",
    ]


def test_legacy_certs_is_no_exemption(image, monkeypatch):
    found = vendored_pip(image, "25.0", [BAD], SITE)
    monkeypatch.setenv("PIP_USE_DEPRECATED", "legacy-certs")

    assert failure(image) == [
        f"pip 25.0 {found} trusts Banned Root: link it to the OS bundle",
        f"pip 25.0 {found} is not the cafile: link it to the OS bundle",
    ]


def test_a_pip_whose_version_cannot_be_read_is_judged(image):
    found = vendored_pip(image, "24.2", [BAD], SITE)
    (found.parents[2] / "__init__.py").write_text("")

    assert failure(image) == [
        f"pip (version unread) {found} trusts Banned Root: link it to the OS bundle",
        f"pip (version unread) {found} is not the cafile: link it to the OS bundle",
    ]


@pytest.mark.parametrize("version", ["24.0", "25.0.1"])
def test_a_pips_bundle_linked_to_the_os_bundle_passes(image, capsys, version):
    """The remedy the failure names."""
    found = vendored_pip(image, version, [], SITE)
    found.unlink()
    found.symlink_to(image.cafile)

    image.run()

    assert f"pip {version} {found}: 4 certificates" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("version", "prefix"), [("24.0", ENSUREPIP), ("25.0", "root/.cache/wheels")]
)
def test_a_pip_wheel_holding_a_banned_root_fails_at_any_version(image, version, prefix):
    """A venv made from it installs that pip, bundle and all: an ensurepip
    wheel, or one anywhere else pip can be installed from."""
    wheel = pip_wheel(image, version, [*CLEAN, BAD], prefix)

    assert failure(image) == [
        f"pip wheel {version} {wheel} trusts Banned Root: "
        "remove it or repack it with the OS bundle"
    ]


def test_a_pip_wheel_without_a_bundle_cannot_be_judged_and_fails(image):
    wheel = pip_wheel(image, "24.0", [], ENSUREPIP)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pip/__init__.py", '__version__ = "24.0"\n')

    assert failure(image) == [f"pip wheel 24.0 {wheel}: bundle not found, cannot judge"]


def test_a_pip_wheel_without_a_banned_root_passes(image, capsys):
    """A wheel is not a client: it is not held to the cafile."""
    wheel = pip_wheel(image, "24.0", OTHERS, ENSUREPIP)

    image.run()

    assert f"pip wheel 24.0 {wheel}: 3 certificates" in capsys.readouterr().out


def test_a_grpc_roots_pem_that_is_not_the_cafile_fails(image):
    roots = grpc_roots(image, SITE)
    roots.write_bytes(bundle(OTHERS))

    assert failure(image) == [
        f"grpcio's {roots} is not the cafile: link it to the OS bundle"
    ]


def test_every_grpc_roots_pem_is_read_not_only_the_importable_one(image):
    linked = grpc_roots(image, SITE)
    linked.symlink_to(image.cafile)
    other = grpc_roots(image, "opt/venv/lib/python3.12/site-packages")
    other.write_bytes(bundle([*CLEAN, BAD]))

    assert failure(image) == [
        f"grpcio's {other} trusts Banned Root",
        f"grpcio's {other} is not the cafile: link it to the OS bundle",
    ]


def test_a_grpc_roots_pem_linked_to_the_cafile_passes(image, capsys):
    roots = grpc_roots(image, SITE)
    roots.symlink_to(image.cafile)

    image.run()

    assert capsys.readouterr().out.splitlines() == [
        f"grpcio {roots}: 4 certificates",
        "trust store OK: cafile 4, capath 4, certifi 4 certificates",
    ]


def test_without_certifi_the_other_stores_are_still_judged(image, monkeypatch, capsys):
    """A bare base has no certifi: the pip legs must still run (the control)."""
    monkeypatch.setattr(check, "certifi", None)
    found = vendored_pip(image, "25.0.1", [BAD], SITE)

    assert failure(image) == [
        f"pip 25.0.1 {found} trusts Banned Root: link it to the OS bundle",
        f"pip 25.0.1 {found} is not the cafile: link it to the OS bundle",
    ]
    assert capsys.readouterr().out.splitlines()[0] == "certifi not installed"


def test_the_search_skips_proc_sys_and_dev(image):
    for skipped in ("proc", "sys", "dev"):
        vendored_pip(image, "25.0.1", [BAD], f"{skipped}/site-packages")

    image.run()
