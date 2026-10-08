"""POC settings. Secrets come from .env; everything else is plain constants."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE lines) so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")

# "Today" always means Mountain time, wherever the code runs (GitHub Actions and Streamlit
# Cloud are on UTC). Windows needs the tzdata package for this; without it we fall back to
# the machine's own time zone (None).
LOCAL_TZ_NAME = "America/Denver"
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except ZoneInfoNotFoundError:
    LOCAL_TZ = None

# --- What to scan -----------------------------------------------------------
ORIGINS = ["SLC", "PVU"]
CURRENCY = "usd"
MARKET = "us"                 # Aviasales data market; API default is "ru"
COMPARE_MARKETS = ["ru"]      # extra markets probed briefly to see if they return more/different data
MONTHS_AHEAD = 12             # discovery scan covers this many departure months
MARKET_PROBE_MONTHS = 3

# Calendar-depth probe: per-day pricing for the most-observed destinations
CALENDAR_TOP_DESTINATIONS = 10
CALENDAR_MONTHS = 3

# --- Politeness / safety ----------------------------------------------------
# Documented limits (Help Center "API rate limits", 2024-12): 60-600 req/min per method.
# We stay far below that.
REQUEST_DELAY_SECONDS = 0.35
MAX_API_CALLS_PER_RUN = 400   # hard stop so a bug can never hammer the API
MAX_PAGES = 5

# --- Experimental deal detector --------------------------------------------
MIN_ROUTE_OBSERVATIONS = 10   # don't judge a route with fewer fares than this
DEAL_MAX_RATIO_TO_MEDIAN = 0.75
DEAL_MAX_PERCENTILE = 10.0

# --- SerpApi (Google Travel Explore / Google Flights Deals) -----------------
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "")
SERPAPI_MIN_SEARCHES_LEFT = 25   # never let a run push the plan below this many remaining searches
SERPAPI_MAX_SEARCHES_PER_RUN = 10

# International regions (Google knowledge-graph ids) for SLC. One region per day, rotating.
# The default Explore search from SLC only shows the US/Canada/Mexico.
SERPAPI_REGIONS = {
    "Europe": "/m/02j9z",
    "Asia": "/m/0j0k",
    "Hawaii": "/m/03gh4",
    "Caribbean": "/m/0261m",
    "South America": "/m/06n3y",
    "Oceania": "/m/05nrg",
}

# Other trip lengths (lowest priority, so the budget guard drops them first), alternating days:
#   even days: SLC weekend trips (US / default area)
#   odd days:  SLC 2-week trips to one of these regions (rotating) + PVU weekend trips
# Adds ~1.5 searches/day -> ~210-217 a month in total, under the 225 usable (250 minus the reserve).
SERPAPI_2WK_REGIONS = ["Europe", "Asia", "Caribbean", "South America", "Oceania"]

# Extra SLC variants, one per day, rotating. Each returns a different curated list.
# Tested 2026-09-27: new airports vs the default+region searches -> Canada 14, Outdoors 13,
# Skiing 7, Beaches 6 (a "United States" area search added only 3, so it's not included).
SERPAPI_VARIANTS = {
    "Outdoors": {"interest": "/g/11bc58l13w"},
    "Canada": {"arrival_area_id": "/m/0d060g", "_region": "Canada"},
    "Skiing": {"interest": "/m/071k0"},
    "Beaches": {"interest": "/m/0b3yr"},
}

# --- Email alerts (alerts.py, run after the daily scan) -----------------------
# A fare is emailed when it's at least this far below either reference price.
ALERT_MAX_VS_TYPICAL = -0.50   # vs this destination's median over its previous days ("our typical")
ALERT_MAX_VS_GOOGLE = -0.50    # vs Google's "usual price" from its Deals list
# Don't re-send the same destination/category within this many days unless the price fell further.
ALERT_REPEAT_DAYS = 14
ALERT_REPEAT_MIN_DROP = 0.05   # re-send early only if >= 5% cheaper than the last alert

# --- Output -----------------------------------------------------------------
OUTPUT_DIR = BASE_DIR / "output"
RAW_DIR = OUTPUT_DIR / "raw"
NORMALIZED_DIR = OUTPUT_DIR / "normalized"
REFERENCE_DIR = OUTPUT_DIR / "reference"
SERPAPI_DIR = OUTPUT_DIR / "serpapi"
SERPAPI_RAW_DIR = SERPAPI_DIR / "raw"
SERPAPI_DB = SERPAPI_DIR / "fares.db"
