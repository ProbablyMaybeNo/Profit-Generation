"""
risk.py — Pre-trade risk checks. Every strategy MUST call validate_order
before submitting. Blocks accidental size, shorting, daily loss breach,
position-count blowups, and live-mode orders.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from config.utils import is_paper_mode, load_settings, log


@dataclass
class RiskLimits:
    max_position_usd: float = 1000.0
    max_daily_loss_pct: float = 2.0
    max_open_positions: int = 10
    max_orders_per_day: int = 50
    allow_shorts: bool = False

    @classmethod
    def from_settings(cls) -> "RiskLimits":
        s = load_settings().get("risk", {})
        return cls(
            max_position_usd=float(s.get("max_position_usd", 1000.0)),
            max_daily_loss_pct=float(s.get("max_daily_loss_pct", 2.0)),
            max_open_positions=int(s.get("max_open_positions", 10)),
            max_orders_per_day=int(s.get("max_orders_per_day", 50)),
            allow_shorts=bool(s.get("allow_shorts", False)),
        )


@dataclass
class RiskResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def validate_order(
    symbol: str,
    qty: float,
    side: str,
    price: float,
    *,
    portfolio_value: float,
    equity_at_open: float,
    open_position_symbols: Iterable[str],
    orders_today: int,
    held_qty_for_symbol: float = 0.0,
    limits: Optional[RiskLimits] = None,
) -> RiskResult:
    """
    Validate a proposed order against risk limits.

    Args:
        symbol, qty, side ('buy'|'sell'), price: order details.
        portfolio_value: current total equity.
        equity_at_open: equity at market open today (for daily-loss calc).
        open_position_symbols: symbols currently held.
        orders_today: count of orders submitted today (any status).
        held_qty_for_symbol: signed quantity currently held in this symbol
            (positive long, negative short, 0 flat). Used to detect new shorts.
        limits: override RiskLimits.from_settings().
    """
    reasons: list[str] = []
    limits = limits or RiskLimits.from_settings()

    if not is_paper_mode():
        reasons.append("BLOCKED: not in paper mode — refusing to submit live order")

    side_l = side.lower()
    if side_l not in ("buy", "sell"):
        reasons.append(f"invalid side: {side!r}")

    if qty <= 0:
        reasons.append(f"qty must be positive, got {qty}")
    if price <= 0:
        reasons.append(f"price must be positive, got {price}")

    notional = abs(qty) * price
    if notional > limits.max_position_usd:
        reasons.append(
            f"notional ${notional:,.2f} exceeds max_position_usd "
            f"${limits.max_position_usd:,.2f}"
        )

    if equity_at_open > 0:
        drawdown_pct = (equity_at_open - portfolio_value) / equity_at_open * 100.0
        if drawdown_pct >= limits.max_daily_loss_pct:
            reasons.append(
                f"daily loss circuit-breaker tripped: "
                f"{drawdown_pct:.2f}% >= {limits.max_daily_loss_pct:.2f}%"
            )

    open_set = set(open_position_symbols)
    if symbol not in open_set and len(open_set) >= limits.max_open_positions:
        reasons.append(
            f"open positions {len(open_set)} >= max {limits.max_open_positions}"
        )

    if orders_today >= limits.max_orders_per_day:
        reasons.append(
            f"orders today {orders_today} >= max {limits.max_orders_per_day}"
        )

    if not limits.allow_shorts and side_l == "sell":
        resulting = held_qty_for_symbol - qty
        if resulting < 0:
            reasons.append(
                f"sell would create short position in {symbol} "
                f"(held {held_qty_for_symbol}, selling {qty}); shorts disabled"
            )

    ok = len(reasons) == 0
    return RiskResult(ok=ok, reasons=reasons)


def submit_order_safely(client, *, symbol, qty, side, limit_price, time_in_force="day"):
    """
    Validate then submit a limit order via the Alpaca TradingClient.
    Returns the order object on success. Raises RuntimeError if validation fails.
    """
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    account = client.get_account()
    positions = client.get_all_positions()
    held = next(
        (float(p.qty) for p in positions if p.symbol.upper() == symbol.upper()), 0.0
    )
    today = date.today().isoformat()
    todays_orders = [
        o for o in client.get_orders()
        if str(getattr(o, "submitted_at", "")).startswith(today)
    ]

    result = validate_order(
        symbol=symbol,
        qty=qty,
        side=side,
        price=limit_price,
        portfolio_value=float(account.portfolio_value),
        equity_at_open=float(getattr(account, "last_equity", account.equity)),
        open_position_symbols=[p.symbol for p in positions],
        orders_today=len(todays_orders),
        held_qty_for_symbol=held,
    )

    if not result.ok:
        for r in result.reasons:
            log(f"risk: {r}", "ERROR")
        raise RuntimeError("order rejected by risk module: " + "; ".join(result.reasons))

    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce(time_in_force),
        limit_price=limit_price,
    )
    log(f"risk: submitting {side} {qty} {symbol} @ {limit_price}", "SUCCESS")
    return client.submit_order(req)


if __name__ == "__main__":
    limits = RiskLimits.from_settings()
    print(f"Loaded limits: {limits}")

    print("\n-- valid order --")
    r = validate_order(
        "AAPL", qty=5, side="buy", price=150.0,
        portfolio_value=50000, equity_at_open=50000,
        open_position_symbols=["MSFT"], orders_today=3,
    )
    print(r)

    print("\n-- oversize --")
    r = validate_order(
        "AAPL", qty=100, side="buy", price=150.0,
        portfolio_value=50000, equity_at_open=50000,
        open_position_symbols=[], orders_today=0,
    )
    print(r)

    print("\n-- daily loss tripped --")
    r = validate_order(
        "AAPL", qty=1, side="buy", price=150.0,
        portfolio_value=48000, equity_at_open=50000,
        open_position_symbols=[], orders_today=0,
    )
    print(r)

    print("\n-- accidental short --")
    r = validate_order(
        "AAPL", qty=5, side="sell", price=150.0,
        portfolio_value=50000, equity_at_open=50000,
        open_position_symbols=[], orders_today=0,
        held_qty_for_symbol=0,
    )
    print(r)
