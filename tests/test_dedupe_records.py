import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import dedupe_records as dr  # noqa: E402


def _record(sid, entry, exit_, url=None, merged_from=None):
    rec = {
        "url": url or f"https://example.com/{sid}",
        "title": sid,
        "extra": {
            "strategy_id": sid,
            "entry_rules": entry,
            "exit_rules": exit_,
        },
    }
    if merged_from is not None:
        rec["extra"]["merged_from"] = merged_from
    return rec


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# embed_text
# ---------------------------------------------------------------------------

def test_embed_text_combines_entry_and_exit():
    rec = _record("a", "Long > SMA50", "Exit < SMA50")
    txt = dr.embed_text(rec)
    assert "Long > SMA50" in txt
    assert "Exit < SMA50" in txt


def test_embed_text_returns_empty_when_no_rules():
    rec = {"url": "x", "extra": {"strategy_id": "x"}}
    assert dr.embed_text(rec) == ""


def test_embed_text_handles_missing_extra():
    assert dr.embed_text({}) == ""


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

def test_cosine_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert dr.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert dr.cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_handles_empty_vectors():
    assert dr.cosine_similarity([], []) == 0.0
    assert dr.cosine_similarity([1.0], []) == 0.0


def test_cosine_handles_mismatched_lengths():
    assert dr.cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_cosine_zero_norm_returns_zero():
    assert dr.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# group_duplicates
# ---------------------------------------------------------------------------

def test_group_duplicates_single_singleton_group():
    groups = dr.group_duplicates([[1.0, 0.0]], threshold=0.92)
    assert groups == [[0]]


def test_group_duplicates_two_above_threshold_merge():
    a = [1.0, 0.0, 0.0]
    b = [0.999, 0.01, 0.0]  # near-identical
    c = [0.0, 0.0, 1.0]  # orthogonal
    groups = dr.group_duplicates([a, b, c], threshold=0.92)
    # find the pair group
    pair = [g for g in groups if len(g) == 2][0]
    single = [g for g in groups if len(g) == 1][0]
    assert set(pair) == {0, 1}
    assert single == [2]


def test_group_duplicates_chain_via_transitive_closure():
    a = [1.0, 0.0]
    b = [0.99, 0.14]
    c = [0.96, 0.28]
    groups = dr.group_duplicates([a, b, c], threshold=0.92)
    # all three should land in one group via transitive union
    assert len(groups) == 1
    assert set(groups[0]) == {0, 1, 2}


def test_group_duplicates_threshold_excludes_borderline():
    a = [1.0, 0.0]
    b = [0.5, 0.5]  # cos ~ 0.707, below 0.92
    groups = dr.group_duplicates([a, b], threshold=0.92)
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# _pick_canonical / _merge_group
# ---------------------------------------------------------------------------

def test_pick_canonical_prefers_longer_merged_from_chain():
    a = _record("a", "x", "y", merged_from=[])
    b = _record("b", "x", "y", merged_from=["old1", "old2"])
    c = _record("c", "x", "y", merged_from=["old3"])
    idx = dr._pick_canonical([a, b, c])
    assert idx == 1  # b has longest chain


def test_pick_canonical_breaks_tie_by_longer_url():
    a = _record("a", "x", "y", url="https://example.com/short")
    b = _record("b", "x", "y", url="https://example.com/much-longer-slug-here")
    idx = dr._pick_canonical([a, b])
    assert idx == 1


def test_merge_group_records_dropped_ids_in_merged_from():
    a = _record("a", "x", "y", url="https://example.com/aaaaaa")
    b = _record("b", "x", "y", url="https://example.com/b")
    c = _record("c", "x", "y", url="https://example.com/c")
    kept, dropped = dr._merge_group([a, b, c])
    assert kept["extra"]["strategy_id"] == "a"
    assert set(kept["extra"]["merged_from"]) == {"b", "c"}
    assert len(dropped) == 2


def test_merge_group_folds_prior_merged_from_chain():
    a = _record("a", "x", "y", merged_from=["old-z"])
    b = _record("b", "x", "y")
    kept, dropped = dr._merge_group([a, b])
    assert kept["extra"]["strategy_id"] == "a"
    assert "old-z" in kept["extra"]["merged_from"]
    assert "b" in kept["extra"]["merged_from"]


def test_merge_group_singleton_unchanged():
    a = _record("a", "x", "y")
    kept, dropped = dr._merge_group([a])
    assert kept == a
    assert dropped == []


def test_merge_group_does_not_self_reference():
    a = _record("a", "x", "y", merged_from=["a"])
    b = _record("b", "x", "y")
    kept, dropped = dr._merge_group([a, b])
    # 'a' (the kept canonical's own id) should not appear in its own chain.
    assert "a" not in kept["extra"]["merged_from"]
    assert "b" in kept["extra"]["merged_from"]


# ---------------------------------------------------------------------------
# I/O round-trip
# ---------------------------------------------------------------------------

def test_load_records_returns_empty_for_missing(tmp_path):
    assert dr.load_records(tmp_path / "missing.jsonl") == []


def test_load_records_skips_malformed_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(
        json.dumps({"url": "u", "extra": {}}) + "\n"
        "not json at all\n"
        + json.dumps({"url": "v", "extra": {}}) + "\n",
        encoding="utf-8",
    )
    recs = dr.load_records(p)
    assert len(recs) == 2


def test_write_records_round_trip(tmp_path):
    p = tmp_path / "out.jsonl"
    recs = [{"url": "a", "extra": {"strategy_id": "a"}},
            {"url": "b", "extra": {"strategy_id": "b"}}]
    dr.write_records(p, recs)
    read = dr.load_records(p)
    assert read == recs


# ---------------------------------------------------------------------------
# dedupe() end-to-end with injected embedder
# ---------------------------------------------------------------------------

def test_dedupe_merges_near_duplicates(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        _record("alpha", "Long when RSI < 30", "Exit RSI > 70"),
        _record("alpha-dup", "Long when RSI < 30", "Exit RSI > 70"),
        _record("beta", "Buy on breakout above 20-day high",
                "Sell on close < 10-day low"),
    ])

    def fake_embedder(text):
        # Identical text → identical vector.
        if "RSI" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]

    summary = dr.dedupe(records_path=p, embedder=fake_embedder)
    assert summary["merged"] == 1
    assert summary["groups"] == 1
    remaining = _read_jsonl(p)
    assert len(remaining) == 2
    # The kept record now has a merged_from chain.
    rsi_recs = [r for r in remaining if "RSI" in r["extra"]["entry_rules"]]
    assert len(rsi_recs) == 1
    assert "alpha-dup" in rsi_recs[0]["extra"]["merged_from"] or \
           "alpha" in rsi_recs[0]["extra"]["merged_from"]


def test_dedupe_idempotent_on_already_deduped(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        _record("a", "Long when RSI < 30", "Exit RSI > 70"),
        _record("b", "Buy breakout", "Sell breakdown"),
    ])

    def fake_embedder(text):
        if "RSI" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    s1 = dr.dedupe(records_path=p, embedder=fake_embedder)
    s2 = dr.dedupe(records_path=p, embedder=fake_embedder)
    assert s1["merged"] == 0
    assert s2["merged"] == 0
    # File contents unchanged.
    assert _read_jsonl(p) == [
        _record("a", "Long when RSI < 30", "Exit RSI > 70"),
        _record("b", "Buy breakout", "Sell breakdown"),
    ]


def test_dedupe_dry_run_does_not_write(tmp_path):
    p = tmp_path / "records.jsonl"
    original = [
        _record("a", "Long RSI < 30", "Exit"),
        _record("b", "Long RSI < 30", "Exit"),
    ]
    _write_jsonl(p, original)

    def fake_embedder(text):
        return [1.0, 0.0]

    summary = dr.dedupe(records_path=p, embedder=fake_embedder, dry_run=True)
    assert summary["merged"] == 1
    # File should still have both records.
    assert _read_jsonl(p) == original


def test_dedupe_skips_records_without_rules(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        {"url": "x", "extra": {"strategy_id": "x"}},  # no rules
        _record("a", "Long RSI < 30", "Exit"),
    ])

    def fake_embedder(text):
        return [1.0, 0.0]

    summary = dr.dedupe(records_path=p, embedder=fake_embedder)
    assert summary["skipped"] == 1
    assert summary["embedded"] == 1
    assert summary["merged"] == 0
    # Both should still be in the file.
    assert len(_read_jsonl(p)) == 2


def test_dedupe_handles_embedder_failure_per_record(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        _record("a", "Long RSI", "Exit"),
        _record("b", "Buy breakout", "Sell"),
    ])

    def flaky(text):
        if "RSI" in text:
            raise RuntimeError("ollama down for this one")
        return [0.0, 1.0]

    summary = dr.dedupe(records_path=p, embedder=flaky)
    assert summary["skipped"] == 1
    assert summary["embedded"] == 1
    assert summary["merged"] == 0


def test_dedupe_threshold_excludes_borderline(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        _record("a", "alpha entry rule", "alpha exit rule"),
        _record("b", "beta entry rule", "beta exit rule"),
    ])

    def emb(text):
        if "alpha" in text:
            return [1.0, 0.0]
        return [0.5, 0.5]  # cos ~ 0.707, below 0.92

    summary = dr.dedupe(records_path=p, embedder=emb, threshold=0.92)
    assert summary["merged"] == 0


def test_dedupe_empty_file(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text("", encoding="utf-8")
    summary = dr.dedupe(records_path=p, embedder=lambda t: [1.0])
    assert summary["total"] == 0
    assert summary["merged"] == 0


def test_dedupe_missing_file(tmp_path):
    p = tmp_path / "nope.jsonl"
    summary = dr.dedupe(records_path=p, embedder=lambda t: [1.0])
    assert summary["total"] == 0


def test_dedupe_preserves_non_grouped_records(tmp_path):
    p = tmp_path / "records.jsonl"
    _write_jsonl(p, [
        _record("a", "Long RSI", "Exit"),
        _record("a-dup", "Long RSI", "Exit"),
        _record("b", "Buy breakout", "Sell"),
        _record("c", "Sell on news", "Cover next day"),
    ])
    counter = {"n": 0}

    def emb(text):
        if "RSI" in text:
            return [1.0, 0.0, 0.0]
        if "breakout" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    summary = dr.dedupe(records_path=p, embedder=emb)
    assert summary["merged"] == 1
    remaining = _read_jsonl(p)
    assert len(remaining) == 3
    sids = {r["extra"]["strategy_id"] for r in remaining}
    # 'b' and 'c' must survive unchanged
    assert "b" in sids
    assert "c" in sids


# ---------------------------------------------------------------------------
# Ollama plumbing (mocked)
# ---------------------------------------------------------------------------

def _mock_ollama_response(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def test_call_ollama_embed_round_trip(monkeypatch):
    monkeypatch.setattr(
        dr, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(
            {"embedding": [0.1, 0.2, 0.3]}
        ),
    )
    vec = dr.call_ollama_embed("some text")
    assert vec == [0.1, 0.2, 0.3]


def test_call_ollama_embed_accepts_nested_embeddings_field(monkeypatch):
    monkeypatch.setattr(
        dr, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(
            {"embeddings": [[0.5, 0.6]]}
        ),
    )
    vec = dr.call_ollama_embed("x")
    assert vec == [0.5, 0.6]


def test_call_ollama_embed_raises_on_non_200(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    monkeypatch.setattr(dr, "_ollama_post",
                        lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama embed 500"):
        dr.call_ollama_embed("x")


def test_call_ollama_embed_raises_on_missing_vector(monkeypatch):
    monkeypatch.setattr(
        dr, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response({}),
    )
    with pytest.raises(RuntimeError, match="no vector"):
        dr.call_ollama_embed("x")
