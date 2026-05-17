"""Tests for the public/ static performance page (milestone 4.4.2).

The deliverable is a plain HTML + Chart.js page. Tests verify:
  - The file exists ("build succeeds")
  - HTML structural invariants (head/body/charset/viewport)
  - The page references all three 4.4.1 endpoints
  - Mobile-responsive viewport + media queries
  - No leaks of dashboard internals (sensitive fields, account IDs)
  - The page degrades gracefully when Chart.js fails to load
  - All resources are HTTPS / inline / data URIs (no http:// dependencies)
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PUBLIC_DIR = ROOT / "public"
INDEX_HTML = PUBLIC_DIR / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    """Load the static page once per test module."""
    assert INDEX_HTML.exists(), (
        "public/index.html must exist — 4.4.2 deliverable"
    )
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Build / file presence
# ---------------------------------------------------------------------------

def test_public_dir_exists():
    assert PUBLIC_DIR.is_dir()


def test_index_html_non_empty(html):
    assert len(html) > 1000  # arbitrary lower bound; sanity check


# ---------------------------------------------------------------------------
# HTML structural invariants
# ---------------------------------------------------------------------------

def test_has_doctype(html):
    assert html.lstrip().lower().startswith("<!doctype html>")


def test_has_charset(html):
    assert 'charset="UTF-8"' in html or 'charset="utf-8"' in html


def test_has_viewport_for_mobile(html):
    # Required for mobile responsiveness.
    assert 'name="viewport"' in html
    assert "width=device-width" in html


def test_has_lang_attr(html):
    assert re.search(r'<html\s+[^>]*lang="[a-z]{2,}"', html)


def test_has_title(html):
    assert re.search(r"<title>[^<]+</title>", html)


def test_has_meta_description(html):
    assert 'name="description"' in html


# ---------------------------------------------------------------------------
# Public API endpoint references
# ---------------------------------------------------------------------------

def test_references_equity_curve_endpoint(html):
    assert "/api/public/equity_curve" in html


def test_references_strategies_endpoint(html):
    assert "/api/public/strategies" in html


def test_references_last_30d_pnl_endpoint(html):
    assert "/api/public/last_30d_pnl" in html


# ---------------------------------------------------------------------------
# Mobile-responsiveness
# ---------------------------------------------------------------------------

def test_has_responsive_media_queries(html):
    """At least one @media (max-width: ...) query for smaller screens."""
    assert re.search(r"@media\s*\(\s*max-width", html)


def test_grid_layout_collapses_on_mobile(html):
    """The stat-row grid uses 1fr on narrow screens — the canonical
    Refactoring-UI-ish pattern."""
    # The stat-row rule + its mobile collapse to grid-template-columns: 1fr
    assert re.search(
        r"stat-row\s*\{[^}]*grid-template-columns:[^}]*1fr\s+1fr", html
    )
    assert re.search(
        r"@media[^{]*\)\s*\{[^}]*stat-row[^}]*grid-template-columns:\s*1fr",
        html,
    )


# ---------------------------------------------------------------------------
# Security / leak prevention
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forbidden", [
    "alpaca_account_id", "account_number", "secret_key", "api_key",
    "portfolio_value", "buying_power", "fill_price",
    # Don't reveal an internal route here either.
    "/api/state", "/api/kill_switch",
])
def test_no_leakage_of_sensitive_identifiers(html, forbidden):
    assert forbidden not in html, (
        f"public/index.html references {forbidden!r} — must not."
    )


def test_no_http_only_resources(html):
    """No plain-http:// URLs — every external resource must be HTTPS or
    inline / data URI. Required for a Vercel deployment that browsers
    auto-upgrade.

    Allowlist: XML namespace URIs (e.g. http://www.w3.org/2000/svg) are
    spec-mandated identifiers, not fetchable resources — exclude them.
    """
    NAMESPACE_ALLOWLIST = ("http://www.w3.org/",)
    insecure = [
        u for u in re.findall(r"\bhttp://[^\s\"'<>]+", html)
        if not u.startswith(NAMESPACE_ALLOWLIST)
    ]
    assert insecure == [], f"insecure resources: {insecure}"


def test_chartjs_pinned_version(html):
    """Chart.js must be pinned to a specific major. Floating tags break
    the page silently when chartjs ships a breaking change."""
    assert re.search(r"chart\.js@\d+\.\d+\.\d+/dist/", html), (
        "Chart.js script tag must pin a x.y.z version"
    )


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

def test_page_handles_chartjs_load_failure(html):
    """If Chart is undefined at first paint, the script must retry — the
    table-only experience still works on slow CDN."""
    assert "typeof Chart" in html
    # And there's a retry path.
    assert "setTimeout" in html


def test_page_has_loading_state(html):
    """Initial table state must say something other than blank cells —
    perceived performance."""
    assert "loading" in html.lower()


# ---------------------------------------------------------------------------
# Snapshot — frozen identifiers that must stay
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("anchor", [
    'id="equityCurveCanvas"',
    'id="pnlBig"',
    'id="finalPct"',
    'id="maxDd"',
    'id="strategiesTable"',
    'id="strategiesBody"',
    'id="lastUpdated"',
])
def test_snapshot_has_required_anchors(html, anchor):
    assert anchor in html, (
        f"public page snapshot regression — lost anchor {anchor!r}"
    )
