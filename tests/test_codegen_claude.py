"""Tests for monitoring.codegen_claude (milestone 4.3.1).

Drop-in replacement for the Ollama codegen path: same input contract,
same output schema. Prompt caching markers, cache_key stability,
response parsing, and the integration seam with codegen_strategy.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import codegen_claude as cc  # noqa: E402
from monitoring import llm_codegen  # noqa: E402


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def test_system_blocks_carries_cache_control():
    blocks = cc.build_system_blocks()
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "text"
    assert "cache_control" in block
    assert block["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_includes_pattern_and_examples():
    blocks = cc.build_system_blocks()
    text = blocks[0]["text"]
    assert "PATTERN" in text
    assert "EXAMPLES" in text
    # Each few-shot example name must appear in the cached preamble.
    for _desc, name, _code in cc.FEW_SHOT_EXAMPLES:
        assert name in text


def test_at_least_five_few_shot_examples():
    """Per global CLAUDE.md: >= 5 few-shot examples for cache hit-rate."""
    assert len(cc.FEW_SHOT_EXAMPLES) >= 5


def test_build_user_message_substitutes_fn_name():
    msg = cc.build_user_message(
        "compute_widget", entry_rules="long when X", exit_rules="exit when Y",
    )
    assert "compute_widget" in msg
    assert "long when X" in msg
    assert "exit when Y" in msg


def test_build_user_message_handles_blank_risk():
    msg = cc.build_user_message(
        "compute_widget", entry_rules="e", exit_rules="x",
    )
    assert "(none)" in msg


# ---------------------------------------------------------------------------
# cache_key stability
# ---------------------------------------------------------------------------

def test_cache_key_stable_across_calls():
    a = cc.cache_key("compute_widget",
                     entry_rules="x", exit_rules="y", risk_management="z")
    b = cc.cache_key("compute_widget",
                     entry_rules="x", exit_rules="y", risk_management="z")
    assert a == b


def test_cache_key_changes_on_input_change():
    a = cc.cache_key("compute_widget",
                     entry_rules="x", exit_rules="y")
    b = cc.cache_key("compute_widget",
                     entry_rules="x_DIFFERENT", exit_rules="y")
    assert a != b


def test_cache_key_normalizes_whitespace():
    a = cc.cache_key("compute_widget",
                     entry_rules="x", exit_rules="y")
    b = cc.cache_key("compute_widget",
                     entry_rules=" x ", exit_rules=" y ")
    assert a == b


# ---------------------------------------------------------------------------
# call_claude — response parsing
# ---------------------------------------------------------------------------

def _stub_response(text: str, *, status: int = 200,
                    cache_read: int = 0, cache_creation: int = 0,
                    input_tokens: int = 1, output_tokens: int = 1):
    resp = MagicMock()
    resp.status_code = status
    if status != 200:
        resp.text = "error body"
    resp.json.return_value = {
        "content": [{"type": "text", "text": text}],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    return resp


def test_call_claude_parses_text_and_usage(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda url, headers, payload, timeout:
                        _stub_response("def compute_x():\n    pass\n",
                                       cache_read=1234, cache_creation=42,
                                       input_tokens=50, output_tokens=20))
    out = cc.call_claude(
        "compute_x", entry_rules="e", exit_rules="x", api_key="test",
    )
    assert out["text"].startswith("def compute_x(")
    assert out["cache_read_tokens"] == 1234
    assert out["cache_creation_tokens"] == 42
    assert out["input_tokens"] == 50
    assert out["output_tokens"] == 20


def test_call_claude_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k:
                        _stub_response("ignored", status=500))
    with pytest.raises(RuntimeError) as exc:
        cc.call_claude("compute_x", entry_rules="e", exit_rules="x",
                        api_key="test")
    assert "500" in str(exc.value)


def test_call_claude_raises_on_empty_content(monkeypatch):
    def empty(*a, **k):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"content": []}
        return r
    monkeypatch.setattr(cc, "_anthropic_post", empty)
    with pytest.raises(RuntimeError) as exc:
        cc.call_claude("compute_x", entry_rules="e", exit_rules="x",
                        api_key="test")
    assert "no content" in str(exc.value).lower()


def test_call_claude_raises_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cc, "_load_api_key", lambda: "")
    with pytest.raises(RuntimeError) as exc:
        cc.call_claude("compute_x", entry_rules="e", exit_rules="x")
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_call_claude_payload_carries_cache_control(monkeypatch):
    captured = {}

    def capture(url, headers, payload, timeout):
        captured["payload"] = payload
        captured["headers"] = headers
        return _stub_response("def compute_x():\n    pass\n")

    monkeypatch.setattr(cc, "_anthropic_post", capture)
    cc.call_claude("compute_x", entry_rules="e", exit_rules="x",
                    api_key="test")
    sys_block = captured["payload"]["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}
    assert captured["headers"]["x-api-key"] == "test"
    assert captured["headers"]["anthropic-version"] == cc.ANTHROPIC_VERSION


# ---------------------------------------------------------------------------
# generate_compute_fn — full pipeline with mocked API
# ---------------------------------------------------------------------------

_GOOD_FN_CODE = """\
def compute_widget(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["long_entry"] = (df["close"] < df["close"].shift(1)).fillna(False)
    out["long_exit"] = (df["close"] > df["close"].shift(1)).fillna(False)
    return out
"""


def test_generate_compute_fn_happy_path(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k: _stub_response(_GOOD_FN_CODE))
    code = cc.generate_compute_fn(
        "compute_widget", entry_rules="e", exit_rules="x",
        api_key="test",
    )
    assert "def compute_widget" in code


def test_generate_compute_fn_invokes_on_usage(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k:
                        _stub_response(_GOOD_FN_CODE,
                                       cache_read=100, cache_creation=10,
                                       input_tokens=50, output_tokens=20))
    seen = []
    cc.generate_compute_fn(
        "compute_widget", entry_rules="e", exit_rules="x",
        api_key="test", on_usage=lambda u: seen.append(u),
    )
    assert len(seen) == 1
    assert seen[0]["cache_read_tokens"] == 100
    assert seen[0]["output_tokens"] == 20


def test_generate_compute_fn_rejects_forbidden_imports(monkeypatch):
    bad = "import os\n" + _GOOD_FN_CODE
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k: _stub_response(bad))
    with pytest.raises(ValueError) as exc:
        cc.generate_compute_fn(
            "compute_widget", entry_rules="e", exit_rules="x",
            api_key="test",
        )
    # Reuses llm_codegen.validate_ast — same error pathway.
    assert "forbidden" in str(exc.value).lower()


def test_generate_compute_fn_rejects_when_function_missing(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k:
                        _stub_response("def compute_NOTTHESAMENAME():\n  pass\n"))
    with pytest.raises(ValueError) as exc:
        cc.generate_compute_fn(
            "compute_widget", entry_rules="e", exit_rules="x",
            api_key="test",
        )
    assert "compute_widget" in str(exc.value)


def test_generate_compute_fn_on_usage_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(cc, "_anthropic_post",
                        lambda *a, **k: _stub_response(_GOOD_FN_CODE))

    def boom(u):
        raise RuntimeError("usage callback explodes")

    # The pipeline must still produce code even though on_usage failed.
    code = cc.generate_compute_fn(
        "compute_widget", entry_rules="e", exit_rules="x",
        api_key="test", on_usage=boom,
    )
    assert "def compute_widget" in code


# ---------------------------------------------------------------------------
# fn_name_from_strategy_id parity with the Ollama adapter
# ---------------------------------------------------------------------------

def test_fn_name_parity_with_ollama_path():
    sid = "botnet101-buy-5day-low"
    assert (cc.fn_name_from_strategy_id(sid)
            == llm_codegen.fn_name_from_strategy_id(sid))


# ---------------------------------------------------------------------------
# Integration seam — codegen_record(provider="claude") routes correctly.
# ---------------------------------------------------------------------------

def test_codegen_record_routes_to_claude(monkeypatch):
    from scripts import codegen_strategy as csmod

    called = {}

    def fake_claude_gen(fn_name, *, entry_rules, exit_rules,
                        risk_management="", model=None, temperature=0.1,
                        api_key=None, on_usage=None):
        called["fn_name"] = fn_name
        called["entry"] = entry_rules
        called["model"] = model
        return _GOOD_FN_CODE

    monkeypatch.setattr(cc, "generate_compute_fn", fake_claude_gen)

    record = {
        "extra": {
            "strategy_id": "widget-x",
            "entry_rules": "long on X",
            "exit_rules": "exit on Y",
        },
    }
    out = csmod.codegen_record(record, provider="claude", dry_run=True)
    assert out["ok"] is True
    assert called["fn_name"] == "compute_widget_x"
    assert called["entry"] == "long on X"


def test_codegen_record_rejects_unknown_provider():
    from scripts import codegen_strategy as csmod
    record = {"extra": {"strategy_id": "s",
                        "entry_rules": "e", "exit_rules": "x"}}
    out = csmod.codegen_record(record, provider="gemini")
    assert out["ok"] is False
    assert "gemini" in out["error"]
