"""The degraded case service's ``delete_in_progress`` matches the interface (#2510).

``MinimalCaseService`` is duck-typed, so only a test holds it to
``ICaseService``. This pins its ``delete_in_progress``: the interface's
parameters, and ``False``, as its delete is one synchronous step that holds no
tombstone. It does not make ``GET /cases/{case_id}`` work on the stand-in: that
read raises earlier, at ``get_case_team_ids``, which the stand-in lacks (#2548).
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
