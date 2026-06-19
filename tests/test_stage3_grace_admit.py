"""Stage 3.2 (master plan) — grace-admit cold-start for a paused strategy.

M12's evidence gate needs >= 20 fresh closes, but a paused strategy can't
accumulate any while paused (chicken-and-egg). grace_admit is the operator's
deliberate cold-start: unpause ONE proven-but-paused strategy at grace
(reduced) size to start accumulating honest closes, enforcing one-at-a-time so
only a single strategy ever bootstraps at once. It ACTS (unpauses + opens the
probation window) but bypasses only the evidence/correlation gates — never the
one-at-a-time guard. M12.evaluate_candidate then graduates or re-pauses it.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import reintroduction as ri  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


def _paused_strategy(conn, sid):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    sh.pause_strategy(conn, sid, reason="reset", source="test", pause_days=365)
    conn.commit()


def test_grace_admit_unpauses_and_opens_window(tmp_path):
    conn = db.init_db(tmp_path / "ga.db")
    _paused_strategy(conn, "botnet101-3-bar-low")
    assert sh.is_paused(conn, "botnet101-3-bar-low") is True

    res = ri.grace_admit(conn, "botnet101-3-bar-low", grace_days=30)
    assert res["admitted"] is True
    # actually unpaused...
    assert sh.is_paused(conn, "botnet101-3-bar-low") is False
    # ...and occupies the one-at-a-time probation slot.
    active = [a["strategy_id"] for a in ri.active_admissions(conn)]
    assert "botnet101-3-bar-low" in active
    conn.close()


def test_grace_admit_refused_when_not_paused(tmp_path):
    conn = db.init_db(tmp_path / "ga2.db")
    db.upsert_strategy(conn, {"extra": {"strategy_id": "live-strat"}})
    conn.commit()
    res = ri.grace_admit(conn, "live-strat")
    assert res["admitted"] is False
    assert "not paused" in res["reason"]
    conn.close()


def test_grace_admit_enforces_one_at_a_time(tmp_path):
    conn = db.init_db(tmp_path / "ga3.db")
    _paused_strategy(conn, "strat-a")
    _paused_strategy(conn, "strat-b")

    first = ri.grace_admit(conn, "strat-a", grace_days=30)
    assert first["admitted"] is True

    # A second cold-start while strat-a is in its window must be refused...
    second = ri.grace_admit(conn, "strat-b", grace_days=30)
    assert second["admitted"] is False
    assert "one-at-a-time" in second["reason"]
    # ...and strat-b stays paused (no partial action).
    assert sh.is_paused(conn, "strat-b") is True
    conn.close()


def test_grace_admit_then_window_blocks_other_candidates(tmp_path):
    conn = db.init_db(tmp_path / "ga4.db")
    _paused_strategy(conn, "strat-a")
    ri.grace_admit(conn, "strat-a", grace_days=30)
    # The open window blocks M12 from green-lighting any OTHER strategy.
    one = ri.evaluate_one_at_a_time(conn, "strat-b")
    assert one["passed"] is False
    assert "strat-a" in one["reason"]
    conn.close()
