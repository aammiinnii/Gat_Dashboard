#!/usr/bin/env python3
"""
Gatik Fleet Watch - daily scraper / agent.

Captures the public "Live Operations" board from https://www.gatik.ai/ and
APPENDS new trip rows to data/history.json (an append-only archive).

The board is JavaScript-rendered, may be a <table> OR <div> rows, and is often
lazy-loaded only when it scrolls into view. So we drive a real headless browser
(Playwright), scroll the WHOLE page to trigger loading, WAIT for truck rows to
appear, expand "Load more", then extract rows two ways:
    1. structured <table> parsing (preferred),
    2. pattern parsing of the page text (truck id + 2 times + driving time + status).

If nothing is found, the page's actual text is printed to the log so the cause
is visible without downloading anything.

Run locally:
    pip install -r requirements.txt
    python -m playwright install chromium
    python scraper.py            # DEBUG=1 for extra logging + HTML dump
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


def scroll_through_page(page):
    """Scroll top->bottom in steps so intersection-observer content loads."""
    try:
        height = page.evaluate("document.body.scrollHeight") or 4000
    except Exception:
        height = 4000
    y = 0
    while y < height + 2000:
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(450)
        y += 1200
        # stop early if rows already appeared
        try:
            if page.evaluate(f"() => /{TRUCK_RE}/.test(document.body.innerText)"):
                dbg(f"truck rows appeared after scrolling ~{y}px")
                break
        except Exception:
            pass
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def count_truck_rows(page):
    """How many truck rows are currently rendered (de-duplicated)."""
    try:
        return len(set(re.findall(TRUCK_RE, page.inner_text("body"))))
    except Exception:
        return 0


def load_more_locator(page):
    """The smallest visible element whose text is 'Load more'."""
    return page.get_by_text(re.compile(r"load\s*more", re.I)).first


def expand_all_rows(page):
    """Keep clicking 'Load more' until the list stops growing.

    Stops only when the button is gone OR several clicks in a row add no new
    rows — so it captures the COMPLETE list no matter how many pages there are.
    """
    prev = count_truck_rows(page)
    dbg(f"rows before expand: {prev}")
    stagnant = 0
    for i in range(500):  # generous cap; we really stop on no-growth
        loc = load_more_locator(page)
        try:
            if loc.count() == 0 or not loc.is_visible():
                dbg("no visible 'Load more' left — list fully expanded")
                break
        except Exception:
            break

        try:
            loc.scroll_into_view_if_needed(timeout=4000)
            loc.click(timeout=4000)
        except Exception as e:
            try:
                loc.evaluate("el => el.click()")   # JS fallback click
            except Exception:
                dbg(f"could not click 'Load more' ({e}) — stopping")
                break

        # Wait for new rows to appear after the click (up to ~7s).
        grew = False
        for _ in range(24):
            page.wait_for_timeout(300)
            now = count_truck_rows(page)
            if now > prev:
                prev = now
                grew = True
                break

        if grew:
            stagnant = 0
            dbg(f"after click {i + 1}: {prev} rows")
        else:
            stagnant += 1
            if stagnant >= 3:
                dbg(f"no new rows after {stagnant} clicks — assuming complete")
                break

    dbg(f"rows after expand: {prev}")


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
    best, best_pos = "", -1
    for w in STATUS_WORDS:
        for m in re.finditer(r"\b" + re.escape(w) + r"\b", tail, re.I):
            if m.start() > best_pos:
                best, best_pos = w, m.start()
    return best


def extract_from_text(text):
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
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            locale="en-US", timezone_id="America/Chicago")
        page = ctx.new_page()
        log(f"Loading {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            dbg("networkidle not reached in 30s (continuing)")

        scroll_through_page(page)
        try:
            page.wait_for_function(
                f"() => /{TRUCK_RE}/.test(document.body.innerText)", timeout=30_000)
            dbg("truck rows detected")
        except Exception:
            dbg("no truck rows after scrolling + wait")

        expand_all_rows(page)

        rows = extract_from_tables(page)
        if not rows:
            dbg("no <table> rows - using text fallback")
            rows = extract_from_text(page.inner_text("body"))

        if not rows:
            # Make the failure visible right in the log.
            body = page.inner_text("body")
            log("---- DIAGNOSTIC: no rows found ----")
            log(f"page title : {page.title()!r}")
            log(f"final url   : {page.url}")
            log(f"body length : {len(body)} chars")
            log(f"<table> count: {len(page.query_selector_all('table'))}")
            log("---- first 3000 chars of page text ----")
            log(body[:3000])
            log("---- end diagnostic ----")
            if DEBUG:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                DEBUG_HTML.write_text(page.content())
                DEBUG_TXT.write_text(body)

        ctx.close(); browser.close()
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
            "Copy the DIAGNOSTIC block above and send it for a fix.")
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
