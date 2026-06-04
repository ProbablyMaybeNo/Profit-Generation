# A3 orphan-outcome sweep — operator runbook

The A3 broker-reconcile sweep (`monitoring.reconcile_positions.sweep_orphan_outcomes`)
closes OPEN outcomes whose real broker position is already gone, with
`exit_reason='reconciled_no_position'`, at the best last-known mark:

1. recorded non-terminal SELL fill price
2. latest `snapshots.close`
3. latest `intraday_bars.close`
4. latest **daily-bar close** (`wide_bars.fetch_wide_daily_bars`) — B1, last resort

Outcomes whose symbol the broker still holds are never touched. Orphans with
no resolvable price (not even a daily close) are honestly skipped.

## B2 — one-time backlog backfill

The first live run swept 32 of ~189 orphans and skipped 175 (no price). B1
gives those 175 a daily-close mark. To clear the existing backlog, the
operator runs the guarded entry point (it is opt-in — it never auto-runs on
import, and it reuses `sweep_orphan_outcomes`, no parallel logic):

```
py -3.13 -m monitoring.reconcile_positions --sweep-orphans --no-alert
```

(`--backfill` is an accepted alias for `--sweep-orphans`.)

- Idempotent: already-closed outcomes drop out of the OPEN candidate set, so
  re-running is safe and sweeps nothing new.
- Held-symbol safe: an outcome whose symbol is still a live broker position is
  never closed.
- It prints `Orphan sweep: scanned=… swept=… skipped=… held=…` so the operator
  can confirm convergence. Drop `--no-alert` if a Telegram drift alert is wanted.

Run it once against the live DB after the B1 deploy is in place.

## B3 — scheduled daily trigger

`schedulers/run_reconcile.bat` (nightly Windows Task `register_reconcile.bat`)
previously ran `python -m monitoring.reconcile_positions` with no flag, so
`main()` defaulted `sweep_orphans=False` and the nightly task did NOT sweep.

The fix is to pass `--sweep-orphans` in the bat:

```
"%PY%" -m monitoring.reconcile_positions --sweep-orphans >> "%LOGFILE%" 2>&1
```

This makes the orphan sweep fire every night alongside the drift check. The
auto_trader intraday loop also still sweeps after each `order_sync`.
