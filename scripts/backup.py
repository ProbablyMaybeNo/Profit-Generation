"""backup.py — Nightly backup + restore for the Profit Generation system.

Backs up the three files that capture the trading-state truth:

  - data/trading.db       (signals, outcomes, paper trades, equity, paused)
  - data/records.jsonl    (validator verdicts, test_runs)
  - config/settings.json  (auto_trade tuning, crypto cap, kill switch path)

Backups land in D:\\Backups\\profit-generation\\YYYY-MM-DD\\. The script
keeps the last `keep_days` daily snapshots (default 30) and prunes
older directories.

For trading.db we go via `sqlite3 .backup` (atomic, doesn't tear a
WAL-mode read/write). The other files are plain `shutil.copy2` since
they're written atomically by the project's existing writers.

CLI:
  py -3.13 scripts/backup.py                       # backup + prune (default)
  py -3.13 scripts/backup.py --no-prune            # backup only
  py -3.13 scripts/backup.py --backup-root D:/Tmp  # alt dest
  py -3.13 scripts/backup.py --restore 2026-05-16  # restore from date
  py -3.13 scripts/backup.py --list                # list snapshots present
  py -3.13 scripts/backup.py --keep-days 60        # extended retention
"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BACKUP_ROOT = Path("D:/Backups/profit-generation")
DEFAULT_KEEP_DAYS = 30

# Files included in every snapshot (relative to ROOT).
BACKUP_FILES = [
    Path("data") / "trading.db",
    Path("data") / "records.jsonl",
    Path("config") / "settings.json",
]


def _today_iso(now_fn: Optional[Callable[[], datetime]] = None) -> str:
    fn = now_fn or datetime.now
    return fn().date().isoformat()


def _snapshot_dir(backup_root: Path, day: str) -> Path:
    return backup_root / day


def _copy_sqlite_atomically(src: Path, dst: Path) -> None:
    """Use SQLite's online-backup API so concurrent readers/writers don't
    corrupt the copy. Falls back to shutil.copy2 if `src` isn't actually
    a SQLite db (helpful in tests with a placeholder file)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(str(src)) as source, \
             sqlite3.connect(str(dst)) as target:
            source.backup(target)
    except sqlite3.DatabaseError:
        # Not a real sqlite db; fall back to byte copy.
        shutil.copy2(src, dst)


def backup_once(
    *,
    project_root: Path = ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    files: Optional[List[Path]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict:
    """Run one backup pass. Returns a manifest dict describing what was
    copied (and what was skipped because source was missing)."""
    files = files or BACKUP_FILES
    day = _today_iso(now_fn)
    dest_dir = _snapshot_dir(backup_root, day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: List[Dict] = []
    skipped: List[Dict] = []
    for rel in files:
        src = project_root / rel
        if not src.exists():
            skipped.append({"file": str(rel), "reason": "source missing"})
            continue
        dst = dest_dir / rel.name
        if src.suffix == ".db":
            _copy_sqlite_atomically(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append({
            "file": str(rel),
            "src": str(src),
            "dst": str(dst),
            "bytes": dst.stat().st_size,
        })
    manifest = {
        "snapshot_date": day,
        "backup_root": str(backup_root),
        "snapshot_dir": str(dest_dir),
        "copied": copied,
        "skipped": skipped,
        "created_at": (now_fn or datetime.now)().isoformat(timespec="seconds"),
    }
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    return manifest


def list_snapshots(backup_root: Path = DEFAULT_BACKUP_ROOT) -> List[str]:
    """Return ISO-date snapshot names present under backup_root, sorted
    descending (most recent first). Anything that doesn't parse as a
    date is skipped silently."""
    if not backup_root.exists():
        return []
    out = []
    for child in backup_root.iterdir():
        if not child.is_dir():
            continue
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        out.append(child.name)
    return sorted(out, reverse=True)


def prune(
    *,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict:
    """Delete snapshot directories older than `keep_days`. Anything
    outside that retention window is removed in full. Returns
    {kept: [...], pruned: [...]}."""
    if keep_days < 0:
        raise ValueError(f"keep_days must be >= 0, got {keep_days}")
    today = (now_fn or datetime.now)().date()
    cutoff = today - timedelta(days=keep_days)
    kept, pruned = [], []
    for name in list_snapshots(backup_root):
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if d < cutoff:
            shutil.rmtree(backup_root / name, ignore_errors=True)
            pruned.append(name)
        else:
            kept.append(name)
    return {"kept": kept, "pruned": pruned, "cutoff": cutoff.isoformat(),
            "keep_days": keep_days}


def restore(
    snapshot_date: str,
    *,
    project_root: Path = ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    files: Optional[List[Path]] = None,
    overwrite: bool = False,
) -> Dict:
    """Copy files from `backup_root/snapshot_date/` back into the live
    project layout. Refuses to overwrite live files unless `overwrite=True`
    — safer default for the typical "I want to inspect an old snapshot"
    case. Returns a manifest of restored / skipped paths."""
    files = files or BACKUP_FILES
    src_dir = _snapshot_dir(backup_root, snapshot_date)
    if not src_dir.exists():
        raise FileNotFoundError(f"no snapshot at {src_dir}")
    restored: List[Dict] = []
    skipped: List[Dict] = []
    for rel in files:
        src = src_dir / rel.name
        dst = project_root / rel
        if not src.exists():
            skipped.append({"file": str(rel), "reason": "snapshot missing this file"})
            continue
        if dst.exists() and not overwrite:
            skipped.append({"file": str(rel),
                              "reason": "live file exists; pass --overwrite to replace"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append({"file": str(rel), "src": str(src), "dst": str(dst)})
    return {"snapshot_date": snapshot_date, "snapshot_dir": str(src_dir),
            "restored": restored, "skipped": skipped, "overwrite": overwrite}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backup / restore tool")
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT,
                        help=f"Backup destination root (default: {DEFAULT_BACKUP_ROOT})")
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS,
                        help=f"Retention window for prune (default {DEFAULT_KEEP_DAYS})")
    parser.add_argument("--no-prune", action="store_true",
                        help="Skip the prune step after backup")
    parser.add_argument("--list", action="store_true",
                        help="List snapshots present and exit")
    parser.add_argument("--restore", metavar="YYYY-MM-DD",
                        help="Restore from the given snapshot date instead "
                             "of taking a new backup")
    parser.add_argument("--overwrite", action="store_true",
                        help="With --restore: replace live files even if "
                             "they already exist")
    args = parser.parse_args(argv)

    if args.list:
        for name in list_snapshots(args.backup_root):
            print(name)
        return 0
    if args.restore:
        result = restore(args.restore, backup_root=args.backup_root,
                          overwrite=args.overwrite)
        print(json.dumps(result, indent=2))
        return 0
    manifest = backup_once(backup_root=args.backup_root)
    print(json.dumps(manifest, indent=2))
    if not args.no_prune:
        prune_result = prune(backup_root=args.backup_root,
                              keep_days=args.keep_days)
        print(json.dumps(prune_result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
