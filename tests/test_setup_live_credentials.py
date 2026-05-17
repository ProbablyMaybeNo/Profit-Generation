"""Tests for scripts/setup_live_credentials (milestone 4.1.2).

Wizard schema validation, refusal-on-existing, dry-run mode, atomic
write, and Notion-post seam. Live API calls are mocked via validator_fn.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "setup_live_credentials", ROOT / "scripts" / "setup_live_credentials.py",
)
sl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sl)


def _base_creds():
    return {
        "alpaca": {"api_key": "paper_key", "secret_key": "paper_secret",
                   "paper": True,
                   "base_url": "https://paper-api.alpaca.markets"},
        "polygon": {"api_key": "p"},
        "fred": {"api_key": "f"},
        "notion": {"integration_token": "secret_x"},
        "telegram": {"bot_token": "t", "chat_id": "1"},
        "tradingview": {"webhook_secret": "tv"},
    }


def _good_validator():
    return lambda k, s: {
        "status": "ACTIVE",
        "blocked": False,
        "account_number": "AC123",
        "currency": "USD",
    }


def _write_creds(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# _is_placeholder / _mask
# ---------------------------------------------------------------------------

def test_is_placeholder_empty():
    assert sl._is_placeholder("") is True


def test_is_placeholder_marker():
    assert sl._is_placeholder("PASTE_YOUR_KEY_HERE") is True
    assert sl._is_placeholder("YOUR_SECRET") is True


def test_is_placeholder_real_key():
    assert sl._is_placeholder("AKIAJ_real_looking_key_xxxx") is False


def test_mask_short():
    assert sl._mask("abc") == "***"


def test_mask_long():
    out = sl._mask("AKIAJ1234567890")
    assert out.startswith("AKI")
    assert out.endswith("890")
    assert "..." in out
    assert "1234567890" not in out


# ---------------------------------------------------------------------------
# _live_section_populated
# ---------------------------------------------------------------------------

def test_live_section_populated_absent():
    assert sl._live_section_populated({}) is False


def test_live_section_populated_empty():
    assert sl._live_section_populated({"alpaca_live": {}}) is False


def test_live_section_populated_placeholder():
    creds = {"alpaca_live": {"api_key": "PASTE_YOUR_KEY", "secret_key": "x"}}
    assert sl._live_section_populated(creds) is False


def test_live_section_populated_real():
    creds = {"alpaca_live": {
        "api_key": "AKIAJrealkey",
        "secret_key": "realsecret",
        "paper": False,
    }}
    assert sl._live_section_populated(creds) is True


# ---------------------------------------------------------------------------
# validate_live_keys
# ---------------------------------------------------------------------------

def test_validate_rejects_empty_api_key():
    with pytest.raises(sl.WizardError):
        sl.validate_live_keys("", "secret",
                              validator_fn=lambda k, s: {})


def test_validate_rejects_placeholder():
    with pytest.raises(sl.WizardError):
        sl.validate_live_keys("PASTE_YOUR_KEY_HERE", "secret",
                              validator_fn=lambda k, s: {})


def test_validate_rejects_non_active():
    bad = lambda k, s: {"status": "INACTIVE", "blocked": False,
                        "account_number": "A1", "currency": "USD"}
    with pytest.raises(sl.WizardError) as exc:
        sl.validate_live_keys("k", "s", validator_fn=bad)
    assert "INACTIVE" in str(exc.value)


def test_validate_rejects_blocked():
    bad = lambda k, s: {"status": "ACTIVE", "blocked": True,
                        "account_number": "A1", "currency": "USD"}
    with pytest.raises(sl.WizardError) as exc:
        sl.validate_live_keys("k", "s", validator_fn=bad)
    assert "blocked" in str(exc.value).lower()


def test_validate_passes_through_api_error():
    def boom(k, s):
        raise RuntimeError("403 forbidden")
    with pytest.raises(sl.WizardError) as exc:
        sl.validate_live_keys("k", "s", validator_fn=boom)
    assert "403" in str(exc.value)


def test_validate_returns_summary_on_success():
    out = sl.validate_live_keys("k", "s", validator_fn=_good_validator())
    assert out["status"] == "ACTIVE"
    assert out["account_number"] == "AC123"


# ---------------------------------------------------------------------------
# write_live_section + save_credentials
# ---------------------------------------------------------------------------

def test_write_live_section_preserves_other_sections():
    creds = _base_creds()
    new = sl.write_live_section(creds, api_key="K", secret_key="S")
    assert new["alpaca"]["paper"] is True  # untouched
    assert new["polygon"]["api_key"] == "p"
    assert new["alpaca_live"]["api_key"] == "K"
    assert new["alpaca_live"]["secret_key"] == "S"
    assert new["alpaca_live"]["paper"] is False
    assert new["alpaca_live"]["base_url"] == sl.LIVE_BASE_URL


def test_save_credentials_atomic_write(tmp_path):
    target = tmp_path / "credentials.json"
    sl.save_credentials(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    # No leftover tmp file
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# run_wizard — happy path + refusal + dry-run
# ---------------------------------------------------------------------------

def test_wizard_missing_credentials_file(tmp_path):
    nonexistent = tmp_path / "nope.json"
    with pytest.raises(sl.WizardError) as exc:
        sl.run_wizard(credentials_path=nonexistent,
                      api_key="K", secret_key="S")
    assert "not found" in str(exc.value)


def test_wizard_refuses_existing_live_section(tmp_path):
    creds = _base_creds()
    creds["alpaca_live"] = {"api_key": "existingK",
                             "secret_key": "existingS",
                             "paper": False}
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)
    with pytest.raises(sl.WizardError) as exc:
        sl.run_wizard(credentials_path=p,
                      api_key="newK", secret_key="newS",
                      validator_fn=_good_validator())
    assert "--force" in str(exc.value)


def test_wizard_overwrites_with_force(tmp_path):
    creds = _base_creds()
    creds["alpaca_live"] = {"api_key": "existingK",
                             "secret_key": "existingS",
                             "paper": False}
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)
    result = sl.run_wizard(
        credentials_path=p, api_key="newK", secret_key="newS",
        force=True, no_notion=True,
        validator_fn=_good_validator(),
    )
    assert result["action"] == "installed"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["alpaca_live"]["api_key"] == "newK"


def test_wizard_dry_run_does_not_write(tmp_path):
    creds = _base_creds()
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)
    result = sl.run_wizard(
        credentials_path=p, api_key="K", secret_key="S",
        dry_run=True, no_notion=True,
        validator_fn=_good_validator(),
    )
    assert result["action"] == "noop_dry_run"
    assert result["wrote"] is False
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "alpaca_live" not in on_disk


def test_wizard_happy_path_writes_and_notion(tmp_path):
    creds = _base_creds()
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)
    notion_calls = []

    def fake_notion(**kw):
        notion_calls.append(kw)
        return {"id": "notion-page-1"}

    result = sl.run_wizard(
        credentials_path=p,
        api_key="AKIAJrealkey1234", secret_key="realsecret1234",
        validator_fn=_good_validator(),
        notion_fn=fake_notion,
    )
    assert result["action"] == "installed"
    assert result["wrote"] is True
    assert result["notion_page_id"] == "notion-page-1"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["alpaca_live"]["api_key"] == "AKIAJrealkey1234"
    assert on_disk["alpaca_live"]["paper"] is False
    assert on_disk["alpaca_live"]["base_url"] == sl.LIVE_BASE_URL
    # API key is masked in the notion payload
    assert notion_calls[0]["api_key_masked"].endswith("234")
    assert "realsecret" not in notion_calls[0]["api_key_masked"]


def test_wizard_notion_failure_is_non_fatal(tmp_path):
    creds = _base_creds()
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)

    def boom_notion(**kw):
        raise RuntimeError("notion 500")

    result = sl.run_wizard(
        credentials_path=p, api_key="K", secret_key="S",
        validator_fn=_good_validator(),
        notion_fn=boom_notion,
    )
    assert result["action"] == "installed"
    # Keys still written even though notion blew up.
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["alpaca_live"]["api_key"] == "K"
    # No notion_page_id key when post failed.
    assert "notion_page_id" not in result


def test_wizard_invalid_validator_propagates(tmp_path):
    creds = _base_creds()
    p = tmp_path / "credentials.json"
    _write_creds(p, creds)
    bad = lambda k, s: {"status": "INACTIVE", "blocked": False,
                        "account_number": "x", "currency": "USD"}
    with pytest.raises(sl.WizardError):
        sl.run_wizard(credentials_path=p,
                      api_key="K", secret_key="S",
                      validator_fn=bad, no_notion=True)
    # File untouched.
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert "alpaca_live" not in on_disk
