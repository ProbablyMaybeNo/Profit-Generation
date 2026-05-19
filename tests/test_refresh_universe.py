import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import refresh_universe as ru  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — minimal Wikipedia-shaped HTML
# ---------------------------------------------------------------------------

SP500_HTML = """
<html><body>
<table id="constituents" class="wikitable">
  <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
  <tr><td><a href="/wiki/Apple_Inc.">AAPL</a></td><td>Apple Inc.</td><td>Information Technology</td><td>Hardware</td></tr>
  <tr><td>MSFT</td><td>Microsoft</td><td>Information Technology</td><td>Software</td></tr>
  <tr><td>NVDA</td><td>Nvidia</td><td>Information Technology</td><td>Semiconductors</td></tr>
  <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector Holdings</td></tr>
</table>
</body></html>
"""

NDX_HTML = """
<html><body>
<table id="constituents" class="wikitable">
  <tr><th>Company</th><th>Symbol</th><th>GICS Sector</th></tr>
  <tr><td>Apple Inc.</td><td>AAPL</td><td>Information Technology</td></tr>
  <tr><td>Microsoft</td><td>MSFT</td><td>Information Technology</td></tr>
  <tr><td>Nvidia</td><td>NVDA</td><td>Information Technology</td></tr>
  <tr><td>Tesla</td><td>TSLA</td><td>Consumer Discretionary</td></tr>
  <tr><td>Meta Platforms</td><td>META</td><td>Communication Services</td></tr>
</table>
</body></html>
"""


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_parse_sp500_html_extracts_symbol_name_sector():
    rows = ru.parse_sp500_html(SP500_HTML)
    syms = [r.symbol for r in rows]
    assert syms == ["AAPL", "MSFT", "NVDA", "BRK.B"]
    assert rows[0].name == "Apple Inc."
    assert rows[0].sector == "Information Technology"


def test_parse_sp500_html_empty_when_no_table():
    assert ru.parse_sp500_html("<html><body><p>nope</p></body></html>") == []


def test_parse_ndx_html_handles_company_first_layout():
    rows = ru.parse_ndx_html(NDX_HTML)
    syms = [r.symbol for r in rows]
    assert "AAPL" in syms and "MSFT" in syms and "TSLA" in syms and "META" in syms
    assert len(syms) == 5
    apple = next(r for r in rows if r.symbol == "AAPL")
    assert apple.name == "Apple Inc."


def test_parse_ndx_html_handles_symbol_first_fallback():
    html = """
    <html><body>
    <table class="wikitable">
      <tr><th>foo</th><th>bar</th><th>baz</th></tr>
      <tr><td>AAPL</td><td>Apple</td><td>Tech</td></tr>
      <tr><td>MSFT</td><td>Microsoft</td><td>Tech</td></tr>
    </table>
    </body></html>
    """
    rows = ru.parse_ndx_html(html)
    syms = [r.symbol for r in rows]
    assert "AAPL" in syms and "MSFT" in syms


def test_parse_ndx_html_empty_when_no_table():
    assert ru.parse_ndx_html("<html></html>") == []


# ---------------------------------------------------------------------------
# CSV write
# ---------------------------------------------------------------------------


def test_write_universe_csv_dedupes_and_writes(tmp_path):
    rows = [
        ru.IndexRow("AAPL", "Apple", "Tech"),
        ru.IndexRow("MSFT", "Microsoft", "Tech"),
        ru.IndexRow("AAPL", "Apple Dupe", "Tech"),  # dupe drop
    ]
    out = tmp_path / "sp500.csv"
    ru.write_universe_csv(rows, out)

    content = out.read_text(encoding="utf-8")
    lines = [l for l in content.splitlines() if l]
    assert lines[0] == "symbol,name,sector"
    assert "AAPL,Apple,Tech" in lines
    assert "MSFT,Microsoft,Tech" in lines
    assert len([l for l in lines if l.startswith("AAPL,")]) == 1


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_diff_symbols_against_existing_file(tmp_path):
    existing = tmp_path / "sp500.csv"
    existing.write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nXYZ,Old,Energy\n",
        encoding="utf-8",
    )

    added, removed = ru.diff_symbols(["AAPL", "MSFT"], existing)

    assert added == ["MSFT"]
    assert removed == ["XYZ"]


def test_diff_symbols_against_missing_file(tmp_path):
    added, removed = ru.diff_symbols(
        ["AAPL", "MSFT"], tmp_path / "does_not_exist.csv",
    )
    assert added == ["AAPL", "MSFT"]
    assert removed == []


def test_diff_symbols_no_change(tmp_path):
    existing = tmp_path / "sp500.csv"
    existing.write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nMSFT,MS,Tech\n",
        encoding="utf-8",
    )
    added, removed = ru.diff_symbols(["AAPL", "MSFT"], existing)
    assert added == []
    assert removed == []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_refresh_writes_csvs_and_skips_telegram_when_unchanged(tmp_path):
    # Pre-seed with the EXACT symbols the fixtures will return
    (tmp_path / "sp500.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nMSFT,MS,Tech\nNVDA,Nvidia,Tech\nBRK.B,Berkshire,Fin\n",
        encoding="utf-8",
    )
    (tmp_path / "nasdaq100.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nMSFT,MS,Tech\nNVDA,Nvidia,Tech\nTSLA,Tesla,Cons\nMETA,Meta,Comm\n",
        encoding="utf-8",
    )

    sent = []

    summary = ru.refresh(
        dry_run=False,
        enable_telegram=True,
        sp500_fetcher=lambda url: SP500_HTML,
        ndx_fetcher=lambda url: NDX_HTML,
        telegram_sender=lambda m: sent.append(m),
        base_dir=tmp_path,
    )

    assert summary["sp500"]["added"] == []
    assert summary["sp500"]["removed"] == []
    assert summary["nasdaq100"]["added"] == []
    assert summary["nasdaq100"]["removed"] == []
    assert sent == []  # no diff, no alert


def test_refresh_alerts_on_diff(tmp_path):
    # Existing only has AAPL — fixture adds MSFT/NVDA/BRK.B
    (tmp_path / "sp500.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nDEAD,Dead Corp,Tech\n",
        encoding="utf-8",
    )
    (tmp_path / "nasdaq100.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nMSFT,MS,Tech\nNVDA,Nvidia,Tech\nTSLA,Tesla,Cons\nMETA,Meta,Comm\n",
        encoding="utf-8",
    )

    sent = []
    summary = ru.refresh(
        dry_run=False,
        enable_telegram=True,
        sp500_fetcher=lambda url: SP500_HTML,
        ndx_fetcher=lambda url: NDX_HTML,
        telegram_sender=lambda m: sent.append(m),
        base_dir=tmp_path,
    )

    assert "MSFT" in summary["sp500"]["added"]
    assert "DEAD" in summary["sp500"]["removed"]
    assert len(sent) == 1
    assert "[universe-refresh]" in sent[0]
    assert "S&P 500" in sent[0]


def test_refresh_dry_run_does_not_write(tmp_path):
    # No files pre-existing
    sent = []
    summary = ru.refresh(
        dry_run=True,
        enable_telegram=False,
        sp500_fetcher=lambda url: SP500_HTML,
        ndx_fetcher=lambda url: NDX_HTML,
        telegram_sender=lambda m: sent.append(m),
        base_dir=tmp_path,
    )

    assert summary["sp500"]["count"] == 4
    assert summary["nasdaq100"]["count"] == 5
    # No CSVs written
    assert not (tmp_path / "sp500.csv").exists()
    assert not (tmp_path / "nasdaq100.csv").exists()
    assert sent == []


def test_refresh_no_telegram_flag_suppresses_alert(tmp_path):
    sent = []
    ru.refresh(
        dry_run=False,
        enable_telegram=False,
        sp500_fetcher=lambda url: SP500_HTML,
        ndx_fetcher=lambda url: NDX_HTML,
        telegram_sender=lambda m: sent.append(m),
        base_dir=tmp_path,
    )
    assert sent == []


def test_refresh_idempotent_second_run_is_noop(tmp_path):
    sent = []
    common = dict(
        dry_run=False,
        enable_telegram=True,
        sp500_fetcher=lambda url: SP500_HTML,
        ndx_fetcher=lambda url: NDX_HTML,
        telegram_sender=lambda m: sent.append(m),
        base_dir=tmp_path,
    )
    s1 = ru.refresh(**common)
    s2 = ru.refresh(**common)
    # First run created files (added everything), second run sees no diff
    assert s1["sp500"]["added"]  # first run had adds
    assert s2["sp500"]["added"] == [] and s2["sp500"]["removed"] == []
    assert s2["nasdaq100"]["added"] == [] and s2["nasdaq100"]["removed"] == []
    assert len(sent) == 1  # only the first run alerted


def test_refresh_skips_write_when_parser_returns_empty(tmp_path):
    (tmp_path / "sp500.csv").write_text(
        "symbol\nAAPL\n", encoding="utf-8",
    )
    pre = (tmp_path / "sp500.csv").read_text(encoding="utf-8")

    ru.refresh(
        dry_run=False,
        enable_telegram=False,
        sp500_fetcher=lambda url: "<html></html>",  # parser returns []
        ndx_fetcher=lambda url: NDX_HTML,
        base_dir=tmp_path,
    )

    # Original sp500.csv preserved when parser returned 0 rows
    assert (tmp_path / "sp500.csv").read_text(encoding="utf-8") == pre


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_dry_run(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ru, "_fetch_html",
                        lambda url, fetcher=None: SP500_HTML if "S%26P" in url else NDX_HTML)
    monkeypatch.setattr(ru.universe_loader, "UNIVERSE_DIR", tmp_path)
    rc = ru.main(["--dry-run", "--no-telegram"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"sp500"' in out
    assert '"nasdaq100"' in out
