"""Fail the image build unless a pip that ignores PIP_CERT, run ``--isolated`` or
with PIP_CERT unset, verifies against exactly the OS cafile's roots (#2756).

Measured from pip's own session: the bundle requests' ``cert_verify`` names, or,
where it names none (requests 2.32.0-2.32.4 with ``verify`` True), requests'
``DEFAULT_CA_BUNDLE_PATH`` (``certifi.where()``); a bundle named None (a trusted
host's InsecureHTTPAdapter) fails. It imports pip's private API, which
check_image_trust_store.py deliberately does not. Probed on pip 24.0, 24.1.2, 24.2,
25.0.1, 25.2 and 25.3 (requests 2.31.0-2.32.5). The truststore context a pip >= 24.2
adapter holds also loads that bundle: it is reported, not measured."""

import os
import ssl
import sys
import types

for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
    os.environ.pop(name, None)  # the reference is the OS cafile, the measure pip's

import pip  # noqa: E402
from pip._internal.commands import create_command  # noqa: E402
from pip._vendor import certifi  # noqa: E402

URL = "https://pypi.org/simple/"


def roots(cafile: str) -> set[bytes]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cafile=cafile)
    except ssl.SSLError as error:  # A refused block can follow a trusted one.
        if error.reason != "NO_CERTIFICATE_OR_CRL_FOUND":
            raise
    return set(context.get_ca_certs(binary_form=True))


def leg(name: str, args: list[str], reference: set[bytes]) -> str | None:
    file = certifi.where()  # requests 2.32.0-2.32.4 load it on import
    try:
        from pip._vendor.requests.utils import DEFAULT_CA_BUNDLE_PATH

        command = create_command("download", isolated="--isolated" in args)
        session = command._build_session(command.parse_args(args)[0])
        merged = session.merge_environment_settings(URL, {}, None, None, None)
        verify = merged["verify"]
        adapter, conn = session.get_adapter(URL), types.SimpleNamespace()
        adapter.cert_verify(conn, URL, verify, None)
        if getattr(adapter, "_ssl_context", None) is not None:
            print(f"{name}: the adapter's truststore context is present, not measured")
        # The default only where requests 2.32.0-2.32.4 set no ca_certs: an explicit
        # None (InsecureHTTPAdapter, a trusted host) or a directory cert fails.
        default = DEFAULT_CA_BUNDLE_PATH if verify is True else None
        file = getattr(conn, "ca_certs", default)
        loaded = roots(file)
    except (OSError, TypeError) as error:  # SSLError is an OSError
        return f"{name}: pip's bundle {file} does not load: {error}"
    if loaded != reference:
        return (
            f"{name}: pip verifies against {file}, not the OS cafile's roots: "
            "link it to the OS bundle"
        )
    print(f"{name}: {file}, the OS cafile's {len(reference)} certificates")
    return None


def main() -> None:
    reference = set(ssl.create_default_context().get_ca_certs(binary_form=True))
    print(f"pip {pip.__version__} {pip.__file__}")
    problems = [leg("--isolated", ["--isolated"], reference)]
    os.environ.pop("PIP_CERT", None)
    problems.append(leg("PIP_CERT unset", [], reference))
    if problems := [problem for problem in problems if problem]:
        sys.exit("pip root set check failed:\n" + "\n".join(problems))
    print("pip root set OK")


if __name__ == "__main__":
    main()
