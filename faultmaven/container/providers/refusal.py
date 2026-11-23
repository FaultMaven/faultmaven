"""The refusal a factory's own construction failure reaches (#3003).

Its own module because the provider modules and the container call it, and
``providers.services`` already imports ``providers.infrastructure``.
"""

from __future__ import annotations

from typing import Any

from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.utils.exception_name import exception_log_name


def unbuilt_where_must_not_degrade(
    name: str, error: BaseException, settings: Any
) -> None:
    """Refuse the start when a configured service fails to build where the
    deployment must not degrade (principles §7, "Startup").

    A factory that caught its own construction failure and returned ``None``
    hid it from the container's must-not-degrade gate. Under the composition's
    ``settings.must_not_degrade`` this raises
    ``RequiredServiceConstructionError`` naming ``name`` and the error's class
    chain, never its text. Elsewhere it returns, and the caller, having logged
    its warning, serves without the service.
    """
    if settings.must_not_degrade:
        raise RequiredServiceConstructionError(
            f"{name} could not be constructed where the deployment must not "
            f"degrade: {exception_log_name(error)}"
        ) from error
