"""
Scrape TradingView India daytrading strategy listings (page 1 + page 2),
detail metadata from each script page, emit scraper bundle + SQLite.

Run: py -3.13 scripts/scrape_tradingview_daytrading_strategies.py
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LISTING_URLS: list[tuple[int, str]] = [
    (
        1,
        "https://in.tradingview.com/scripts/daytrading/?script_type=strategies",
    ),
    (
        2,
        "https://in.tradingview.com/scripts/daytrading/page-2/?script_type=strategies",
    ),
]

ROOT = Path(__file__).resolve().parents[1]
JOB_SLUG = "tradingview-in-daytrading-strategies-2026-04-26"
OUT_DIR = ROOT / "data" / "scrapes" / JOB_SLUG
DB_PATH = ROOT / "data" / "tradingview_strategies.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def tv_markup_to_readable(text: str) -> str:
    if not text:
        return ""
    t = text
    t = re.sub(r"\[b\](.*?)\[/b\]", r"\1", t, flags=re.DOTALL)
    t = t.replace("[list]", "").replace("[/list]", "")
    t = re.sub(r"\[\*\]", "\n- ", t)
    t = re.sub(r"\[i\](.*?)\[/i\]", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def script_key_from_url(url: str) -> str:
    m = re.search(r"/script/([^/]+)/?", url)
    return (m.group(1) if m else url).lower()


def longest_json_description(html: str) -> str | None:
    key = '"description":"'
    best: str | None = None
    pos = 0
    dec = json.JSONDecoder()
    while True:
        i = html.find(key, pos)
        if i < 0:
            break
        i += len(key) - 1
        try:
            val, end = dec.raw_decode(html, i)
        except json.JSONDecodeError:
            pos = i + 1
            continue
        if isinstance(val, str) and len(val) > (len(best or "")):
            best = val
        pos = end
    return best


def parse_meta(html: str) -> dict[str, str | None]:
    og_title = og_desc = None
    m = re.search(r'property="og:title" content="([^"]*)"', html)
    if m:
        og_title = m.group(1)
    m = re.search(r'property="og:description" content="([^"]*)"', html)
    if m:
        og_desc = m.group(1).replace("\\n", "\n")
    title = author = None
    if og_title and " by " in og_title:
        parts = og_title.rsplit(" by ", 1)
        title = parts[0].strip()
        author = parts[1].strip()
    for u in re.findall(r'"username":"([^"]+)"', html):
        if u and u != "Guest" and re.match(r"^[A-Za-z0-9_]+$", u):
            if not author:
                author = u
            break
    return {"title": title, "author": author, "og_description": og_desc}


def fetch_detail(session: requests.Session, url: str) -> dict:
    time.sleep(random.uniform(0.04, 0.12))
    r = session.get(url, timeout=45)
    r.raise_for_status()
    html = r.text
    meta = parse_meta(html)
    raw_desc = longest_json_description(html)
    readable = _collapse_ws(tv_markup_to_readable(raw_desc or ""))
    og_plain = _collapse_ws((meta.get("og_description") or "").replace("\\n", "\n"))
    summary_body = readable if len(readable) > len(og_plain) else og_plain
    agent_summary = (
        f"Title: {meta.get('title') or ''}\n"
        f"Author: {meta.get('author') or ''}\n"
        f"URL: {url}\n"
        f"---\n{summary_body}"
    )
    return {
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "description_plain": og_plain,
        "description_full_readable": readable,
        "description_raw_tv": raw_desc or "",
        "agent_summary": agent_summary,
    }


async def collect_all_listing_urls(errors: list) -> list[tuple[int, str]]:
    combined: list[tuple[int, str]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1400, "height": 900},
            locale="en-IN",
        )
        page = await ctx.new_page()
        for page_no, listing_url in LISTING_URLS:
            try:
                await page.goto(listing_url, wait_until="networkidle", timeout=120000)
                await page.wait_for_timeout(7000)
                hrefs = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href*="/script/"]'))
                    .map(a => a.getAttribute('href')).filter(Boolean)"""
                )
            except Exception as e:
                errors.append({"url": listing_url, "stage": "browser", "error": str(e)})
                continue
            seen_local: set[str] = set()
            for h in hrefs:
                if not h:
                    continue
                if "#" in h:
                    h = h.split("#")[0]
                h = h.rstrip("/")
                if "/script/" not in h:
                    continue
                if not h.startswith("http"):
                    h = "https://in.tradingview.com" + h
                key = script_key_from_url(h + "/")
                if key in seen_local:
                    continue
                seen_local.add(key)
                combined.append((page_no, h + "/"))
        await browser.close()
    return combined


def write_record_jsonl(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def init_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_strategies (
            script_key TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            title TEXT,
            author TEXT,
            listing_page INTEGER,
            description_plain TEXT,
            description_full_readable TEXT,
            agent_summary TEXT,
            captured_at TEXT NOT NULL,
            extra_json TEXT
        )
        """
    )
    conn.commit()


def upsert_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO tv_strategies (
            script_key, source_url, title, author, listing_page,
            description_plain, description_full_readable, agent_summary,
            captured_at, extra_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(script_key) DO UPDATE SET
            source_url=excluded.source_url,
            title=excluded.title,
            author=excluded.author,
            listing_page=excluded.listing_page,
            description_plain=excluded.description_plain,
            description_full_readable=excluded.description_full_readable,
            agent_summary=excluded.agent_summary,
            captured_at=excluded.captured_at,
            extra_json=excluded.extra_json
        """,
        (
            row["script_key"],
            row["source_url"],
            row["title"],
            row["author"],
            row["listing_page"],
            row["description_plain"],
            row["description_full_readable"],
            row["agent_summary"],
            row["captured_at"],
            row["extra_json"],
        ),
    )


def main() -> None:
    started = _now_iso()
    errors: list[dict] = []

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "raw").mkdir(exist_ok=True)
    (OUT_DIR / "images").mkdir(exist_ok=True)
    (OUT_DIR / "cache").mkdir(exist_ok=True)

    jsonl_path = OUT_DIR / "records.jsonl"
    csv_path = OUT_DIR / "records.csv"
    if jsonl_path.exists():
        jsonl_path.unlink()

    raw_listing = asyncio.run(collect_all_listing_urls(errors))
    seen: set[str] = set()
    listings: list[tuple[int, str]] = []
    for page_no, url in raw_listing:
        k = script_key_from_url(url)
        if k in seen:
            continue
        seen.add(k)
        listings.append((page_no, url))

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_sqlite(conn)

    records: list[dict] = []
    for page_no, url in listings:
        sk = script_key_from_url(url)
        try:
            detail = fetch_detail(session, url)
        except Exception as e:
            errors.append({"url": url, "stage": "fetch", "error": str(e)})
            continue
        cap = _now_iso()
        rid = hashlib.sha1((url + (detail["title"] or "")).encode()).hexdigest()[:12]
        rec = {
            "id": rid,
            "name": _collapse_ws(detail["title"]),
            "description": detail["description_plain"],
            "source_url": url,
            "image_url": "",
            "image_path": "",
            "captured_at": cap,
            "source": {
                "technique": "browser|html",
                "selector_or_endpoint": f"listing_page_{page_no}+meta+json",
                "confidence": "high",
            },
            "extra": {
                "author": detail["author"],
                "listing_page": page_no,
                "description_full_readable": detail["description_full_readable"],
                "agent_summary": detail["agent_summary"],
            },
        }
        records.append(rec)
        write_record_jsonl(jsonl_path, rec)
        upsert_row(
            conn,
            {
                "script_key": sk,
                "source_url": url,
                "title": detail["title"],
                "author": detail["author"],
                "listing_page": page_no,
                "description_plain": detail["description_plain"],
                "description_full_readable": detail["description_full_readable"],
                "agent_summary": detail["agent_summary"],
                "captured_at": cap,
                "extra_json": json.dumps(
                    {"raw_tv_chars": len(detail["description_raw_tv"])},
                    ensure_ascii=False,
                ),
            },
        )
    conn.commit()
    conn.close()

    fieldnames = [
        "id",
        "name",
        "author",
        "source_url",
        "listing_page",
        "description_plain",
        "description_full_readable",
        "captured_at",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as cf:
        w = csv.DictWriter(cf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in records:
            ex = r.get("extra") or {}
            w.writerow(
                {
                    "id": r["id"],
                    "name": r.get("name", ""),
                    "author": ex.get("author", ""),
                    "source_url": r["source_url"],
                    "listing_page": ex.get("listing_page", ""),
                    "description_plain": r.get("description", ""),
                    "description_full_readable": ex.get("description_full_readable", ""),
                    "captured_at": r.get("captured_at", ""),
                }
            )

    manifest = {
        "job": JOB_SLUG,
        "target": LISTING_URLS[0][1],
        "started_at": started,
        "completed_at": _now_iso(),
        "techniques_used": ["browser", "html"],
        "total_records": len(records),
        "records_with_images": 0,
        "errors": errors,
        "rate_limits_hit": 0,
        "auth": {"method": "none", "source": "none"},
        "next_steps": [
            "Use agent_summary for LLM backtest design",
            "Pull Pine source manually or via authenticated flow if needed",
        ],
        "consumer_notes": (
            "Strategies from TradingView India daytrading / strategies, pages 1–2. "
            "description_full_readable is TV markup converted to plain structure. "
            f"SQLite: {DB_PATH}"
        ),
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    readme = f"""# {JOB_SLUG}

TradingView India — Daytrading — Strategies (listing pages 1 and 2).

## Outputs

- `records.jsonl` / `records.csv` — one row per strategy
- `manifest.json` — provenance

## SQLite

`{DB_PATH.relative_to(ROOT)}` table `tv_strategies`.

## Re-run

`py -3.13 scripts/scrape_tradingview_daytrading_strategies.py`
"""
    (OUT_DIR / "README.md").write_text(readme, encoding="utf-8")
    print(
        f"Done. records={len(records)} bundle={OUT_DIR} db={DB_PATH} errors={len(errors)}"
    )


if __name__ == "__main__":
    main()
