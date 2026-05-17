"""Tests for scripts/deploy_public (milestone 4.4.3).

Deploy command construction, precondition gating, runner indirection,
failure-path Telegram alert. Real `vercel` CLI is never invoked —
runner_fn is the injection seam.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "deploy_public", ROOT / "scripts" / "deploy_public.py",
)
dp = importlib.util.module_from_spec(SPEC)
sys.modules["deploy_public"] = dp
SPEC.loader.exec_module(dp)


# ---------------------------------------------------------------------------
# build_deploy_command
# ---------------------------------------------------------------------------

def test_build_command_prod_by_default(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    cmd = dp.build_deploy_command(public_dir=p)
    assert cmd[0] == "vercel"
    assert "deploy" in cmd
    assert "--prod" in cmd
    assert "--yes" in cmd


def test_build_command_includes_token_when_supplied(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    cmd = dp.build_deploy_command(public_dir=p, token="vt_xxx")
    assert "--token" in cmd
    assert "vt_xxx" in cmd


def test_build_command_omits_token_when_absent(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    cmd = dp.build_deploy_command(public_dir=p, token=None)
    assert "--token" not in cmd


def test_build_command_passes_public_dir(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    cmd = dp.build_deploy_command(public_dir=p)
    # Resolved absolute path should appear in argv.
    assert str(p.resolve()) in cmd


def test_build_command_supports_non_prod(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    cmd = dp.build_deploy_command(public_dir=p, prod=False)
    assert "--prod" not in cmd


# ---------------------------------------------------------------------------
# check_preconditions
# ---------------------------------------------------------------------------

def test_precondition_missing_public_dir(tmp_path):
    missing = tmp_path / "nope"
    out = dp.check_preconditions(public_dir=missing,
                                  which_fn=lambda b: "/usr/bin/vercel",
                                  token="t")
    assert out["ok"] is False
    assert "missing" in out["reason"]


def test_precondition_missing_index_html(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    out = dp.check_preconditions(public_dir=p,
                                  which_fn=lambda b: "/usr/bin/vercel",
                                  token="t")
    assert out["ok"] is False
    assert "index.html" in out["reason"]


def test_precondition_vercel_cli_missing(tmp_path):
    p = tmp_path / "public"
    p.mkdir()
    (p / "index.html").write_text("ok", encoding="utf-8")
    out = dp.check_preconditions(public_dir=p,
                                  which_fn=lambda b: None,
                                  token="t")
    assert out["ok"] is False
    assert "PATH" in out["reason"]


def test_precondition_no_token_no_link(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    p = tmp_path / "public"
    p.mkdir()
    (p / "index.html").write_text("ok", encoding="utf-8")
    out = dp.check_preconditions(
        public_dir=p,
        which_fn=lambda b: "/usr/bin/vercel",
        token=None,
        link_file=tmp_path / ".vercel" / "project.json",
    )
    assert out["ok"] is False
    assert "Vercel project" in out["reason"]


def test_precondition_passes_with_token(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    p = tmp_path / "public"
    p.mkdir()
    (p / "index.html").write_text("ok", encoding="utf-8")
    out = dp.check_preconditions(
        public_dir=p,
        which_fn=lambda b: "/usr/bin/vercel",
        token="vt_xxx",
        link_file=tmp_path / ".vercel" / "project.json",
    )
    assert out["ok"] is True


def test_precondition_passes_with_link_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    p = tmp_path / "public"
    p.mkdir()
    (p / "index.html").write_text("ok", encoding="utf-8")
    link = tmp_path / ".vercel" / "project.json"
    link.parent.mkdir(parents=True)
    link.write_text("{}", encoding="utf-8")
    out = dp.check_preconditions(
        public_dir=p,
        which_fn=lambda b: "/usr/bin/vercel",
        token=None,
        link_file=link,
    )
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# run_deploy — runner_fn injection
# ---------------------------------------------------------------------------

def _good_runner(cmd, *, timeout):
    r = MagicMock()
    r.returncode = 0
    r.stdout = "Deploying...\nhttps://profit-gen-abc123.vercel.app\n"
    r.stderr = ""
    return r


def _bad_runner(cmd, *, timeout):
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = "Error: build failed: missing index.html"
    return r


@pytest.fixture()
def linked_public(tmp_path, monkeypatch):
    """A fully-set-up public dir + linked .vercel project, plus a
    vercel-on-PATH stub. Used by the runner tests."""
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    p = tmp_path / "public"
    p.mkdir()
    (p / "index.html").write_text("ok", encoding="utf-8")
    link = tmp_path / ".vercel" / "project.json"
    link.parent.mkdir(parents=True)
    link.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dp, "PUBLIC_DIR", p)
    monkeypatch.setattr(dp, "VERCEL_LINK_FILE", link)
    monkeypatch.setattr(dp.shutil, "which", lambda b: "/usr/bin/vercel")
    return p


def test_run_deploy_happy_path(linked_public):
    out = dp.run_deploy(public_dir=linked_public, token="vt_xxx",
                        runner_fn=_good_runner)
    assert out["ok"] is True
    assert out["deploy_url"] == "https://profit-gen-abc123.vercel.app"
    assert out["returncode"] == 0


def test_run_deploy_propagates_failure(linked_public):
    out = dp.run_deploy(public_dir=linked_public, token="vt_xxx",
                        runner_fn=_bad_runner)
    assert out["ok"] is False
    assert out["returncode"] == 1
    assert "build failed" in out["stderr"]


def test_run_deploy_skips_subprocess_when_preconditions_fail(tmp_path,
                                                              monkeypatch):
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.setattr(dp.shutil, "which", lambda b: None)
    called = []

    def runner(cmd, *, timeout):
        called.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    out = dp.run_deploy(public_dir=tmp_path / "no",
                        runner_fn=runner)
    assert out["ok"] is False
    assert called == [], "subprocess must not run when preconditions fail"


# ---------------------------------------------------------------------------
# Failure-path alerts
# ---------------------------------------------------------------------------

def test_send_failure_alert_fires_on_failure():
    sent = []
    sent_ok = dp.send_failure_alert(
        {"ok": False, "returncode": 1, "stderr": "bang", "stdout": ""},
        alert_fn=lambda t: (sent.append(t), True)[1],
    )
    assert sent_ok is True
    assert "failed" in sent[0].lower()
    assert "bang" in sent[0]


def test_send_failure_alert_silent_on_success():
    sent = []
    sent_ok = dp.send_failure_alert(
        {"ok": True, "returncode": 0, "stderr": "", "stdout": "url"},
        alert_fn=lambda t: (sent.append(t), True)[1],
    )
    assert sent_ok is False
    assert sent == []


def test_send_failure_alert_handles_alert_exception():
    def boom(_t):
        raise RuntimeError("telegram down")
    sent_ok = dp.send_failure_alert(
        {"ok": False, "returncode": 1, "stderr": "bang", "stdout": ""},
        alert_fn=boom,
    )
    assert sent_ok is False


# ---------------------------------------------------------------------------
# render_notion_markdown
# ---------------------------------------------------------------------------

def test_notion_markdown_success_shape():
    md = dp.render_notion_markdown(
        {"ok": True, "deploy_url": "https://x.vercel.app",
         "returncode": 0, "stdout": "", "stderr": ""}
    )
    assert "success" in md.lower()
    assert "https://x.vercel.app" in md


def test_notion_markdown_failure_shape():
    md = dp.render_notion_markdown(
        {"ok": False, "deploy_url": None,
         "returncode": 1, "stdout": "", "stderr": "missing index.html"},
    )
    assert "failed" in md.lower()
    assert "missing index.html" in md


def test_post_to_notion_uses_injected_poster():
    captured = {}

    def fake_poster(*, title, markdown, ok):
        captured["title"] = title
        captured["ok"] = ok
        return {"id": "page-1"}

    out = dp.post_to_notion(
        {"ok": True, "deploy_url": "https://x", "returncode": 0,
         "stdout": "", "stderr": ""},
        poster=fake_poster,
    )
    assert out["id"] == "page-1"
    assert "OK" in captured["title"]
    assert captured["ok"] is True


# ---------------------------------------------------------------------------
# Scheduler artifacts exist
# ---------------------------------------------------------------------------

def test_deploy_public_bat_exists():
    assert (ROOT / "schedulers" / "deploy_public.bat").exists()


def test_register_public_deploy_bat_exists():
    assert (ROOT / "schedulers" / "register_public_deploy.bat").exists()
