#!/usr/bin/env python3
"""
Gatik Fleet Watch - daily scraper / agent.

Captures the public "Live Operations" board from https://www.gatik.ai/ and
APPENDS new trip rows to data/history.json (an append-only archive).

The board is rendered by JavaScript and may be laid out as a <table> OR as
<div> rows, and a "Load more" button reveals extra rows. So we drive a real
headless browser (Playwright), WAIT until truck rows actually appear, expand
everything, then extract rows two ways:
    1. structured <table> parsing (preferred), and if that yields nothing,
    2. pattern parsing of the page text (truck id + 2 times + driving time + status).

Dedup key = capture_date + truck_id + start_time + end_time.

Run locally:
    pip install -r requirements.txt
    python -m playwright install chromium
    python scraper.py            # DEBUG=1 for verbose logging + HTML dump
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
DEBUG_TXT = DATA_DIR / "_debug_last_text.txt"

HEADER_MAP = {
    "truck": "truck_id", "start time": "start_time", "end time": "end_time",
    "driving time": "driving_time", "stops": "stops", "status": "status",
}

# Status words we recognise on the board (longest first so "On Time" beats "Time").
STATUS_WORDS = ["On Time", "In Transit", "En Route", "Completed", "Loading",
                "Departed", "Arrived", "Delayed", "Parked", "Ready", "Active"]

TRUCK_RE = r"G-\d{2,4}[A-Z]"
TIME_RE = r"\d{1,2}:\d{2}\s*[AP]M"
DRIVE_RE = r"\d{1,2}:\d{2}"


def log(*a): print(*a, flush=True)
def dbg(*a):
    if DEBUG: print("[debug]", *a, flush=True)


def parse_driving_minutes(text):
    if not text: return 0
    m = re.search(r"(\d+):(\d+)", text)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def clean(t): return re.sub(r"\s+", " ", (t or "")).strip()


def expand_all_rows(page):
    for i in range(60):
        btn = page.query_selector(
            "xpath=//*[self::button or self::a or self::div or self::span]"
            "[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]"
        )
        if not btn:
            dbg(f"no more 'Load more' after {i} clicks"); break
        try:
            btn.scroll_into_view_if_needed(timeout=4000)
            btn.click(timeout=4000)
            page.wait_for_timeout(900)
        except Exception as e:
            dbg(f"stopped expanding ({e})"); break


def extract_from_tables(page):
    rows = []
    for ti, table in enumerate(page.query_selector_all("table")):
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


def find_status(tail):
    # The "Status" column is the last one, so prefer the status word that
    # appears latest in the text after the driving time.
    best, best_pos = "", -1
    for w in STATUS_WORDS:
        for m in re.finditer(r"\b" + re.escape(w) + r"\b", tail, re.I):
            if m.start() > best_pos:
                best, best_pos = w, m.start()
    return best


def extract_from_text(text):
    """Resilient fallback: pull rows out of the rendered page text by pattern."""
    flat = clean(text)
    row_re = re.compile(
        rf"({TRUCK_RE})\s+({TIME_RE})\s+({TIME_RE})\s+({DRIVE_RE})\s*hrs", re.I)
    matches = list(row_re.finditer(flat))
    dbg(f"text fallback matched {len(matches)} rows")
    rows = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(flat)
        tail = flat[m.end():end]
        rows.append({
            "truck_id": m.group(1).upper(),
            "start_time": clean(m.group(2)),
            "end_time": clean(m.group(3)),
            "driving_time": f"{m.group(4)} hrs",
            "stops": "",
            "status": find_status(tail),
        })
    return rows


def scrape_rows():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        log(f"Loading {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)

        # Wait until actual truck rows show up in the rendered text.
        try:
            page.wait_for_function(
                f"() => /{TRUCK_RE}/.test(document.body.innerText)", timeout=45_000)
            dbg("truck rows detected in page text")
        except Exception:
            dbg("no truck rows appeared within 45s")

        # Nudge any lazy content and let the board settle.
        try:
            page.mouse.wheel(0, 2000); page.wait_for_timeout(1500)
        except Exception:
            pass

        expand_all_rows(page)

        rows = extract_from_tables(page)
        if not rows:
            dbg("no <table> rows - using text fallback")
            rows = extract_from_text(page.inner_text("body"))

        if not rows and DEBUG:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            DEBUG_HTML.write_text(page.content())
            DEBUG_TXT.write_text(page.inner_text("body"))
            dbg(f"no rows - dumped {DEBUG_HTML} and {DEBUG_TXT}")

        browser.close()
        return rows


def normalize(rows, captured_at, capture_date):
    out = []
    for r in rows:
        out.append({
            "captured_at": captured_at, "capture_date": capture_date,
            "truck_id": r.get("truck_id", ""),
            "start_time": r.get("start_time", ""),
            "end_time": r.get("end_time", ""),
            "driving_time": r.get("driving_time", ""),
            "driving_minutes": parse_driving_minutes(r.get("driving_time", "")),
            "stops": r.get("stops", ""), "status": r.get("status", ""),
        })
    return out


def row_key(r):
    return (r.get("capture_date"), r.get("truck_id"),
            r.get("start_time"), r.get("end_time"))


def main():
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
            "Download the 'debug-page' artifact to see what the page returned.")
        return 1

    history = []
    if HISTORY.exists():
        try:
            history = json.loads(HISTORY.read_text() or "[]")
        except json.JSONDecodeError:
            log("WARN: history.json unreadable - starting fresh")

    seen = {row_key(r) for r in history}
    added = [r for r in snapshot if row_key(r) not in seen]
    history.extend(added)

    HISTORY.write_text(json.dumps(history, indent=2))
    LATEST.write_text(json.dumps(snapshot, indent=2))
    log(f"Added {len(added)} new rows - archive now holds {len(history)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
