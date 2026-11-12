"""One TLS root set, extended by ``TLS_EXTRA_CA_FILE``, named in every owned
variable before any client is imported (§13.4, #2692).

Each test builds an OS bundle of generated CA certificates and points the
resolution at it, so no test depends on this host's bundle except the child
process test, which re-applies the host's real set. ``tests/conftest.py``
clears the owned variables before each test and restores them after.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import dotenv
import dotenv.main
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from faultmaven.config import tls_root_set
from faultmaven.config.tls_root_set import (
    EMPTY_CAPATH,
    FILE_VARIABLES,
    OWNED_VARIABLES,
    apply_root_set,
)

pytestmark = pytest.mark.unit

ROOT = Path(tls_root_set.__file__).resolve().parents[2]


def _ca_pem(name: str, ca: bool = True) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _digest(pem: bytes) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.decode())).hexdigest()


def _loaded(path: str) -> set[str]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=path)
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(True)}


ROOTS = [_ca_pem(f"os root {index}") for index in range(3)]
EXTRA = _ca_pem("private CA")


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    """The set's written files land under the test's tmp_path; no .env is read."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")


def _os_paths(monkeypatch, cafile: str, capath: str = "/nonexistent-capath") -> None:
    paths = ssl.DefaultVerifyPaths(
        cafile, capath, "SSL_CERT_FILE", cafile, "SSL_CERT_DIR", capath
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: paths)
    monkeypatch.setattr(tls_root_set, "DISTRIBUTION_BUNDLES", ())


@pytest.fixture
def os_bundle(tmp_path, monkeypatch) -> Path:
    bundle = tmp_path / "os-bundle.pem"
    bundle.write_bytes(b"".join(ROOTS))
    _os_paths(monkeypatch, str(bundle))
    return bundle


@pytest.fixture
def extra_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "extra-ca.pem"
    path.write_bytes(EXTRA)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(path))
    return path


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("content", [None, b"not a certificate\n"])
def test_an_unreadable_extra_file_refuses_naming_the_setting(
    os_bundle, tmp_path, monkeypatch, content
):
    path = tmp_path / "extra.pem"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(path))
    with pytest.raises(SystemExit, match="TLS_EXTRA_CA_FILE=.*must be a readable PEM"):
        apply_root_set()
    assert "SSL_CERT_FILE" not in os.environ


def test_no_loadable_os_bundle_refuses_naming_each_path_tried(tmp_path, monkeypatch):
    empty_capath = tmp_path / "capath"
    empty_capath.mkdir()
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"), str(empty_capath))
    with pytest.raises(SystemExit) as refusal:
        apply_root_set()
    message = str(refusal.value)
    assert str(tmp_path / "absent.pem") in message and str(empty_capath) in message
    assert "Install Certificates" in message and "TLS_EXTRA_CA_FILE" in message


@pytest.mark.parametrize("name", FILE_VARIABLES)
def test_an_inherited_variable_holding_another_root_refuses(
    os_bundle, tmp_path, monkeypatch, name
):
    other = tmp_path / "other.pem"
    other.write_bytes(_ca_pem("someone else's root"))
    monkeypatch.setenv(name, str(other))
    with pytest.raises(SystemExit) as refusal:
        apply_root_set()
    message = str(refusal.value)
    assert f"{name}=" in message and "TLS_EXTRA_CA_FILE" in message
    assert f"remove {name}" in message


def test_an_inherited_file_one_certificate_short_refuses(
    os_bundle, tmp_path, monkeypatch
):
    short = tmp_path / "short.pem"
    short.write_bytes(b"".join(ROOTS[:-1]))
    monkeypatch.setenv("SSL_CERT_FILE", str(short))
    with pytest.raises(SystemExit, match="SSL_CERT_FILE="):
        apply_root_set()


def test_an_inherited_os_bundle_refuses_once_the_extra_ca_is_set(
    os_bundle, extra_file, monkeypatch
):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(os_bundle))
    with pytest.raises(SystemExit, match="plus TLS_EXTRA_CA_FILE"):
        apply_root_set()


def test_an_inherited_capath_holding_a_hashed_certificate_refuses(
    os_bundle, tmp_path, monkeypatch
):
    capath = tmp_path / "capath"
    capath.mkdir()
    (capath / "0123abcd.0").write_bytes(_ca_pem("capath-only CA"))
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    with pytest.raises(SystemExit, match="SSL_CERT_DIR="):
        apply_root_set()


def _relabel(pem: bytes, label: bytes) -> bytes:
    return pem.replace(b" CERTIFICATE-----", b" " + label + b"-----")


@pytest.mark.parametrize("label", [b"X509 CERTIFICATE", b"TRUSTED CERTIFICATE"])
def test_a_root_in_a_non_plain_block_refuses_inherited_or_extra(
    os_bundle, tmp_path, monkeypatch, label
):
    """OpenSSL loads such a block as a root, but it cannot be compared."""
    hidden = tmp_path / "hidden.pem"
    hidden.write_bytes(b"".join(ROOTS) + _relabel(_ca_pem("rogue"), label))
    monkeypatch.setenv("SSL_CERT_FILE", str(hidden))
    with pytest.raises(SystemExit, match="SSL_CERT_FILE="):
        apply_root_set()
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(hidden))
    with pytest.raises(SystemExit, match="TLS_EXTRA_CA_FILE=.*CERTIFICATE"):
        apply_root_set()


def test_a_stale_set_an_earlier_start_wrote_says_restart(
    os_bundle, extra_file, monkeypatch
):
    apply_root_set()
    extra_file.write_bytes(_ca_pem("rotated private CA"))
    with pytest.raises(SystemExit) as refusal:
        apply_root_set()
    assert "restart that process" in str(refusal.value)
    assert "remove" not in str(refusal.value)


@pytest.mark.parametrize("unsafe", ["group-writable", "symlink"])
def test_a_set_directory_others_can_write_refuses_naming_tmpdir(
    os_bundle, extra_file, tmp_path, unsafe
):
    directory = tmp_path / f"faultmaven-tls-{os.getuid()}"
    if unsafe == "symlink":
        (tmp_path / "elsewhere").mkdir()
        directory.symlink_to(tmp_path / "elsewhere")
    else:
        directory.mkdir()
        directory.chmod(0o770)
    with pytest.raises(SystemExit, match="TMPDIR"):
        apply_root_set()


# --- the OS bundle's resolution ----------------------------------------------


def test_a_distribution_bundle_is_used_when_openssls_default_file_is_absent(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "distribution.pem"
    bundle.write_bytes(b"".join(ROOTS))
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"))
    monkeypatch.setattr(tls_root_set, "DISTRIBUTION_BUNDLES", (str(bundle),))
    assert apply_root_set().path == str(bundle)


def test_a_capath_only_os_bundle_is_built_into_a_file(tmp_path, monkeypatch):
    capath = tmp_path / "capath"
    capath.mkdir()
    for index, pem in enumerate(ROOTS):
        (capath / f"0000000{index}.0").write_bytes(pem)
    (capath / "README").write_text("not a certificate")
    _os_paths(monkeypatch, str(tmp_path / "absent.pem"), str(capath))
    applied = apply_root_set()
    assert applied.path.startswith(str(tmp_path / "faultmaven-tls-"))
    assert _loaded(applied.path) == {_digest(pem) for pem in ROOTS}


# --- the set -----------------------------------------------------------------


def test_unset_names_the_os_bundle_in_every_variable_and_an_empty_capath(os_bundle):
    applied = apply_root_set()
    assert applied.path == str(os_bundle) and applied.certificates == len(ROOTS)
    assert {name: os.environ[name] for name in FILE_VARIABLES} == dict.fromkeys(
        FILE_VARIABLES, str(os_bundle)
    )
    assert os.environ["SSL_CERT_DIR"] == EMPTY_CAPATH
    assert os.listdir(EMPTY_CAPATH) == ["README"]


def test_set_names_a_file_holding_the_os_bundle_and_the_extra_ca(os_bundle, extra_file):
    applied = apply_root_set()
    expected = {_digest(pem) for pem in (*ROOTS, EXTRA)}
    assert applied.path != str(os_bundle)
    assert _loaded(applied.path) == expected and applied.certificates == len(expected)
    assert all(os.environ[name] == applied.path for name in FILE_VARIABLES)
    assert applied.extra_ca_file == str(extra_file)


def test_an_os_bundle_without_a_trailing_newline_still_loads_whole(
    os_bundle, extra_file
):
    os_bundle.write_bytes(b"".join(ROOTS).rstrip(b"\n"))
    assert len(_loaded(apply_root_set().path)) == len(ROOTS) + 1


def test_an_inherited_equal_file_by_link_or_by_bytes_is_exported_by_real_path(
    os_bundle, tmp_path, monkeypatch
):
    link = tmp_path / "link.pem"
    link.symlink_to(os_bundle)
    copy = tmp_path / "copy.pem"
    copy.write_bytes(os_bundle.read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    monkeypatch.setenv("AWS_CA_BUNDLE", str(copy))
    assert apply_root_set().path == os.path.realpath(os_bundle)
    assert os.environ["AWS_CA_BUNDLE"] == os.path.realpath(os_bundle)


def test_an_inherited_relative_path_is_exported_absolute(os_bundle, monkeypatch):
    monkeypatch.chdir(os_bundle.parent)
    monkeypatch.setenv("SSL_CERT_FILE", os_bundle.name)
    apply_root_set()
    assert os.environ["SSL_CERT_FILE"] == os.path.realpath(os_bundle)


def test_an_extra_file_keeps_its_certificates_and_drops_everything_else(
    os_bundle, tmp_path, monkeypatch
):
    """A fullchain with its key: the non-CA leaf loads, the key is never copied."""
    leaf = _ca_pem("a server leaf", ca=False)
    key = b"-----BEGIN PRIVATE KEY-----\nTUlJ\n-----END PRIVATE KEY-----\n"
    chain = tmp_path / "fullchain.pem"
    chain.write_bytes(leaf + EXTRA + key)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", str(chain))
    written = Path(apply_root_set().path).read_bytes()
    assert b"PRIVATE KEY" not in written
    assert tls_root_set.pem_digests(written) == {
        _digest(pem) for pem in (*ROOTS, leaf, EXTRA)
    }


def test_an_inherited_file_with_the_same_roots_reordered_and_commented_is_accepted(
    os_bundle, tmp_path, monkeypatch
):
    nix = tmp_path / "nix-bundle.pem"
    nix.write_bytes(b"# a comment\n" + b"\n# another\n".join(reversed(ROOTS)))
    monkeypatch.setenv("SSL_CERT_FILE", str(nix))
    assert apply_root_set().path == str(nix)


def test_two_applies_name_one_file_and_write_nothing_else(
    os_bundle, extra_file, monkeypatch
):
    first = apply_root_set()
    for name in OWNED_VARIABLES:  # a second start, not a child
        monkeypatch.delenv(name)
    second = apply_root_set()
    assert second.path == first.path
    assert os.listdir(Path(first.path).parent) == [Path(first.path).name]


@pytest.mark.parametrize("held", [(), ROOTS])
def test_an_inherited_capath_empty_or_holding_exactly_the_set_is_accepted(
    os_bundle, tmp_path, monkeypatch, held
):
    capath = tmp_path / "capath"
    capath.mkdir()
    for index, pem in enumerate(held):
        (capath / f"abcdef0{index}.0").write_bytes(pem)
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    apply_root_set()
    assert os.environ["SSL_CERT_DIR"] == EMPTY_CAPATH


@pytest.mark.parametrize("key", ["TLS_EXTRA_CA_FILE", "tls_extra_ca_file"])
def test_a_dotenv_extra_ca_file_is_read_in_any_case(
    os_bundle, tmp_path, monkeypatch, key
):
    (tmp_path / "extra.pem").write_bytes(EXTRA)
    (tmp_path / ".env").write_text(f"{key}=extra.pem\n")
    monkeypatch.delenv("FAULTMAVEN_SKIP_DOTENV")
    monkeypatch.setattr(dotenv, "dotenv_values", dotenv.main.dotenv_values)
    monkeypatch.chdir(tmp_path)
    assert _digest(EXTRA) in _loaded(apply_root_set().path)


def test_a_relative_extra_file_is_exported_absolute_and_a_child_elsewhere_reapplies(
    tmp_path, monkeypatch
):
    """The host's real OS bundle: the child resolves it too."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "extra-ca.pem").write_bytes(EXTRA)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TLS_EXTRA_CA_FILE", "data/extra-ca.pem")
    parent = apply_root_set()
    assert os.environ["TLS_EXTRA_CA_FILE"] == str(tmp_path / "data" / "extra-ca.pem")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    child = subprocess.run(
        [sys.executable, "-c", "from faultmaven.config.tls_root_set import "
         "apply_root_set; print(apply_root_set().path)"],
        cwd=elsewhere, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT), "TMPDIR": str(tmp_path)},
    )  # fmt: skip
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == parent.path
    assert set(OWNED_VARIABLES) <= set(os.environ)
