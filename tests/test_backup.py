"""Tests for scripts.backup — milestone 3.5.2."""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import backup as bk  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def fake_project(tmp_path):
    """Build a minimal Profit-Generation-shaped project layout in tmp_path."""
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    # Real sqlite db so the .backup API works.
    db_path = tmp_path / "data" / "trading.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t(v) VALUES('hello'), ('world')")
        conn.commit()
    (tmp_path / "data" / "records.jsonl").write_text(
        '{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps({"auto_trade": {"enabled": True}}), encoding="utf-8")
    return tmp_path


def _now(year, month, day):
    return datetime(year, month, day, 22, 30, 0)


# ---------- backup_once ----------

def test_backup_creates_dated_directory(fake_project, tmp_path):
    backup_root = tmp_path / "backups"
    manifest = bk.backup_once(
        project_root=fake_project, backup_root=backup_root,
        now_fn=lambda: _now(2026, 5, 16),
    )
    assert manifest["snapshot_date"] == "2026-05-16"
    snap = backup_root / "2026-05-16"
    assert snap.is_dir()
    assert (snap / "trading.db").is_file()
    assert (snap / "records.jsonl").is_file()
    assert (snap / "settings.json").is_file()
    assert (snap / "manifest.json").is_file()


def test_backup_manifest_lists_every_file(fake_project, tmp_path):
    backup_root = tmp_path / "backups"
    manifest = bk.backup_once(
        project_root=fake_project, backup_root=backup_root,
        now_fn=lambda: _now(2026, 5, 16),
    )
    files = [c["file"] for c in manifest["copied"]]
    assert "data\\trading.db" in files or "data/trading.db" in files
    assert "data\\records.jsonl" in files or "data/records.jsonl" in files
    assert "config\\settings.json" in files or "config/settings.json" in files
    assert manifest["skipped"] == []


def test_backup_skips_missing_sources_without_error(fake_project, tmp_path):
    (fake_project / "data" / "records.jsonl").unlink()
    manifest = bk.backup_once(
        project_root=fake_project, backup_root=tmp_path / "backups",
        now_fn=lambda: _now(2026, 5, 16),
    )
    skipped_files = [s["file"] for s in manifest["skipped"]]
    assert any("records.jsonl" in f for f in skipped_files)
    # Other files still copied.
    assert any("trading.db" in c["file"] for c in manifest["copied"])


def test_backup_db_file_integrity(fake_project, tmp_path):
    """Acceptance: backup file integrity. Round-trip the db row."""
    backup_root = tmp_path / "backups"
    bk.backup_once(project_root=fake_project, backup_root=backup_root,
                    now_fn=lambda: _now(2026, 5, 16))
    db_copy = backup_root / "2026-05-16" / "trading.db"
    with sqlite3.connect(str(db_copy)) as conn:
        rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["hello", "world"]


def test_backup_uses_sqlite_backup_api_for_db(fake_project, tmp_path):
    """When the db is actively being written, the .backup API should
    still produce a self-consistent copy. Smoke: copy → open → query."""
    backup_root = tmp_path / "backups"
    # Open a writer connection to the source — sqlite.backup() should
    # still produce a valid copy.
    src = fake_project / "data" / "trading.db"
    writer = sqlite3.connect(str(src))
    try:
        bk.backup_once(project_root=fake_project, backup_root=backup_root,
                        now_fn=lambda: _now(2026, 5, 16))
        copy = backup_root / "2026-05-16" / "trading.db"
        with sqlite3.connect(str(copy)) as r:
            (n,) = r.execute("SELECT COUNT(*) FROM t").fetchone()
        assert n == 2
    finally:
        writer.close()


# ---------- list_snapshots ----------

def test_list_snapshots_returns_descending_iso_dates(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    for d in ("2026-05-14", "2026-05-16", "2026-05-15"):
        (root / d).mkdir()
    (root / "not-a-date").mkdir()  # filtered out
    (root / "ignore.txt").write_text("x")
    assert bk.list_snapshots(root) == ["2026-05-16", "2026-05-15", "2026-05-14"]


def test_list_snapshots_empty_for_missing_root(tmp_path):
    assert bk.list_snapshots(tmp_path / "nope") == []


# ---------- prune ----------

def test_prune_keeps_last_30_days_by_default(tmp_path):
    """`keep_days=30` keeps snapshots with date >= today - 30 days
    (inclusive), which is 31 days of snapshots (today + the prior 30)."""
    root = tmp_path / "b"
    root.mkdir()
    today = datetime(2026, 5, 16).date()
    # 60 days of snapshots.
    for i in range(60):
        d = today - timedelta(days=i)
        (root / d.isoformat()).mkdir()
    result = bk.prune(backup_root=root, keep_days=30,
                       now_fn=lambda: _now(2026, 5, 16))
    # All snapshots within the last 30 days (inclusive of cutoff) kept.
    assert len(result["kept"]) == 31
    assert len(result["pruned"]) == 29
    # The cutoff is today - 30 days.
    cutoff = (today - timedelta(days=30)).isoformat()
    assert result["cutoff"] == cutoff
    # Filesystem matches.
    on_disk = sorted(p.name for p in root.iterdir())
    assert len(on_disk) == 31


def test_prune_keep_days_zero_removes_everything(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    for i in range(5):
        d = (datetime(2026, 5, 16).date() - timedelta(days=i)).isoformat()
        (root / d).mkdir()
    result = bk.prune(backup_root=root, keep_days=0,
                       now_fn=lambda: _now(2026, 5, 16))
    # keep_days=0 → cutoff=today; today's snapshot is NOT pruned (not <
    # cutoff). Older ones are.
    assert "2026-05-16" in result["kept"]
    assert all(name != "2026-05-16" for name in result["pruned"])


def test_prune_rejects_negative_keep_days(tmp_path):
    with pytest.raises(ValueError):
        bk.prune(backup_root=tmp_path, keep_days=-1)


def test_prune_silent_on_missing_root(tmp_path):
    result = bk.prune(backup_root=tmp_path / "nope", keep_days=30)
    assert result["pruned"] == []


# ---------- restore ----------

def test_restore_copies_files_back(fake_project, tmp_path):
    backup_root = tmp_path / "backups"
    bk.backup_once(project_root=fake_project, backup_root=backup_root,
                    now_fn=lambda: _now(2026, 5, 16))
    # Mutate the live files so we can confirm the restore overwrote.
    (fake_project / "config" / "settings.json").write_text("CORRUPTED", encoding="utf-8")
    res = bk.restore("2026-05-16", project_root=fake_project,
                      backup_root=backup_root, overwrite=True)
    # All three files restored.
    assert len(res["restored"]) == 3
    settings = (fake_project / "config" / "settings.json").read_text(encoding="utf-8")
    assert "CORRUPTED" not in settings
    assert "auto_trade" in settings


def test_restore_refuses_to_overwrite_by_default(fake_project, tmp_path):
    backup_root = tmp_path / "backups"
    bk.backup_once(project_root=fake_project, backup_root=backup_root,
                    now_fn=lambda: _now(2026, 5, 16))
    res = bk.restore("2026-05-16", project_root=fake_project,
                      backup_root=backup_root)
    # Live files all exist → everything skipped.
    assert res["restored"] == []
    assert len(res["skipped"]) == 3
    for sk in res["skipped"]:
        assert "overwrite" in sk["reason"]


def test_restore_raises_for_missing_snapshot(fake_project, tmp_path):
    with pytest.raises(FileNotFoundError):
        bk.restore("1999-01-01", project_root=fake_project,
                    backup_root=tmp_path / "backups")


def test_restore_skips_files_missing_from_snapshot(fake_project, tmp_path):
    backup_root = tmp_path / "backups"
    bk.backup_once(project_root=fake_project, backup_root=backup_root,
                    now_fn=lambda: _now(2026, 5, 16))
    # Remove a file from the snapshot — restore should report it as
    # skipped (missing from snapshot), not silently succeed.
    (backup_root / "2026-05-16" / "records.jsonl").unlink()
    # Remove non-DB live counterparts so overwrite=False still copies them.
    (fake_project / "data" / "records.jsonl").unlink()
    (fake_project / "config" / "settings.json").unlink()
    res = bk.restore("2026-05-16", project_root=fake_project,
                      backup_root=backup_root)
    skipped_files = [s["file"] for s in res["skipped"]]
    # records.jsonl missing in snapshot → skipped.
    assert any("records.jsonl" in f for f in skipped_files)
    # settings.json present in snapshot AND live is now gone → restored.
    assert any("settings.json" in r["file"] for r in res["restored"])


# ---------- CLI smoke ----------

def test_cli_list_after_backup(fake_project, tmp_path, capsys, monkeypatch):
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(bk, "ROOT", fake_project)
    bk.main(["--backup-root", str(backup_root)])
    capsys.readouterr()
    bk.main(["--backup-root", str(backup_root), "--list"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) >= 1
    # Today's snapshot should show up.
    today = datetime.now().date().isoformat()
    assert today in out


# ---------- scheduler files ----------

def test_scheduler_files_exist_and_reference_backup_task():
    schedulers = ROOT / "schedulers"
    reg = schedulers / "register_backup.bat"
    run = schedulers / "run_backup.bat"
    assert reg.exists()
    assert run.exists()
    reg_text = reg.read_text(encoding="utf-8")
    assert "TradingSystem\\Backup" in reg_text
    assert "scripts\\backup.py" in run.read_text(encoding="utf-8")
