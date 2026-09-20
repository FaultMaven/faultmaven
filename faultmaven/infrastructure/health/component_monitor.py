"""
Component Health Monitoring

Every check in this module performs real I/O against the dependency it names,
or reports ``UNKNOWN``. There are no simulated checks: a check that cannot
fail is indistinguishable from no check at all, which is what #1515 was.

**Fatal vs degraded.** Each component declares ``fatal`` — whether the process
can still usefully answer requests without it: it has no fallback, and with it
down essentially no request can be served. ``database`` is the only one that
qualifies. Everything else is degraded: still visible in the body, never a
reason to pull a pod or restart it. The distinction is load-bearing because a
non-200 on a probe path turns a dependency outage into a restart loop; see the
``/health`` and ``/readiness`` handlers in ``main.py`` for which endpoint
carries the verdict in its status code and why.

**Fatal is not the same question as readiness-fatal.** ``fatal`` grades the
severity reported in ``/health``'s body. The *readiness-fatal* set
(``ComponentHealthMonitor.readiness_fatal_components``) is the narrower thing
whose status code pulls a pod out of its Service, and it takes a second
condition on top of ``fatal``: the component must be able to fail on **one
replica while the others keep serving**. That second condition is what
readiness is FOR — a readiness failure shifts traffic to a healthy sibling —
and where every replica fails together there is no sibling, so the gate only
empties the Service. Conflating the two is #1524; the set and the argument
for today's membership live at ``_initialize_default_components``.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

#: Deadline for a single component probe.
#:
#: A probe with no deadline does not report a hung dependency, it BECOMES one:
#: ``/health`` stops answering, and since production points liveness, readiness
#: and startup probes at it, a hung ChromaDB or Presidio empties the Service in
#: ~60s and SIGKILLs every pod at ~4 min — the exact outcome the fatal/degraded
#: split exists to prevent, reached by latency instead of by status code. None
#: of the underlying clients bounds itself anywhere near a probe interval:
#: chromadb is built with ``timeout=None``, the Presidio path is 21s worst case
#: (10s + 1s backoff + 10s), and redis sits at ``socket_timeout=10`` — which
#: already EQUALS readiness's own ``timeoutSeconds``.
#:
#: 3s because the tightest probe timeout that reads ``/health`` is the
#: startupProbe's 5s, and the whole response must fit inside it.
_PROBE_TIMEOUT_SECONDS = 3.0

#: Deadline for a full ``check_all_components`` sweep. Above the per-probe
#: deadline (the probes run concurrently, so one slow probe should not eat the
#: sweep) and still inside the 5s startup timeout.
_ALL_COMPONENTS_TIMEOUT_SECONDS = 4.0

#: Probes run their blocking work HERE, never on ``asyncio.to_thread``'s
#: default executor.
#:
#: ``wait_for`` bounds the *await*, not the thread: ``run_in_executor`` work
#: that has already started is not cancellable, so a hung dependency strands
#: one worker per probe for as long as the dependency hangs. On the default
#: executor (``min(32, cpu+4)`` workers, shared with everything else) six
#: probes a minute would exhaust it in minutes and then starve every other
#: ``to_thread`` caller in the process — ``DataSanitizer.asanitize`` above all,
#: which is on the request path. A small dedicated pool CONFINES that: the
#: leak is capped at ``max_workers`` threads, nothing outside health checks
#: can be starved by it, and once the pool is full a further probe's work item
#: is merely queued — so it is cancelled unstarted when its ``wait_for``
#: expires, and the component reports unhealthy, which is the truth.
_PROBE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="fm-health-probe"
)

#: Redis key the session-store probe asks about. It is never written, so the
#: probe is a pure read: EXISTS on an absent key is O(1) and allocates nothing.
_SESSION_PROBE_KEY = "__faultmaven_health_probe__"

#: Input for the sanitizer's local functional probe. Must contain something the
#: redaction pattern table is expected to match, or the probe proves nothing.
_SANITIZER_PROBE_INPUT = "health probe from 10.11.12.13"


class HealthStatus(Enum):
    """Component health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Represents the health status of a single component.

    The three 24h figures measure *this monitor's own probes* — the only
    per-component history the process keeps — and are named for that. They are
    not request-level SLA: request availability is the SLA tracker's job, fed
    by the logging middleware and the LLM router.
    """

    component_name: str
    status: HealthStatus
    response_time_ms: float
    last_error: Optional[str] = None
    probe_availability_24h: float = 100.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    last_check: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    probe_failures_24h: int = 0
    probe_successes_24h: int = 0
    #: The process cannot usefully answer requests without this component.
    #: Grades ``/health``'s body; on its own it does NOT gate traffic.
    fatal: bool = False
    #: This component can fail for THIS pod alone while sibling replicas keep
    #: serving. Only a component that is both ``fatal`` and
    #: ``fails_per_replica`` joins the readiness-fatal set.
    fails_per_replica: bool = False


@dataclass
class DependencyMapping:
    """Maps component dependencies and criticality."""

    component: str
    critical_dependencies: List[str] = field(default_factory=list)
    optional_dependencies: List[str] = field(default_factory=list)
    dependent_components: List[str] = field(default_factory=list)


class ComponentHealthMonitor:
    """Monitors health of individual components with SLA tracking."""

    def __init__(self):
        """Initialize component health monitor."""
        self.logger = logging.getLogger(__name__)
        self.component_health: Dict[str, ComponentHealth] = {}
        self.dependency_map: Dict[str, DependencyMapping] = {}
        self.health_history: Dict[str, List[Tuple[datetime, HealthStatus, float]]] = {}
        #: Components whose failure means the process cannot usefully serve.
        #: Grades the ``/health`` body; does not by itself gate traffic.
        self.fatal_components: Set[str] = set()
        #: The readiness-fatal set — the components ``/readiness`` answers 503
        #: for, which removes this pod from its Service. Derived, never
        #: assigned: a component joins it by declaring BOTH ``fatal`` and
        #: ``fails_per_replica`` at registration. **Today it is empty**, and
        #: that is a decision, not an oversight — read
        #: ``_initialize_default_components`` before adding to it.
        self.readiness_fatal_components: Set[str] = set()
        # RLS-bypass posture (role attributes + table ownership) is static for the
        # life of the process/DB role, so determine it once and reuse it — the
        # per-probe DB cost then stays just the SELECT 1 connectivity check.
        self._rls_posture: Optional[Dict[str, Any]] = None
        self._initialize_default_components()

    def _initialize_default_components(self) -> None:
        """Initialize monitoring for default FaultMaven components.

        Two independent declarations per component, because they answer two
        different questions (#1524):

        ``fatal`` — can this process usefully answer requests without it?
        Only ``database`` cannot:

        - ``database``    — no fallback, and every route that does anything
                            touches it.
        - ``redis`` /
          ``session_store`` — sessions re-establish and the rest of the
                            surface keeps answering. Note what is NOT an
                            argument for that: cloud keeps
                            ``RedisTokenRevocationStore``
                            (``create_token_revocation_store`` keys on
                            DEPLOYMENT_MODE, not on what the cache turned out
                            to be), so there is no database-backed revocation
                            fallback in the only deployment where readiness
                            matters.
        - ``vector_store``,
          ``knowledge_base`` — retrieval degrades; turns, uploads and every
                            read still work.
        - ``llm_provider`` — the router has fallback chains and the non-LLM
                            surface keeps working.
        - ``sanitizer``   — degrades to regex-only redaction by design.
        - ``tracer``      — observability only.

        ``fails_per_replica`` — can it fail for THIS pod while its siblings
        keep serving? **Nothing here declares it**, so the readiness-fatal
        set (``fatal and fails_per_replica``) is empty and ``/readiness``
        agrees with ``/health`` today. That is deliberate, and it is the
        whole of #1524's ruling.

        ‼ **``database`` is fatal and is deliberately NOT readiness-fatal.**
        Before you "fix" that, the argument, because it has been litigated:
        one shared PostgreSQL primary sits behind every replica
        (``DATABASE_HOST`` names a single Service, the chart runs 1 primary +
        1 *read* replica, the API runs 3 pods), so the database does not fail
        for one pod — it fails for all of them at once. Gating readiness on
        it converts a partial outage into a total one: a 60-second primary
        restart drops every pod from Endpoints, ingress then refuses every
        path — including ``/health``, ``/metrics`` and JWT validation, none
        of which touch the database — and "503 with a body" becomes
        "connection refused" at the moment the fleet most needs to be
        diagnosable. Readiness buys nothing there, because there is no
        healthy sibling to shift traffic to. A shared dependency being down
        is an **alert**, not a readiness signal.

        A second, independent hazard if it were added: ``_check_database_health``
        checks a connection out of the same process-global pool that serves
        requests, so any checkout timeout (including this module's own 3s
        probe cap) reports UNHEALTHY. Pod A saturates under load, fails
        readiness, sheds its traffic onto B and C, their pools saturate, and
        the fleet oscillates. Readiness touching nothing cannot join that loop.

        The same shared-failure argument independently excludes ``redis``,
        ``session_store`` and ``llm_provider`` — every replica shares one
        Redis and the same providers — but those are not ``fatal`` either, so
        they fail the first condition as well.

        Whoever adds the first genuinely per-pod component here is the one who
        decides the policy this set was left empty for.
        """
        default_components = {
            "database": {
                "dependencies": [],
                "fatal": True,
                "fails_per_replica": False,  # one shared primary — see above
            },
            "llm_provider": {
                "dependencies": [],
                "fatal": False,
                "fails_per_replica": False,
            },
            "knowledge_base": {
                "dependencies": ["database", "vector_store"],
                "fatal": False,
                "fails_per_replica": False,
            },
            "session_store": {
                "dependencies": ["redis"],
                "fatal": False,
                "fails_per_replica": False,
            },
            "vector_store": {
                "dependencies": [],
                "fatal": False,
                "fails_per_replica": False,
            },
            "redis": {
                "dependencies": [],
                "fatal": False,
                "fails_per_replica": False,
            },
            "sanitizer": {
                "dependencies": [],
                "fatal": False,
                "fails_per_replica": False,
            },
            "tracer": {
                "dependencies": [],
                "fatal": False,
                "fails_per_replica": False,
            },
        }

        for component, config in default_components.items():
            self.register_component(
                component,
                dependencies=config["dependencies"],
                fatal=config["fatal"],
                fails_per_replica=config["fails_per_replica"],
            )

    def register_component(
        self,
        component_name: str,
        dependencies: Optional[List[str]] = None,
        fatal: bool = False,
        fails_per_replica: bool = False,
    ) -> None:
        """Register a component for health monitoring.

        Args:
            component_name: Name of the component to monitor
            dependencies: List of components this component depends on
            fatal: Whether the process cannot usefully serve without it
            fails_per_replica: Whether it can fail for THIS pod alone while
                sibling replicas keep serving. Together with ``fatal`` this
                is the membership test for the readiness-fatal set — see
                ``_initialize_default_components`` for why no shipped
                component declares it.
        """
        # Initialize component health
        self.component_health[component_name] = ComponentHealth(
            component_name=component_name,
            status=HealthStatus.UNKNOWN,
            response_time_ms=0.0,
            dependencies=dependencies or [],
            fatal=fatal,
            fails_per_replica=fails_per_replica,
        )

        # Set up dependency mapping
        self.dependency_map[component_name] = DependencyMapping(
            component=component_name, critical_dependencies=dependencies or []
        )

        if fatal:
            self.fatal_components.add(component_name)
        else:
            self.fatal_components.discard(component_name)

        # The readiness-fatal set is the CONJUNCTION, computed here and
        # nowhere else. "Fatal to serving" alone is not enough: pulling the
        # pod has to be able to help, which needs a healthy sibling to shift
        # traffic to.
        if fatal and fails_per_replica:
            self.readiness_fatal_components.add(component_name)
        else:
            self.readiness_fatal_components.discard(component_name)

        # Initialize health history
        self.health_history[component_name] = []

        self.logger.info(
            f"Registered component for health monitoring: {component_name}"
        )

    async def check_component_health(self, component_name: str) -> ComponentHealth:
        """Check health of a specific component, under a deadline.

        The deadline is the point. A dependency that HANGS is the common
        failure — chromadb is constructed with ``timeout=None``, and behind
        the cluster's auth proxy a stalled read sits for ``proxy_read_timeout
        300s`` — and an unbounded probe does not report it, it joins it:
        ``/health`` stops answering, and every probe that reads ``/health``
        acts on the silence.

        A component that exceeds its deadline is UNHEALTHY, not UNKNOWN. "Did
        not answer in time" is a fact about the dependency, not an absence of
        information, and it is the same answer the caller would get.

        Args:
            component_name: Name of component to check

        Returns:
            Current health status of the component
        """
        if component_name not in self.component_health:
            self.logger.warning(f"Component not registered: {component_name}")
            return ComponentHealth(
                component_name=component_name,
                status=HealthStatus.UNKNOWN,
                response_time_ms=0.0,
            )

        start_time = time.time()

        try:
            # Perform component-specific health check
            health_result = await asyncio.wait_for(
                self._perform_health_check(component_name),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            response_time = (time.time() - start_time) * 1000

            # Update component health
            component_health = self.component_health[component_name]
            component_health.status = health_result["status"]
            component_health.response_time_ms = response_time
            component_health.last_error = health_result.get("error")
            component_health.last_check = datetime.now(timezone.utc)
            # Replace rather than merge: a key the probe no longer reports is a
            # key that is no longer true, and merging would keep serving the
            # last value it ever had as if it were current.
            component_health.metadata = dict(health_result.get("metadata", {}))

            # Record in history FIRST — the 24h figures below are derived from
            # the window, not accumulated in counters. Counters never expired,
            # so a "24h" count was really a lifetime count.
            self._record_health_history(
                component_name, component_health.status, response_time
            )
            self._refresh_probe_stats(component_health)

            return component_health

        except Exception as e:
            # `asyncio.TimeoutError` IS `TimeoutError` on 3.11+, so this arm
            # catches the deadline too; name it, because "timed out" and
            # "raised" are different findings to whoever reads last_error.
            if isinstance(e, asyncio.TimeoutError):
                error = (
                    f"probe exceeded {_PROBE_TIMEOUT_SECONDS:g}s and was "
                    "abandoned (the dependency did not answer)"
                )
                self.logger.error(f"Health check timed out for {component_name}")
            else:
                error = str(e)
                self.logger.error(f"Health check failed for {component_name}: {e}")

            # Update with error status
            component_health = self.component_health[component_name]
            component_health.status = HealthStatus.UNHEALTHY
            component_health.response_time_ms = (time.time() - start_time) * 1000
            component_health.last_error = error
            component_health.metadata = {}
            component_health.last_check = datetime.now(timezone.utc)
            self._record_health_history(
                component_name,
                HealthStatus.UNHEALTHY,
                component_health.response_time_ms,
            )
            self._refresh_probe_stats(component_health)

            return component_health

    async def _perform_health_check(self, component_name: str) -> Dict[str, Any]:
        """Perform actual health check for a component.

        Args:
            component_name: Name of component to check

        Returns:
            Health check result with status and metadata
        """
        # Component-specific health check logic
        if component_name == "database":
            return await self._check_database_health()
        elif component_name == "llm_provider":
            return await self._check_llm_provider_health()
        elif component_name == "knowledge_base":
            return await self._check_knowledge_base_health()
        elif component_name == "session_store":
            return await self._check_session_store_health()
        elif component_name == "vector_store":
            return await self._check_vector_store_health()
        elif component_name == "redis":
            return await self._check_redis_health()
        elif component_name == "sanitizer":
            return await self._check_sanitizer_health()
        elif component_name == "tracer":
            return await self._check_tracer_health()
        else:
            return await self._generic_health_check(component_name)

    async def _check_database_health(self) -> Dict[str, Any]:
        """Check DB connectivity and RLS tenant-isolation posture.

        Connectivity is a real `SELECT 1`. On PostgreSQL we additionally detect
        whether the connected role would BYPASS Row-Level Security — i.e. it is a
        superuser, has BYPASSRLS, or OWNS the tenanted tables (PostgreSQL exempts
        all three from RLS). Such a role silently defeats tenant isolation, so we
        report DEGRADED: the app must connect as a non-owner, non-superuser role
        (see docs/operations/rls-app-role.md in faultmaven-enterprise-infra).

        On SQLite (single-tenant standalone) RLS does not apply, so a successful
        connection is HEALTHY.
        """
        # Lazy imports: this module is a global singleton instantiated at import,
        # so defer coupling with the persistence layer to call time.
        from sqlalchemy import text

        from faultmaven.infrastructure.persistence.database import get_db_session

        try:
            async with get_db_session() as session:
                # Connectivity probe.
                await session.execute(text("SELECT 1"))

                dialect = session.get_bind().dialect.name
                if dialect != "postgresql":
                    # SQLite / standalone: single-tenant, no RLS to enforce.
                    return {
                        "status": HealthStatus.HEALTHY,
                        "metadata": {
                            "database_type": dialect,
                            "rls_applicable": False,
                        },
                    }

                # Posture is static per role/process — compute once, then cache.
                # A successful connectivity probe still runs every call above.
                if self._rls_posture is not None:
                    rls = self._rls_posture
                else:
                    rls = await self._detect_rls_bypass(session)
                    if not rls.get("check_error"):
                        self._rls_posture = rls
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e)}

        if rls.get("check_error"):
            # Connectivity is fine; we just couldn't determine RLS posture. Don't
            # cry wolf — report HEALTHY but record why the check was inconclusive.
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "database_type": "postgresql",
                    "rls_applicable": True,
                    "rls_check_error": rls["check_error"],
                },
            }

        metadata = {
            "database_type": "postgresql",
            "rls_applicable": True,
            "db_role": rls["role"],
            "rls_bypassed": rls["bypassed"],
            "rls_bypass_reasons": rls["reasons"],
        }

        if rls["bypassed"]:
            return {
                "status": HealthStatus.DEGRADED,
                "error": (
                    f"PostgreSQL role '{rls['role']}' BYPASSES Row-Level Security "
                    f"({', '.join(rls['reasons'])}); tenant isolation is NOT "
                    "enforced. The app must connect as a non-owner, non-superuser "
                    "role."
                ),
                "metadata": metadata,
            }

        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _detect_rls_bypass(self, session: Any) -> Dict[str, Any]:
        """Return the RLS-bypass posture of the connected PostgreSQL role.

        A connection bypasses RLS if its role is a superuser, has the BYPASSRLS
        attribute, or owns the tenanted tables. Probes role attributes plus
        ownership of the `cases` table (a representative tenanted table). Returns
        ``{"check_error": ...}`` if the posture can't be determined.
        """
        from sqlalchemy import text

        try:
            result = await session.execute(text("""
                    SELECT
                        r.rolname AS role,
                        r.rolsuper AS is_superuser,
                        r.rolbypassrls AS has_bypassrls,
                        EXISTS (
                            SELECT 1 FROM pg_tables t
                            WHERE t.tablename = 'cases'
                              AND t.tableowner = r.rolname
                        ) AS owns_tenanted_tables
                    FROM pg_roles r
                    WHERE r.rolname = current_user
                    """))
            row = result.mappings().fetchone()
        except Exception as e:
            return {"check_error": str(e)}

        if row is None:
            return {"check_error": "current_user not found in pg_roles"}

        reasons: List[str] = []
        if row["is_superuser"]:
            reasons.append("superuser")
        if row["has_bypassrls"]:
            reasons.append("bypassrls")
        if row["owns_tenanted_tables"]:
            reasons.append("owns_tenanted_tables")

        return {
            "role": row["role"],
            "bypassed": bool(reasons),
            "reasons": reasons,
        }

    @staticmethod
    def _live_container() -> Optional[Any]:
        """The DI container, but only once its lifespan initialisation ran.

        Before that, every service attribute is simply absent, which is not a
        failure — it is "nothing has been wired yet". Returning ``None`` keeps
        the caller from reporting an unhealthy dependency when what is really
        true is that we cannot tell.
        """
        try:
            from faultmaven.container import container
        except Exception:  # pragma: no cover - import-time failure
            return None
        return container if getattr(container, "_initialized", False) else None

    @staticmethod
    def _unavailable(reason: str) -> Dict[str, Any]:
        """Result for "the container has not wired anything yet"."""
        return {"status": HealthStatus.UNKNOWN, "error": reason, "metadata": {}}

    @staticmethod
    async def _in_probe_thread(fn: Callable[..., Any], *args: Any) -> Any:
        """Run blocking probe work on the health pool, never the shared one.

        See ``_PROBE_EXECUTOR``: this is what keeps a hung dependency from
        consuming the default executor that the request path shares.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_PROBE_EXECUTOR, fn, *args)

    def _service_state(self, container: Any, name: str) -> Optional[Dict[str, Any]]:
        """Non-``None`` when ``name`` is absent from a wired container.

        Distinguishes a service that was *deliberately* turned off (degraded —
        the operator asked for this) from one whose construction *failed*
        (unhealthy). Collapsing the two makes a supported configuration look
        broken, which is how a health signal gets ignored.
        """
        if getattr(container, name, None) is not None:
            return None

        registry = getattr(container, "_registry", None)
        error = None
        if registry is not None:
            for failed_name, failed_error in registry.get_failed_services():
                if failed_name == name:
                    error = failed_error or "service failed to initialize"
                    break
        if error is not None:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": error,
                "metadata": {"wired": False},
            }
        return {
            "status": HealthStatus.DEGRADED,
            "error": f"{name} is not enabled in this deployment",
            "metadata": {"wired": False},
        }

    async def _check_llm_provider_health(self) -> Dict[str, Any]:
        """Report what the LLM router can route to, without spending a call.

        No provider in the catalogue exposes an unbilled liveness endpoint —
        the only real connectivity test in this codebase is a completion, and
        billing one every ten seconds per pod is not a health check. So this
        reads the state the router already keeps from *real* traffic: which
        providers initialised with credentials, which the registry has marked
        unhealthy after consecutive failures, and whether the router's own
        circuit breaker is open. Those numbers are observed, not simulated.
        """
        container = self._live_container()
        if container is None:
            return self._unavailable("container not initialized")

        absent = self._service_state(container, "llm_provider")
        if absent is not None:
            return absent

        router = container.llm_provider
        registry = getattr(router, "registry", None)
        if registry is None:
            return {
                "status": HealthStatus.UNKNOWN,
                "error": "router exposes no provider registry",
                "metadata": {},
            }

        # `_ensure_initialized` constructs provider SDK clients on first use.
        # That is local work with no network, and it is idempotent, so the
        # cost lands once on the first probe after boot.
        available = await self._in_probe_thread(registry.get_available_providers)
        summary = await self._in_probe_thread(registry.get_provider_health_summary)
        unhealthy = sorted(
            name
            for name, state in summary.items()
            if state.get("health") == "unhealthy"
        )

        breaker = getattr(router, "circuit_breaker", None)
        breaker_state = getattr(breaker, "state", None)

        metadata: Dict[str, Any] = {
            "available_providers": sorted(available),
            "unhealthy_providers": unhealthy,
        }
        if breaker_state is not None:
            metadata["circuit_breaker"] = breaker_state
        metrics = getattr(router, "connection_metrics", None)
        if isinstance(metrics, dict):
            for key in ("total_calls", "successful_calls", "failed_calls"):
                if key in metrics:
                    metadata[key] = metrics[key]

        if not available:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": "no LLM provider is configured with usable credentials",
                "metadata": metadata,
            }
        if breaker_state == "open":
            return {
                "status": HealthStatus.DEGRADED,
                "error": "LLM router circuit breaker is open",
                "metadata": metadata,
            }
        if unhealthy:
            return {
                "status": HealthStatus.DEGRADED,
                "error": f"providers marked unhealthy: {', '.join(unhealthy)}",
                "metadata": metadata,
            }
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _check_knowledge_base_health(self) -> Dict[str, Any]:
        """Query ``knowledge_items``; report the count only where it is true.

        On PostgreSQL this session carries no tenant scope, so RLS answers for
        an empty tenant and a count would come back a confident zero — a new
        fabrication in place of the old ``document_count: 1250``. There the
        probe asks the cheaper question it can actually use, ``LIMIT 1``,
        which still proves the table is reachable. Paying for a ``COUNT(*)``
        every ten seconds to discard the answer would be the worst of both.
        """
        from sqlalchemy import func, literal, select

        from faultmaven.infrastructure.persistence.database import get_db_session
        from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

        try:
            async with get_db_session() as session:
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    await session.execute(
                        select(literal(1)).select_from(KnowledgeItemModel).limit(1)
                    )
                    count = None
                else:
                    count = await session.scalar(
                        select(func.count()).select_from(KnowledgeItemModel)
                    )
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        metadata: Dict[str, Any] = {"backend": dialect}
        if count is not None:
            metadata["knowledge_items"] = int(count)
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _check_session_store_health(self) -> Dict[str, Any]:
        """Read through the session store itself, not just its socket.

        ``redis`` below pings the connection; this asks the store for a key it
        will never find. Same socket, one layer up — so a wrapper that is
        misconfigured while the connection is fine still shows up here.
        """
        container = self._live_container()
        if container is None:
            return self._unavailable("container not initialized")

        absent = self._service_state(container, "session_store")
        if absent is not None:
            return absent

        store = container.session_store
        try:
            await store.exists(_SESSION_PROBE_KEY)
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        return {
            "status": HealthStatus.HEALTHY,
            "metadata": {
                "backend": _redis_backend_name(getattr(store, "redis_client", None))
            },
        }

    async def _check_vector_store_health(self) -> Dict[str, Any]:
        """Count the KB collection, which is both liveness and the real size.

        Deliberately reaches the collection directly rather than through
        ``ChromaDBVectorStore.count()``: that path shares a circuit breaker
        with production traffic, and a probe every ten seconds would keep
        resetting the failure count that breaker exists to accumulate.
        """
        container = self._live_container()
        if container is None:
            return self._unavailable("container not initialized")

        absent = self._service_state(container, "vector_store")
        if absent is not None:
            return absent

        store = container.vector_store
        collection = getattr(store, "collection", None)
        if collection is None:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": "vector store exposes no collection",
                "metadata": {},
            }

        try:
            # chromadb's client is synchronous; keep it off the event loop.
            count = await self._in_probe_thread(collection.count)
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        metadata: Dict[str, Any] = {"vectors": int(count)}
        name = getattr(store, "collection_name", None)
        if name:
            metadata["collection"] = name
        client = getattr(store, "client", None)
        if client is not None:
            metadata["client"] = type(client).__name__
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _check_redis_health(self) -> Dict[str, Any]:
        """PING the Redis connection the session store is built on.

        Reports which backend answered, because standalone silently
        substitutes in-process FakeRedis when the configured server is
        unreachable. A PING that only ever reaches a fake is worth knowing
        about; that substitution is otherwise a single warning at boot.
        """
        container = self._live_container()
        if container is None:
            return self._unavailable("container not initialized")

        absent = self._service_state(container, "redis_client")
        if absent is not None:
            return absent

        client = container.redis_client
        try:
            await client.ping()
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        metadata: Dict[str, Any] = {"backend": _redis_backend_name(client)}
        try:
            metadata["keys"] = int(await client.dbsize())
        except Exception:
            # DBSIZE is a nicety; a PING that answered is the health signal.
            pass
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _check_sanitizer_health(self) -> Dict[str, Any]:
        """Redact a fixed string through the REGEX path and check it changed.

        ``apply_regex_redaction``, never ``sanitize()``. ``sanitize()`` is only
        local when Presidio was never established: once ``analyzer_available``
        is true — the shipped cloud posture — it POSTs ``/analyze`` through
        ``call_external_sync``, and that records on the circuit breaker that
        gates real redaction. Both directions are wrong. A flaky analyzer:
        probe successes every ten seconds keep resetting ``failure_count``, so
        three consecutive real failures are rarely observed and the breaker
        stops opening when traffic needs it. A hung analyzer: the probe alone
        opens it in ~60s with no user traffic at all, and under the cloud
        fail-closed posture an open breaker makes every turn raise
        ``RedactionUnavailableError``.

        This is the same hazard as
        ``test_vector_store_avoids_the_production_circuit_breaker`` guards for
        ChromaDB — a health check must not change the behaviour it measures —
        and it is worse here because the client it would perturb is the one
        enforcing PII redaction. The regex entry point cannot reach the
        network at all, so the property is structural rather than incidental.

        Presidio's reachability is still reported, from the flags the
        sanitizer latched at construction; regex-only operation where Presidio
        was configured is DEGRADED, and where it was never configured is
        healthy.
        """
        container = self._live_container()
        if container is None:
            return self._unavailable("container not initialized")

        absent = self._service_state(container, "sanitizer")
        if absent is not None:
            return absent

        sanitizer = container.sanitizer
        try:
            redacted = await self._in_probe_thread(
                sanitizer.apply_regex_redaction, _SANITIZER_PROBE_INPUT, {}
            )
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        probed = bool(getattr(sanitizer, "presidio_probed", False))
        analyzer = getattr(sanitizer, "analyzer_available", None)
        anonymizer = getattr(sanitizer, "anonymizer_available", None)
        metadata: Dict[str, Any] = {
            "redaction_verified": redacted != _SANITIZER_PROBE_INPUT,
            "presidio_configured": probed,
        }
        if probed:
            # Only meaningful once Presidio was reached for; otherwise both
            # flags are False by configuration and say nothing.
            metadata["presidio_analyzer"] = bool(analyzer)
            metadata["presidio_anonymizer"] = bool(anonymizer)
        patterns = getattr(sanitizer, "pattern_replacements", None)
        if patterns is not None:
            metadata["redaction_patterns"] = len(patterns)

        if redacted == _SANITIZER_PROBE_INPUT:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": "sanitizer returned its input unredacted",
                "metadata": metadata,
            }
        if probed and not analyzer:
            return {
                "status": HealthStatus.DEGRADED,
                "error": "Presidio analyzer unavailable; regex-only redaction",
                "metadata": metadata,
            }
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _check_tracer_health(self) -> Dict[str, Any]:
        """Ask whether a traced call would actually record a span right now.

        ``tracing_is_effective()`` live-reads the Opik SDK rather than a
        config flag, so it separates "tracing is off" (fine) from "tracing is
        configured on and silently recording nothing" (degraded).
        """
        from faultmaven.config.settings import get_settings
        from faultmaven.infrastructure.observability.tracing import tracing_is_effective

        try:
            effective = bool(tracing_is_effective())
            enabled = bool(get_settings().observability.opik_enabled)
        except Exception as e:
            return {"status": HealthStatus.UNHEALTHY, "error": str(e), "metadata": {}}

        metadata: Dict[str, Any] = {"enabled": enabled, "effective": effective}

        container = self._live_container()
        tracer = getattr(container, "tracer", None) if container else None
        metrics = getattr(tracer, "connection_metrics", None)
        if isinstance(metrics, dict):
            for key in ("total_calls", "successful_calls", "failed_calls"):
                if key in metrics:
                    metadata[key] = metrics[key]

        if enabled and not effective:
            return {
                "status": HealthStatus.DEGRADED,
                "error": "tracing is enabled but no span would be recorded",
                "metadata": metadata,
            }
        return {"status": HealthStatus.HEALTHY, "metadata": metadata}

    async def _generic_health_check(self, component_name: str) -> Dict[str, Any]:
        """An unregistered component has no probe, and says so.

        There is nothing to call, so the honest answer is UNKNOWN. Returning
        HEALTHY here would mean any name at all could be reported healthy.
        """
        return {
            "status": HealthStatus.UNKNOWN,
            "error": f"no health probe is defined for '{component_name}'",
            "metadata": {},
        }

    def _refresh_probe_stats(self, health: ComponentHealth) -> None:
        """Recompute the 24h probe figures from the pruned history window.

        A probe counts as *available* unless it came back UNHEALTHY: DEGRADED
        means the dependency answered and is doing reduced work, which is
        availability. Whether it is degraded is a separate axis and is already
        in ``status``. Counting degraded as downtime would make availability
        and status the same signal, reported twice.
        """
        window = self.health_history.get(health.component_name, [])
        total = len(window)
        failures = sum(1 for _, status, _ in window if status == HealthStatus.UNHEALTHY)
        health.probe_failures_24h = failures
        health.probe_successes_24h = total - failures
        health.probe_availability_24h = (
            round(((total - failures) / total) * 100, 2) if total else 100.0
        )

    def _record_health_history(
        self, component_name: str, status: HealthStatus, response_time: float
    ) -> None:
        """Record health check result in history."""
        if component_name not in self.health_history:
            self.health_history[component_name] = []

        # Add new record
        self.health_history[component_name].append(
            (datetime.now(timezone.utc), status, response_time)
        )

        # Keep only last 24 hours of history
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
        self.health_history[component_name] = [
            record
            for record in self.health_history[component_name]
            if record[0] >= cutoff_time
        ]

    async def check_all_components(self) -> Dict[str, ComponentHealth]:
        """Check every registered component concurrently, under a sweep budget.

        Each probe already carries its own deadline; this is the backstop for
        anything that escapes it — a probe that does not observe cancellation
        promptly, or a future registration that forgets its own bound. The
        whole sweep is what ``/health`` waits on, so an unbounded sweep is an
        unbounded endpoint whatever the individual probes promise.

        ``asyncio.wait`` rather than ``wait_for(gather(...))``: cancelling a
        gather discards the results of the probes that DID answer, and a
        report that loses seven components because the eighth hung is worse
        than the hang.

        Returns:
            Dictionary mapping component names to their health status
        """
        names = list(self.component_health.keys())
        tasks = {
            asyncio.ensure_future(self.check_component_health(name)): name
            for name in names
        }

        done, pending = await asyncio.wait(
            tasks.keys(), timeout=_ALL_COMPONENTS_TIMEOUT_SECONDS
        )

        for task in pending:
            task.cancel()

        health_results: Dict[str, ComponentHealth] = {}

        for task in pending:
            component_name = tasks[task]
            self.logger.error(
                f"Health check exceeded the sweep budget for {component_name}"
            )
            health_results[component_name] = ComponentHealth(
                component_name=component_name,
                status=HealthStatus.UNHEALTHY,
                response_time_ms=_ALL_COMPONENTS_TIMEOUT_SECONDS * 1000,
                last_error=(
                    f"probe exceeded the {_ALL_COMPONENTS_TIMEOUT_SECONDS:g}s "
                    "sweep budget and was abandoned"
                ),
                dependencies=self.component_health[component_name].dependencies,
                fatal=self.component_health[component_name].fatal,
                fails_per_replica=self.component_health[
                    component_name
                ].fails_per_replica,
            )

        for task in done:
            component_name = tasks[task]
            error = task.exception()
            if error is not None:
                self.logger.error(f"Health check failed for {component_name}: {error}")
                health_results[component_name] = ComponentHealth(
                    component_name=component_name,
                    status=HealthStatus.UNHEALTHY,
                    response_time_ms=0.0,
                    last_error=str(error),
                    dependencies=self.component_health[component_name].dependencies,
                    fatal=self.component_health[component_name].fatal,
                    fails_per_replica=self.component_health[
                        component_name
                    ].fails_per_replica,
                )
            else:
                health_results[component_name] = task.result()

        # Preserve registration order; `asyncio.wait` returns unordered sets.
        return {name: health_results[name] for name in names}

    def get_dependency_map(self) -> Dict[str, List[str]]:
        """Get dependency mapping for all components.

        Returns:
            Dictionary mapping components to their dependencies
        """
        return {
            component: mapping.critical_dependencies + mapping.optional_dependencies
            for component, mapping in self.dependency_map.items()
        }

    def get_critical_path_dependencies(self) -> Dict[str, List[str]]:
        """Get critical path dependencies for all components.

        Returns:
            Dictionary mapping components to their critical dependencies only
        """
        return {
            component: mapping.critical_dependencies
            for component, mapping in self.dependency_map.items()
        }

    def get_overall_health_status(self) -> Tuple[HealthStatus, Dict[str, Any]]:
        """Get overall system health status based on all components.

        Returns:
            Tuple of overall status and summary information
        """
        if not self.component_health:
            return HealthStatus.UNKNOWN, {"reason": "No components registered"}

        status_counts: Dict[str, int] = {}
        fatal_unhealthy: List[str] = []

        for component_name, health in self.component_health.items():
            status = health.status
            status_counts[status.value] = status_counts.get(status.value, 0) + 1

            # Fatal means "cannot serve", so only UNHEALTHY counts. DEGRADED
            # is a component that still works (the database reports it for an
            # RLS-bypassing role), and UNKNOWN means we could not tell — and
            # "we could not tell" must never be the reason a pod is taken out
            # of service.
            if health.fatal and status == HealthStatus.UNHEALTHY:
                fatal_unhealthy.append(component_name)

        if fatal_unhealthy:
            overall_status = HealthStatus.UNHEALTHY
            reason = (
                "Components fatal to serving are unhealthy: "
                f"{', '.join(fatal_unhealthy)}"
            )
        elif status_counts.get("unhealthy", 0) > 0:
            overall_status = HealthStatus.DEGRADED
            reason = f"{status_counts['unhealthy']} components unhealthy"
        elif status_counts.get("degraded", 0) > 0:
            overall_status = HealthStatus.DEGRADED
            reason = f"{status_counts['degraded']} components degraded"
        elif status_counts.get("unknown", 0) > 0:
            overall_status = HealthStatus.DEGRADED
            reason = f"{status_counts['unknown']} components not determinable"
        else:
            overall_status = HealthStatus.HEALTHY
            reason = "All components healthy"

        # Availability of this monitor's own probes over the retained window —
        # a real measurement now that the probes do real I/O, and named for
        # what it measures rather than borrowing the word "SLA" from the
        # request-level tracker, which measures something else entirely.
        availabilities = [
            health.probe_availability_24h for health in self.component_health.values()
        ]
        probe_availability = (
            sum(availabilities) / len(availabilities) if availabilities else 100.0
        )

        summary = {
            "reason": reason,
            "component_counts": status_counts,
            "probe_availability_24h": round(probe_availability, 2),
            "fatal_unhealthy": fatal_unhealthy,
            "total_components": len(self.component_health),
        }

        return overall_status, summary

    async def check_serving_readiness(self) -> Tuple[bool, Dict[str, Any]]:
        """Should this pod stay in its Service right now?

        Checks only the **readiness-fatal** set — ``fatal`` *and*
        ``fails_per_replica`` — because readiness gates traffic and every
        extra dependency in that gate is another way to pull a pod that could
        still have served. Returns ``(ready, detail)``.

        That set is empty today, so this answers ready without probing
        anything and ``/readiness`` agrees with ``/health``. Deliberate, not
        vestigial: the wiring is live, and the first component that can
        genuinely fail on one replica turns it into a real gate. See
        ``_initialize_default_components``.
        """
        names = sorted(self.readiness_fatal_components)
        results = await asyncio.gather(
            *(self.check_component_health(name) for name in names),
            return_exceptions=True,
        )

        blocking: List[str] = []
        detail: Dict[str, Any] = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                self.logger.error(f"Readiness check failed for {name}: {result}")
                blocking.append(name)
                detail[name] = {"status": HealthStatus.UNHEALTHY.value}
                continue
            detail[name] = {
                "status": result.status.value,
                "response_time_ms": round(result.response_time_ms, 2),
            }
            if result.status == HealthStatus.UNHEALTHY:
                blocking.append(name)

        return not blocking, {
            "checked": names,
            "blocking": blocking,
            "components": detail,
        }

    def get_component_metrics(self, component_name: str) -> Dict[str, Any]:
        """Get detailed metrics for a specific component.

        Args:
            component_name: Name of component to get metrics for

        Returns:
            Dictionary with detailed component metrics
        """
        if component_name not in self.component_health:
            return {"error": f"Component {component_name} not found"}

        health = self.component_health[component_name]
        history = self.health_history.get(component_name, [])

        # Calculate metrics from history
        if history:
            response_times = [rt for _, _, rt in history]
            avg_response_time = sum(response_times) / len(response_times)
            max_response_time = max(response_times)
            min_response_time = min(response_times)
        else:
            avg_response_time = health.response_time_ms
            max_response_time = health.response_time_ms
            min_response_time = health.response_time_ms

        return {
            "component_name": component_name,
            "current_status": health.status.value,
            "current_response_time_ms": health.response_time_ms,
            "fatal": health.fatal,
            "probe_availability_24h": health.probe_availability_24h,
            "last_error": health.last_error,
            "last_check": health.last_check.isoformat(),
            "dependencies": health.dependencies,
            "metadata": health.metadata,
            "probes_24h": {
                "success_count": health.probe_successes_24h,
                "error_count": health.probe_failures_24h,
                "avg_response_time_ms": round(avg_response_time, 2),
                "max_response_time_ms": round(max_response_time, 2),
                "min_response_time_ms": round(min_response_time, 2),
                "total_checks": len(history),
            },
        }


def _redis_backend_name(client: Any) -> str:
    """``"fakeredis"`` or ``"redis"`` — which implementation answered."""
    if client is None:
        return "unknown"
    try:
        from faultmaven.infrastructure.redis_client import is_fakeredis

        return "fakeredis" if is_fakeredis(client) else "redis"
    except Exception:  # pragma: no cover - import-time failure
        return "unknown"


# Global component health monitor instance
component_monitor = ComponentHealthMonitor()
