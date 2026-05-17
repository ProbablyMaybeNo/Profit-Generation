"""Intraday-bar strategy variants.

The botnet101 mean-reversion logic is timeframe-agnostic — the rules speak
in bars, not in calendar days (e.g. "long if close < lowest_low of prior
N bars"). This package re-exposes a small representative subset designed
to be fed Alpaca 5-minute / 15-minute bars instead of daily yfinance bars.

Signals emitted by the validator with these compute_fns carry
`bar_interval="5m"` or `"15m"` so they stay distinct from EOD signals.
"""
