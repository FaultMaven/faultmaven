"""The API's idle keep-alive outlasts its proxy's (infra#377)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.config.settings import ServerSettings
from faultmaven.server import run_server

pytestmark = pytest.mark.unit

NGINX_UPSTREAM_KEEPALIVE_S = 60


def test_default_exceeds_ingress_nginx_upstream_keepalive():
    assert (
        ServerSettings.model_fields["timeout_keep_alive"].default
        > NGINX_UPSTREAM_KEEPALIVE_S
    )


@pytest.mark.parametrize("workers", [1, 3])
def test_run_passes_timeout_keep_alive(workers):
    server = ServerSettings(workers=workers, timeout_keep_alive=90)
    with (
        patch("faultmaven.config.settings.get_settings") as get,
        patch("uvicorn.run") as run,
        # The stores refusal and the launcher migration have tests of their own.
        patch("faultmaven.config.worker_stores.refuse_configured_per_process_stores"),
    ):
        get.return_value = SimpleNamespace(server=server, run_startup_migrations=False)
        run_server()
    kwargs = run.call_args.kwargs
    assert kwargs["timeout_keep_alive"] == 90
    assert kwargs["workers"] == workers
