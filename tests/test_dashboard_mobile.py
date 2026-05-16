"""Smoke tests for the mobile CSS pass — they verify the rules exist and
that the page still renders for the test client. Visual regression is
explicitly out of scope per the milestone."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def test_viewport_meta_present(client):
    text = client.get("/").get_data(as_text=True)
    assert 'name="viewport"' in text
    assert "width=device-width" in text


def test_mobile_media_query_present(client):
    text = client.get("/").get_data(as_text=True)
    # The 480px breakpoint is the iPhone-width pass.
    assert "@media (max-width: 480px)" in text


def test_mobile_container_padding_overridden(client):
    text = client.get("/").get_data(as_text=True)
    # Within the mobile block, container padding shrinks to 10px on the sides.
    mobile_block = text.split("@media (max-width: 480px)")[1].split("</style>")[0]
    assert ".container" in mobile_block
    assert "padding:" in mobile_block


def test_mobile_action_queue_stacks(client):
    text = client.get("/").get_data(as_text=True)
    mobile_block = text.split("@media (max-width: 480px)")[1].split("</style>")[0]
    # Action queue rows collapse to single column.
    assert ".aq-item" in mobile_block
    assert "grid-template-columns: 1fr" in mobile_block


def test_mobile_table_overflow_handled(client):
    text = client.get("/").get_data(as_text=True)
    mobile_block = text.split("@media (max-width: 480px)")[1].split("</style>")[0]
    # Cards become horizontally scrollable so wide tables don't push body.
    assert "overflow-x" in mobile_block


def test_existing_breakpoint_still_present(client):
    """The 1100px desktop breakpoint should remain untouched."""
    text = client.get("/").get_data(as_text=True)
    assert "@media (max-width: 1100px)" in text
