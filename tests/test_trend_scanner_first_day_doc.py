"""Structure check for docs/TREND_SCANNER_FIRST_DAY.md (milestone
5.5.7.2). The acceptance says "≤ 10 procedures, each ≤ 5 steps" —
this test enforces that contract so future edits can't silently
break the format.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def doc_text():
    path = ROOT / "docs" / "TREND_SCANNER_FIRST_DAY.md"
    assert path.exists(), f"missing playbook at {path}"
    return path.read_text(encoding="utf-8")


def test_playbook_exists(doc_text):
    assert "Trend Scanner First-Day Playbook" in doc_text


def test_at_most_ten_procedures(doc_text):
    """Top-level numbered procedures (## N. ...) — must not exceed 10."""
    proc_headers = re.findall(r"^## (\d+)\.\s+", doc_text, re.MULTILINE)
    assert len(proc_headers) <= 10, (
        f"too many procedures ({len(proc_headers)}); cap is 10"
    )
    # Numbers must be unique 1..N
    nums = sorted(int(n) for n in proc_headers)
    assert nums == sorted(set(nums)), f"duplicate procedure numbers: {nums}"


def test_each_procedure_has_at_most_five_steps(doc_text):
    """Inside each ## N. block, the count of `^\\d+\\.\\s` step lines
    must be  5."""
    blocks = re.split(r"^## \d+\.\s+", doc_text, flags=re.MULTILINE)
    # blocks[0] is the preamble before the first ## — skip it.
    for i, block in enumerate(blocks[1:], start=1):
        # Step lines are top-of-line numbered: "1. ...". Sub-bullets
        # are indented, so anchor strictly to column 0.
        steps = re.findall(r"^(\d+)\.\s", block, re.MULTILINE)
        assert len(steps) <= 5, (
            f"procedure {i} has {len(steps)} steps (cap 5): "
            f"first 200 chars: {block[:200]!r}"
        )


def test_mandatory_sections_present(doc_text):
    """The playbook must include the rollback + abort blocks because
    those are the safety net Ross relies on under pressure."""
    assert re.search(r"^## \d+\.\s+Abort criteria", doc_text, re.MULTILINE)
    assert re.search(r"^## \d+\.\s+Rollback", doc_text, re.MULTILINE)
    assert "trend_scanner_enabled" in doc_text
    assert "kill_switch engage" in doc_text


def test_cross_references_to_existing_assets(doc_text):
    """The doc must reference real artefacts so a tired Ross can
    actually find them."""
    assert "scripts/smoke_trend_scanner.py" in doc_text
    assert "config/settings.json" in doc_text
    assert "max_new_entries_per_day" in doc_text
    assert "scanner activity" in doc_text  # the 5.5.6.1 card
