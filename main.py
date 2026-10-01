"""Run the Travelpayouts Data API proof-of-concept for SLC and PVU.

    python main.py            # collect + analyze + write report
    python main.py --dry-run  # show the request plan, make no API calls
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date

import config
from analyze_coverage import dedupe_key, run_analysis
from travelpayouts_client import (NORMALIZED_FIELDS, CallBudgetExceeded, TravelpayoutsClient,
                                  fetch_reference_data, normalize, now_utc)


def upcoming_months(n: int) -> list[str]:
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class Collector:
    def __init__(self, client: TravelpayoutsClient):
        self.client = client
        self.rows: list[dict] = []
        # per source label: calls, records returned, records kept (matching origin), empty responses
        self.stats = defaultdict(Counter)
        self.calendar_probe = defaultdict(dict)   # origin -> dest -> {source: distinct dates}

    def fetch(self, label: str, path: str, params: dict, origin: str, paged_limit: int | None = None):
        """Call an endpoint (optionally paging), normalize, keep rows departing from `origin`."""
        kept_all = []
        for page in range(1, config.MAX_PAGES + 1 if paged_limit else 2):
            p = dict(params, currency=config.CURRENCY)
            if paged_limit:
                p["page"] = page
            observed_at = now_utc()
            body = self.client.get(path, p)
            self.stats[label]["calls"] += 1
            rows = normalize(body, label, p, observed_at)
            kept = [r for r in rows if origin in (r["origin"], r["origin_airport"])]
            self.stats[label]["records"] += len(rows)
            self.stats[label]["kept"] += len(kept)
            if body is None:
                self.stats[label]["errors"] += 1
            elif not rows:
                self.stats[label]["empty"] += 1
            kept_all.extend(kept)
            data = (body or {}).get("data")
            if not paged_limit or not isinstance(data, list) or len(data) < paged_limit:
                break
        self.rows.extend(kept_all)
        return kept_all

    # --- Discovery: destination NOT specified -------------------------------
    def discover(self, origin: str):
        months = upcoming_months(config.MONTHS_AHEAD)
        mk = config.MARKET
        print(f"\n[{origin}] discovery scan ({len(months)} months, market={mk})")
        for month in months:
            for one_way in ("false", "true"):
                self.fetch(f"v3/prices_for_dates ({'OW' if one_way == 'true' else 'RT'})",
                           "/aviasales/v3/prices_for_dates",
                           dict(origin=origin, departure_at=month, one_way=one_way, direct="false",
                                sorting="price", unique="false", limit=1000, market=mk),
                           origin, paged_limit=1000)
            print(f"  {month}: {len(self.rows)} rows so far", flush=True)

        for one_way in ("false", "true"):
            self.fetch(f"v3/get_latest_prices ({'OW' if one_way == 'true' else 'RT'})",
                       "/aviasales/v3/get_latest_prices",
                       dict(origin=origin, period_type="year", group_by="dates", one_way=one_way,
                            sorting="price", limit=1000, show_to_affiliates="false", market=mk),
                       origin, paged_limit=1000)
        self.fetch("v3/search_by_price_range", "/aviasales/v3/search_by_price_range",
                   dict(origin=origin, destination="-", value_min=0, value_max=100000,
                        one_way="false", direct="false", limit=1000, market=mk),
                   origin, paged_limit=1000)
        self.fetch("v1/prices/cheap (deprecated)", "/v1/prices/cheap",
                   dict(origin=origin, market=mk), origin)
        self.fetch("v1/city-directions (deprecated)", "/v1/city-directions",
                   dict(origin=origin), origin)
        print(f"  other discovery endpoints done: {len(self.rows)} rows so far")

        # Does the data "market" change what we get? (API default is ru)
        for market in config.COMPARE_MARKETS:
            for month in months[:config.MARKET_PROBE_MONTHS]:
                self.fetch(f"v3/prices_for_dates (RT, market={market})",
                           "/aviasales/v3/prices_for_dates",
                           dict(origin=origin, departure_at=month, one_way="false", direct="false",
                                sorting="price", unique="false", limit=1000, market=market),
                           origin, paged_limit=1000)

    # --- Calendar depth: destination specified ------------------------------
    def calendar_probe_for(self, origin: str):
        per_dest = Counter()
        seen = set()
        for r in self.rows:
            if r["origin"] == origin or r["origin_airport"] == origin:
                k = dedupe_key(r)
                if k not in seen:
                    seen.add(k)
                    per_dest[r["destination"]] += 1
        top = [d for d, _ in per_dest.most_common(config.CALENDAR_TOP_DESTINATIONS) if d]
        months = upcoming_months(config.CALENDAR_MONTHS + 1)[1:]  # skip the partial current month
        print(f"[{origin}] calendar probe: {len(top)} destinations x {len(months)} months: {', '.join(top)}")
        for dest in top:
            for month in months:
                g = self.fetch("v3/grouped_prices (calendar)", "/aviasales/v3/grouped_prices",
                               dict(origin=origin, destination=dest, departure_at=month,
                                    group_by="departure_at", min_trip_duration=3,
                                    max_trip_duration=14, market=config.MARKET), origin)
                m = self.fetch("v2/month-matrix (calendar)", "/v2/prices/month-matrix",
                               dict(origin=origin, destination=dest, month=f"{month}-01",
                                    show_to_affiliates="false", one_way="false", trip_duration=1,
                                    limit=31, market=config.MARKET), origin)
                probe = self.calendar_probe[origin].setdefault(dest, Counter())
                probe["grouped_prices_days"] += len({r["departure_date"] for r in g})
                probe["month_matrix_days"] += len({r["departure_date"] for r in m})
                probe["months"] += 1


def plan_summary() -> int:
    n_months = config.MONTHS_AHEAD
    per_origin = (n_months * 2) + 2 + 1 + 1 + 1 + len(config.COMPARE_MARKETS) * config.MARKET_PROBE_MONTHS
    per_origin += config.CALENDAR_TOP_DESTINATIONS * config.CALENDAR_MONTHS * 2
    return per_origin * len(config.ORIGINS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the request plan only")
    args = ap.parse_args()

    est = plan_summary()
    print(f"Planned API calls (before extra pages): ~{est}  (hard cap {config.MAX_API_CALLS_PER_RUN})")
    if args.dry_run:
        return

    print("Loading reference data (airports/cities/countries; public, no token)...")
    fetch_reference_data()

    client = TravelpayoutsClient(config.TRAVELPAYOUTS_TOKEN)
    col = Collector(client)
    started = now_utc()
    try:
        for origin in config.ORIGINS:
            col.discover(origin)
            col.calendar_probe_for(origin)
    except CallBudgetExceeded as exc:
        print(f"!! {exc}; stopping collection early and analyzing what we have.")
    finished = now_utc()

    config.NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = config.NORMALIZED_DIR / "fare_observations.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=NORMALIZED_FIELDS)
        w.writeheader()
        w.writerows(col.rows)

    meta = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "api_calls_total": client.calls,
        "api_calls_by_path": dict(client.calls_by_endpoint),
        "source_stats": {k: dict(v) for k, v in col.stats.items()},
        "errors": {k: v[:20] for k, v in client.errors.items()},
        "calendar_probe": {o: {d: dict(c) for d, c in v.items()} for o, v in col.calendar_probe.items()},
        "config": {k: getattr(config, k) for k in (
            "ORIGINS", "CURRENCY", "MARKET", "COMPARE_MARKETS", "MONTHS_AHEAD",
            "CALENDAR_TOP_DESTINATIONS", "CALENDAR_MONTHS")},
    }
    (config.OUTPUT_DIR / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nSaved {len(col.rows)} normalized rows -> {csv_path}")

    run_analysis()


if __name__ == "__main__":
    main()
