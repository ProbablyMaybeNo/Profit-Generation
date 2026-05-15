import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import scrape_github_strategies as sg  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _repo(full_name="foo/bar", stars=100, pushed="2026-04-01T12:00:00Z",
          branch="main"):
    owner = full_name.split("/")[0]
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "stargazers_count": stars,
        "pushed_at": pushed,
        "default_branch": branch,
        "owner": {"login": owner},
    }


def _good_item(sid="alpha"):
    return {
        "strategy_id": sid,
        "title": f"{sid} title",
        "entry_rules": "Long when close > 50-bar SMA.",
        "exit_rules": "Exit when close < 50-bar SMA.",
        "risk_management": "Stop at 2x ATR(20).",
    }


# ---------------------------------------------------------------------------
# Search query construction
# ---------------------------------------------------------------------------

def test_build_search_q_combines_filters():
    q = sg._build_search_q('"trading strategy"', min_stars=30,
                           since_pushed=date(2025, 5, 15))
    assert '"trading strategy"' in q
    assert "stars:>=30" in q
    assert "pushed:>=2025-05-15" in q


def test_search_repos_caps_at_max_results():
    def fake_get_json(url, params=None, headers=None, timeout=30.0):
        assert "search/repositories" in url
        return {"items": [_repo(f"u/r{i}") for i in range(50)]}

    rl = sg.RateLimiter(min_interval=0.0, sleep=lambda s: None,
                        now=lambda: 0.0)
    repos = sg.search_repos(
        '"x"', min_stars=10, since_pushed=date(2025, 1, 1),
        token=None, max_results=5,
        http_get_json=fake_get_json, rate_limiter=rl,
    )
    assert len(repos) == 5


def test_search_repos_sends_auth_header_when_token_present():
    captured = {}

    def fake_get_json(url, params=None, headers=None, timeout=30.0):
        captured["headers"] = headers
        return {"items": []}

    rl = sg.RateLimiter(min_interval=0.0, sleep=lambda s: None,
                        now=lambda: 0.0)
    sg.search_repos(
        '"x"', min_stars=10, since_pushed=date(2025, 1, 1),
        token="mytoken", max_results=5,
        http_get_json=fake_get_json, rate_limiter=rl,
    )
    assert captured["headers"]["Authorization"] == "Bearer mytoken"


def test_search_repos_omits_auth_header_without_token():
    captured = {}

    def fake_get_json(url, params=None, headers=None, timeout=30.0):
        captured["headers"] = headers
        return {"items": []}

    rl = sg.RateLimiter(min_interval=0.0, sleep=lambda s: None,
                        now=lambda: 0.0)
    sg.search_repos(
        '"x"', min_stars=10, since_pushed=date(2025, 1, 1),
        token=None, max_results=5,
        http_get_json=fake_get_json, rate_limiter=rl,
    )
    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# File picking heuristic
# ---------------------------------------------------------------------------

def test_pick_strategy_files_matches_keywords():
    paths = [
        "README.md",
        "src/strategy_alpha.py",
        "scripts/run.py",
        "indicators/rsi.pine",
        "strategies/momentum.py",
        "tests/test_helpers.py",
    ]
    picked = sg.pick_strategy_files(paths, max_files=3)
    assert "src/strategy_alpha.py" in picked
    assert "indicators/rsi.pine" in picked
    assert "strategies/momentum.py" in picked
    assert "README.md" not in picked
    assert "tests/test_helpers.py" not in picked
    assert len(picked) == 3


def test_pick_strategy_files_caps_at_max():
    paths = [f"strategy_{i}.py" for i in range(10)]
    picked = sg.pick_strategy_files(paths, max_files=2)
    assert len(picked) == 2


def test_pick_strategy_files_returns_empty_when_no_match():
    assert sg.pick_strategy_files(["README.md", "main.py"]) == []


# ---------------------------------------------------------------------------
# assemble_text
# ---------------------------------------------------------------------------

def test_assemble_text_includes_readme_and_files():
    out = sg.assemble_text(
        "readme body",
        {"strategy.py": "def x(): pass", "other.pine": "// pine"},
    )
    assert "readme body" in out
    assert "strategy.py" in out
    assert "def x()" in out
    assert "other.pine" in out


def test_assemble_text_truncates_per_file():
    big = "A" * 100_000
    out = sg.assemble_text(big, {"f.py": big}, per_file_limit=100)
    assert out.count("A") <= 250  # 100 readme + 100 file + breathing room


def test_assemble_text_handles_empty_inputs():
    assert sg.assemble_text("", {}) == ""


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def test_parse_extraction_accepts_clean_json():
    raw = json.dumps(_good_item("alpha"))
    item = sg.parse_extraction(raw)
    assert item is not None
    assert item["strategy_id"] == "alpha"


def test_parse_extraction_strips_markdown_fence():
    raw = "```json\n" + json.dumps(_good_item("a")) + "\n```"
    item = sg.parse_extraction(raw)
    assert item is not None
    assert item["strategy_id"] == "a"


def test_parse_extraction_returns_none_on_NONE_marker():
    assert sg.parse_extraction("NONE") is None
    assert sg.parse_extraction("```\nNONE\n```") is None


def test_parse_extraction_rejects_missing_fields():
    bad = {"strategy_id": "a", "title": "t"}
    assert sg.parse_extraction(json.dumps(bad)) is None


def test_parse_extraction_rejects_bad_strategy_id_shape():
    bad = {**_good_item(), "strategy_id": "Has Spaces"}
    assert sg.parse_extraction(json.dumps(bad)) is None


def test_parse_extraction_handles_chatter_before_json():
    raw = "Sure! Here you go:\n\n" + json.dumps(_good_item("alpha"))
    item = sg.parse_extraction(raw)
    assert item is not None


def test_parse_extraction_returns_none_on_garbage():
    assert sg.parse_extraction("not json at all") is None


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------

def test_build_record_matches_untested_schema():
    item = _good_item("trend-follow")
    repo = _repo("alice/trend-follow", stars=250)
    rec = sg.build_record(item, repo=repo, model="qwen2.5-coder:14b")
    assert rec["url"] == "https://github.com/alice/trend-follow"
    assert rec["author"] == "alice"
    assert rec["source"] == "github.com"
    assert "github" in rec["tags"]
    assert "UNTESTED" in rec["tags"]
    extra = rec["extra"]
    assert extra["strategy_id"] == "gh-trend-follow"
    assert extra["current_verdict"] == "UNTESTED"
    assert extra["tested"] is False
    assert extra["entry_rules"] == item["entry_rules"]
    assert extra["methodology_family"] == "github-extracted"
    assert extra["scraper"] == "scrape_github_strategies"
    assert extra["llm_model"] == "qwen2.5-coder:14b"
    assert extra["github_repo"] == "alice/trend-follow"
    assert extra["github_stars"] == 250


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_load_existing_source_urls_handles_missing(tmp_path):
    assert sg.load_existing_source_urls(tmp_path / "nope.jsonl") == set()


def test_load_existing_source_urls_reads_url_field(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        json.dumps({"url": "https://github.com/a/b", "extra": {}}) + "\n" +
        json.dumps({"url": "https://github.com/c/d", "extra": {}}) + "\n" +
        "garbage\n",
        encoding="utf-8",
    )
    urls = sg.load_existing_source_urls(p)
    assert urls == {"https://github.com/a/b", "https://github.com/c/d"}


# ---------------------------------------------------------------------------
# load_github_token (credentials.json optional)
# ---------------------------------------------------------------------------

def test_load_github_token_missing_creds(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("not there")

    monkeypatch.setattr("config.utils.load_credentials", boom)
    assert sg.load_github_token() is None


def test_load_github_token_no_github_section(monkeypatch):
    monkeypatch.setattr("config.utils.load_credentials",
                        lambda: {"alpaca": {}})
    assert sg.load_github_token() is None


def test_load_github_token_returns_token(monkeypatch):
    monkeypatch.setattr(
        "config.utils.load_credentials",
        lambda: {"github": {"token": "ghp_abc"}},
    )
    assert sg.load_github_token() == "ghp_abc"


def test_load_github_token_ignores_blank(monkeypatch):
    monkeypatch.setattr(
        "config.utils.load_credentials",
        lambda: {"github": {"token": "   "}},
    )
    assert sg.load_github_token() is None


# ---------------------------------------------------------------------------
# rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_enforces_min_interval():
    sleeps = []
    now_val = [0.0]
    rl = sg.RateLimiter(
        min_interval=1.0,
        sleep=lambda s: (sleeps.append(s), now_val.__setitem__(0, now_val[0] + s)),
        now=lambda: now_val[0],
    )
    rl.wait()
    rl.wait()
    rl.wait()
    assert len(sleeps) == 2
    assert all(s >= 0.99 for s in sleeps)


# ---------------------------------------------------------------------------
# scrape() end-to-end with injected fakes
# ---------------------------------------------------------------------------

class _NoSleepRL(sg.RateLimiter):
    def __init__(self):
        super().__init__(min_interval=0.0, sleep=lambda s: None,
                         now=lambda: 0.0)


def _fake_components(repos_per_query, tree_by_repo, readme_by_repo,
                     file_by_repo, llm_by_repo):
    searched = []
    listed = []
    readmes_fetched = []
    files_fetched = []
    prompts_sent = []

    def searcher(*, query, min_stars, since_pushed, token, max_results):
        searched.append(query)
        return repos_per_query.get(query, [])[:max_results]

    def file_lister(full_name):
        listed.append(full_name)
        return tree_by_repo.get(full_name, [])

    def readme_fetcher(full_name):
        readmes_fetched.append(full_name)
        return readme_by_repo.get(full_name, "")

    def file_fetcher(full_name, path, branch):
        files_fetched.append((full_name, path))
        return file_by_repo.get((full_name, path), "")

    def llm_caller(prompt):
        prompts_sent.append(prompt)
        # Look up which repo this prompt is for by scanning the body
        for full_name, raw in llm_by_repo.items():
            if full_name in prompt:
                return raw
        return "NONE"

    return (searcher, file_lister, readme_fetcher, file_fetcher, llm_caller,
            {"searched": searched, "listed": listed,
             "readmes": readmes_fetched, "files": files_fetched,
             "prompts": prompts_sent})


def test_scrape_writes_records_for_extracted_strategies(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {
        '"trading strategy"': [_repo("alice/s1"), _repo("bob/s2")],
    }
    trees = {
        "alice/s1": ["strategy.py", "README.md"],
        "bob/s2": ["strategies/x.py"],
    }
    readmes = {"alice/s1": "long-momentum readme", "bob/s2": "rsi readme"}
    file_body = {
        ("alice/s1", "strategy.py"): "def alpha(): pass",
        ("bob/s2", "strategies/x.py"): "def beta(): pass",
    }
    llm = {
        "alice/s1": json.dumps(_good_item("a")),
        "bob/s2": json.dumps(_good_item("b")),
    }
    searcher, file_lister, readme_fn, file_fn, llm_fn, _ = _fake_components(
        repos, trees, readmes, file_body, llm)

    summary = sg.scrape(
        queries=['"trading strategy"'],
        min_stars=10, max_repos=10,
        records_path=records,
        rate_limiter=_NoSleepRL(),
        repo_searcher=searcher,
        file_lister=file_lister,
        readme_fetcher=readme_fn,
        file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["new"] == 2
    assert summary["malformed"] == 0
    lines = [json.loads(l) for l in records.read_text(encoding="utf-8").splitlines() if l]
    assert {ln["extra"]["strategy_id"] for ln in lines} == {"gh-a", "gh-b"}


def test_scrape_dedupes_against_existing_urls(tmp_path):
    records = tmp_path / "records.jsonl"
    records.write_text(
        json.dumps({"url": "https://github.com/alice/s1", "extra": {}}) + "\n",
        encoding="utf-8",
    )
    repos = {'"q"': [_repo("alice/s1"), _repo("bob/s2")]}
    trees = {"bob/s2": ["strategy.py"]}
    readmes = {"bob/s2": "readme"}
    file_body = {("bob/s2", "strategy.py"): "code"}
    llm = {"bob/s2": json.dumps(_good_item("b"))}
    searcher, file_lister, readme_fn, file_fn, llm_fn, calls = _fake_components(
        repos, trees, readmes, file_body, llm)

    summary = sg.scrape(
        queries=['"q"'], min_stars=10, max_repos=10,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["skipped"] == 1
    assert summary["new"] == 1
    # The already-seen repo must not be processed further.
    assert "alice/s1" not in calls["readmes"]


def test_scrape_handles_NONE_llm_response_as_malformed(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {'"q"': [_repo("alice/s1")]}
    trees = {"alice/s1": ["strategy.py"]}
    readmes = {"alice/s1": "readme"}
    file_body = {("alice/s1", "strategy.py"): "code"}
    llm = {"alice/s1": "NONE"}
    searcher, file_lister, readme_fn, file_fn, llm_fn, _ = _fake_components(
        repos, trees, readmes, file_body, llm)

    summary = sg.scrape(
        queries=['"q"'], min_stars=10, max_repos=10,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["new"] == 0
    assert summary["malformed"] == 1


def test_scrape_handles_llm_exception_gracefully(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {'"q"': [_repo("alice/s1"), _repo("bob/s2")]}
    trees = {"alice/s1": ["strategy.py"], "bob/s2": ["strategy.py"]}
    readmes = {"alice/s1": "r", "bob/s2": "r"}
    file_body = {("alice/s1", "strategy.py"): "c",
                 ("bob/s2", "strategy.py"): "c"}

    def llm_fn(prompt):
        if "alice/s1" in prompt:
            raise RuntimeError("boom")
        return json.dumps(_good_item("b"))

    searcher, file_lister, readme_fn, file_fn, _, _ = _fake_components(
        repos, trees, readmes, file_body, {})

    summary = sg.scrape(
        queries=['"q"'], min_stars=10, max_repos=10,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["new"] == 1
    assert summary["malformed"] == 1


def test_scrape_honors_max_repos_cap(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {'"q"': [_repo(f"u/r{i}") for i in range(10)]}
    trees = {f"u/r{i}": ["strategy.py"] for i in range(10)}
    readmes = {f"u/r{i}": "r" for i in range(10)}
    file_body = {(f"u/r{i}", "strategy.py"): "c" for i in range(10)}
    llm = {f"u/r{i}": json.dumps(_good_item(f"s{i}")) for i in range(10)}
    searcher, file_lister, readme_fn, file_fn, llm_fn, _ = _fake_components(
        repos, trees, readmes, file_body, llm)

    summary = sg.scrape(
        queries=['"q"'], min_stars=10, max_repos=3,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["new"] == 3


def test_scrape_dry_run_does_not_write(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {'"q"': [_repo("alice/s1")]}
    trees = {"alice/s1": ["strategy.py"]}
    readmes = {"alice/s1": "r"}
    file_body = {("alice/s1", "strategy.py"): "c"}
    llm = {"alice/s1": json.dumps(_good_item("a"))}
    searcher, file_lister, readme_fn, file_fn, llm_fn, _ = _fake_components(
        repos, trees, readmes, file_body, llm)

    summary = sg.scrape(
        queries=['"q"'], min_stars=10, max_repos=10,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
        dry_run=True,
    )
    assert summary["new"] == 1
    assert not records.exists()


def test_scrape_continues_when_search_fails(tmp_path):
    records = tmp_path / "records.jsonl"
    repos = {'"q1"': [], '"q2"': [_repo("ok/r1")]}
    trees = {"ok/r1": ["strategy.py"]}
    readmes = {"ok/r1": "r"}
    file_body = {("ok/r1", "strategy.py"): "c"}
    llm = {"ok/r1": json.dumps(_good_item("a"))}

    def searcher(*, query, min_stars, since_pushed, token, max_results):
        if query == '"q1"':
            raise RuntimeError("github down")
        return repos.get(query, [])

    def file_lister(full_name):
        return trees.get(full_name, [])

    def readme_fn(full_name):
        return readmes.get(full_name, "")

    def file_fn(full_name, path, branch):
        return file_body.get((full_name, path), "")

    def llm_fn(prompt):
        for fn, raw in llm.items():
            if fn in prompt:
                return raw
        return "NONE"

    summary = sg.scrape(
        queries=['"q1"', '"q2"'], min_stars=10, max_repos=10,
        records_path=records, rate_limiter=_NoSleepRL(),
        repo_searcher=searcher, file_lister=file_lister,
        readme_fetcher=readme_fn, file_fetcher=file_fn,
        ollama_caller=llm_fn,
    )
    assert summary["new"] == 1


# ---------------------------------------------------------------------------
# Ollama plumbing (mocked, same pattern as test_llm_strategy_generator)
# ---------------------------------------------------------------------------

def _mock_ollama_response(text: str):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"response": text}
    return r


def test_call_ollama_round_trip(monkeypatch):
    raw = json.dumps(_good_item("a"))
    monkeypatch.setattr(
        sg, "_ollama_post",
        lambda url, payload, timeout: _mock_ollama_response(raw),
    )
    out = sg.call_ollama("dummy", temperature=0.3)
    assert out == raw


def test_call_ollama_raises_on_non_200(monkeypatch):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    monkeypatch.setattr(sg, "_ollama_post",
                        lambda url, payload, timeout: bad)
    with pytest.raises(RuntimeError, match="ollama 500"):
        sg.call_ollama("p")
