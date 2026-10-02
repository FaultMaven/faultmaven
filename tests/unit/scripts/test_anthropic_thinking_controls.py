"""Tests for scripts/anthropic_thinking_controls.py (#1800).

The script spends money, so nothing here may reach the network: every test
runs with ``_send`` and ``httpx.post`` replaced by a function that calls
``pytest.fail``. That raises a ``BaseException``, which the script's
``except Exception`` cannot record as a result row and so cannot swallow. The
send-loop tests replace ``_send`` with a recording fake.
"""

import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "anthropic_thinking_controls",
    REPO_ROOT / "scripts" / "anthropic_thinking_controls.py",
)
atc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(atc)
_REAL_SEND = atc._send

MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1")

KEY_MARKER = "MARKER7f3c9e1d"
FAKE_KEY = f"sk-ant-test-{KEY_MARKER}-not-a-real-key"

#: In the send-loop fake, call 4 (opus-5-5 omitted) times out and call 6
#: (fable-5-1 omitted) returns a 200 that ``summarize_response`` cannot parse.
TIMEOUT_CALL = 4
UNPARSEABLE_CALL = 6
UNPARSEABLE = {"content": [{"text": "a block with no type"}]}


def _body(model="claude-opus-5", **extra):
    return {"model": model, "max_tokens": 8000, "messages": [], **extra}


def _no_network(*a, **k):
    pytest.fail("network reached: a test would have made a live call")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(atc, "_send", _no_network)
    monkeypatch.setattr(httpx, "post", _no_network)


@pytest.mark.unit
class TestNetworkGuard:
    """The guard must escape ``main``'s ``except Exception``.

    An ``AssertionError`` guard would be recorded as a result row and the test
    that reached the network would pass.
    """

    def test_send_guard_is_not_an_exception(self):
        with pytest.raises(pytest.fail.Exception):
            try:
                atc._send(_body(), FAKE_KEY, 1.0)
            except Exception:
                pass

    def test_httpx_post_guard_is_not_an_exception(self, monkeypatch):
        # Loopback, so even an unguarded httpx.post can never reach the API.
        monkeypatch.setattr(atc, "API_URL", "http://127.0.0.1:9/v1/messages")
        with pytest.raises(pytest.fail.Exception):
            try:
                _REAL_SEND(_body(), FAKE_KEY, 1.0)
            except Exception:
                pass


@pytest.mark.unit
class TestBuildVariant:
    def test_omitted_strips_thinking_and_effort_keeps_other_output_config(self):
        body = _body(
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": "x"},
        )
        out = atc.build_variant(body, "claude-opus-5", "omitted")
        assert "thinking" not in out
        assert out["output_config"] == {"format": "x"}

    def test_omitted_drops_empty_output_config(self):
        out = atc.build_variant(
            _body(output_config={"effort": "high"}), "claude-opus-5", "omitted"
        )
        assert "output_config" not in out

    def test_effort_low(self):
        out = atc.build_variant(_body(), "claude-opus-5", "effort_low")
        assert out["output_config"]["effort"] == "low"
        assert "thinking" not in out

    def test_disabled(self):
        out = atc.build_variant(_body(), "claude-opus-5", "disabled")
        assert out["thinking"] == {"type": "disabled"}
        assert "effort" not in out.get("output_config", {})

    @pytest.mark.parametrize("variant", atc.VARIANTS)
    def test_input_not_mutated(self, variant):
        body = _body(thinking={"type": "adaptive"}, output_config={"effort": "high"})
        before = copy.deepcopy(body)
        atc.build_variant(body, "claude-opus-5", variant)
        assert body == before

    def test_wrong_model_raises(self):
        with pytest.raises(ValueError):
            atc.build_variant(_body("claude-opus-5-5"), "claude-opus-5", "omitted")

    def test_unknown_variant_raises(self):
        with pytest.raises(ValueError):
            atc.build_variant(_body(), "claude-opus-5", "high")


@pytest.mark.unit
class TestSummarizeResponse:
    def test_200_with_thinking(self):
        payload = {
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking"},
                {"type": "text"},
                {"type": "tool_use", "name": "InvestigationResponse_Diagnosis"},
            ],
            "usage": {
                "input_tokens": 6100,
                "output_tokens": 5397,
                "output_tokens_details": {"thinking_tokens": 1206},
                "cache_creation_input_tokens": 32000,
                "cache_read_input_tokens": 0,
            },
        }
        row = atc.summarize_response(200, payload, 49.04)
        assert row["visible_tokens"] == 4191
        assert row["thinking_tokens"] == 1206
        assert row["cache_creation_input_tokens"] == 32000
        assert row["cache_read_input_tokens"] == 0
        assert row["blocks"] == [
            "thinking",
            "text",
            "tool_use:InvestigationResponse_Diagnosis",
        ]
        assert row["elapsed_s"] == 49.0

    def test_no_thinking_tokens_gives_none_visible(self):
        row = atc.summarize_response(200, {"usage": {"output_tokens": 10}}, 1.0)
        assert row["visible_tokens"] is None

    def test_400_gives_error_message(self):
        row = atc.summarize_response(400, {"error": {"message": "nope"}}, 0.5)
        assert row["error"] == "nope"
        assert "output_tokens" not in row


@pytest.mark.unit
class TestPlan:
    def test_approved_plan_is_the_2026_09_30_ruling(self):
        assert atc.APPROVED_PLAN == (
            ("claude-opus-5", "omitted"),
            ("claude-opus-5", "effort_low"),
            ("claude-opus-5", "disabled"),
            ("claude-opus-5-5", "omitted"),
            ("claude-opus-5-5", "effort_low"),
            ("claude-fable-5-1", "omitted"),
            ("claude-fable-5-1", "effort_low"),
        )

    def test_plan_requests_returns_the_seven_in_order(self):
        calls = atc.plan_requests({m: _body(m) for m in MODELS})
        assert [(m, v) for m, v, _ in calls] == list(atc.APPROVED_PLAN)
        assert all(body["model"] == m for m, _, body in calls)

    def test_missing_model_raises(self):
        with pytest.raises(ValueError):
            atc.plan_requests({m: _body(m) for m in MODELS[:2]})


def _write(tmp_path, extra=None, models=MODELS):
    """``--request`` args for one captured body per model; ``extra`` adds fields per model."""
    args = []
    for m in models:
        p = tmp_path / f"{m}.json"
        p.write_text(json.dumps({"body": _body(m, **(extra or {}).get(m, {}))}))
        args += ["--request", f"{m}={p}"]
    return args


def _all_output(capsys, *paths):
    captured = capsys.readouterr()
    files = [p.read_text() for p in paths if p.exists()]
    return [captured.out, captured.err, *files]


@pytest.mark.unit
class TestMainRefusals:
    """Everything here runs with ``--send`` and a key: only the refusal stands
    between it and the network guard."""

    @pytest.fixture(autouse=True)
    def key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    def test_without_send_prints_seven_and_sends_nothing(self, tmp_path, capsys):
        out = tmp_path / "o.jsonl"
        rc = atc.main(_write(tmp_path) + ["--out", str(out)])
        assert rc == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 7
        assert not out.exists()

    def test_no_key_exits_2_and_sends_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        out = tmp_path / "o.jsonl"
        rc = atc.main(_write(tmp_path) + ["--out", str(out), "--send"])
        assert rc == 2
        assert not out.exists()

    @pytest.mark.parametrize(
        "key",
        [
            FAKE_KEY + "\r",
            FAKE_KEY + "\n",
            FAKE_KEY + " ",
            "\t" + FAKE_KEY,
            FAKE_KEY[:10] + "\x7f" + FAKE_KEY[10:],
        ],
        ids=["trailing-cr", "trailing-lf", "trailing-space", "leading-tab", "del"],
    )
    def test_key_with_whitespace_or_control_char_is_refused(
        self, key, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        out = tmp_path / "o.jsonl"
        rc = atc.main(_write(tmp_path) + ["--out", str(out), "--send"])
        assert rc == 2
        assert not out.exists()
        assert all(KEY_MARKER not in text for text in _all_output(capsys, out))

    @pytest.mark.parametrize(
        "extra, bound",
        [
            ({"max_tokens": 8001}, "max_tokens 8001"),
            ({"max_tokens": None}, "max_tokens None"),
            ({"stream": False}, "stream"),
            (
                {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
                "'web_search' has no input_schema",
            ),
            ({"system": "x" * 250_000}, "over 250000"),
        ],
        ids=["max-tokens", "max-tokens-missing", "stream", "server-tool", "size"],
    )
    def test_body_out_of_bounds_is_refused(self, extra, bound, tmp_path, capsys):
        out = tmp_path / "o.jsonl"
        args = _write(tmp_path, {"claude-opus-5-5": extra})
        rc = atc.main(args + ["--out", str(out), "--send"])
        assert rc == 2
        assert not out.exists()
        err = capsys.readouterr().err
        assert "claude-opus-5-5" in err and bound in err
        assert "claude-opus-5 " not in err and "claude-fable-5-1" not in err

    def test_body_at_the_bounds_is_accepted(self, tmp_path, capsys):
        tool = {"name": "t", "input_schema": {"type": "object"}}
        extra = {m: {"max_tokens": 8000, "tools": [tool]} for m in MODELS}
        rc = atc.main(_write(tmp_path, extra) + ["--out", str(tmp_path / "o.jsonl")])
        assert rc == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 7

    def test_unplanned_model_is_refused(self, tmp_path, capsys):
        out = tmp_path / "o.jsonl"
        args = _write(tmp_path) + _write(tmp_path, models=("claude-sonnet-5-5",))
        rc = atc.main(args + ["--out", str(out), "--send"])
        assert rc == 2
        assert not out.exists()
        assert "claude-sonnet-5-5" in capsys.readouterr().err

    def test_duplicate_model_is_refused(self, tmp_path, capsys):
        out = tmp_path / "o.jsonl"
        args = _write(tmp_path) + _write(tmp_path, models=("claude-opus-5-5",))
        rc = atc.main(args + ["--out", str(out), "--send"])
        assert rc == 2
        assert not out.exists()
        assert "claude-opus-5-5" in capsys.readouterr().err

    def test_unreadable_results_line_is_refused(self, tmp_path):
        out = tmp_path / "o.jsonl"
        out.write_text("not a row\n")
        rc = atc.main(_write(tmp_path) + ["--out", str(out), "--send"])
        assert rc == 2
        assert out.read_text() == "not a row\n"


@pytest.fixture
def sent(monkeypatch):
    """A recording fake ``_send``; see ``TIMEOUT_CALL`` and ``UNPARSEABLE_CALL``."""
    calls = []

    def fake(body, key, timeout):
        calls.append((body, key))
        if len(calls) == TIMEOUT_CALL:
            # Carries the key, as a transport error that quotes the request does:
            # h11's "Illegal header value b'<key>\r'" is the reproduced case.
            raise httpx.ReadTimeout(f"timed out; x-api-key: {key}")
        if len(calls) == UNPARSEABLE_CALL:
            return 200, copy.deepcopy(UNPARSEABLE), 2.0
        usage = {"output_tokens": 100, "output_tokens_details": {"thinking_tokens": 0}}
        return 200, {"stop_reason": "tool_use", "content": [], "usage": usage}, 1.0

    monkeypatch.setattr(atc, "_send", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    return calls


def _plan_bodies():
    return [body for _, _, body in atc.plan_requests({m: _body(m) for m in MODELS})]


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.unit
class TestSendLoop:
    def test_sends_the_seven_once_in_order_and_records_failures(
        self, sent, tmp_path, capsys
    ):
        out = tmp_path / "o.jsonl"
        raw_out = tmp_path / "o.jsonl.raw.jsonl"
        rc = atc.main(_write(tmp_path) + ["--out", str(out), "--send"])
        assert rc == 0

        assert [body for body, _ in sent] == _plan_bodies()  # 7, in order, no retry
        assert all(key == FAKE_KEY for _, key in sent)

        rows = _rows(out)
        assert [(r["model"], r["variant"]) for r in rows] == list(atc.APPROVED_PLAN)
        timeout_row = rows[TIMEOUT_CALL - 1]
        assert timeout_row["http"] is None
        assert timeout_row["error"].startswith("ReadTimeout: ")
        assert atc.REDACTED in timeout_row["error"]
        summary_row = rows[UNPARSEABLE_CALL - 1]
        assert summary_row["http"] == 200
        assert summary_row["summary_error"].startswith("TypeError: ")

        raw = {(r["model"], r["variant"]): r for r in _rows(raw_out)}
        timed_out = atc.APPROVED_PLAN[TIMEOUT_CALL - 1]
        assert set(raw) == set(atc.APPROVED_PLAN) - {timed_out}
        kept = raw[atc.APPROVED_PLAN[UNPARSEABLE_CALL - 1]]
        assert kept["http"] == 200 and kept["response"] == UNPARSEABLE

        for text in _all_output(capsys, out, raw_out):
            assert KEY_MARKER not in text

    def test_second_run_with_the_same_out_sends_nothing(self, sent, tmp_path, capsys):
        out = tmp_path / "o.jsonl"
        args = _write(tmp_path) + ["--out", str(out), "--send"]
        assert atc.main(args) == 0
        before = out.read_text()
        capsys.readouterr()
        sent.clear()

        assert atc.main(args) == 0
        assert sent == []
        assert out.read_text() == before
        skipped = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [(s["model"], s["variant"]) for s in skipped] == list(atc.APPROVED_PLAN)
        assert all(s["skipped"] == str(out) for s in skipped)

    def test_only_pairs_without_a_row_are_sent(self, sent, tmp_path):
        out = tmp_path / "o.jsonl"
        done = atc.APPROVED_PLAN[:3]
        out.write_text(
            "".join(
                json.dumps({"model": m, "variant": v, "http": None}) + "\n"
                for m, v in done
            )
        )
        rc = atc.main(_write(tmp_path) + ["--out", str(out), "--send"])
        assert rc == 0
        assert [body for body, _ in sent] == _plan_bodies()[3:]
