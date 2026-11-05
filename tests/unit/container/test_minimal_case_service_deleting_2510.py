"""The degraded case service answers the detail read's delete question (#2510).

``GET /cases/{case_id}`` asks the case service ``delete_in_progress``.
``MinimalCaseService`` is duck-typed, so without the method the read would raise
there. Its delete is one synchronous step that holds no tombstone, so it never
sees one running.
"""

import inspect

import pytest

from faultmaven.models.interfaces_case import ICaseService

pytestmark = [pytest.mark.unit]


async def test_the_stand_in_answers_as_the_interface_declares_with_no_delete():
    # Via the package facade, not ``_container_impl`` directly (an import cycle).
    from faultmaven.container import DIContainer

    service = object.__new__(DIContainer)._create_minimal_case_service()
    declared = inspect.signature(ICaseService.delete_in_progress).parameters
    served = inspect.signature(service.delete_in_progress).parameters

    assert list(served) == list(declared)[1:]  # less ``self``
    answer = await service.delete_in_progress(
        case_id="case_2510aaaaaaaa", enterprise_id="ent_2510"
    )
    assert answer is False
