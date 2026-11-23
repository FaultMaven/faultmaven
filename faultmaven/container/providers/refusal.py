"""The refusal a factory's own construction failure reaches (#3003).

Its own module because every provider module calls it, and
``providers.services`` already imports ``providers.infrastructure``.
"""

from __future__ import annotations

from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.utils.exception_name import exception_log_name


def unbuilt_where_must_not_degrade(name: str, error: BaseException) -> None:
    """Refuse the start when a configured service fails to build where the
    deployment must not degrade (principles §7, "Startup").

    A factory that caught its own construction failure and returned ``None``
    hid it from the container's must-not-degrade gate. Under
    ``settings.must_not_degrade`` this raises ``RequiredServiceConstructionError``
    naming ``name`` and the error's class chain, never its text. Elsewhere it
    returns, and the caller, having logged its warning, serves without the
    service.
    """
    from faultmaven.config.settings import get_settings

    if get_settings().must_not_degrade:
        raise RequiredServiceConstructionError(
            f"{name} could not be constructed where the deployment must not "
            f"degrade: {exception_log_name(error)}"
        ) from error
