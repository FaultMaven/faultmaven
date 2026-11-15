"""One switch, one path: every collector goes through the metrics shim (#2748).

Three modules built their collectors straight from ``prometheus_client`` and
gated only on the import, so with the library installed and ``ENABLE_METRICS``
off they still registered on the default registry and recorded (#2736). The
switch also had three spellings: ``ENABLE_METRICS`` (the shim's),
``METRICS_ENABLED`` (a setting nothing read) and ``PROMETHEUS_ENABLED`` (the APM
pushgateway exporter, a second metrics path, #2780).

Pin 2: importing each module that imports ``prometheus_client`` or calls the
shim, in a fresh interpreter with the switch off, registers nothing. Outside the
shim, no module imports anything from ``prometheus_client`` but the listed
exposition, registry and parser names, and none imports a ``Prometheus*`` name
from the shim or its package, or reads one as an attribute of a name bound to
either. A plain ``import prometheus_client`` fails too. Pin 3: no setting, CI
line, doc, ``.env.example`` or compose file names another switch. #2780: the
APM integration has no Prometheus exporter.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic_settings import BaseSettings

import faultmaven
from faultmaven.config import settings as settings_module
from faultmaven.infrastructure import shims
from faultmaven.infrastructure.observability import tracing
from faultmaven.infrastructure.observability.apm_integration import (
    APMIntegration,
    APMProvider,
)
from faultmaven.infrastructure.shims.metrics import NoOpMetric, is_real
from faultmaven.modules.auth.infrastructure.metrics import oauth_metrics

pytestmark = pytest.mark.unit

REPO_ROOT = Path(faultmaven.__file__).resolve().parent.parent

_SHIM = "faultmaven.infrastructure.shims.metrics"
_SHIM_PACKAGE = "faultmaven.infrastructure.shims"
_EXPORTER = "faultmaven.infrastructure.observability.metrics_exporters.prometheus"
_EXTRACTOR = "faultmaven.modules.preprocessing.extractors.metrics_extractor"

#: Every module under ``faultmaven/`` that imports ``prometheus_client`` or
#: calls the metrics shim; the scan below must find exactly these.
COLLECTOR_MODULES = (
    "faultmaven.api.middleware.logging",
    "faultmaven.core.investigation.lifecycle_metrics",
    "faultmaven.core.investigation.reliability_metrics",
    "faultmaven.core.investigation.tool_loop_metrics",
    "faultmaven.infrastructure.concurrency.inflight",
    "faultmaven.infrastructure.health.component_monitor",
    "faultmaven.infrastructure.health.sla_tracker",
    # In pieces: a string naming the dispatch whole trips #1998's lexical scan.
    "faultmaven.infrastructure.llm." + "dispatch",
    "faultmaven.infrastructure.llm.metering",
    "faultmaven.infrastructure.llm.router",
    "faultmaven.infrastructure.llm.usage_ledger",
    "faultmaven.infrastructure.observability.evidence_metrics",
    "faultmaven.infrastructure.observability.funnel_metrics",
    "faultmaven.infrastructure.observability.loop_lag",
    _EXPORTER,
    "faultmaven.infrastructure.observability.tracing",
    "faultmaven.infrastructure.shims",
    "faultmaven.infrastructure.shims.metrics",
    "faultmaven.modules.auth.domain.services.user_service",
    "faultmaven.modules.auth.infrastructure.metrics.oauth_metrics",
    "faultmaven.modules.auth.infrastructure.metrics.revocation_metrics",
    "faultmaven.modules.auth.infrastructure.metrics.session_metrics",
    _EXTRACTOR,
)


def _module_name(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _source(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute module *node* imports from, relative imports resolved."""
    source = node.module or ""
    if node.level:
        base = module.split(".")
        base = base[: len(base) - node.level + (1 if is_package else 0)]
        source = ".".join(base + ([source] if source else []))
    return source


def _imports_metrics(node: ast.AST, module: str, is_package: bool) -> bool:
    """Whether *node* imports ``prometheus_client`` or the metrics shim."""
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == "prometheus_client" for a in node.names)
    if not isinstance(node, ast.ImportFrom):
        return False
    source = _source(node, module, is_package)
    names = {a.name for a in node.names}
    return (
        source.split(".")[0] == "prometheus_client"
        or source == _SHIM
        or (source == _SHIM_PACKAGE and "metrics" in names)
        or (
            source == _SHIM_PACKAGE
            and bool(names & set(shims._EXPORTS_BY_SUBMODULE["metrics"]))
        )
    )


def _enumerate() -> set:
    found = set()
    for path in (REPO_ROOT / "faultmaven").rglob("*.py"):
        module = _module_name(path)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(_imports_metrics(n, module, is_package) for n in ast.walk(tree)):
            found.add(module)
    return found


def test_the_list_is_every_importer_and_shim_caller():
    assert _enumerate() == set(COLLECTOR_MODULES)


#: Every ``prometheus_client`` import outside the shim: (module, imported from,
#: name). Exposition, registry and parser only; no collector class, and no
#: ``Prometheus*`` class the shim binds.
_ALLOWED_PROMETHEUS_IMPORTS = {
    # ``/metrics``: serialises the default registry the shim's collectors fill.
    (_EXPORTER, "prometheus_client", "CONTENT_TYPE_LATEST"),
    (_EXPORTER, "prometheus_client", "REGISTRY"),
    (_EXPORTER, "prometheus_client", "generate_latest"),
    # Parses an uploaded Prometheus text exposition; builds no collector.
    (_EXTRACTOR, "prometheus_client.parser", "text_string_to_metric_families"),
}


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a chain of names and attributes, else ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else ""
    return ""


def _prometheus_imports(source: str, module: str, is_package: bool = False) -> set:
    """(module, from, name) for each ``prometheus_client`` import in *source*
    (a bare ``import`` is ``*``: it reaches every class), and for each
    ``Prometheus*`` name it takes from the shim or its package, imported or
    read as an attribute of a name bound to either."""
    shim_modules = {_SHIM, _SHIM_PACKAGE}
    found, bound = set(), {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "prometheus_client":
                    found.add((module, a.name, "*"))
                # ``import x.y as m`` binds m to x.y; ``import x.y`` binds x.
                bound[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            origin = _source(node, module, is_package)
            for a in node.names:
                if origin.split(".")[0] == "prometheus_client":
                    found.add((module, origin, a.name))
                elif origin in shim_modules and (
                    a.name == "*" or a.name.startswith("Prometheus")
                ):
                    found.add((module, origin, a.name))
                bound[a.asname or a.name] = f"{origin}.{a.name}"
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr.startswith("Prometheus"):
            head, _, rest = _dotted(node.value).partition(".")
            owner = ".".join(filter(None, [bound.get(head, ""), rest]))
            if owner in shim_modules:
                found.add((module, owner, node.attr))
    return found


def test_only_the_shim_imports_a_collector_class():
    found = set()
    for path in (REPO_ROOT / "faultmaven").rglob("*.py"):
        module = _module_name(path)
        if module != _SHIM:
            source = path.read_text(encoding="utf-8")
            found |= _prometheus_imports(source, module, path.name == "__init__.py")
    assert found == _ALLOWED_PROMETHEUS_IMPORTS


@pytest.mark.parametrize(
    "line",
    [
        "from prometheus_client import Counter",
        "from prometheus_client.metrics import Gauge",
        "import prometheus_client",
        f"from {_SHIM} import PrometheusCounter",
        f"from {_SHIM_PACKAGE} import PrometheusGauge",
        f"import {_SHIM} as m\nm.PrometheusCounter('x', 'd')",
        f"from {_SHIM_PACKAGE} import metrics\nmetrics.PrometheusHistogram",
        f"import {_SHIM}\n{_SHIM}.PrometheusSummary",
        "from . import metrics as m\nm.PrometheusInfo",
        f"from {_SHIM} import *",
    ],
)
def test_the_import_check_sees_a_collector_class(line):
    """Positive control: a planted collector import is outside the allow-list."""
    planted = _prometheus_imports(line, f"{_SHIM_PACKAGE}.planted")
    assert planted and not planted & _ALLOWED_PROMETHEUS_IMPORTS


_MARKER = "@@REGISTRY@@"

_CHILD = r"""
import importlib, json, sys
from prometheus_client import REGISTRY

def families():
    return {family.name for family in REGISTRY.collect()}

before = families()
importlib.import_module(sys.argv[1])
gained = sorted(families() - before)
import faultmaven
print("@@REGISTRY@@" + json.dumps({"file": faultmaven.__file__, "gained": gained}))
"""


def _gained(module: str, tmp_path: Path, **env: str) -> list:
    """The families importing *module* adds to the default registry, in a fresh
    interpreter with no ``.env`` and only *env* set."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, module],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT),
            "FAULTMAVEN_SKIP_DOTENV": "1",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(_MARKER))
    result = json.loads(line[len(_MARKER) :])
    assert result["file"].startswith(str(REPO_ROOT))
    return result["gained"]


@pytest.mark.parametrize("module", COLLECTOR_MODULES)
def test_importing_with_the_switch_off_registers_nothing(module, tmp_path):
    pytest.importorskip("prometheus_client")
    assert _gained(module, tmp_path) == []


def test_the_probe_sees_a_registration_with_the_switch_on(tmp_path):
    """Positive control: the same probe, switch on, sees the OAuth families."""
    pytest.importorskip("prometheus_client")
    gained = _gained(
        "faultmaven.modules.auth.infrastructure.metrics.oauth_metrics",
        tmp_path,
        ENABLE_METRICS="true",
    )
    assert "oauth_authorization_requests" in gained
    assert "oauth_token_exchange_duration_seconds" in gained


#: Spellings of the metrics switch other than ``ENABLE_METRICS``.
_OTHER_SWITCHES = {"METRICS_ENABLED", "PROMETHEUS_ENABLED"}


def _env_names(model: type) -> set:
    """Every environment variable a settings class's fields bind."""
    prefix = model.model_config.get("env_prefix", "")
    names = set()
    for name, field in model.model_fields.items():
        names.add(f"{prefix}{name}".upper())
        for alias in (field.alias, field.validation_alias):
            choices = getattr(alias, "choices", [alias])
            names |= {c.upper() for c in choices if isinstance(c, str)}
    return names


def test_no_setting_names_another_switch():
    models = [
        value
        for value in vars(settings_module).values()
        if isinstance(value, type)
        and issubclass(value, BaseSettings)
        and value.__module__ == settings_module.__name__
    ]
    bound = set().union(*(_env_names(m) for m in models))
    assert "OPIK_ENABLED" in bound  # the walk reached ObservabilitySettings
    assert bound & _OTHER_SWITCHES == set()


def _env_keys(node) -> set:
    """Every key of every ``env:`` mapping in a parsed workflow."""
    keys = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                keys |= set(value)
            keys |= _env_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _env_keys(item)
    return keys


def test_no_ci_env_line_names_another_switch():
    keys = set()
    for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        keys |= _env_keys(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert "ENABLE_METRICS" in keys
    assert keys & _OTHER_SWITCHES == set()


#: Where a switch's name would reach an operator or a CI run.
_SWITCH_SCAN = ("docs/**/*", ".env.example", "docker-compose*.yml", ".github/**/*")

#: Lines allowed to name another switch: none.
_SWITCH_MENTIONS_ALLOWED: set = set()


def _switch_mentions(root: Path) -> set:
    """(path, line number) of each line under *root*'s scanned files naming
    ``METRICS_ENABLED`` or ``PROMETHEUS_ENABLED``, ``run:`` lines included."""
    found = set()
    for pattern in _SWITCH_SCAN:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
            for number, line in enumerate(text.splitlines(), 1):
                if any(name in line for name in _OTHER_SWITCHES):
                    found.add((path.relative_to(root).as_posix(), number))
    return found


def test_no_doc_env_template_compose_file_or_ci_line_names_another_switch():
    assert _switch_mentions(REPO_ROOT) == _SWITCH_MENTIONS_ALLOWED


def test_the_switch_scan_sees_a_planted_line(tmp_path):
    """Positive control: a planted mention in each scanned place is found."""
    planted = {
        "docs/operations/x.md": "Set `METRICS_ENABLED=true`.",
        ".env.example": "PROMETHEUS_ENABLED=false",
        "docker-compose.yml": "      - METRICS_ENABLED=true",
        ".github/workflows/ci.yml": "      - run: PROMETHEUS_ENABLED=true pytest",
    }
    for rel, line in planted.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"first line\n{line}\n", encoding="utf-8")
    assert _switch_mentions(tmp_path) == {(rel, 2) for rel in planted}


def test_the_apm_integration_has_no_prometheus_exporter():
    """#2780: the pushgateway exporter, its provider and its settings are gone."""
    assert "prometheus" not in {provider.value for provider in APMProvider}
    observability = settings_module.ObservabilitySettings.model_fields
    assert [f for f in observability if f.startswith("prometheus")] == []

    apm = APMIntegration(settings=settings_module.get_settings())
    stats = apm.get_export_statistics()
    assert set(stats["configurations"]) == {"opik", "generic"}
    assert not hasattr(apm, "_export_to_prometheus")


@pytest.mark.parametrize("stand_in", [None, NoOpMetric()], ids=["module", "no-op"])
def test_the_oauth_recorder_is_enabled_by_its_collectors(monkeypatch, stand_in):
    """Read from the collector, not the environment flipped since it was built."""
    if stand_in is not None:
        monkeypatch.setattr(oauth_metrics, "oauth_authorization_requests", stand_in)
    real = is_real(oauth_metrics.oauth_authorization_requests)
    monkeypatch.setenv("ENABLE_METRICS", "false" if real else "true")
    assert oauth_metrics.OAuthMetricsRecorder(()).enabled is real


@pytest.mark.parametrize("stand_in", [None, NoOpMetric()], ids=["module", "no-op"])
async def test_the_tracer_health_reports_whether_its_collectors_record(
    monkeypatch, stand_in
):
    if stand_in is not None:
        monkeypatch.setattr(tracing, "GENERIC_FUNCTION_DURATION", stand_in)
    real = is_real(tracing.GENERIC_FUNCTION_DURATION)
    monkeypatch.setenv("ENABLE_METRICS", "false" if real else "true")
    tracer = tracing.OpikTracer(settings=settings_module.get_settings())
    report = (await tracer.health_check())["tracer_specific"]
    assert report["metrics_recording"] is real
    assert "prometheus_available" not in report
