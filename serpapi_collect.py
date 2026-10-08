"""Daily SerpApi collector for SLC/PVU. Designed to run once per day (~4-5 searches).

    python serpapi_collect.py              # run today's searches, then print the report
    python serpapi_collect.py --dry-run    # show today's plan + remaining budget, no searches
    python serpapi_collect.py --import-probes   # load already-saved raw responses (no searches)
"""
import argparse
import json
from datetime import date, datetime

import config
import serpapi_client as sc
from serpapi_report import run_report

BASE = {"currency": "USD", "hl": "en", "gl": "us"}
EXPLORE_1WK = {"engine": "google_travel_explore", "travel_duration": 2}  # Explore: 2 = 1 week
DEALS_1WK = {"engine": "google_flights_deals", "travel_duration": 1}     # Deals:   1 = 1 week
EXPLORE_WEEKEND = {"engine": "google_travel_explore", "travel_duration": 1}
EXPLORE_2WK = {"engine": "google_travel_explore", "travel_duration": 3}


def todays_plan(day: date) -> list[tuple[int, str, dict]]:
    """(priority, label, params). Lower priority number = more important."""
    n = day.toordinal()
    region, region_id = list(config.SERPAPI_REGIONS.items())[n % len(config.SERPAPI_REGIONS)]
    plan = [
        (1, "SLC explore default", {**BASE, **EXPLORE_1WK, "departure_id": "SLC"}),
        (1, "PVU explore default", {**BASE, **EXPLORE_1WK, "departure_id": "PVU"}),
        (2, "SLC deals", {**BASE, **DEALS_1WK, "departure_id": "SLC"}),
        (2, f"SLC explore {region}", {**BASE, **EXPLORE_1WK, "departure_id": "SLC",
                                      "arrival_area_id": region_id, "_region": region}),
    ]
    variant, extra = list(config.SERPAPI_VARIANTS.items())[n % len(config.SERPAPI_VARIANTS)]
    plan.append((3, f"SLC explore {variant}", {**BASE, **EXPLORE_1WK, "departure_id": "SLC", **extra}))
    if n % 2 == 0:
        plan.append((3, "PVU deals", {**BASE, **DEALS_1WK, "departure_id": "PVU"}))
        plan.append((4, "SLC explore weekend", {**BASE, **EXPLORE_WEEKEND, "departure_id": "SLC"}))
    else:
        region2 = config.SERPAPI_2WK_REGIONS[(n // 2) % len(config.SERPAPI_2WK_REGIONS)]
        plan.append((4, f"SLC explore {region2} 2wk", {**BASE, **EXPLORE_2WK, "departure_id": "SLC",
                                                      "arrival_area_id": config.SERPAPI_REGIONS[region2],
                                                      "_region": region2}))
        plan.append((4, "PVU explore weekend", {**BASE, **EXPLORE_WEEKEND, "departure_id": "PVU"}))
    return sorted(plan, key=lambda p: p[0])


def today() -> date:
    return datetime.now(config.LOCAL_TZ).date()


def already_ran_today(con, label: str) -> bool:
    rows = con.execute("SELECT run_at FROM searches WHERE label = ?", (label,)).fetchall()
    return any(datetime.fromisoformat(r["run_at"]).astimezone(config.LOCAL_TZ).date() == today()
               for r in rows)


def import_probes(con):
    """Load raw probe files saved before the collector existed. Uses no searches."""
    region_by_id = {v: k for k, v in config.SERPAPI_REGIONS.items()}
    n = 0
    for f in sorted(config.SERPAPI_RAW_DIR.glob("probe_*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        params = dict(rec["params"])
        region = region_by_id.get(params.get("arrival_area_id"), "default")
        params["_region"] = region
        # same labels as todays_plan() so a same-day run doesn't repeat these searches
        if "deals" in params["engine"]:
            label = f"{params['departure_id']} deals"
        else:
            label = f"{params['departure_id']} explore {region}"
        added, s = sc.store(con, label, params, rec["body"], f.name)
        print(f"  {f.name}: {s['priced_airports']} airports, {added} rows added")
        n += added
    print(f"Imported {n} observations from probe files.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--import-probes", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-run searches already done today")
    args = ap.parse_args()

    con = sc.connect()
    if args.import_probes:
        import_probes(con)
        return

    client = sc.SerpApiClient(config.SERPAPI_API_KEY)
    acct = client.account()
    left = acct["total_searches_left"] or 0
    allowed = max(0, left - config.SERPAPI_MIN_SEARCHES_LEFT)
    plan = [p for p in todays_plan(today()) if args.force or not already_ran_today(con, p[1])]

    print(f"SerpApi plan: {acct['plan_name']}, {left} searches left "
          f"(used {acct['this_month_usage']}, renews {acct['plan_renewal_date']}). "
          f"Reserve {config.SERPAPI_MIN_SEARCHES_LEFT} -> may use {allowed} now.")
    if len(plan) > allowed:
        print(f"!! Budget guard: trimming today's plan from {len(plan)} to {allowed} searches.")
        plan = plan[:allowed]
    print("Today's searches:" if plan else "Nothing to run today (already done or no budget).")
    for pri, label, _ in plan:
        print(f"  [{pri}] {label}")
    if args.dry_run or not plan:
        return

    for _, label, params in plan:
        try:
            body, raw_file = client.search(label, {k: v for k, v in params.items() if not k.startswith("_")})
        except sc.BudgetExceeded as exc:
            print(f"!! {exc}")
            break
        added, s = sc.store(con, label, params, body, raw_file)
        err = f"  ERROR: {s['error']}" if s["error"] else ""
        print(f"  {label}: {s['items']} items -> {s['priced_airports']} priced airports{err}")

    acct = client.account()
    print(f"Searches made this run: {client.searches_made}. Remaining on plan: {acct['total_searches_left']}.\n")
    run_report(con)


if __name__ == "__main__":
    main()
