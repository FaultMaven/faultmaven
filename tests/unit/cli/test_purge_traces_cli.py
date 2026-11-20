"""``fm-purge-traces``: its refusals, its counts and its exit codes (#2700).

The purge runs for real over the fake backend of ``test_trace_purge_2700.py``;
only the SDK's configuration is replaced, as opik is not in the dev install.
"""

import sys

import pytest

from faultmaven.cli import purge_traces
from faultmaven.config import settings as settings_module
from faultmaven.infrastructure.observability import opik_sdk, trace_erasure
from tests.unit.infrastructure.observability.test_trace_purge_2700 import (  # noqa: F401
    OLD,
    _Backend,
    fake_sdk,
)

pytestmark = pytest.mark.unit

BEFORE = "2026-11-18T00:00:00Z"
CONFIGURED = opik_sdk.TraceBackend(
    sdk_available=True,
    configured=True,
    url="http://127.0.0.1:5173/api/",
    workspace="default",
    project="faultmaven",
    reason=None,
)


@pytest.fixture
def run(monkeypatch, capsys):
    """``main()`` with ``argv`` on ``store``; answers (exit code, stdout)."""
    monkeypatch.setattr(
        purge_traces._log_chain, "install_command_logging", lambda: None
    )
    monkeypatch.setattr(purge_traces, "apply_root_set", lambda: None)
    monkeypatch.setattr(settings_module, "get_settings", lambda: None)

    def run(argv, store, record=CONFIGURED):
        def configure(_settings):
            if isinstance(record, Exception):
                raise record
            return record

        real = trace_erasure.purge_traces_before

        def purge(cutoff, **kwargs):
            return real(
                cutoff, client_factory=lambda: store, sleep=store.sleep, **kwargs
            )

        monkeypatch.setattr(opik_sdk, "configure", configure)
        monkeypatch.setattr(trace_erasure, "purge_traces_before", purge)
        monkeypatch.setattr(sys, "argv", ["fm-purge-traces", *argv])
        with pytest.raises(SystemExit) as exited:
            purge_traces.main()
        return exited.value.code, capsys.readouterr().out

    return run


DELETE = ["--before", BEFORE, "--delete", "--confirm-project", "faultmaven"]


def test_the_dry_run_prints_the_target_and_counts_and_deletes_nothing(run):
    store = _Backend(traces={"t1": OLD}, spans=[("t1", OLD)])

    code, out = run(["--before", BEFORE], store)

    assert code == 0 and store.deleted == []
    assert "http://127.0.0.1:5173/api/  workspace: default  project: faultmaven" in out
    assert "Started before 2026-11-18T00:00:00.000000Z: 1 traces, 1 spans" in out


def test_the_delete_prints_before_and_after_and_exits_0_when_none_remain(run):
    store = _Backend(traces={"t1": OLD}, spans=[("t1", OLD)])

    code, out = run(DELETE, store)

    assert code == 0 and store.traces == {} and store.spans == []
    assert "1 traces, 1 spans" in out and "Left after the delete: 0 traces, 0" in out


def test_spans_left_after_the_wait_exit_3_for_a_re_run(run):
    store = _Backend(spans=[("stuck", OLD)])
    store.lost = type("Always", (set,), {"__contains__": lambda *_: True})()

    code, out = run(DELETE, store)

    assert code == 3 and "Left after the delete: 0 traces, 1 spans" in out


def test_error_text_left_in_the_project_exits_4(run):
    store = _Backend(traces={"t1": OLD}, errors=1)

    code, out = run(DELETE, store)

    assert code == 4 and "With error text" in out


def test_a_backend_that_fails_during_the_delete_exits_3(run):
    store = _Backend(traces={"t1": OLD})

    def refused(**_):
        raise RuntimeError("down")

    store.rest_client.traces.delete_traces = refused

    assert run(DELETE, store)[0] == 3


@pytest.mark.parametrize(
    "argv, record",
    [
        (["--before", BEFORE, "--delete"], CONFIGURED),
        (DELETE[:-1] + ["other-project"], CONFIGURED),
        (["--before", BEFORE], opik_sdk.TraceBackend(reason="OPIK_ENABLED is false")),
        (["--before", BEFORE], opik_sdk.TraceContentCaptureRefused("capture")),
    ],
    ids=["no-confirm", "another-project", "no-backend", "capture-requested"],
)
def test_a_refusal_exits_1_and_deletes_nothing(run, argv, record):
    store = _Backend(traces={"t1": OLD})

    code, out = run(argv, store, record)

    assert code == 1 and "Refusing" in out
    assert store.deleted == [] and store.reads == []


@pytest.mark.parametrize("before", ["2026-11-18T00:00:00", "yesterday"])
def test_a_before_that_names_no_instant_is_a_usage_error(run, before):
    store = _Backend()

    assert run(["--before", before], store)[0] == 2
    assert store.reads == []
