"""
One-time update: append 2026-04 marketing iteration video as supplementary source
to the existing ross-cameron-five-pillar record. Does NOT create a new record —
the strategy methodology is identical to what was already tested as FAIL.

Run from this directory:
    py -3.13 _update_cameron_2026_video.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
RECORDS_PATH = HERE / "records.jsonl"
TARGET_ID = "ross-cameron-five-pillar"

ADDITIONAL_SECTION = """

---

## Additional source — 2026-04 video (marketing iteration)

**Source doc:** `source_docs/ross_cameron_51_days_million.txt` (108,766 chars,
20,611 words, transcript of "How I Made $1,000,000 in 51 Days of Day Trading
(Full Training)").

**Why logged as supplementary, not a new record:** Methodology is identical to
the 2024 record above. Same five universe pillars, same bull-flag micro-pullback
entry, same 2:1 stop:target, same position-sizing meta-rules (1/4-size warmup,
add-to-winners, walk-away rule, max-loss = daily goal). Creating a separate
record would risk a future session redoing already-completed FAIL backtest work.

### Updated marketing claims (unverified)
- "$1,000,000 in 51 consecutive trading days" (~Jan-Mar 2026)
- "76 consecutive green days" — unverifiable streak claim
- "Average winner $1,800, average loser $761" over 936 trades, 71.4% accuracy
- Lifetime P&L revised to $12.5M (from $12.3M in earlier record)
- "$98,754 in one morning on ATNF" example
- "$475,000 best green day"

### Genuinely new methodology refinement
- **Add-to-winners with stop moved to breakeven** — explicit rule. Once an
  initial position is in profit (~20c), double size and move stop to entry.
  Frames this as "20,000 share position with effectively zero risk after
  cushion is built". This was implicit in earlier sources but is now stated
  as an explicit position-management rule.
- **Walk-away rule** — if no A-quality setup taken in 30 minutes, call the
  day. Cap daily max loss at the daily profit goal.

### Why these refinements don't change the FAIL verdict
The refinements are *psychological / position-management* layers, not edge-
generating mechanics. The underlying entry trigger (first candle to make a new
high after pullback) was already shown to fire systematically late, with stops
hit 2x more often than targets in the 48-trade out-of-sample run. Adding to a
winning position only amplifies a positive expectancy mechanic; if the base
mechanic has negative expectancy at realistic small-cap slippage, scaling
into winners just compounds the same edge problem on the trades where you
got lucky and hides the systematic late-entry issue. The walk-away rule
reduces drawdown variance but does not produce alpha.

### Audited P&L caveat (recurring)
Cameron cites "third-party CPA audited" P&L. The Warrior Trading business
model is well-documented to derive substantial revenue from education product
sales (Warrior Pro mentorship, Day Trade Dash software). Independent
verification would distinguish trading P&L from education P&L. As recorded in
the trading log lessons-learned: cherry-picked equity curves and aggregate
lifetime numbers from course-selling traders are not the same as a held-out
backtest. The honest negative result from the Phase B test stands.

### Logged 2026-04-28
"""


def main() -> None:
    lines = RECORDS_PATH.read_text(encoding="utf-8").splitlines(keepends=False)
    updated = []
    matched = False
    for line in lines:
        if not line.strip():
            updated.append(line)
            continue
        rec = json.loads(line)
        if rec.get("extra", {}).get("strategy_id") == TARGET_ID:
            existing_body = rec["extra"].get("description_full_readable", "")
            # Idempotent: only append once
            if "Additional source — 2026-04 video" not in existing_body:
                rec["extra"]["description_full_readable"] = existing_body + ADDITIONAL_SECTION
                rec["extra"]["last_updated_iso"] = "2026-04-28"
                tags = rec.get("tags", [])
                if "marketing-iteration-2026" not in tags:
                    tags.append("marketing-iteration-2026")
                rec["tags"] = tags
                matched = True
                print(f"Updated record: {TARGET_ID}")
            else:
                print(f"Skipped: {TARGET_ID} already has 2026-04 supplementary section")
        updated.append(json.dumps(rec, ensure_ascii=False))

    if not matched:
        print(f"WARNING: target id {TARGET_ID} not found in records.jsonl")
        return

    RECORDS_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print(f"Wrote {RECORDS_PATH}")


if __name__ == "__main__":
    main()
