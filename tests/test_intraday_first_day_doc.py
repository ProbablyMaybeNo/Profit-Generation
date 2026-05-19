"""Structural tests for docs/INTRADAY_FIRST_DAY.md (milestone 5.7.2).

The doc itself is the deliverable; these tests pin its structure so a
careless edit doesn't drop critical content (kill-switch reference,
rollback procedure, the ten-procedure constraint).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "INTRADAY_FIRST_DAY.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.exists(), f"missing first-day doc at {DOC}"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text):
    assert len(doc_text) > 1000


def test_doc_has_first_day_heading(doc_text):
    assert "Intraday First-Day Playbook" in doc_text


def test_doc_references_kill_switch(doc_text):
    assert "kill_switch" in doc_text
    assert "engage" in doc_text


def test_doc_references_intraday_enabled_flag(doc_text):
    assert "intraday_enabled" in doc_text


def test_doc_references_smoke_script(doc_text):
    assert "smoke_intraday_lifecycle" in doc_text


def test_doc_has_rollback_procedure(doc_text):
    assert "Rollback" in doc_text or "rollback" in doc_text


def test_doc_has_abort_criteria(doc_text):
    assert "Abort criteria" in doc_text


def test_doc_has_at_most_10_procedures(doc_text):
    """Acceptance: ≤ 10 procedures.

    Procedure headings start with `## N. ` where N is 1..10.
    """
    proc_headings = re.findall(r"^##\s+(\d+)\.\s+", doc_text, re.MULTILINE)
    assert len(proc_headings) <= 10, (
        f"doc has {len(proc_headings)} procedures, plan caps at 10"
    )
    assert len(proc_headings) >= 5, (
        f"doc has only {len(proc_headings)} procedures, too few to be useful"
    )


def test_each_procedure_has_at_most_5_steps(doc_text):
    """Acceptance: each procedure ≤ 5 steps."""
    # Split on procedure headings.
    sections = re.split(r"^##\s+\d+\.\s+.*$", doc_text, flags=re.MULTILINE)
    # First section is preamble before first procedure; skip it.
    failures = []
    for i, section in enumerate(sections[1:], start=1):
        # Stop at the next H2 (which is also a section break already
        # handled by the split). Then count numbered list items at the
        # *top* level. Pattern: lines starting with "N. " (1.-9.) at
        # column 0. Sub-bullets indent so they won't match.
        steps = re.findall(r"^(\d+)\.\s+", section, flags=re.MULTILINE)
        # The numbered steps reset per procedure; the highest visible
        # step number is the step count.
        nums = [int(s) for s in steps]
        if not nums:
            continue
        max_step = max(nums)
        if max_step > 5:
            failures.append((i, max_step))
    assert not failures, (
        f"procedures exceed 5-step cap: {failures}"
    )


def test_doc_cross_references_phase5_plan(doc_text):
    assert "PHASE5_PLAN" in doc_text


def test_doc_references_close_intraday_positions(doc_text):
    """The EOD sweep is a load-bearing piece of the first-day procedure."""
    assert "close_intraday_positions" in doc_text


def test_doc_references_dashboard(doc_text):
    """Operator watches the dashboard during the first day."""
    assert "dashboard" in doc_text.lower()
