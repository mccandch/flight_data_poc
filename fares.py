"""Shared fare calculations for the app (app.py) and the email alerts (alerts.py).
Pandas only (no Streamlit), so the daily cloud scan can import it too."""
import json
import sqlite3
from urllib.parse import quote

import pandas as pd

import config

MIN_HISTORY_DAYS = 5   # prior days of history a destination needs before "vs our typical" is shown
ALASKA_TZ = ("America/Anchorage", "America/Juneau", "America/Sitka", "America/Nome",
             "America/Yakutat", "America/Metlakatla")
US_TERRITORIES_CARIBBEAN = {"Puerto Rico", "U.S. Virgin Islands"}


def load(db_path=None):
    con = sqlite3.connect(f"file:{db_path or config.SERPAPI_DB}?mode=ro", uri=True)
    obs = pd.read_sql("SELECT * FROM fare_observations", con)
    searches = pd.read_sql("SELECT * FROM searches", con)
    con.close()
    airports = {a["code"]: a for a in json.loads(
        (config.REFERENCE_DIR / "airports.json").read_text(encoding="utf-8"))}

    obs["observed"] = local_time(obs["observed_at"])
    obs["day"] = obs["observed"].dt.date
    obs["dep"] = pd.to_datetime(obs["departure_date"], errors="coerce")
    obs["ret"] = pd.to_datetime(obs["return_date"], errors="coerce")
    obs["city"] = obs["destination_name"].str.split(" / ").str[0]
    obs["region_group"] = _region_groups(obs, airports)
    obs["lat"] = obs["destination"].map(lambda c: (airports.get(c) or {}).get("coordinates", {}).get("lat"))
    obs["lon"] = obs["destination"].map(lambda c: (airports.get(c) or {}).get("coordinates", {}).get("lon"))
    obs["gf_link"] = [gf_url(o, d, a, b) for o, d, a, b in
                      zip(obs["origin"], obs["destination"], obs["departure_date"], obs["return_date"])]
    searches["run"] = local_time(searches["run_at"])
    return obs, searches


def local_time(utc_strings: pd.Series) -> pd.Series:
    """UTC ISO strings -> naive Mountain-time timestamps (the cloud server's own clock is UTC)."""
    return pd.to_datetime(utc_strings, utc=True).dt.tz_convert(config.LOCAL_TZ_NAME).dt.tz_localize(None)


def _region_groups(obs: pd.DataFrame, airports: dict) -> pd.Series:
    """One display region per destination airport."""
    searched = (obs[~obs["region"].isin(["default", "Hawaii", "United States"])]
                .groupby("destination")["region"].agg(lambda s: s.mode().iat[0]))
    groups = {}
    for dest, country in obs[["destination", "country"]].drop_duplicates("destination").itertuples(index=False):
        tz = (airports.get(dest) or {}).get("time_zone", "")
        if tz == "Pacific/Honolulu":
            g = "Hawaii"
        elif country == "United States":
            g = "Alaska" if tz in ALASKA_TZ else "Domestic"
        elif country in US_TERRITORIES_CARIBBEAN:
            g = "Caribbean"
        elif dest in searched.index:
            g = searched[dest]
        elif country in ("Canada", "Mexico"):
            g = "Canada & Mexico"
        else:
            g = "Other international"
        groups[dest] = "Canada & Mexico" if g == "Canada" else g
    return obs["destination"].map(groups)


def gf_url(origin, dest, dep, ret) -> str:
    if not dep:
        return ""
    q = f"Flights to {dest} from {origin} on {dep}" + (f" through {ret}" if ret else " oneway")
    return "https://www.google.com/travel/flights?q=" + quote(q)


def daily_min(obs: pd.DataFrame) -> pd.DataFrame:
    """Cheapest price per origin/destination/day (several searches can see the same airport)."""
    idx = obs.groupby(["origin", "destination", "day"])["price"].idxmin()
    return obs.loc[idx]


def latest_fares(obs: pd.DataFrame) -> pd.DataFrame:
    """The most recent cheapest fare per origin/destination, plus:
    vs_typical  price vs the median of this destination's PREVIOUS days (needs MIN_HISTORY_DAYS of them)
    vs_google   price vs Google's 'usual price' from the most recent Deals list that included it."""
    dm = daily_min(obs)
    latest = dm.loc[dm.groupby(["origin", "destination"])["day"].idxmax()].copy()

    last_day = latest[["origin", "destination", "day"]].rename(columns={"day": "last_day"})
    prior = dm.merge(last_day, on=["origin", "destination"])
    prior = prior[prior["day"] < prior["last_day"]]
    prior_stats = prior.groupby(["origin", "destination"]).agg(
        prior_days=("day", "nunique"), median_price=("price", "median"))
    all_stats = dm.groupby(["origin", "destination"]).agg(
        days_tracked=("day", "nunique"), lowest_seen=("price", "min"))
    latest = latest.join(all_stats, on=["origin", "destination"]).join(prior_stats, on=["origin", "destination"])
    latest["prior_days"] = latest["prior_days"].fillna(0).astype(int)
    enough = latest["prior_days"] >= MIN_HISTORY_DAYS
    latest["vs_typical"] = (latest["price"] / latest["median_price"] - 1).where(enough)

    google = (obs[obs["source"] == "google_flights_deals"].sort_values("observed")
              .groupby(["origin", "destination"]).agg(google_usual=("typical_price", "last"),
                                                       google_usual_day=("day", "last")))
    latest = latest.drop(columns=["typical_price", "discount_pct"]).join(google, on=["origin", "destination"])
    latest["vs_google"] = latest["price"] / latest["google_usual"] - 1
    latest["nights"] = (latest["ret"] - latest["dep"]).dt.days
    return latest


def stops_label(s) -> str:
    if pd.isna(s):
        return "?"
    return "Nonstop" if s == 0 else f"{int(s)} stop" + ("s" if s > 1 else "")
