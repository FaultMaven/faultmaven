"""#1787: a labelled counter declares its children, and the bounded ones exist at 0.

``prometheus_client`` creates a labelled child on its first ``.labels()``, so a
series first appears already at 1 and ``increase()``/``rate()`` never see its
first event. The shim's ``Counter`` therefore refuses a labelled counter that
declares neither ``label_values`` (every combination created at 0) nor
``lazy_labels`` (why a label is open-ended), with metrics on or off.

The refusals need no ``prometheus_client``. The tests that need a real counter
skip without it; CI's cloud slice runs them.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven
from faultmaven.infrastructure.shims.metrics import (
    DECLARED_CHILDREN,
    LAZY_DECLARED,
    MAX_DECLARED_CHILDREN,
    Counter,
    NoOpMetric,
    create_children,
    declare_lazy,
)

pytestmark = pytest.mark.unit

_BAD_DECLARATIONS = {
    "neither": {},
    "both": {"label_values": {"a": ("x",)}, "lazy_labels": "open"},
    "a key that is not a label": {"label_values": {"b": ("x",)}},
    "an empty value set": {"label_values": {"a": ()}},
    "a bare string as a value set": {"label_values": {"a": "xy"}},
    "an empty lazy reason": {"lazy_labels": "  "},
    "too many children": {
        "label_values": {"a": [str(i) for i in range(MAX_DECLARED_CHILDREN + 1)]}
    },
}


@pytest.mark.parametrize("enabled", ["false", "true"])
@pytest.mark.parametrize("shape", sorted(_BAD_DECLARATIONS))
def test_an_undeclared_labelled_counter_is_refused(monkeypatch, enabled, shape):
    monkeypatch.setenv("ENABLE_METRICS", enabled)
    with pytest.raises(ValueError, match="'fm1787_refused_total'"):
        Counter(
            "fm1787_refused_total", "d", labelnames=["a"], **_BAD_DECLARATIONS[shape]
        )


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_an_unlabelled_counter_takes_neither(monkeypatch, enabled):
    monkeypatch.setenv("ENABLE_METRICS", enabled)
    with pytest.raises(ValueError, match="'fm1787_plain_total'"):
        Counter("fm1787_plain_total", "d", lazy_labels="no labels to be open")
    with pytest.raises(ValueError, match="'fm1787_plain_total'"):
        Counter("fm1787_plain_total", "d", label_values={})


def test_metrics_off_creates_nothing(monkeypatch):
    monkeypatch.setenv("ENABLE_METRICS", "false")
    before = list(DECLARED_CHILDREN)
    counter = Counter(
        "fm1787_off_total", "d", labelnames=["a"], label_values={"a": ("x", "y")}
    )
    assert isinstance(counter, NoOpMetric)
    create_children(counter, {"a": ("x",)})
    assert DECLARED_CHILDREN == before


def _samples(registry, family_name):
    return {
        tuple(sorted(s.labels.items())): s.value
        for family in registry.collect()
        if family.name == family_name
        for s in family.samples
        if s.name.endswith("_total")
    }


def test_every_declared_child_is_exposed_at_zero_before_any_event(monkeypatch):
    prometheus_client = pytest.importorskip("prometheus_client")
    monkeypatch.setenv("ENABLE_METRICS", "true")
    registry = prometheus_client.CollectorRegistry()
    counter = Counter(
        "fm1787_on_total",
        "d",
        labelnames=["gate", "reason"],
        registry=registry,
        label_values={"gate": ("g1", "g2"), "reason": ("stale", "untargeted")},
    )
    zero = {
        (("gate", g), ("reason", r)): 0.0
        for g in ("g1", "g2")
        for r in ("stale", "untargeted")
    }
    assert _samples(registry, "fm1787_on") == zero
    assert DECLARED_CHILDREN[-1] == (
        "fm1787_on",
        ("gate", "reason"),
        (("g1", "stale"), ("g1", "untargeted"), ("g2", "stale"), ("g2", "untargeted")),
    )

    counter.labels(gate="g2", reason="stale").inc()
    assert _samples(registry, "fm1787_on") == {
        **zero,
        (("gate", "g2"), ("reason", "stale")): 1.0,
    }


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_a_generator_value_set_is_read_once(monkeypatch, enabled):
    monkeypatch.setenv("ENABLE_METRICS", enabled)
    if enabled == "false":
        counter = Counter(
            "fm1787_gen_off_total",
            "d",
            labelnames=["a"],
            label_values={"a": (v for v in ("x", "y"))},
        )
        assert isinstance(counter, NoOpMetric)
        return
    prometheus_client = pytest.importorskip("prometheus_client")
    registry = prometheus_client.CollectorRegistry()
    Counter(
        "fm1787_gen_total",
        "d",
        labelnames=["a"],
        registry=registry,
        label_values={"a": (v for v in ("x", "y"))},
    )
    assert _samples(registry, "fm1787_gen") == {(("a", "x"),): 0.0, (("a", "y"),): 0.0}


def test_declare_lazy_refuses_an_empty_reason():
    with pytest.raises(ValueError, match="empty reason"):
        declare_lazy(NoOpMetric(), " ")


def test_a_direct_counter_gets_nothing_with_metrics_disabled(monkeypatch):
    prometheus_client = pytest.importorskip("prometheus_client")
    monkeypatch.setenv("ENABLE_METRICS", "false")
    registry = prometheus_client.CollectorRegistry()
    counter = prometheus_client.Counter(
        "fm1787_disabled_total", "d", ["event"], registry=registry
    )
    declared, lazy = list(DECLARED_CHILDREN), list(LAZY_DECLARED)
    create_children(counter, {"event": ("offered", "saved")})
    declare_lazy(counter, "event is open")
    assert _samples(registry, "fm1787_disabled") == {}
    assert (DECLARED_CHILDREN, LAZY_DECLARED) == (declared, lazy)


def test_create_children_declares_a_counter_built_without_the_shim(monkeypatch):
    prometheus_client = pytest.importorskip("prometheus_client")
    monkeypatch.setenv("ENABLE_METRICS", "true")
    registry = prometheus_client.CollectorRegistry()
    counter = prometheus_client.Counter(
        "fm1787_direct_total", "d", ["event"], registry=registry
    )
    assert _samples(registry, "fm1787_direct") == {}
    create_children(counter, {"event": ("offered", "saved")})
    assert _samples(registry, "fm1787_direct") == {
        (("event", "offered"),): 0.0,
        (("event", "saved"),): 0.0,
    }
    with pytest.raises(ValueError, match="'fm1787_direct'"):
        create_children(counter, {"other": ("x",)})
    declare_lazy(counter, "event is open")
    assert LAZY_DECLARED[-1] == ("fm1787_direct", "event is open")


# Every module that defines a labelled counter. The child imports them with
# metrics on and reads the default registry, which is what /metrics serves.
_METRIC_MODULES = (
    "faultmaven.infrastructure.shims.metrics",
    "faultmaven.core.investigation.lifecycle_metrics",
    "faultmaven.core.investigation.reliability_metrics",
    "faultmaven.core.investigation.tool_loop_metrics",
    "faultmaven.infrastructure.observability.evidence_metrics",
    "faultmaven.infrastructure.observability.tracing",
    "faultmaven.modules.auth.domain.services.user_service",
    "faultmaven.modules.auth.infrastructure.metrics.oauth_metrics",
    "faultmaven.modules.auth.infrastructure.metrics.revocation_metrics",
    "faultmaven.modules.auth.infrastructure.metrics.session_metrics",
)

_MARKER = "@@RESULT@@"

_CHILD = r"""
import importlib, json, sys
from prometheus_client import REGISTRY, Counter

modules = [importlib.import_module(name) for name in sys.argv[1:]]

# Three rare counters added after #1776 fixed the class once (#1787's examples).
named = {
    "driver_release": REGISTRY.get_sample_value(
        "faultmaven_case_driver_release_failed_total", {"hook": "deactivation"}
    ),
    "chat_data_saved": REGISTRY.get_sample_value(
        "faultmaven_chat_data_offer_events_total", {"event": "saved"}
    ),
    "unleased_no_store": REGISTRY.get_sample_value(
        "case_unleased_turns_total", {"reason": "no_store"}
    ),
    # The exchange's client mismatch (#2750).
    "client_mismatch": REGISTRY.get_sample_value(
        "oauth_token_exchange_errors_total",
        {"grant_type": "authorization_code", "error_code": "CLIENT_MISMATCH"},
    ),
}

from faultmaven.infrastructure.shims.metrics import DECLARED_CHILDREN, LAZY_DECLARED

# Every labelled counter bound at module level in the metric modules, whether
# built through the shim or straight from prometheus_client, is declared.
recorded = {name for name, _, _ in DECLARED_CHILDREN} | {n for n, _ in LAZY_DECLARED}
undeclared = sorted(
    {
        value._name
        for module in modules
        for value in vars(module).values()
        if isinstance(value, Counter) and value._labelnames
    }
    - recorded
)

missing, nonzero = [], []
for name, labelnames, combos in DECLARED_CHILDREN:
    for combo in combos:
        value = REGISTRY.get_sample_value(name + "_total", dict(zip(labelnames, combo)))
        if value is None:
            missing.append([name, list(combo)])
    for family in REGISTRY.collect():
        if family.name == name:
            nonzero += [
                [s.name, s.labels]
                for s in family.samples
                if s.name.endswith("_total") and s.value != 0
            ]
print("@@RESULT@@" + json.dumps({
    "file": importlib.import_module("faultmaven").__file__,
    "named": named,
    "declared": sorted(name for name, _, _ in DECLARED_CHILDREN),
    "undeclared": undeclared,
    "missing": missing,
    "nonzero": nonzero,
}))
"""


def test_every_bounded_series_is_exported_at_zero_from_import(tmp_path):
    pytest.importorskip("prometheus_client")
    repo_root = Path(faultmaven.__file__).resolve().parent.parent
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(repo_root),
        "ENABLE_METRICS": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, *_METRIC_MODULES],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(_MARKER))
    result = json.loads(line[len(_MARKER) :])
    assert result["file"].startswith(str(repo_root))
    assert result["named"] == {
        "driver_release": 0.0,
        "chat_data_saved": 0.0,
        "unleased_no_store": 0.0,
        "client_mismatch": 0.0,
    }
    assert len(result["declared"]) == len(set(result["declared"]))
    assert result["undeclared"] == []
    assert result["missing"] == []
    assert result["nonzero"] == []
