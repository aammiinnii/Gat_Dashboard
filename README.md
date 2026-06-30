# Fleet Watch - Gatik Autonomous Operations Archive

A small, free, self-running agent that captures Gatik's public **Live Operations**
board every day, archives every trip, and serves a website to search, filter, and
chart it.

```
GitHub Action (daily cron) -> scraper.py -> data/history.json (committed) -> index.html (GitHub Pages)
```

No server to run, nothing to pay for. GitHub runs the scraper on a schedule and
GitHub Pages hosts the website. `data/history.json` already contains a real
snapshot, so the site shows live data the moment you deploy it.

---

## Put it on GitHub (about 5 minutes)

1. **Create a new repository** on GitHub (e.g. `gatik-fleet-watch`). Public is
   fine; Pages is free on public repos.

2. **Upload these files.** Easiest way: on the empty repo page click
   **uploading an existing file**, then drag in everything from this folder
   (keep the folders intact - `.github/` and `data/` must stay as-is). Commit.
   *Git users:* `git init && git add . && git commit -m "init" && git push`.

3. **Allow the agent to commit.** Repo **Settings -> Actions -> General ->
   Workflow permissions** -> select **Read and write permissions** -> Save.

4. **Run it once now.** **Actions** tab -> *Scrape Gatik fleet data* ->
   **Run workflow**. Watch it go green, then open `data/history.json` - it should
   have today's rows. After this, it runs **automatically every day at 06:10 UTC**.

5. **Publish the website.** **Settings -> Pages** -> Source *Deploy from a branch*
   -> Branch `main` / `/ (root)` -> Save. Your site goes live at
   `https://<your-username>.github.io/<repo>/` within a minute or two.

That's the whole thing. Each daily run appends the new day's trips to the same
file, and the website updates itself.

---

## What gets captured

| Field | Example |
|---|---|
| `capture_date` | `2026-06-30` |
| `captured_at` | `2026-06-30T06:10:00+00:00` |
| `truck_id` | `G-001A` |
| `start_time` / `end_time` | `11:40 AM` / `12:48 PM` |
| `driving_time` / `driving_minutes` | `0:52 hrs` / `52` |
| `stops` | (when present) |
| `status` | `Completed` / `On Time` / `Ready` / `Loading` / `Parked` |

The public board doesn't publish a route/mission name or mileage, so those aren't
captured. If Gatik adds columns later, the scraper maps headers automatically -
see `HEADER_MAP` in `scraper.py`.

---

## Run it locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install chromium
python scraper.py            # add DEBUG=1 for verbose logging
```

Then serve the folder and open the site:

```bash
python -m http.server 8000   # then visit http://localhost:8000
```

---

## How it works / good to know

- **Why a browser, not a simple request?** Gatik's table is rendered by
  JavaScript and has a *Load more* button. A plain HTTP fetch often returns the
  page with no table at all, so the agent drives a headless browser (Playwright)
  to render it and expand every row. That's why the run is reliable.
- **No duplicates.** A trip is keyed by date + truck + start + end, so running
  more often than daily is safe.
- **If a run finds nothing**, it exits red and (because `DEBUG=1` in the workflow)
  uploads the page HTML as a build artifact called *debug-page*. Download it to
  see what the page returned, or send it over and the selectors can be adjusted.
- **Change the time** by editing the `cron` line in
  `.github/workflows/scrape.yml`.
- Independent archive of publicly displayed data; not affiliated with Gatik.
