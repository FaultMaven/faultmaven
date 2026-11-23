"""The one way an ``fm-*`` command prints a caught exception (#2889).

Principles §7 rule 1: a print names an exception by its class, never its text,
which may embed a third party's value. An operator command's refusal is the
exception: its own first-party refusal sentence, built only from literals, the
command's arguments, identifiers our code holds, and a caught exception's class
chain (``exception_log_name``), is the operator's whole instruction, and is
printed whole. :func:`refusal_text` decides which by the
exception's exact class, so a subclass, or a class nobody audited, prints its
class name.

A class joins :data:`AUDITED_REFUSALS` when every construction of it in
``faultmaven/`` builds its message that way; the census
(``tests/unit/architecture/test_log_message_channel_census_2889.py``) fails a
construction that reads a caught exception. Classes are named by string, so
this module imports none of them.
"""

from __future__ import annotations

import sys

from faultmaven.utils.exception_name import exception_log_name

#: ``module.qualname`` of each audited refusal class; each raise site audited
#: in #2889 S3i.
AUDITED_REFUSALS = frozenset(
    {
        # 2 raises: the id and literals.
        "faultmaven.cli._tenant.EnterpriseRefused",
        # Raises: literals, arguments, ids, and refusal_text or
        # exception_log_name of a caught exception.
        "faultmaven.cli.personal_tenant._Refused",
        "faultmaven.cli.reassign_cases._Refused",
        # 9 raises: literals only.
        "faultmaven.modules.auth.exceptions.SSOProvisioningError",
        # Its own raises: settings, role names and literals. Not its
        # subclasses, which carry a driver's text.
        "faultmaven.config.deployment_coherence.DeploymentCoherenceError",
        # 1 raise: a literal.
        "faultmaven.infrastructure.observability.opik_sdk.TraceContentCaptureRefused",
        # 1 raise (through ``misscoped``): ids and literals.
        "faultmaven.modules.auth.domain.services.organization_membership_service"
        ".MembershipRemovalMisscoped",
        # 1 raise (through ``incomplete``, shared with the role change):
        # ``what_landed``, ``still_valid`` and ``retry_hint`` are ids and
        # literals, and the caught failure is named by exception_log_name.
        "faultmaven.modules.auth.domain.services.organization_membership_service"
        ".MembershipRemovalIncomplete",
        # 1 raise: the setting's name and literals.
        "faultmaven.bootstrap.data_init.UnusableDataDirError",
        # 1 raise: the enterprise id.
        "faultmaven.infrastructure.persistence.tenant_bootstrap.EnterpriseNotUsable",
    }
)


def refusal_text(error: BaseException) -> str:
    """``error``'s message when its exact class is audited, else its class
    name (:func:`exception_log_name`). For a ``print``'s argument under
    ``cli/`` only."""
    kind = type(error)
    if f"{_module_name(kind.__module__)}.{kind.__qualname__}" in AUDITED_REFUSALS:
        return str(error)
    return exception_log_name(error)


def _module_name(module: str) -> str:
    """The module's import name: a command run as ``python -m faultmaven.cli.x``
    defines its classes in ``__main__``, whose spec names the module."""
    if module != "__main__":
        return module
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", None) or module
