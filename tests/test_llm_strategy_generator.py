import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import llm_strategy_generator as lsg  # noqa: E402


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def test_prompt_includes_category_and_count():
    p = lsg.build_prompt(category="mean-reversion", count=8)
    assert "mean-reversion" in p
    assert "8" in p
    assert "JSON" in p
    assert "look-ahead" in p
    assert "daily" in p.lower()


def test_prompt_requires_required_fields():
    p = lsg.build_prompt(category="breakout", count=3)
    for f in lsg.REQUIRED_FIELDS:
        assert f in p


def test_prompt_honors_avoid_list():
    p = lsg.build_prompt(category="x", count=1, avoid=["Bollinger", "RSI"])
    assert "Bollinger" in p
    assert "RSI" in p
    assert "Do NOT" in p


def test_prompt_omits_avoid_clause_when_empty():
    p = lsg.build_prompt(category="x", count=1, avoid=[])
    assert "Do NOT" not in p


def test_prompt_avoid_ignores_blank_entries():
    p = lsg.build_prompt(category="x", count=1, avoid=["", "  ", "MACD"])
    assert "MACD" in p


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _good_item(sid="alpha-strat"):
    return {
        "strategy_id": sid,
        "title": f"{sid} title",
        "entry_rules": "Long when close > 20-bar high.",
        "exit_rules": "Exit when close < 10-bar low.",
        "risk_management": "Stop at 2x ATR(20).",
    }


def test_parse_response_json_array():
    items = [_good_item("a"), _good_item("b")]
    raw = json.dumps(items)
    parsed = lsg.parse_response(raw)
    assert len(parsed) == 2
    assert parsed[0]["strategy_id"] == "a"


def test_parse_response_strips_markdown_fences():
    items = [_good_item("a")]
    raw = "```json\n" + json.dumps(items) + "\n```"
    parsed = lsg.parse_response(raw)
    assert len(parsed) == 1


def test_parse_response_drops_malformed_items():
    items = [
        _good_item("a"),
        {"strategy_id": "missing-fields"},  # invalid
        {"strategy_id": "b", "title": "t", "entry_rules": "e",
         "exit_rules": "x", "risk_management": ""},  # blank risk
        _good_item("c"),
    ]
    raw = json.dumps(items)
    parsed = lsg.parse_response(raw)
    assert {p["strategy_id"] for p in parsed} == {"a", "c"}


def test_parse_response_rejects_bad_strategy_id():
    items = [
        {**_good_item(), "strategy_id": "Has Spaces"},
        {**_good_item(), "strategy_id": "UPPER-CASE"},
        {**_good_item("ok-one")},
    ]
    raw = json.dumps(items)
    parsed = lsg.parse_response(raw)
    assert [p["strategy_id"] for p in parsed] == ["ok-one"]


def test_parse_response_handles_chatter_before_array():
    items = [_good_item("a")]
    raw = "Sure! Here's the JSON you requested:\n\n" + json.dumps(items)
    parsed = lsg.parse_response(raw)
    assert len(parsed) == 1


def test_parse_response_returns_empty_on_garbage():
    assert lsg.parse_response("not json at all") == []


def test_parse_response_falls_back_to_per_line_objects():
    items = [_good_item("a"), _good_item("b")]
    raw = "\n".join(json.dumps(it) + "," for it in items)
    parsed = lsg.parse_response(raw)
    assert {p["strategy_id"] for p in parsed} == {"a", "b"}


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------

def test_build_record_matches_untested_schema():
    item = _good_item("trend-pullback")
    rec = lsg.build_record(item, category="mean-reversion", model="qwen2.5-coder:14b")
    assert rec["source"] == "llm_strategy_generator"
    assert rec["author"].startswith("llm:")
    assert "UNTESTED" in rec["tags"]
    assert "mean-reversion" in rec["tags"]
    extra = rec["extra"]
    assert extra["strategy_id"] == "llm-trend-pullback"
    assert extra["current_verdict"] == "UNTESTED"
    assert extra["tested"] is False
    assert extra["entry_rules"] == item["entry_rules"]
    assert extra["exit_rules"] == item["exit_rules"]
    assert extra["risk_management"] == item["risk_management"]
    assert extra["methodology_family"] == "llm-mean-reversion"
    assert extra["scraper"] == "llm_strategy_generator"
    assert extra["llm_model"] == "qwen2.5-coder:14b"
    assert extra["llm_category"] == "mean-reversion"


def test_build_record_url_is_unique_per_strategy():
    rec1 = lsg.build_record(_good_item("a"), category="x", model="m")
    rec2 = lsg.build_record(_good_item("b"), category="x", model="m")
    assert rec1["url"] != rec2["url"]


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_load_existing_strategy_ids_handles_missing_file(tmp_path):
    assert lsg.load_existing_strategy_ids(tmp_path / "nope.jsonl") == set()


def test_load_existing_strategy_ids_reads_extra_strategy_id(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        json.dumps({"url": "u1", "extra": {"strategy_id": "alpha"}}) + "\n" +
        json.dumps({"url": "u2", "extra": {"strategy_id": "beta"}}) + "\n" +
        "not json\n" +
        json.dumps({"url": "u3", "extra": {}}) + "\n",
        encoding="utf-8",
    )
    ids = lsg.load_existing_strategy_ids(p)
    assert ids == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# generate() end-to-end with injected ollama_caller
# ---------------------------------------------------------------------------

def test_generate_writes_records_to_jsonl(tmp_path):
    items = [_good_item(f"s{i}") for i in range(5)]
    raw = json.dumps(items)
    records = tmp_path / "records.jsonl"

    summary = lsg.generate(
        category="mean-reversion",
        count=5,
        records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 5
    assert summary["deduped"] == 0
    assert summary["malformed"] == 0
    lines = [json.loads(l) for l in records.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 5
    assert all(rec["extra"]["current_verdict"] == "UNTESTED" for rec in lines)
    assert all(rec["extra"]["strategy_id"].startswith("llm-s") for rec in lines)


def test_generate_dedupes_against_existing_records(tmp_path):
    records = tmp_path / "records.jsonl"
    records.write_text(
        json.dumps({"url": "u",
                    "extra": {"strategy_id": "llm-already-here"}}) + "\n",
        encoding="utf-8",
    )
    items = [_good_item("already-here"), _good_item("fresh-one")]
    raw = json.dumps(items)
    summary = lsg.generate(
        category="x", count=2, records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 1
    assert summary["deduped"] == 1


def test_generate_dedupes_within_same_run(tmp_path):
    records = tmp_path / "records.jsonl"
    items = [_good_item("dup"), _good_item("dup"), _good_item("unique")]
    raw = json.dumps(items)
    summary = lsg.generate(
        category="x", count=3, records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 2
    assert summary["deduped"] == 1


def test_generate_skips_malformed_items_gracefully(tmp_path):
    records = tmp_path / "records.jsonl"
    items = [
        _good_item("a"),
        {"strategy_id": "no-fields"},
        _good_item("b"),
    ]
    raw = json.dumps(items)
    summary = lsg.generate(
        category="x", count=3, records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 2
    # one malformed = requested(3) - accepted(2) - deduped(0) ... in this case
    # the parser drops the malformed, so received reflects parsed count + diff
    assert summary["malformed"] >= 1


def test_generate_honors_dry_run(tmp_path):
    records = tmp_path / "records.jsonl"
    raw = json.dumps([_good_item("a")])
    summary = lsg.generate(
        category="x", count=1, records_path=records,
        dry_run=True, ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 1
    assert not records.exists()


def test_generate_caps_at_hard_limit(tmp_path):
    records = tmp_path / "records.jsonl"
    raw = json.dumps([_good_item(f"s{i}") for i in range(lsg.HARD_COUNT_CAP + 10)])
    summary = lsg.generate(
        category="x", count=lsg.HARD_COUNT_CAP + 25,
        records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    # After clamp, only HARD_COUNT_CAP get accepted.
    assert summary["accepted"] == lsg.HARD_COUNT_CAP


def test_generate_caps_at_requested_count_even_if_llm_overshoots(tmp_path):
    records = tmp_path / "records.jsonl"
    raw = json.dumps([_good_item(f"s{i}") for i in range(20)])
    summary = lsg.generate(
        category="x", count=5, records_path=records,
        ollama_caller=lambda prompt: raw,
    )
    assert summary["accepted"] == 5


def test_generate_passes_avoid_into_prompt(tmp_path):
    records = tmp_path / "records.jsonl"
    captured = {}

    def caller(prompt):
        captured["prompt"] = prompt
        return json.dumps([_good_item("a")])

    lsg.generate(
        category="x", count=1, avoid=["Bollinger", "RSI"],
        records_path=records, ollama_caller=caller,
    )
    assert "Bollinger" in captured["prompt"]
    assert "RSI" in captured["prompt"]


def test_generate_zero_count_raises(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        lsg.generate(
            category="x", count=0, records_path=tmp_path / "r.jsonl",
            ollama_caller=lambda p: "[]",
        )


# ---------------------------------------------------------------------------
# Ollama plumbing (mocked, like test_llm_codegen)
# ---------------------------------------------------------------------------

def _mock_ollama_response(text: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"response": text}
    return r


def test_call_ollama_round_trip(monkeypatch):
    raw = json.dumps([_good_item("a")])
    monkeypatch.setattr(
        lsg, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(raw),
    )
    out = lsg.call_ollama("dummy", model="mymodel", temperature=0.5)
    assert out == raw


def test_call_ollama_raises_on_non_200(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    monkeypatch.setattr(lsg, "_ollama_post",
                        lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama 500"):
        lsg.call_ollama("p")


def test_call_ollama_raises_on_empty(monkeypatch):
    monkeypatch.setattr(
        lsg, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(""),
    )
    with pytest.raises(RuntimeError, match="empty"):
        lsg.call_ollama("p")


def test_generate_end_to_end_with_mocked_ollama(monkeypatch, tmp_path):
    items = [_good_item("alpha"), _good_item("beta")]
    raw = json.dumps(items)
    monkeypatch.setattr(
        lsg, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(raw),
    )
    records = tmp_path / "records.jsonl"
    summary = lsg.generate(
        category="momentum", count=2, records_path=records,
    )
    assert summary["accepted"] == 2
    lines = [json.loads(l) for l in records.read_text(encoding="utf-8").splitlines() if l]
    assert {ln["extra"]["strategy_id"] for ln in lines} == {
        "llm-alpha", "llm-beta",
    }
