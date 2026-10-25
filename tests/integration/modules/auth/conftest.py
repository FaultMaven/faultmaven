"""Fixtures of the auth integration modules."""

import pytest


@pytest.fixture(scope="module")
def oauth_environment(request):
    """The module's ``OAUTH_ENVIRONMENT``, set while its tests run (#2291).

    An OAuth module builds its app with ``rebuild_app_under(OAUTH_ENVIRONMENT)``
    and its routes read the same settings at request time, so its tests need
    that environment too, and no other module's do. Set here at module scope and
    put back after the module's last test, with the settings singleton cleared
    on both sides; a module asks for it with
    ``pytestmark = pytest.mark.usefixtures("oauth_environment")``.
    """
    from faultmaven.config.settings import reset_settings

    with pytest.MonkeyPatch.context() as patch:
        for name, value in request.module.OAUTH_ENVIRONMENT.items():
            patch.setenv(name, value)
        reset_settings()
        yield
    reset_settings()
