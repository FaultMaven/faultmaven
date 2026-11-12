"""Fail the image build unless a pip that ignores PIP_CERT, run ``--isolated`` or
with PIP_CERT unset, verifies against exactly the OS cafile's roots (#2756).

Measured from pip's own session through requests' ``cert_verify``, so it imports
pip's private API, which check_image_trust_store.py deliberately does not. The
truststore context a pip >= 24.2 adapter holds also loads that bundle: it is
reported, not measured."""

import os
import ssl
import sys
import types

for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
    os.environ.pop(name, None)  # the reference is the OS cafile, the measure pip's

import pip  # noqa: E402
from pip._internal.commands import create_command  # noqa: E402

URL = "https://pypi.org/simple/"


def roots(cafile: str) -> set[bytes]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=cafile)
    return set(context.get_ca_certs(binary_form=True))


def leg(name: str, args: list[str], reference: set[bytes]) -> str | None:
    command = create_command("download", isolated="--isolated" in args)
    session = command._build_session(command.parse_args(args)[0])
    verify = session.merge_environment_settings(URL, {}, None, None, None)["verify"]
    adapter, conn = session.get_adapter(URL), types.SimpleNamespace()
    adapter.cert_verify(conn, URL, verify, None)
    if getattr(adapter, "_ssl_context", None) is not None:
        print(f"{name}: the adapter's truststore context is present, not measured")
    file = getattr(conn, "ca_certs", None)
    if not file or roots(file) != reference:
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
