#!/usr/bin/env python3
"""
Gatik Fleet Watch - daily scraper / agent.

Captures the public "Live Operations" table from https://www.gatik.ai/ and
APPENDS new trip rows to data/history.json (an append-only archive).

The table is rendered by JavaScript and has a "Load more" button, so we drive a
real headless browser (Playwright) to render the page, expand every row, and read
the table out of the DOM. A plain HTTP request is NOT reliable for this site.

Dedup key = capture_date + truck_id + start_time + end_time, so running more than
once a day never creates duplicates, while the same truck/time on a NEW day is
archived as a new record.

Run locally:
    pip install -r requirements.txt
    python -m playwright install chromium
    python scraper.py

Env vars:
    DEBUG=1     extra logging + dumps the page HTML to data/_debug_last_page.html
                when no rows are found (handy for fixing selectors)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

URL = os.environ.get("GATIK_URL", "https://www.gatik.ai/")
DEBUG = os.environ.get("DEBUG", "") not in ("", "0", "false", "False")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
HISTORY = DATA_DIR / "history.json"
LATEST = DATA_DIR / "latest.json"
DEBUG_HTML = DATA_DIR / "_debug_last_page.html"

# Header text on the page -> canonical field name we store.
HEADER_MAP = {
    "truck": "truck_id",
    "start time": "start_time",
    "end time": "end_time",
    "driving time": "driving_time",
    "stops": "stops",
    "status": "status",
}


def log(*a):
    print(*a, flush=True)


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, flush=True)


def parse_driving_minutes(text: str) -> int:
    """'0:52 hrs' -> 52 ; '1:08 hrs' -> 68."""
    if not text:
        return 0
    m = re.search(r"(\d+):(\d+)", text)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def expand_all_rows(page):
    """Click every 'Load more' control until none remain."""
    for i in range(60):  # hard cap so we can never loop forever
        btn = page.query_selector(
            "xpath=//*[self::button or self::a or self::div or self::span]"
            "[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]"
        )
        if not btn:
            dbg(f"no more 'Load more' after {i} clicks")
            break
        try:
            btn.scroll_into_view_if_needed(timeout=4000)
            btn.click(timeout=4000)
            page.wait_for_timeout(900)
        except Exception as e:
            dbg(f"stopped expanding ({e})")
            break


def extract_from_tables(page) -> list[dict]:
    rows: list[dict] = []
    tables = page.query_selector_all("table")
    dbg(f"{len(tables)} table(s) on page")
    for ti, table in enumerate(tables):
        headers = [clean(h.inner_text()).lower()
                   for h in table.query_selector_all("thead th, thead td")]
        dbg(f"table {ti} headers: {headers}")
        keys = [HEADER_MAP.get(h) for h in headers]
        if "truck_id" not in keys:
            continue
        for tr in table.query_selector_all("tbody tr"):
            cells = tr.query_selector_all("td, th")
            rec = {}
            for i, cell in enumerate(cells):
                key = keys[i] if i < len(keys) else None
                if key:
                    rec[key] = clean(cell.inner_text())
            if rec.get("truck_id"):
                rows.append(rec)
        if rows:
            return rows
    return rows


def scrape_rows() -> list[dict]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        log(f"Loading {URL} ...")
        page.goto(URL, wait_until="networkidle", timeout=60_000)

        # Give the live board a chance to render; don't hard-fail if absent.
        try:
            page.wait_for_selector("table tbody tr", timeout=30_000)
        except Exception:
            dbg("no 'table tbody tr' appeared within 30s")

        expand_all_rows(page)
        rows = extract_from_tables(page)

        if not rows and DEBUG:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            DEBUG_HTML.write_text(page.content())
            dbg(f"no rows found - dumped page HTML to {DEBUG_HTML}")

        browser.close()
        return rows


def normalize(rows, captured_at, capture_date):
    out = []
    for r in rows:
        out.append({
            "captured_at": captured_at,
            "capture_date": capture_date,
            "truck_id": r.get("truck_id", ""),
            "start_time": r.get("start_time", ""),
            "end_time": r.get("end_time", ""),
            "driving_time": r.get("driving_time", ""),
            "driving_minutes": parse_driving_minutes(r.get("driving_time", "")),
            "stops": r.get("stops", ""),
            "status": r.get("status", ""),
        })
    return out


def row_key(r):
    return (r.get("capture_date"), r.get("truck_id"),
            r.get("start_time"), r.get("end_time"))


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    captured_at = now.isoformat(timespec="seconds")
    capture_date = now.strftime("%Y-%m-%d")

    try:
        raw = scrape_rows()
    except Exception as e:
        log(f"ERROR while scraping: {e}")
        return 1

    snapshot = normalize(raw, captured_at, capture_date)
    log(f"Captured {len(snapshot)} rows at {captured_at}")

    if not snapshot:
        log("No rows captured - archive left unchanged. "
            "Re-run with DEBUG=1 to inspect the page.")
        return 1

    history = []
    if HISTORY.exists():
        try:
            history = json.loads(HISTORY.read_text() or "[]")
        except json.JSONDecodeError:
            log("WARN: history.json unreadable - starting a fresh archive")

    seen = {row_key(r) for r in history}
    added = [r for r in snapshot if row_key(r) not in seen]
    history.extend(added)

    HISTORY.write_text(json.dumps(history, indent=2))
    LATEST.write_text(json.dumps(snapshot, indent=2))
    log(f"Added {len(added)} new rows - archive now holds {len(history)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
