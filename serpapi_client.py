"""SerpApi client (Google Travel Explore + Google Flights Deals), normalizer and SQLite store."""
import json
import re
import sqlite3
from datetime import date, datetime, timezone

import requests

import config

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"  # free; does not use a search


class BudgetExceeded(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SerpApiClient:
    def __init__(self, api_key: str):
        if not api_key or "paste" in api_key:
            raise SystemExit("SERPAPI_API_KEY is missing from .env")
        self.api_key = api_key
        self.searches_made = 0
        config.SERPAPI_RAW_DIR.mkdir(parents=True, exist_ok=True)

    def account(self) -> dict:
        a = requests.get(ACCOUNT_URL, params={"api_key": self.api_key}, timeout=30).json()
        return {k: a.get(k) for k in ("plan_name", "plan_searches_left", "total_searches_left",
                                      "searches_per_month", "this_month_usage",
                                      "plan_renewal_date", "this_hour_searches")}

    def search(self, label: str, params: dict) -> tuple[dict, str]:
        """One billable search. Returns (body, raw_file_name)."""
        if self.searches_made >= config.SERPAPI_MAX_SEARCHES_PER_RUN:
            raise BudgetExceeded(f"SERPAPI_MAX_SEARCHES_PER_RUN={config.SERPAPI_MAX_SEARCHES_PER_RUN}")
        self.searches_made += 1
        resp = requests.get(SEARCH_URL, params={**params, "api_key": self.api_key}, timeout=120)
        try:
            body = resp.json()
        except ValueError:
            body = {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        body.get("search_metadata", {}).pop("json_endpoint", None)  # contains nothing secret, but is noise
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]', '_', label)}.json"
        (config.SERPAPI_RAW_DIR / fname).write_text(
            json.dumps({"label": label, "params": params, "status": resp.status_code, "body": body},
                       indent=1, ensure_ascii=False), encoding="utf-8")
        return body, fname


# ---------------------------------------------------------------------------
# Normalization: one row per destination AIRPORT per search.
# (Explore lists several "destinations" per airport, e.g. Denver + Rocky Mountain NP -> DEN.)
# ---------------------------------------------------------------------------
# The two engines number their trip-length presets differently.
_TRIP_KINDS = {
    "google_travel_explore": {1: "weekend", 2: "1 week", 3: "2 weeks"},
    "google_flights_deals": {1: "1 week", 2: "weekend", 3: "2 weeks"},
}


def trip_kind(params: dict) -> str:
    """'weekend' / '1 week' / '2 weeks' (both engines default to 1 week)."""
    return _TRIP_KINDS.get(params.get("engine"), {}).get(int(params.get("travel_duration") or 0), "1 week")


def normalize(body: dict, label: str, params: dict) -> tuple[list[dict], dict]:
    meta = body.get("search_metadata", {})
    observed_at = _meta_time(meta.get("created_at")) or now_utc()
    engine = params.get("engine", "")
    items = body.get("destinations") or body.get("deals") or []
    rows, by_airport = [], {}
    for it in items:
        price = it.get("flight_price", it.get("price"))
        airport = (it.get("destination_airport") or {}).get("code") or it.get("arrival_airport_code")
        if price is None or not airport:
            continue
        if airport in by_airport:
            by_airport[airport]["destination_name"] += " / " + it.get("name", "")
            continue
        dep = it.get("start_date") or it.get("outbound_date") or ""
        ret = it.get("end_date") or it.get("return_date") or ""
        trip_length = (date.fromisoformat(ret) - date.fromisoformat(dep)).days if dep and ret else None
        row = {
            "observed_at": observed_at,
            "source": engine,
            "search_label": label,
            "search_id": meta.get("id", ""),
            "origin": params.get("departure_id", ""),
            "destination": airport,
            "destination_name": it.get("name", ""),
            "country": it.get("country", ""),
            "region": params.get("_region", "default"),
            "departure_date": dep,
            "return_date": ret,
            "trip_length": trip_length,
            "trip_kind": trip_kind(params),
            "price": float(price),
            "currency": params.get("currency", "USD"),
            "airline": it.get("airline", ""),
            "stops": it.get("number_of_stops", it.get("stops")),
            "flight_duration_min": it.get("flight_duration"),
            "typical_price": it.get("average_price"),       # Deals engine only (Google's opinion)
            "discount_pct": it.get("discount_percentage"),  # Deals engine only
            "hotel_price": it.get("hotel_price"),
            "google_link": it.get("flight_link") or it.get("link", ""),
        }
        by_airport[airport] = row
        rows.append(row)
    summary = {"items": len(items), "priced_airports": len(rows), "error": body.get("error", "")}
    return rows, summary


def _meta_time(s):
    # "2026-09-28 01:47:06 UTC"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    run_at TEXT, label TEXT, engine TEXT, origin TEXT, region TEXT,
    params TEXT, serpapi_search_id TEXT UNIQUE, raw_file TEXT,
    items INTEGER, priced_airports INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS fare_observations (
    id INTEGER PRIMARY KEY,
    search_row_id INTEGER REFERENCES searches(id),
    observed_at TEXT, source TEXT, search_label TEXT, origin TEXT, destination TEXT,
    destination_name TEXT, country TEXT, region TEXT, departure_date TEXT, return_date TEXT,
    trip_length INTEGER, trip_kind TEXT, price REAL, currency TEXT, airline TEXT, stops INTEGER,
    flight_duration_min INTEGER, typical_price REAL, discount_pct REAL, hotel_price REAL,
    google_link TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_route ON fare_observations(origin, destination, source);
"""

OBS_COLS = ["observed_at", "source", "search_label", "origin", "destination", "destination_name",
            "country", "region", "departure_date", "return_date", "trip_length", "trip_kind", "price", "currency",
            "airline", "stops", "flight_duration_min", "typical_price", "discount_pct", "hotel_price",
            "google_link"]


def connect() -> sqlite3.Connection:
    config.SERPAPI_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.SERPAPI_DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Add columns introduced after the database was created (existing rows were all 1-week trips)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(fare_observations)")}
    if "trip_kind" not in cols:
        con.execute("ALTER TABLE fare_observations ADD COLUMN trip_kind TEXT")
        con.execute("UPDATE fare_observations SET trip_kind = '1 week' WHERE trip_kind IS NULL")
        con.commit()


def store(con, label, params, body, raw_file) -> tuple[int, dict]:
    """Save one search + its observations. Returns (rows inserted, summary). Skips duplicates."""
    rows, summary = normalize(body, label, params)
    sid = body.get("search_metadata", {}).get("id") or f"{raw_file}"
    clean_params = {k: v for k, v in params.items() if not k.startswith("_")}
    try:
        cur = con.execute(
            "INSERT INTO searches (run_at, label, engine, origin, region, params, serpapi_search_id,"
            " raw_file, items, priced_airports, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rows[0]["observed_at"] if rows else now_utc(), label, params.get("engine"),
             params.get("departure_id"), params.get("_region", "default"), json.dumps(clean_params),
             sid, raw_file, summary["items"], summary["priced_airports"], summary["error"]))
    except sqlite3.IntegrityError:
        return 0, summary  # this search was already stored
    search_row_id = cur.lastrowid
    con.executemany(
        f"INSERT INTO fare_observations (search_row_id, {', '.join(OBS_COLS)}) "
        f"VALUES (?, {', '.join('?' for _ in OBS_COLS)})",
        [(search_row_id, *[r[c] for c in OBS_COLS]) for r in rows])
    con.commit()
    return len(rows), summary
