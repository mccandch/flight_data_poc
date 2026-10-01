"""Analyze saved observations and write the POC report.

Can be re-run on its own (no API calls):  python analyze_coverage.py
"""
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime

import config
from travelpayouts_client import fetch_reference_data, parse_ts

LINE = "=" * 70
FIELDS_TO_CHECK = ["origin", "destination", "price", "departure_date", "return_date",
                   "airline", "stops", "source_last_updated", "expires_at", "flight_number"]


def dedupe_key(r: dict) -> tuple:
    return (r["origin"], r["destination"], r["departure_date"], r["return_date"],
            str(r["price"]), r["airline"], str(r["stops"]))


class Geo:
    def __init__(self, ref):
        self.airports = {a["code"]: a for a in ref["airports"]}
        self.cities = {c["code"]: c for c in ref["cities"]}
        self.countries = {c["code"]: c.get("name") or c["code"] for c in ref["countries"]}

    def city(self, code):
        if code in self.cities:
            return code
        a = self.airports.get(code)
        return a.get("city_code") or code if a else code

    def country(self, code):
        c = self.cities.get(self.city(code)) or self.airports.get(code) or {}
        return c.get("country_code", "??")

    def name(self, code):
        c = self.cities.get(self.city(code))
        return c["name"] if c and c.get("name") else code


def load_rows():
    path = config.NORMALIZED_DIR / "fare_observations.csv"
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        try:
            r["price"] = float(r["price"])
        except (TypeError, ValueError):
            r["price"] = None
    return [r for r in rows if r["price"] is not None and r["destination"]]


def unique(rows):
    seen, out = set(), []
    for r in rows:
        k = dedupe_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def fmt_money(v, cur="usd"):
    return f"${v:,.0f}" if cur.lower() == "usd" else f"{v:,.0f} {cur.upper()}"


def fmt_dates(r):
    dep = r["departure_date"]
    ret = r["return_date"]
    try:
        d = datetime.fromisoformat(dep).strftime("%b %d")
        if ret:
            return f"{d} - {datetime.fromisoformat(ret).strftime('%b %d')} ({r['trip_length']} nights)"
        return f"{d} one-way"
    except ValueError:
        return f"{dep} {ret}"


def freshness(rows, run_at):
    """Age of the fare observation according to the API's own found_at timestamp."""
    ages, stamps = [], []
    for r in rows:
        ts = parse_ts(r["source_last_updated"])
        if ts:
            stamps.append(ts)
            ages.append((run_at - ts).total_seconds() / 3600)
    if not ages:
        return None
    return {
        "n": len(ages), "oldest": min(stamps), "newest": max(stamps),
        "median_age_h": statistics.median(ages),
        "pct_under_24h": 100 * sum(a <= 24 for a in ages) / len(ages),
        "pct_under_48h": 100 * sum(a <= 48 for a in ages) / len(ages),
        "pct_over_7d": 100 * sum(a > 168 for a in ages) / len(ages),
    }


def percentile_rank(values_sorted, v):
    return 100.0 * sum(x <= v for x in values_sorted) / len(values_sorted)


def route_stats_and_deals(uniq, geo):
    """EXPERIMENTAL: within-snapshot comparison of fares on the same route + trip type.
    This compares different travel dates against each other, NOT prices over time."""
    by_route = defaultdict(list)
    for r in uniq:
        by_route[(geo.city(r["destination"]), r["trip_type"])].append(r)
    stats, deals = {}, []
    for key, rs in by_route.items():
        prices = sorted(r["price"] for r in rs)
        med = statistics.median(prices)
        stats[key] = {"n": len(prices), "min": prices[0], "median": med,
                      "mean": statistics.fmean(prices)}
        if len(prices) < config.MIN_ROUTE_OBSERVATIONS:
            continue
        for r in rs:
            ratio = r["price"] / med if med else 1
            pct = percentile_rank(prices, r["price"])
            if ratio <= config.DEAL_MAX_RATIO_TO_MEDIAN and pct <= config.DEAL_MAX_PERCENTILE:
                deals.append({**r, "ratio": ratio, "pct": pct, "route_n": len(prices),
                              "route_median": med, "is_route_min": r["price"] == prices[0]})
    # one line per route (its cheapest flagged fare), strongest first
    best = {}
    for d in sorted(deals, key=lambda d: d["ratio"]):
        best.setdefault((geo.city(d["destination"]), d["trip_type"]), d)
    return stats, sorted(best.values(), key=lambda d: d["ratio"])


def origin_section(origin, rows, meta, geo, run_at, out):
    rows = [r for r in rows if origin in (r["origin"], r["origin_airport"])]
    uniq = unique(rows)
    p = out.append
    p(LINE)
    p(f"{origin} COVERAGE")
    p(LINE)
    if not rows:
        p("No fare observations returned for this origin.\n")
        return {"origin": origin, "unique": 0}

    dests = {geo.city(r["destination"]) for r in uniq}
    dom = {d for d in dests if geo.country(d) == "US"}
    intl = dests - dom
    countries = Counter(geo.country(d) for d in intl)
    p(f"Destinations discovered: {len(dests)}")
    p(f"  Domestic:      {len(dom)}")
    p(f"  International: {len(intl)}  ({len(countries)} countries)")
    if countries:
        p("  Top intl countries: " + ", ".join(
            f"{geo.countries.get(c, c)} ({n})" for c, n in countries.most_common(12)))
    p("")
    p(f"Fare rows returned (all endpoints): {len(rows):,}")
    p(f"Unique fares (deduplicated):        {len(uniq):,}")
    rt = [r for r in uniq if r["trip_type"] == "RT"]
    p(f"  Round-trip: {len(rt):,}   One-way: {len(uniq) - len(rt):,}")
    p("")

    # travel-date coverage
    deps = sorted({r["departure_date"] for r in uniq if r["departure_date"]})
    pairs = {(r["departure_date"], r["return_date"]) for r in rt}
    months = Counter(r["departure_date"][:7] for r in uniq if r["departure_date"])
    p(f"Distinct departure dates: {len(deps)}" + (f"  ({deps[0]} .. {deps[-1]})" if deps else ""))
    p(f"Distinct round-trip date pairs: {len(pairs):,}")
    p("Unique fares by departure month: " + ", ".join(f"{m}:{n}" for m, n in sorted(months.items())))
    lengths = [int(r["trip_length"]) for r in rt if str(r["trip_length"]).lstrip("-").isdigit()]
    if lengths:
        p(f"Round-trip length: median {statistics.median(lengths):.0f} nights, "
          f"range {min(lengths)}-{max(lengths)}")
    p("")

    # freshness
    f = freshness(rows, run_at)
    p("Freshness (from API 'found_at' = when an Aviasales user search saw this price):")
    if f:
        p(f"  Rows with a found_at timestamp: {f['n']:,} of {len(rows):,}")
        p(f"  Oldest observation: {f['oldest']:%Y-%m-%d %H:%M} UTC")
        p(f"  Newest observation: {f['newest']:%Y-%m-%d %H:%M} UTC")
        p(f"  Median observation age: {f['median_age_h']:.1f} hours")
        p(f"  <=24h: {f['pct_under_24h']:.0f}%   <=48h: {f['pct_under_48h']:.0f}%   >7d: {f['pct_over_7d']:.0f}%")
    else:
        p("  No rows carried a found_at timestamp.")
    by_src = defaultdict(list)
    for r in rows:
        by_src[r["source"]].append(r)
    for src, rs in sorted(by_src.items()):
        fs = freshness(rs, run_at)
        if fs:
            p(f"    {src:42s} median age {fs['median_age_h']:6.1f} h  (n={fs['n']})")
    p("")

    # calendar
    probe = meta.get("calendar_probe", {}).get(origin, {})
    multi_date = sum(1 for d in dests if len({r['departure_date'] for r in uniq
                                              if geo.city(r['destination']) == d}) >= 5)
    p(f"Destinations with fares on >=5 distinct departure dates: {multi_date} of {len(dests)}")
    if probe:
        p(f"Calendar probe (top {len(probe)} destinations, per-day pricing, "
          f"{next(iter(probe.values())).get('months', 0)} months each):")
        p(f"  {'dest':6s}{'grouped_prices days':>22s}{'month-matrix days':>20s}")
        for d, c in probe.items():
            p(f"  {d:6s}{c.get('grouped_prices_days', 0):>22d}{c.get('month_matrix_days', 0):>20d}"
              f"   {geo.name(d)}")
        with_cal = sum(1 for c in probe.values()
                       if c.get("grouped_prices_days", 0) or c.get("month_matrix_days", 0))
        p(f"  Probed destinations returning any calendar data: {with_cal} of {len(probe)}")
    p("")

    # market comparison
    by_market = defaultdict(list)
    probe_months = sorted({r["departure_date"][:7] for r in rows
                           if r["source"].startswith("v3/prices_for_dates (RT, market=")})
    for r in rows:
        if r["source"].startswith("v3/prices_for_dates (RT") and r["departure_date"][:7] in probe_months:
            by_market[r["market"] or "?"].append(r)
    if len(by_market) > 1:
        p(f"Data-market comparison (prices_for_dates RT, months {', '.join(probe_months)}):")
        for m, rs in sorted(by_market.items()):
            u = unique(rs)
            p(f"  market={m}: {len(u):,} unique fares, {len({geo.city(r['destination']) for r in u})} destinations")
        p("")

    # route depth + experimental deals
    stats, deals = route_stats_and_deals(uniq, geo)
    judged = [k for k, s in stats.items() if s["n"] >= config.MIN_ROUTE_OBSERVATIONS]
    p(f"Routes (destination x RT/OW): {len(stats)};  with >= {config.MIN_ROUTE_OBSERVATIONS} "
      f"unique fares: {len(judged)}")
    deepest = sorted(stats.items(), key=lambda kv: -kv[1]["n"])[:10]
    if deepest:
        p("  Deepest routes: " + ", ".join(f"{d}/{t}:{s['n']}" for (d, t), s in deepest))
    p("")
    p("Sample EXPERIMENTAL 'unusually cheap' fares")
    p(f"  (<= {config.DEAL_MAX_RATIO_TO_MEDIAN:.0%} of route median AND <= {config.DEAL_MAX_PERCENTILE:.0f}th "
      f"percentile, routes with >= {config.MIN_ROUTE_OBSERVATIONS} fares).")
    p("  Compares travel dates within THIS snapshot only - not history. Verify before trusting.")
    if not deals:
        p("  None flagged.")
    for d in deals[:12]:
        cur = d["currency"] or config.CURRENCY
        p("")
        p(f"  {origin} -> {geo.name(d['destination'])} ({d['destination']})  [{d['trip_type']}]")
        p(f"  {fmt_money(d['price'], cur)}   {fmt_dates(d)}   stops: {d['stops'] or '?'}   airline: {d['airline'] or '?'}")
        p(f"  route median {fmt_money(d['route_median'], cur)} ({1 - d['ratio']:.0%} below), "
          f"percentile {d['pct']:.1f}, n={d['route_n']}{', route low' if d['is_route_min'] else ''}")
        p(f"  seen: {d['source_last_updated'] or 'n/a'}   via {d['source']}")
        p(f"  {d['google_flights_url']}")
    p("")
    return {"origin": origin, "rows": len(rows), "unique": len(uniq), "destinations": len(dests),
            "domestic": len(dom), "international": len(intl), "routes_judgeable": len(judged),
            "freshness_median_h": f["median_age_h"] if f else None, "deals_flagged": len(deals)}


def field_completeness(rows, out):
    p = out.append
    p(LINE)
    p("FIELDS RETURNED PER ENDPOINT (% of rows where field is non-empty)")
    p(LINE)
    hdr = "".join(f"{f[:9]:>10s}" for f in FIELDS_TO_CHECK)
    p(f"{'source':42s}{'rows':>7s}{hdr}")
    by_src = defaultdict(list)
    for r in rows:
        by_src[r["source"]].append(r)
    for src, rs in sorted(by_src.items()):
        cells = "".join(f"{100 * sum(1 for r in rs if str(r.get(f, '')).strip()) / len(rs):>9.0f}%"
                        for f in FIELDS_TO_CHECK)
        p(f"{src[:41]:42s}{len(rs):>7d}{cells}")
    p("(return_date is empty for one-way fares by design; found_at = source_last_updated)")
    p("")


def efficiency(rows, meta, out):
    p = out.append
    p(LINE)
    p("API EFFICIENCY")
    p(LINE)
    total_calls = meta.get("api_calls_total", 0)
    uniq = unique(rows)
    p(f"{'source':42s}{'calls':>6s}{'rows':>8s}{'kept':>8s}{'kept/call':>11s}{'empty':>7s}{'err':>5s}")
    for src, s in sorted(meta.get("source_stats", {}).items()):
        calls = s.get("calls", 0)
        p(f"{src[:41]:42s}{calls:>6d}{s.get('records', 0):>8d}{s.get('kept', 0):>8d}"
          f"{(s.get('kept', 0) / calls if calls else 0):>11.1f}{s.get('empty', 0):>7d}{s.get('errors', 0):>5d}")
    p("")
    p(f"API calls made:                     {total_calls}")
    p(f"Fare rows obtained:                 {len(rows):,}")
    p(f"Unique fare observations:           {len(uniq):,}")
    if total_calls:
        p(f"Observations per API call (rows):   {len(rows) / total_calls:.1f}")
        p(f"Observations per API call (unique): {len(uniq) / total_calls:.1f}")
    p(f"Run time: {meta.get('started_at')} -> {meta.get('finished_at')}")
    if meta.get("errors"):
        p("")
        p("Errors (first few per endpoint):")
        for path, errs in meta["errors"].items():
            p(f"  {path}: {errs[:3]}")
    p("")


def run_analysis():
    meta_path = config.OUTPUT_DIR / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    geo = Geo(fetch_reference_data())
    rows = load_rows()
    run_at = parse_ts(meta.get("finished_at")) or max(
        (parse_ts(r["observed_at"]) for r in rows), default=None)

    out = ["TRAVELPAYOUTS DATA API - SLC/PVU PROOF OF CONCEPT", f"Generated {datetime.now():%Y-%m-%d %H:%M}", ""]
    summaries = [origin_section(o, rows, meta, geo, run_at, out) for o in config.ORIGINS]
    field_completeness(rows, out)
    efficiency(rows, meta, out)

    report = "\n".join(out)
    (config.OUTPUT_DIR / "report.txt").write_text(report, encoding="utf-8")
    (config.OUTPUT_DIR / "summary.json").write_text(json.dumps(summaries, indent=2, default=str),
                                                     encoding="utf-8")
    print(report)
    print(f"Report saved to {config.OUTPUT_DIR / 'report.txt'}")


if __name__ == "__main__":
    run_analysis()
