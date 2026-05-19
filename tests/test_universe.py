import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import universe  # noqa: E402


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_load_trend_universe_reads_default_files(tmp_path):
    (tmp_path / "sp500.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nMSFT,Microsoft,Tech\n",
        encoding="utf-8",
    )
    (tmp_path / "nasdaq100.csv").write_text(
        "symbol,name,sector\nAAPL,Apple,Tech\nNVDA,Nvidia,Tech\n",
        encoding="utf-8",
    )
    (tmp_path / "etfs.csv").write_text(
        "symbol,name,category\nSPY,SPDR S&P 500,Broad\n",
        encoding="utf-8",
    )

    out = universe.load_trend_universe(universe_dir=tmp_path)

    assert out == ["AAPL", "MSFT", "NVDA", "SPY"]


def test_load_trend_universe_deduplicates_across_sources(tmp_path):
    _write(tmp_path / "sp500.csv", "symbol\nAAPL\nMSFT\nNVDA\n")
    _write(tmp_path / "nasdaq100.csv", "symbol\nAAPL\nMSFT\nNVDA\nTSLA\n")
    _write(tmp_path / "etfs.csv", "symbol\nSPY\n")

    out = universe.load_trend_universe(universe_dir=tmp_path)

    assert sorted(set(out)) == out  # already sorted + unique
    assert out.count("AAPL") == 1
    assert "TSLA" in out
    assert "SPY" in out
    assert len(out) == 5


def test_load_trend_universe_handles_missing_file_gracefully(tmp_path, caplog):
    _write(tmp_path / "sp500.csv", "symbol\nAAPL\nMSFT\n")
    # nasdaq100.csv and etfs.csv missing on purpose

    out = universe.load_trend_universe(universe_dir=tmp_path)

    assert out == ["AAPL", "MSFT"]


def test_load_trend_universe_handles_empty_symbol_cells(tmp_path):
    _write(
        tmp_path / "sp500.csv",
        "symbol,name\nAAPL,Apple\n,Empty\n   ,Spaces\nMSFT,Microsoft\n",
    )

    out = universe.load_trend_universe(
        files=("sp500.csv",), universe_dir=tmp_path,
    )

    assert out == ["AAPL", "MSFT"]


def test_load_trend_universe_uppercases_symbols(tmp_path):
    _write(tmp_path / "sp500.csv", "symbol\naapl\nmsft\nNvda\n")

    out = universe.load_trend_universe(
        files=("sp500.csv",), universe_dir=tmp_path,
    )

    assert out == ["AAPL", "MSFT", "NVDA"]


def test_load_trend_universe_handles_missing_symbol_column(tmp_path):
    _write(tmp_path / "sp500.csv", "ticker,name\nAAPL,Apple\nMSFT,Microsoft\n")

    out = universe.load_trend_universe(
        files=("sp500.csv",), universe_dir=tmp_path,
    )

    assert out == []


def test_universe_breakdown_reports_per_source_counts(tmp_path):
    _write(tmp_path / "sp500.csv", "symbol\nAAPL\nMSFT\n")
    _write(tmp_path / "nasdaq100.csv", "symbol\nAAPL\nNVDA\n")
    _write(tmp_path / "etfs.csv", "symbol\nSPY\n")

    breakdown = universe.universe_breakdown(universe_dir=tmp_path)

    assert breakdown["per_source"] == {
        "sp500.csv": 2,
        "nasdaq100.csv": 2,
        "etfs.csv": 1,
    }
    assert breakdown["unique_total"] == 4


# -------- repo snapshot integration check (real CSVs in data/universes/) --------

def test_real_universe_files_load_and_dedupe():
    out = universe.load_trend_universe()
    # ~600 symbols expected per plan; we ship 553 after dedupe — sanity bands.
    assert 450 <= len(out) <= 750, f"unexpected universe size: {len(out)}"
    # Should contain major indices' bellwethers
    for required in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ", "IWM"):
        assert required in out, f"{required} missing from universe"
    # Strict dedupe
    assert len(out) == len(set(out))


def test_real_breakdown_each_source_populated():
    breakdown = universe.universe_breakdown()
    per = breakdown["per_source"]
    assert per["sp500.csv"] >= 490
    assert per["nasdaq100.csv"] >= 90
    assert per["etfs.csv"] >= 25
    assert breakdown["unique_total"] >= 450
