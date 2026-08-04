"""Signature-conformance guard for module contracts, real impls and stand-ins.

Why this exists
---------------
fm#964: ``AuthSessionService.get_session`` lacked the ``validate`` keyword its
own module contract (``ISessionService``, ``modules/auth/contracts.py``)
declares. Five call sites passed it and 500'd in production — while every test
suite stayed green, because the container stand-in ``MinimalSessionService``
(``faultmaven/_container_impl.py``) *did* accept the keyword. Nothing in the
repo compared the three surfaces, and mypy runs neither in CI nor in
pre-commit, so the divergence was invisible until users hit it.

This module is the root-cause fix: one generic ``inspect.signature`` engine
plus a declarative registry, so the whole defect class fails RED here.

Three rule families
-------------------
Rule A — a real implementation conforms to its contract.
Rule B — a container stand-in conforms to the same contract.
Rule C — the stand-in's surface is a SUBSET of the real class's surface: for
    every public method the stand-in exposes beyond the contract, the real
    class must have it, and every call that binds against the stand-in's
    signature must also bind against the real one. A stand-in that accepts
    what the real class rejects hides live 500s — that is exactly how fm#964
    and the dead ``cleanup_session_data`` route stayed invisible.

A completeness tripwire scans ``faultmaven/modules/*/contracts.py`` and
``faultmaven/models/interfaces_case.py`` for ``I*`` interfaces so a newly
added contract that is registered nowhere fails RED rather than passing
silently. The scan FAILS CLOSED: an import error or an empty enumeration is a
test failure, never a skip.

NOT covered
-----------
This guard is signature-level only. RETURN-SHAPE conformance (a stand-in that
returns a ``dict`` where the real service returns a ``CaseMessagesResponse``,
or a contract annotated ``-> Case`` whose impl returns ``None``) is *not*
checked here, nor is any behavioural equivalence. Those need a different
instrument; see ``test_extractor_protocol_conformance.py`` for the
round-trip-probe style that covers return shapes for a small protocol.
"""

from __future__ import annotations

import abc
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytest

# ============================================================
# Signature-compatibility engine
# ============================================================

_VAR_KINDS = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
_EMPTY = inspect.Parameter.empty


@dataclass(frozen=True)
class Divergence:
    """One concrete way two signatures disagree.

    Rendered per-interface / per-method / per-rule so a reviewer reading a
    failure sees exactly which pair diverged and how.
    """

    rule: str
    interface: str
    member: str
    left: str
    right: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"[Rule {self.rule}] {self.interface}.{self.member}: "
            f"{self.left} vs {self.right} -- {self.detail}"
        )


def _unwrap(func: Any) -> Any:
    """Strip decorators (``@trace`` and friends) to reach the real function.

    ``functools.wraps`` sets ``__wrapped__``, which both ``inspect.signature``
    and ``inspect.unwrap`` follow — but we unwrap explicitly because
    ``inspect.iscoroutinefunction`` does NOT follow it, and a sync wrapper
    around an async function (or the reverse) would otherwise be misread.
    """
    return inspect.unwrap(func)


def _is_coroutine(func: Any) -> bool:
    return inspect.iscoroutinefunction(_unwrap(func))


def _signature_of(func: Any) -> inspect.Signature:
    """Signature with any leading ``self``/``cls`` dropped.

    Handles both unbound functions taken off a class (``self`` present) and
    bound methods taken off an instance (``self`` already consumed), so the
    two are directly comparable.
    """
    sig = inspect.signature(_unwrap(func))
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    return sig.replace(parameters=params)


def _named_params(sig: inspect.Signature) -> Dict[str, inspect.Parameter]:
    return {p.name: p for p in sig.parameters.values() if p.kind not in _VAR_KINDS}


def _has_var_keyword(sig: inspect.Signature) -> bool:
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _has_var_positional(sig: inspect.Signature) -> bool:
    return any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
    )


def _kinds_compatible(declared: inspect.Parameter, provided: inspect.Parameter) -> bool:
    """Can ``provided`` be passed the way ``declared`` says callers will pass it?"""
    P = inspect.Parameter
    if declared.kind is P.POSITIONAL_OR_KEYWORD:
        # Callers may use either form; only POSITIONAL_OR_KEYWORD accepts both.
        return provided.kind is P.POSITIONAL_OR_KEYWORD
    if declared.kind is P.KEYWORD_ONLY:
        return provided.kind in (P.KEYWORD_ONLY, P.POSITIONAL_OR_KEYWORD)
    if declared.kind is P.POSITIONAL_ONLY:
        return provided.kind in (P.POSITIONAL_ONLY, P.POSITIONAL_OR_KEYWORD)
    return True


def _describe(func: Any, owner: str) -> str:
    try:
        return f"{owner}{_signature_of(func)}"
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return owner


def compare_to_interface(
    *,
    rule: str,
    interface_name: str,
    member: str,
    declared: Any,
    provided: Any,
    declared_label: str,
    provided_label: str,
) -> List[Divergence]:
    """Rules A and B: ``provided`` must honour every call ``declared`` permits."""
    out: List[Divergence] = []

    def add(detail: str) -> None:
        out.append(
            Divergence(
                rule=rule,
                interface=interface_name,
                member=member,
                left=_describe(declared, declared_label),
                right=_describe(provided, provided_label),
                detail=detail,
            )
        )

    if _is_coroutine(declared) != _is_coroutine(provided):
        add(
            f"coroutine mismatch: contract is "
            f"{'async' if _is_coroutine(declared) else 'sync'}, implementation is "
            f"{'async' if _is_coroutine(provided) else 'sync'}"
        )
        return out

    try:
        declared_sig = _signature_of(declared)
        provided_sig = _signature_of(provided)
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        add(f"signature not introspectable: {exc}")
        return out

    declared_params = _named_params(declared_sig)
    provided_params = _named_params(provided_sig)
    provided_kwargs = _has_var_keyword(provided_sig)
    provided_args = _has_var_positional(provided_sig)

    for name, dparam in declared_params.items():
        pparam = provided_params.get(name)
        if pparam is None:
            # **kwargs absorbs any keyword; *args only absorbs positionals.
            absorbed = provided_kwargs or (
                provided_args and dparam.kind is inspect.Parameter.POSITIONAL_ONLY
            )
            if not absorbed:
                add(
                    f"implementation does not accept parameter '{name}' "
                    f"declared by the contract"
                )
            continue
        if not _kinds_compatible(dparam, pparam):
            add(
                f"parameter '{name}' kind {pparam.kind.name} cannot serve "
                f"contract kind {dparam.kind.name}"
            )
        if dparam.default is not _EMPTY and pparam.default != dparam.default:
            add(
                f"parameter '{name}' default is {pparam.default!r}, "
                f"contract declares {dparam.default!r}"
            )

    for name, pparam in provided_params.items():
        if name in declared_params:
            continue
        if pparam.default is _EMPTY:
            add(
                f"implementation REQUIRES parameter '{name}', which the "
                f"contract does not declare — every contract-shaped call fails"
            )

    return out


def compare_standin_subset(
    *,
    interface_name: str,
    member: str,
    standin: Any,
    real: Any,
) -> List[Divergence]:
    """Rule C: every call binding on the stand-in must bind on the real class."""
    out: List[Divergence] = []

    def add(detail: str) -> None:
        out.append(
            Divergence(
                rule="C",
                interface=interface_name,
                member=member,
                left=_describe(standin, "stand-in"),
                right=_describe(real, "real"),
                detail=detail,
            )
        )

    if _is_coroutine(standin) != _is_coroutine(real):
        add(
            f"coroutine mismatch: stand-in is "
            f"{'async' if _is_coroutine(standin) else 'sync'}, real is "
            f"{'async' if _is_coroutine(real) else 'sync'} — the same call site "
            f"cannot serve both"
        )
        return out

    try:
        standin_sig = _signature_of(standin)
        real_sig = _signature_of(real)
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        add(f"signature not introspectable: {exc}")
        return out

    standin_params = _named_params(standin_sig)
    real_params = _named_params(real_sig)
    real_kwargs = _has_var_keyword(real_sig)
    real_args = _has_var_positional(real_sig)

    for name, sparam in standin_params.items():
        rparam = real_params.get(name)
        if rparam is None:
            absorbed = real_kwargs or (
                real_args and sparam.kind is inspect.Parameter.POSITIONAL_ONLY
            )
            if not absorbed:
                add(
                    f"stand-in accepts parameter '{name}' that the real class "
                    f"rejects — a caller shaped against the stand-in 500s in "
                    f"production"
                )
            continue
        if not _kinds_compatible(sparam, rparam):
            add(
                f"parameter '{name}': real kind {rparam.kind.name} cannot serve "
                f"stand-in kind {sparam.kind.name}"
            )
        if sparam.default is not _EMPTY and rparam.default is _EMPTY:
            add(
                f"parameter '{name}' is optional on the stand-in but REQUIRED "
                f"on the real class — a call omitting it binds on one, not the other"
            )

    for name, rparam in real_params.items():
        if name in standin_params:
            continue
        if rparam.default is _EMPTY:
            add(
                f"real class REQUIRES parameter '{name}' the stand-in does not "
                f"accept — a call shaped against the stand-in cannot bind on the real"
            )

    return out


# ============================================================
# Member enumeration
# ============================================================

_SKIP_BASES = {"Protocol", "Generic", "ABC", "object"}


def interface_members(interface: type) -> Dict[str, Any]:
    """Public methods/properties an interface declares, walking its MRO."""
    members: Dict[str, Any] = {}
    for klass in reversed(interface.__mro__):
        if klass.__name__ in _SKIP_BASES:
            continue
        for name, value in vars(klass).items():
            if name.startswith("_"):
                continue
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if isinstance(value, property) or inspect.isfunction(value):
                members[name] = value
    return members


def public_callables(obj: Any) -> Dict[str, Any]:
    """Public callable attributes of a class or instance (no dunders/privates)."""
    out: Dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except AttributeError:  # pragma: no cover - descriptors that raise
            continue
        if callable(value) and (inspect.isfunction(value) or inspect.ismethod(value)):
            out[name] = value
    return out


# ============================================================
# Stand-in construction
# ============================================================


def _bare_container():
    """A DIContainer shell with no session state.

    ``DIContainer.__new__`` returns SESSION STATE (the process singleton), so
    calling it here would mutate/observe whatever the rest of the suite has
    built. ``object.__new__`` gives an unowned shell that is enough to call
    the ``_create_minimal_*`` factories, which are self-contained.
    """
    # Imported via the package facade: importing ``_container_impl`` directly
    # trips a circular import (it re-enters ``faultmaven.container``).
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)


def minimal_session_service():
    return _bare_container()._create_minimal_session_service()


def minimal_case_service():
    return _bare_container()._create_minimal_case_service()


# ============================================================
# Registry
# ============================================================


@dataclass(frozen=True)
class RegistryEntry:
    """One interface, its real implementation(s) and its container stand-in(s).

    ``reals`` are classes (Rule A works off the class, no construction needed).
    ``standins`` are zero-arg factories returning a live stand-in INSTANCE,
    because the stand-ins are closures defined inside container factories.
    """

    interface: type
    reals: Tuple[type, ...]
    standins: Tuple[Tuple[str, Callable[[], Any]], ...] = ()
    #: Contract members whose divergence needs a DESIGN decision, not a
    #: mechanical alignment — ``member -> "PENDING: <reason>"``. Suppressing a
    #: member here is not free: ``test_pending_members_are_still_divergent``
    #: fails RED the moment a pending member stops diverging, so a stale
    #: suppression cannot quietly widen the hole.
    pending_members: Tuple[Tuple[str, str], ...] = ()

    def pending(self) -> Dict[str, str]:
        return dict(self.pending_members)


#: MinimalCaseService is the DEGRADED-MODE case service (container falls back
#: to it when no case repository is available), so these gaps 500 in
#: production today. Filling them is not mechanical: a stand-in
#: ``search_cases`` returning ``[]`` reads to the caller as "no matching
#: cases" when the truth is "search unavailable", which is precisely the
#: incorrect-conclusion failure the product must not produce. The decision —
#: implement degraded semantics, narrow ICaseService, or stop presenting the
#: stand-in as an ICaseService — is an owner call.
_MINIMAL_CASE_SERVICE_PENDING = (
    "PENDING: degraded-mode ICaseService coverage is an owner decision — a "
    "stand-in that answers these with empty/zero values would report absence "
    "where the truth is unavailability."
)


def _build_registry() -> Dict[str, RegistryEntry]:
    """Import every registered interface/implementation pair.

    Imports live here (not at module scope) so a broken import surfaces as a
    named failure inside a test rather than a collection error with no context.
    """
    from faultmaven.infrastructure.persistence.user_repository import (
        InMemoryUserRepository,
        PostgreSQLUserRepository,
        SessionlessUserRepository,
    )
    from faultmaven.models.interfaces_case import ICaseService
    from faultmaven.modules.auth.contracts import (
        IAuthService,
        IOAuthCodeRepository,
        IOAuthService,
        ISessionService,
        ISSOIdentityProvider,
        ISSOOrgMappingRepository,
        IUserRepository,
    )
    from faultmaven.modules.auth.domain.services.auth_service import AuthService
    from faultmaven.modules.auth.domain.services.auth_session_service import (
        AuthSessionService,
    )
    from faultmaven.modules.auth.domain.services.oauth_service import OAuthServiceImpl
    from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (  # noqa: E501
        InMemoryOAuthCodeRepository,
        PostgresOAuthCodeRepository,
        RedisOAuthCodeRepository,
    )
    from faultmaven.modules.auth.infrastructure.repositories.sso_org_mapping_repository import (  # noqa: E501
        SessionlessSSOOrgMappingRepository,
        SSOOrgMappingRepository,
    )
    from faultmaven.modules.auth.infrastructure.sso.workos_provider import (
        WorkOSIdentityProvider,
    )
    from faultmaven.modules.case.contracts import ICaseRepository
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.case_repository import (
        InMemoryCaseRepository,
    )
    from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (  # noqa: E501
        PostgreSQLHybridCaseRepository,
    )
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )
    from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
        SQLiteCaseRepository,
    )
    from faultmaven.modules.knowledge.contracts import (
        IConversionService,
        IKnowledgeService,
        ISuggestionService,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )

    entries = (
        RegistryEntry(
            interface=ISessionService,
            reals=(AuthSessionService,),
            standins=(("MinimalSessionService", minimal_session_service),),
        ),
        RegistryEntry(
            interface=ICaseService,
            reals=(CaseService,),
            standins=(("MinimalCaseService", minimal_case_service),),
            pending_members=tuple(
                (member, _MINIMAL_CASE_SERVICE_PENDING)
                for member in (
                    "add_message_to_case",
                    "get_or_create_case_for_session",
                    "link_session_to_case",
                    "resume_case_in_session",
                    "search_cases",
                    "get_case_analytics",
                    "cleanup_expired_cases",
                )
            ),
        ),
        RegistryEntry(
            interface=ICaseRepository,
            reals=(
                InMemoryCaseRepository,
                SQLiteCaseRepository,
                PostgreSQLHybridCaseRepository,
                SessionlessCaseRepository,
            ),
        ),
        RegistryEntry(interface=IKnowledgeService, reals=(KnowledgeService,)),
        RegistryEntry(interface=IConversionService, reals=(ConversionService,)),
        RegistryEntry(interface=ISuggestionService, reals=(SuggestionService,)),
        RegistryEntry(
            interface=IUserRepository,
            reals=(
                InMemoryUserRepository,
                PostgreSQLUserRepository,
                SessionlessUserRepository,
            ),
        ),
        RegistryEntry(interface=IAuthService, reals=(AuthService,)),
        RegistryEntry(interface=IOAuthService, reals=(OAuthServiceImpl,)),
        RegistryEntry(
            interface=IOAuthCodeRepository,
            reals=(
                InMemoryOAuthCodeRepository,
                RedisOAuthCodeRepository,
                PostgresOAuthCodeRepository,
            ),
        ),
        RegistryEntry(interface=ISSOIdentityProvider, reals=(WorkOSIdentityProvider,)),
        RegistryEntry(
            interface=ISSOOrgMappingRepository,
            reals=(SSOOrgMappingRepository, SessionlessSSOOrgMappingRepository),
        ),
    )
    return {entry.interface.__name__: entry for entry in entries}


#: Interfaces deliberately outside the guard. Every entry needs a one-line
#: reason; the tripwire below fails RED for any interface in neither this dict
#: nor the registry. Exclusions are the exception, not the default.
EXCLUDED_INTERFACES: Dict[str, str] = {
    "IUserQuery": (
        "Declared read-only query port with no concrete implementation in the "
        "repo (no class exposes get_user + get_by_email over User)."
    ),
    "IPermissionChecker": (
        "Declared port with no concrete implementation — nothing defines "
        "can_access(user_id, resource)."
    ),
    "ILocalAuthService": (
        "Declared port with no concrete implementation — no class defines "
        "login/register per this contract."
    ),
    "ICaseStore": "Declared storage port with no concrete implementation.",
    "ICaseNotificationService": (
        "Declared notification port with no concrete implementation."
    ),
    "ICaseIntegrationService": (
        "Declared integration port with no concrete implementation."
    ),
}


# ============================================================
# Completeness tripwire
# ============================================================

_CONTRACT_MODULES = (
    "faultmaven.modules.auth.contracts",
    "faultmaven.modules.case.contracts",
    "faultmaven.modules.knowledge.contracts",
    "faultmaven.models.interfaces_case",
)


def _package_root() -> Path:
    import faultmaven

    return Path(inspect.getfile(faultmaven)).parent


def discover_interface_modules() -> List[str]:
    """Modules the tripwire scans: every module contracts file + interfaces_case.

    Discovered from disk (not hardcoded) so a NEW vertical module's
    ``contracts.py`` is scanned the day it lands.
    """
    root = _package_root()
    found = [
        f"faultmaven.modules.{path.parent.name}.contracts"
        for path in sorted(root.glob("modules/*/contracts.py"))
    ]
    found.append("faultmaven.models.interfaces_case")
    return found


def discover_interfaces() -> Dict[str, type]:
    """Every ``I*`` Protocol/ABC declared in the scanned modules.

    FAILS CLOSED: import errors propagate (the caller turns them into a test
    failure) and an empty result is itself a failure — a swallowed error on
    enumeration produces a gate that passes while checking nothing.
    """
    discovered: Dict[str, type] = {}
    for module_name in discover_interface_modules():
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if not name.startswith("I") or not inspect.isclass(obj):
                continue
            if len(name) < 2 or not name[1].isupper():
                continue
            if obj.__module__ != module_name:
                continue  # re-export, counted where it is declared
            is_protocol = getattr(obj, "_is_protocol", False)
            is_abc = isinstance(obj, abc.ABCMeta)
            if is_protocol or is_abc:
                discovered[name] = obj
    return discovered


# ============================================================
# Test parametrisation
# ============================================================


def _registry_or_fail() -> Dict[str, RegistryEntry]:
    try:
        return _build_registry()
    except Exception as exc:  # noqa: BLE001 - fail closed, never skip
        pytest.fail(
            "Contract registry could not be built (fail-closed): "
            f"{type(exc).__name__}: {exc}"
        )


def _registry_params() -> List[Tuple[str, str]]:
    """(interface_name, real_class_name) pairs, resolved lazily at run time."""
    try:
        registry = _build_registry()
    except Exception:  # pragma: no cover - reported by test_registry_imports
        return []
    return [
        (iface, real.__name__)
        for iface, entry in registry.items()
        for real in entry.reals
    ]


def _standin_params() -> List[Tuple[str, str]]:
    try:
        registry = _build_registry()
    except Exception:  # pragma: no cover - reported by test_registry_imports
        return []
    return [
        (iface, label)
        for iface, entry in registry.items()
        for label, _ in entry.standins
    ]


def _render(divergences: Iterable[Divergence]) -> str:
    lines = [str(d) for d in divergences]
    return "\n".join(f"  - {line}" for line in lines)


def _lookup_member(owner: Any, name: str) -> Optional[Any]:
    value = getattr(owner, name, None)
    if value is None:
        return None
    return value


# ============================================================
# Tests
# ============================================================


@pytest.mark.unit
class TestContractRegistry:
    """The registry itself must be importable and complete."""

    def test_registry_imports(self) -> None:
        """Every registered interface and implementation imports cleanly."""
        registry = _registry_or_fail()
        assert registry, "Contract registry is empty — the guard checks nothing"

    def test_every_interface_is_registered_or_excluded(self) -> None:
        """Tripwire: a new I* contract must be checked or explicitly excluded.

        Fails closed — an import error or an empty enumeration is a failure,
        never a silent pass.
        """
        try:
            discovered = discover_interfaces()
        except Exception as exc:  # noqa: BLE001 - fail closed, never skip
            pytest.fail(
                "Interface enumeration failed (fail-closed): "
                f"{type(exc).__name__}: {exc}"
            )

        assert discovered, (
            "Interface enumeration found ZERO I* contracts. Either the scan "
            "paths are wrong or the modules failed to expose their contracts; "
            "either way this gate would pass while checking nothing."
        )

        registry = _registry_or_fail()
        known = set(registry) | set(EXCLUDED_INTERFACES)
        unregistered = sorted(set(discovered) - known)
        assert not unregistered, (
            "These interfaces are neither checked by the signature-conformance "
            "registry nor listed in EXCLUDED_INTERFACES with a reason:\n"
            + "\n".join(f"  - {name}" for name in unregistered)
            + "\n\nAdd a RegistryEntry (preferred) or an EXCLUDED_INTERFACES "
            "entry explaining why it cannot be checked."
        )

        stale = sorted(
            name
            for name in (set(registry) | set(EXCLUDED_INTERFACES))
            if name not in discovered
        )
        assert not stale, (
            "These registry/exclusion entries name interfaces that no longer "
            "exist in the scanned contract modules:\n"
            + "\n".join(f"  - {name}" for name in stale)
        )

    def test_exclusions_carry_reasons(self) -> None:
        """An exclusion without a reason is an undocumented hole."""
        blank = sorted(
            name for name, reason in EXCLUDED_INTERFACES.items() if not reason.strip()
        )
        assert not blank, f"EXCLUDED_INTERFACES entries with no reason: {blank}"

    def test_pending_members_are_declared_and_labelled(self) -> None:
        """Every pending member must name a real contract member, with a reason."""
        registry = _registry_or_fail()
        problems: List[str] = []
        for interface_name, entry in registry.items():
            declared = set(interface_members(entry.interface))
            for member, reason in entry.pending().items():
                if member not in declared:
                    problems.append(
                        f"{interface_name}.{member}: pending member is not "
                        f"declared by the interface"
                    )
                if not reason.startswith("PENDING:"):
                    problems.append(
                        f"{interface_name}.{member}: pending reason must start "
                        f"with 'PENDING:'"
                    )
                if not entry.standins:
                    problems.append(
                        f"{interface_name}.{member}: pending members only apply "
                        f"to stand-ins, and this entry has none"
                    )
        assert not problems, "\n".join(problems)

    def test_pending_members_are_still_divergent(self) -> None:
        """A pending suppression that no longer suppresses anything is RED.

        Without this, a member could be fixed while its PENDING entry lingers,
        silently exempting it again the next time it regressed. The suppression
        must be removed the moment it stops being needed.
        """
        registry = _registry_or_fail()
        vacuous: List[str] = []
        for interface_name, entry in registry.items():
            pending = entry.pending()
            if not pending:
                continue
            declared_members = interface_members(entry.interface)
            for standin_name, factory in entry.standins:
                standin = factory()
                for member in pending:
                    declared = declared_members.get(member)
                    provided = _lookup_member(standin, member)
                    if provided is None or declared is None:
                        continue  # still missing => suppression is doing work
                    if isinstance(declared, property):
                        continue
                    still_diverges = bool(
                        compare_to_interface(
                            rule="B",
                            interface_name=interface_name,
                            member=member,
                            declared=declared,
                            provided=provided,
                            declared_label=interface_name,
                            provided_label=standin_name,
                        )
                    )
                    if not still_diverges:
                        vacuous.append(f"{interface_name}.{member} ({standin_name})")

        assert not vacuous, (
            "These members carry a PENDING suppression but no longer diverge — "
            "remove the pending_members entry so the guard covers them again:\n"
            + "\n".join(f"  - {name}" for name in sorted(vacuous))
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "interface_name, real_name",
    _registry_params(),
    ids=[f"{i}->{r}" for i, r in _registry_params()],
)
def test_rule_a_real_implementation_conforms_to_contract(
    interface_name: str, real_name: str
) -> None:
    """Rule A: the real implementation honours every call its contract permits.

    This is literally fm#964: a contract-declared keyword missing from the
    implementation must fail here, not in production.
    """
    registry = _registry_or_fail()
    entry = registry[interface_name]
    real = next(r for r in entry.reals if r.__name__ == real_name)

    divergences: List[Divergence] = []
    for member_name, declared in interface_members(entry.interface).items():
        provided = _lookup_member(real, member_name)
        if provided is None:
            divergences.append(
                Divergence(
                    rule="A",
                    interface=interface_name,
                    member=member_name,
                    left=f"{interface_name}",
                    right=real_name,
                    detail="implementation has no such attribute",
                )
            )
            continue
        if isinstance(declared, property):
            # Properties are checked for existence only; their accessors are
            # not part of the callable surface a caller binds against.
            continue
        if not callable(provided):
            divergences.append(
                Divergence(
                    rule="A",
                    interface=interface_name,
                    member=member_name,
                    left=interface_name,
                    right=real_name,
                    detail=f"implementation attribute is {type(provided).__name__}, "
                    "not callable",
                )
            )
            continue
        divergences.extend(
            compare_to_interface(
                rule="A",
                interface_name=interface_name,
                member=member_name,
                declared=declared,
                provided=provided,
                declared_label=interface_name,
                provided_label=real_name,
            )
        )

    assert (
        not divergences
    ), f"{real_name} diverges from its contract {interface_name}:\n" + _render(
        divergences
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "interface_name, standin_name",
    _standin_params(),
    ids=[f"{i}->{s}" for i, s in _standin_params()],
)
def test_rule_b_standin_conforms_to_contract(
    interface_name: str, standin_name: str
) -> None:
    """Rule B: the container stand-in honours the same contract as the real."""
    registry = _registry_or_fail()
    entry = registry[interface_name]
    factory = next(f for label, f in entry.standins if label == standin_name)
    standin = factory()
    pending = entry.pending()

    divergences: List[Divergence] = []
    for member_name, declared in interface_members(entry.interface).items():
        if member_name in pending:
            continue
        provided = _lookup_member(standin, member_name)
        if provided is None:
            divergences.append(
                Divergence(
                    rule="B",
                    interface=interface_name,
                    member=member_name,
                    left=interface_name,
                    right=standin_name,
                    detail="stand-in has no such attribute",
                )
            )
            continue
        if isinstance(declared, property):
            continue
        if not callable(provided):
            divergences.append(
                Divergence(
                    rule="B",
                    interface=interface_name,
                    member=member_name,
                    left=interface_name,
                    right=standin_name,
                    detail=f"stand-in attribute is {type(provided).__name__}, "
                    "not callable",
                )
            )
            continue
        divergences.extend(
            compare_to_interface(
                rule="B",
                interface_name=interface_name,
                member=member_name,
                declared=declared,
                provided=provided,
                declared_label=interface_name,
                provided_label=standin_name,
            )
        )

    assert (
        not divergences
    ), f"{standin_name} diverges from contract {interface_name}:\n" + _render(
        divergences
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "interface_name, standin_name",
    _standin_params(),
    ids=[f"{i}->{s}" for i, s in _standin_params()],
)
def test_rule_c_standin_surface_is_subset_of_real(
    interface_name: str, standin_name: str
) -> None:
    """Rule C: the stand-in exposes nothing the real class cannot serve.

    A stand-in that accepts what the real class rejects hides live 500s: the
    dead ``POST /{session_id}/cleanup`` route called
    ``cleanup_session_data`` which only ever existed on the stand-in, so every
    production call 500'd while the suite stayed green.
    """
    registry = _registry_or_fail()
    entry = registry[interface_name]
    factory = next(f for label, f in entry.standins if label == standin_name)
    standin = factory()

    contract_members = set(interface_members(entry.interface))
    extras = {
        name: func
        for name, func in public_callables(standin).items()
        if name not in contract_members
    }

    divergences: List[Divergence] = []
    for real in entry.reals:
        for member_name, standin_func in sorted(extras.items()):
            real_func = _lookup_member(real, member_name)
            if real_func is None:
                divergences.append(
                    Divergence(
                        rule="C",
                        interface=interface_name,
                        member=member_name,
                        left=standin_name,
                        right=real.__name__,
                        detail=(
                            "stand-in exposes a method the real class does not "
                            "have — any caller reaching it in production 500s"
                        ),
                    )
                )
                continue
            if not callable(real_func):
                divergences.append(
                    Divergence(
                        rule="C",
                        interface=interface_name,
                        member=member_name,
                        left=standin_name,
                        right=real.__name__,
                        detail=f"real attribute is {type(real_func).__name__}, "
                        "not callable",
                    )
                )
                continue
            divergences.extend(
                compare_standin_subset(
                    interface_name=interface_name,
                    member=member_name,
                    standin=standin_func,
                    real=real_func,
                )
            )

    assert not divergences, (
        f"{standin_name} exposes surface its real counterpart cannot serve:\n"
        + _render(divergences)
    )
