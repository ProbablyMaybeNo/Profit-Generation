"""
dedupe_records.py — Source-attribution & near-duplicate merger for records.jsonl.

Embeds each record's `entry_rules + exit_rules` text via Ollama's
`nomic-embed-text` model, then groups records whose pairwise cosine
similarity exceeds a threshold (default 0.92). For each duplicate
group, keeps the "canonical" record — the one with the most source
URLs already accumulated in `extra.merged_from`, breaking ties by
longest `url` string — and records the discarded records'
`extra.strategy_id` (and original URL) into the kept record's
`extra.merged_from` list. Duplicates are removed from records.jsonl.

Idempotent: re-running on an already-deduped file is a no-op. Records
that lack entry/exit rules are left untouched.

CLI:
  py -3.13 scripts/dedupe_records.py
  py -3.13 scripts/dedupe_records.py --threshold 0.95 --dry-run
  py -3.13 scripts/dedupe_records.py --records-path data/scrapes/.../records.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

RECORDS_PATH = (
    ROOT / "data" / "scrapes"
    / "tradingview-in-daytrading-strategies-2026-04-26"
    / "records.jsonl"
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL_DEFAULT = os.environ.get(
    "OLLAMA_EMBED_MODEL", "nomic-embed-text",
)
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "120"))

DEFAULT_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_records(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out: List[Dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_records(path: Path, records: Sequence[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Embedding text construction
# ---------------------------------------------------------------------------

def embed_text(record: Dict) -> str:
    """Return the `entry_rules + exit_rules` text used for embedding.

    Empty string means the record can't be embedded (and is skipped)."""
    extra = record.get("extra") or {}
    entry = (extra.get("entry_rules") or "").strip()
    exit_ = (extra.get("exit_rules") or "").strip()
    if not entry and not exit_:
        return ""
    return f"Entry: {entry}\nExit: {exit_}".strip()


# ---------------------------------------------------------------------------
# Ollama embedding plumbing — indirection seam
# ---------------------------------------------------------------------------

def _ollama_post(url: str, payload: Dict, timeout: float):
    return requests.post(url, json=payload, timeout=timeout)


def call_ollama_embed(text: str, *, model: Optional[str] = None) -> List[float]:
    """Call Ollama's /api/embeddings endpoint, return the vector."""
    url = f"{OLLAMA_URL}/api/embeddings"
    payload = {
        "model": model or OLLAMA_EMBED_MODEL_DEFAULT,
        "prompt": text,
    }
    resp = _ollama_post(url, payload, OLLAMA_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise RuntimeError(f"ollama embed {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    vec = body.get("embedding") or body.get("embeddings")
    if isinstance(vec, list) and vec and isinstance(vec[0], list):
        vec = vec[0]
    if not isinstance(vec, list) or not vec:
        raise RuntimeError("ollama embed returned no vector")
    return [float(x) for x in vec]


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


# ---------------------------------------------------------------------------
# Duplicate grouping (union-find over edges with cos >= threshold)
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def group_duplicates(
    vectors: Sequence[Sequence[float]],
    threshold: float = DEFAULT_THRESHOLD,
) -> List[List[int]]:
    """Return groups of indices whose pairwise cosine >= threshold.

    Singletons are included as length-1 groups so the caller can iterate
    uniformly.
    """
    n = len(vectors)
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if cosine_similarity(vectors[i], vectors[j]) >= threshold:
                uf.union(i, j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Merge strategy — pick the canonical record, fold the rest into merged_from
# ---------------------------------------------------------------------------

def _merged_from_chain(record: Dict) -> List[str]:
    extra = record.get("extra") or {}
    chain = extra.get("merged_from")
    if isinstance(chain, list):
        return [str(x) for x in chain]
    return []


def _strategy_id(record: Dict) -> str:
    extra = record.get("extra") or {}
    return str(extra.get("strategy_id") or record.get("url") or "")


def _pick_canonical(group: Sequence[Dict]) -> int:
    """Index (within the group) of the record to keep.

    Picks the record with the longest existing `merged_from` chain — that
    record has accumulated the most source URLs across prior dedupe runs.
    Tie-break: longest `url` string. Final tie-break: first by stable
    `strategy_id` ascending.
    """
    def key(rec_idx: Tuple[int, Dict]):
        idx, rec = rec_idx
        chain_len = len(_merged_from_chain(rec))
        url_len = len(str(rec.get("url") or ""))
        sid = _strategy_id(rec)
        # negate the things we want maximized so that min() picks them.
        return (-chain_len, -url_len, sid, idx)

    return min(enumerate(group), key=key)[0]


def _merge_group(group: List[Dict]) -> Tuple[Dict, List[Dict]]:
    """Return (kept, dropped) with kept's extra.merged_from updated."""
    if len(group) == 1:
        return group[0], []
    canon_idx = _pick_canonical(group)
    kept = json.loads(json.dumps(group[canon_idx]))  # deep copy
    dropped = [r for i, r in enumerate(group) if i != canon_idx]

    extra = kept.setdefault("extra", {})
    canon_id = _strategy_id(kept)
    existing_chain = [c for c in _merged_from_chain(kept) if c and c != canon_id]
    chain = list(existing_chain)
    for r in dropped:
        sid = _strategy_id(r)
        if sid and sid not in chain and sid != canon_id:
            chain.append(sid)
        # Also fold any prior chain that the dropped record had carried.
        for prior in _merged_from_chain(r):
            if prior and prior not in chain and prior != canon_id:
                chain.append(prior)
    extra["merged_from"] = chain
    return kept, dropped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def dedupe(
    *,
    records_path: Path = RECORDS_PATH,
    threshold: float = DEFAULT_THRESHOLD,
    model: Optional[str] = None,
    dry_run: bool = False,
    embedder: Optional[Callable[[str], List[float]]] = None,
) -> Dict:
    """Read records.jsonl, embed, group, merge, rewrite. Returns a summary.

    `embedder` is injectable for tests; defaults to `call_ollama_embed`.
    """
    used_model = model or OLLAMA_EMBED_MODEL_DEFAULT
    embed_fn = embedder or (lambda txt: call_ollama_embed(txt, model=used_model))

    records = load_records(records_path)
    if not records:
        return {"total": 0, "embedded": 0, "groups": 0, "merged": 0,
                "kept": 0, "skipped": 0, "dropped_ids": []}

    embeddable_idx: List[int] = []
    vectors: List[List[float]] = []
    skipped = 0
    for i, rec in enumerate(records):
        text = embed_text(rec)
        if not text:
            skipped += 1
            continue
        try:
            vec = embed_fn(text)
        except Exception as e:
            log(f"embed failed for record {i}: {e}", "WARNING")
            skipped += 1
            continue
        embeddable_idx.append(i)
        vectors.append(vec)

    groups_local = group_duplicates(vectors, threshold=threshold)
    # Map back to record indices.
    groups_records: List[List[int]] = [
        [embeddable_idx[k] for k in g] for g in groups_local
    ]

    kept_count = 0
    merged_count = 0
    dropped_ids: List[str] = []
    keep_mask = [True] * len(records)
    rewritten: Dict[int, Dict] = {}

    for grp in groups_records:
        if len(grp) == 1:
            continue
        group_recs = [records[i] for i in grp]
        canonical_local = _pick_canonical(group_recs)
        kept, dropped = _merge_group(group_recs)
        canonical_record_idx = grp[canonical_local]
        rewritten[canonical_record_idx] = kept
        kept_count += 1
        merged_count += len(dropped)
        for d in dropped:
            dropped_ids.append(_strategy_id(d))
        for offset, record_idx in enumerate(grp):
            if offset != canonical_local:
                keep_mask[record_idx] = False

    # Skip-only records & non-grouped records pass through unchanged.
    new_records: List[Dict] = []
    for i, rec in enumerate(records):
        if not keep_mask[i]:
            continue
        new_records.append(rewritten.get(i, rec))

    if not dry_run and merged_count > 0:
        write_records(records_path, new_records)

    return {
        "total": len(records),
        "embedded": len(vectors),
        "groups": sum(1 for g in groups_records if len(g) > 1),
        "merged": merged_count,
        "kept": len(new_records),
        "skipped": skipped,
        "dropped_ids": dropped_ids,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-path", default=str(RECORDS_PATH))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log(
        f"dedupe_records start: path={args.records_path} "
        f"threshold={args.threshold} model={args.model or OLLAMA_EMBED_MODEL_DEFAULT}",
        "INFO",
    )
    summary = dedupe(
        records_path=Path(args.records_path),
        threshold=args.threshold,
        model=args.model,
        dry_run=args.dry_run,
    )
    log(
        f"done: total={summary['total']} embedded={summary['embedded']} "
        f"groups={summary['groups']} merged={summary['merged']} "
        f"kept={summary['kept']} skipped={summary['skipped']}",
        "SUCCESS" if summary["merged"] >= 0 else "WARNING",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
