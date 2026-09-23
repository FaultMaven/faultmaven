"""Operator case-read metrics (ADR-012 D9).

Lives in the auth module because the thing being counted is an **operator**
read: ``require_platform_admin`` is the gate, and the deployment posture that
labels each observation is the same axis ``authorize_content_read`` decides on.
Its neighbour ``revocation_metrics`` is the same kind of instrument — a
request-path counter owned by this module rather than by the route that emits it.

‼ It is defined HERE, and not beside the route, for an architectural reason
rather than a tidiness one. ``tests/unit/architecture/test_architecture_boundaries.py``
forbids ``faultmaven/api/**`` importing ``faultmaven.infrastructure.*``, and
allows the metrics shim only for ``middleware`` — "the canonical cross-cutting
metrics facade ... same family as logging/tracing". A route is not middleware, so
a route cannot reach the shim, and middleware cannot help here: it sees an HTTP
request, not which D9 arm was served. Exporting the counter from a module the api
layer may import is what satisfies both facts without widening the boundary rule
to fit one feature.
"""

from faultmaven.infrastructure.shims.metrics import Counter

#: Which operator read was served. Pinned because a call site that spells a
#: surface not in this tuple mints a new series silently, and the question below
#: is then asked of a population that quietly changed shape.
OPERATOR_READ_SURFACES = ("list", "case_detail", "transcript")

#: ``DeploymentMode`` values (``faultmaven/config/settings.py``), which is what
#: ``resolved_deployment_mode()`` returns.
OPERATOR_READ_DEPLOYMENTS = ("standalone", "cloud")

operator_case_reads_total = Counter(
    "faultmaven_operator_case_reads_total",
    "Operator case reads served (ADR-012 D9), labeled by ``surface`` "
    "(list | case_detail | transcript) and ``deployment`` (standalone | cloud). "
    "It exists to answer ONE question: is the STANDALONE arm of these endpoints "
    "still being reached? faultmaven-dashboard#178 stopped that client calling "
    "them in standalone, which leaves the arm serving nobody — but "
    "docs/development/api-contract-changes.md is explicit that a grep over "
    "client source is not evidence (\"'Nobody should still be using it' is not "
    'evidence"), because deployed clients are what matter and self-hosted '
    "installs pin their image tag. The cloud rows are the denominator: they are "
    "what distinguishes 'the standalone arm is unused' from 'this counter is not "
    "wired'. Removing the arm is fm#1613, and waits on standalone reading zero "
    "across a full deploy cycle.",
    ["surface", "deployment"],
)

__all__ = [
    "OPERATOR_READ_DEPLOYMENTS",
    "OPERATOR_READ_SURFACES",
    "operator_case_reads_total",
]
