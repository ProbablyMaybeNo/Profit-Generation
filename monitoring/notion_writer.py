"""
notion_writer.py — Direct Notion REST API client for posting daily reports.

No MCP dependency. Uses a Notion integration token from credentials.json.

Setup (one-time):
  1. https://www.notion.so/profile/integrations -> "+ New integration" -> internal
  2. Copy the Internal Integration Token
  3. Open the Trading Daily Reports DB in Notion -> Share -> add the integration
  4. Same for Trading Patterns & Insights DB
  5. Add to config/credentials.json:
       "notion": {
         "integration_token": "secret_..."
       }
"""

import json
import re
from typing import Dict, List, Optional

import requests

from config.utils import load_credentials


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> Dict[str, str]:
    all_creds = load_credentials()
    section = None
    for k in ("notion", "Notion", "NOTION"):
        if k in all_creds:
            section = all_creds[k]
            break
    if not section:
        raise RuntimeError(
            "credentials.json missing 'notion' (or 'Notion') section. "
            "Create at https://www.notion.so/profile/integrations and "
            "share the Trading Daily Reports + Patterns DBs with it."
        )
    token = (
        section.get("integration_token")
        or section.get("api_key")
        or section.get("token")
    )
    if not token:
        raise RuntimeError(
            f"notion section present but missing token. Found keys: {list(section.keys())}. "
            "Set one of: integration_token, api_key, token."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


_INLINE_PATTERN = re.compile(
    r"(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*]+)\*)"
)


def _parse_inline(text: str) -> List[Dict]:
    """Parse **bold**, `code`, *italic* into rich_text spans with annotations."""
    if not text:
        return []
    out: List[Dict] = []
    pos = 0
    for m in _INLINE_PATTERN.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": {"content": text[pos:m.start()]}})
        bold, code, italic = m.group(2), m.group(3), m.group(4)
        if bold is not None:
            out.append({"type": "text", "text": {"content": bold},
                        "annotations": {"bold": True}})
        elif code is not None:
            out.append({"type": "text", "text": {"content": code},
                        "annotations": {"code": True}})
        elif italic is not None:
            out.append({"type": "text", "text": {"content": italic},
                        "annotations": {"italic": True}})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": {"content": text[pos:]}})
    if not out:
        out.append({"type": "text", "text": {"content": text}})
    return out


def _rich_text(text: str) -> List[Dict]:
    """Notion rich_text array. Parses inline markdown (bold/italic/code).
    Splits any single span > 1900 chars into chunks."""
    spans = _parse_inline(text or "")
    out: List[Dict] = []
    for span in spans:
        content = span["text"]["content"]
        if len(content) <= 1900:
            out.append(span)
            continue
        for i in range(0, len(content), 1900):
            chunk = dict(span)
            chunk["text"] = dict(span["text"])
            chunk["text"]["content"] = content[i:i + 1900]
            out.append(chunk)
    return out


def _is_separator_row(cells: List[str]) -> bool:
    return all(re.fullmatch(r"\s*:?-{3,}:?\s*", c) for c in cells)


def _build_table_block(rows: List[List[str]]) -> Optional[Dict]:
    """Convert a markdown table (list of cell-rows) into a Notion table block."""
    cleaned = [r for r in rows if not _is_separator_row(r)]
    if not cleaned:
        return None
    width = max(len(r) for r in cleaned)
    table_children = []
    for row in cleaned:
        cells = []
        for i in range(width):
            cell_text = row[i].strip() if i < len(row) else ""
            cells.append(_parse_inline(cell_text))
        table_children.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": cells},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": table_children,
        },
    }


def _markdown_to_blocks(md: str) -> List[Dict]:
    """
    Minimal markdown -> Notion blocks converter.
    Handles: # / ## / ### headings, bullets (-, *), tables (-> heading + paragraphs),
    code blocks (```), blank lines, plain paragraphs.

    Tables are rendered as a heading + bulleted list of rows for readability,
    since Notion tables via API are heavy (each cell is its own block).
    """
    blocks: List[Dict] = []
    lines = md.split("\n")
    i = 0
    in_table = False
    table_rows: List[List[str]] = []
    in_code = False
    code_buf: List[str] = []

    def flush_table():
        nonlocal table_rows, in_table
        if not table_rows:
            in_table = False
            return
        block = _build_table_block(table_rows)
        if block is not None:
            blocks.append(block)
        table_rows = []
        in_table = False

    def flush_code():
        nonlocal code_buf, in_code
        if code_buf:
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": _rich_text("\n".join(code_buf)),
                    "language": "plain text",
                },
            })
        code_buf = []
        in_code = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                flush_code()
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c for c in stripped.strip("|").split("|")]
            if not in_table:
                in_table = True
                table_rows = []
            table_rows.append(cells)
            i += 1
            continue
        elif in_table:
            flush_table()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": _rich_text(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": _rich_text(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped.startswith("---"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif stripped.startswith(("- ", "* ")):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich_text(stripped[2:])}})
        elif re.match(r"^\d+\.\s", stripped):
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": _rich_text(re.sub(r"^\d+\.\s", '', stripped))}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _rich_text(stripped)}})
        i += 1

    if in_table:
        flush_table()
    if in_code:
        flush_code()
    return blocks


def _build_properties(report) -> Dict:
    """Build Notion API property payload for a Daily Report row."""
    return {
        "Report": {
            "title": [{"text": {"content": f"Daily Report — {report.report_date.isoformat()}"}}]
        },
        "Date": {"date": {"start": report.report_date.isoformat()}},
        "Market Regime": {"select": {"name": report.market_regime}},
        "Importance": {"number": int(report.importance)},
        "Has Notable Pattern": {"checkbox": bool(report.has_notable_pattern)},
        "Watchlist Count": {"number": len(report.symbols_watched)},
        "Strategy Fires": {"number": len(report.fires)},
        "Symbols Watched": {
            "multi_select": [{"name": s} for s in report.symbols_watched]
        },
        "Tags": {"multi_select": [{"name": t} for t in report.tags]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "auto-scan"}},
    }


# Notion's create-page and append-children endpoints both cap `children`
# at 100 blocks per call. PG-010 fix: split long reports into chunks and
# PATCH the remaining blocks onto the freshly-created page.
NOTION_BLOCKS_PER_CALL = 100


def _append_blocks_to_page(page_id: str, blocks: List[Dict]) -> None:
    """PATCH up to NOTION_BLOCKS_PER_CALL blocks at a time onto a page.
    Used to extend daily reports beyond the 100-block create-page cap."""
    if not blocks:
        return
    for start in range(0, len(blocks), NOTION_BLOCKS_PER_CALL):
        chunk = blocks[start:start + NOTION_BLOCKS_PER_CALL]
        url = f"{NOTION_API}/blocks/{page_id}/children"
        r = requests.patch(url, headers=_headers(),
                           json={"children": chunk}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Notion API {r.status_code} appending blocks: {r.text[:500]}"
            )


def post_daily_report(report, markdown: str, database_id: str) -> Dict:
    """Create a new page in the Trading Daily Reports DB.

    PG-010 (3.5.1): long reports are paginated. The first 100 blocks ship
    in the create-page call; remaining blocks are PATCHed onto the page
    via `_append_blocks_to_page`. The returned dict is the create-page
    response (so callers still get `id`, `url`, etc.).
    """
    all_blocks = _markdown_to_blocks(markdown)
    first_chunk = all_blocks[:NOTION_BLOCKS_PER_CALL]
    remainder = all_blocks[NOTION_BLOCKS_PER_CALL:]
    body = {
        "parent": {"database_id": database_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4ca"},
        "properties": _build_properties(report),
        "children": first_chunk,
    }
    r = requests.post(f"{NOTION_API}/pages", headers=_headers(), json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:500]}")
    page = r.json()
    if remainder:
        page_id = page.get("id")
        if page_id:
            _append_blocks_to_page(page_id, remainder)
            page["appended_blocks"] = len(remainder)
    return page


def post_pattern(
    title: str,
    description: str,
    importance: str,
    pattern_type: str,
    symbols: List[str],
    status: str = "Observation",
    backtest_verdict: str = "UNTESTED",
    times_observed: int = 1,
    date_observed: Optional[str] = None,
    database_id: str = "a5013bd6-7c26-48a5-8029-ac101b9801bf",
) -> Dict:
    """Create a row in the Trading Patterns & Insights DB."""
    from datetime import date
    body = {
        "parent": {"database_id": database_id},
        "properties": {
            "Pattern": {"title": [{"text": {"content": title}}]},
            "Date Observed": {"date": {"start": date_observed or date.today().isoformat()}},
            "Importance": {"select": {"name": importance}},
            "Status": {"select": {"name": status}},
            "Pattern Type": {"select": {"name": pattern_type}},
            "Symbols": {"multi_select": [{"name": s} for s in symbols]},
            "Times Observed": {"number": times_observed},
            "Backtest Verdict": {"select": {"name": backtest_verdict}},
            "Description": {"rich_text": _rich_text(description)},
        },
    }
    r = requests.post(f"{NOTION_API}/pages", headers=_headers(), json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:500]}")
    return r.json()


def smoke_test() -> bool:
    """Confirm the integration token is valid and we can reach Notion."""
    r = requests.get(f"{NOTION_API}/users/me", headers=_headers(), timeout=10)
    return r.status_code == 200


if __name__ == "__main__":
    import sys
    if smoke_test():
        print("Notion API: OK")
    else:
        print("Notion API: FAIL — check integration token", file=sys.stderr)
        sys.exit(1)
