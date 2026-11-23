"""The one way an ``fm-*`` command prints a caught exception (#2889).

Principles §7 rule 1: a print names an exception by its class, never its text,
which may embed a third party's value. An operator command's refusal is the
exception: its own first-party refusal sentence, built only from literals, the
command's arguments and identifiers our code holds, is the operator's whole
instruction, and is printed whole. :func:`refusal_text` decides which by the
exception's exact class, so a subclass, or a class nobody audited, prints its
class name.

A class joins :data:`AUDITED_REFUSALS` when every construction of it in
``faultmaven/`` builds its message that way; the census
(``tests/unit/architecture/test_log_message_channel_census_2889.py``) fails a
construction that reads a caught exception. Classes are named by string, so
this module imports none of them.
"""

from __future__ import annotations

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
        # 1 raise: the setting's name and literals.
        "faultmaven.bootstrap.data_init.UnusableDataDirError",
        # Its sentence: the subject and the enterprise id.
        "faultmaven.infrastructure.persistence.tenant_retirement.EnterpriseRowMissing",
        # 1 raise: the enterprise id.
        "faultmaven.infrastructure.persistence.tenant_bootstrap.EnterpriseNotUsable",
    }
)


def refusal_text(error: BaseException) -> str:
    """``error``'s message when its exact class is audited, else its class
    name (:func:`exception_log_name`). For a ``print`` under ``cli/`` only."""
    kind = type(error)
    if f"{kind.__module__}.{kind.__qualname__}" in AUDITED_REFUSALS:
        return str(error)
    return exception_log_name(error)
