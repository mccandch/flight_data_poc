# Flight Data POC — SLC & PVU

This project tests whether free data sources can drive a personal flight-deal finder for SLC and PVU.

| Source | Status |
|---|---|
| **SerpApi** (Google Travel Explore + Google Flights Deals) | **Active.** Free plan: 250 searches/month. Each search returns ~30–47 destination airports with prices |
| Travelpayouts / Aviasales Data API | **Blocked.** An API token needs a verified website with their script installed. The code is kept (`main.py`) in case that changes |

For every source evaluated, why it was ruled out, and the **possible fallback of scraping
Google Flights ourselves**, see [DATA_SOURCES.md](DATA_SOURCES.md).

## Setup

1. Make a free account at https://serpapi.com and copy your private API key.
2. Put the key in `.env` in this folder: `SERPAPI_API_KEY=...` (see `.env.example`).
3. Run `pip install -r requirements.txt`.

## Daily use

```
python serpapi_collect.py --dry-run   # show today's plan and remaining searches; uses none
python serpapi_collect.py             # run today's searches (4-5), store them, print the report
python serpapi_report.py              # re-print the report; uses no searches
```

**Automated (cloud):** GitHub Actions runs the collector every day at about 7:17 AM Mountain
(6:17 AM in winter; GitHub can start it late). The workflow is `.github/workflows/daily_scan.yml`.
It commits the updated `output/serpapi/fares.db` back to the repo, and Streamlit Cloud redeploys
the app from that commit. The SerpApi key is the repo secret `SERPAPI_API_KEY`.
- Run it by hand: repo → Actions → "Daily flight scan" → Run workflow, or `gh workflow run daily_scan.yml`.
- Raw responses, `report.txt` and the CSV are not committed. Each run keeps them for 30 days as a
  downloadable artifact on its Actions page.
- Get the latest data locally with `git pull` (`run_app.bat` does this for you).

**Local fallback:** the Windows Task Scheduler task "FlightDealCollector" (`run_daily.py`, 8:00 AM)
did this job before the move to GitHub and is now disabled. Keep it disabled while the cloud scan
is on: both spend from the same 250 searches, and a local run changes `fares.db`, which then
conflicts with the cloud's copy. Re-enable it with `Enable-ScheduledTask FlightDealCollector`.

Run the collector **once a day**. Searches already done today (Mountain time) are skipped, so a
second run doesn't use more searches.

**What each day's run searches:**
- The SLC and PVU "anywhere" searches: 1-week round trips over the next ~6 months, any number of stops.
- The SLC Google Deals search.
- One international region for SLC, rotating daily: Europe, Asia, Hawaii, Caribbean, South America, Oceania.
- One extra SLC variant, rotating daily: Outdoors, Canada, Skiing, Beaches. Each returns a
  different curated list of places. Google Explore only lists about 40 priced airports per
  search, so these variants find destinations the default search misses.
- The PVU Google Deals search, every other day.
- Other trip lengths (lowest priority, dropped first if the budget is tight), alternating days:
  - even days: SLC **weekend** trips (US / default area)
  - odd days: SLC **2-week** trips to one international region (rotating Europe, Asia, Caribbean,
    South America, Oceania) and PVU **weekend** trips

That's about 210-217 searches/month, under the 225 usable (250 free minus the 25-search reserve).

Every fare is tagged with its trip length (`trip_kind`: weekend / 1 week / 2 weeks). "Our typical",
the alerts and the price charts compare each trip length only with itself. Google's "usual" price
comes from 1-week Deals results, so only 1-week fares get a "vs Google" figure.

**Safety limits:**
- Before each run, the collector checks your remaining searches using SerpApi's free account endpoint.
- It never goes below 25 remaining (`SERPAPI_MIN_SEARCHES_LEFT`).
- It never makes more than 10 searches in one run (`SERPAPI_MAX_SEARCHES_PER_RUN`).

## Flight browser (UI)

Double-click **Flight Browser** on the desktop, or `run_app.bat`. It opens http://localhost:8501.
It's a read-only Streamlit view of `fares.db`:
- **Fares:** the latest cheapest fare per destination, with filters and Google Flights links.
  Select a row to see that destination's price history.
- **Map**, **Price history**, **Google Deals**, and **Collection status** (searches, balance check).

The UI runs from its own environment (`.venv`: streamlit, pandas) so it can't affect other Python
projects. The collector needs only `requests`.

**Cloud version:** the same `app.py` is deployed on Streamlit Community Cloud from this repo
(main file `app.py`). Its only secret is `SERPAPI_API_KEY`, used by the balance check button;
set it under the app's Settings → Secrets as `SERPAPI_API_KEY = "..."`.

## Email alerts

After each daily cloud scan, `alerts.py` emails the fares that are **at least 50% below** either:
1. **Our typical price**: the destination's median over its previous days (needs 5+ days of history).
2. **Google's "usual" price**: from Google's Deals list.

The email has one section per category; a fare that qualifies for both shows up in both. A fare
already emailed isn't sent again for 14 days unless its price drops at least another 5%. Days
with nothing new send no email. Thresholds are in `config.py` (`ALERT_*`).

**Setup (GitHub repo → Settings → Secrets and variables → Actions):**
| Secret | Value |
|---|---|
| `GMAIL_USER` | the Gmail address that sends the alerts |
| `GMAIL_APP_PASSWORD` | a Gmail app password (Google Account → Security → 2-Step Verification → App passwords) |
| `ALERT_EMAIL_TO` | optional; where to send (defaults to `GMAIL_USER`; comma-separate several) |

`python alerts.py --dry-run` writes `output/alerts_preview.html` and sends/records nothing.

## Output (`output/serpapi/`)

| File | Contents |
|---|---|
| `fares.db` | SQLite: `searches` and `fare_observations` tables. This builds the price history |
| `fare_observations.csv` | The same observations as CSV |
| `report.txt` | Coverage, cheapest fares by region, experimental deals, search efficiency |
| `raw/*.json` | Every SerpApi response exactly as received |

## Deal detection (EXPERIMENTAL)

- **Our own:** today's cheapest price to a destination compared with that destination's own
  history. A fare is flagged at ≤ 80% of its median, or when it's a new record low. This needs
  at least 5 prior days of data first.
- **Google's opinion:** the Deals engine's "usually $X, Y% off". It's shown only for comparison,
  never used as our score.
