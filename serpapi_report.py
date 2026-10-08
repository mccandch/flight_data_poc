"""Coverage / efficiency / EXPERIMENTAL deal report from the SerpApi SQLite store.

    python serpapi_report.py     # no searches used
"""
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime

import config
import serpapi_client as sc

LINE = "=" * 72
MIN_HISTORY_DAYS = 5          # a destination needs this many days of history before we judge it
HISTORY_DEAL_RATIO = 0.80     # flag when today's price <= 80% of that destination's own median


def local_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(config.LOCAL_TZ).date().isoformat()


def fmt_trip(r) -> str:
    try:
        dep = datetime.fromisoformat(r["departure_date"]).strftime("%b %d")
        ret = datetime.fromisoformat(r["return_date"]).strftime("%b %d, %Y")
        return f"{dep} - {ret}"
    except (TypeError, ValueError):
        return "dates n/a"


def stops_txt(s) -> str:
    return "nonstop" if s == 0 else (f"{s} stop" + ("s" if s and s > 1 else "")) if s is not None else "stops ?"


def fare_line(r) -> str:
    return (f"{r['origin']} -> {r['destination_name'].split(' / ')[0][:24]} ({r['destination']}, "
            f"{r['country'][:16]})  ${r['price']:,.0f}  {fmt_trip(r)}  {stops_txt(r['stops'])}  "
            f"{(r['airline'] or '')[:28]}")


def efficiency(con, p):
    p(LINE); p("SEARCH EFFICIENCY (each search = 1 of the monthly free searches)"); p(LINE)
    rows = con.execute("""SELECT label, COUNT(*) n, AVG(priced_airports) avg_air, SUM(priced_airports) tot,
                          SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) errs
                          FROM searches GROUP BY label ORDER BY label""").fetchall()
    p(f"{'search':34s}{'runs':>6s}{'airports/search':>17s}{'observations':>14s}{'errors':>8s}")
    for r in rows:
        p(f"{r['label'][:33]:34s}{r['n']:>6d}{r['avg_air'] or 0:>17.1f}{r['tot'] or 0:>14d}{r['errs']:>8d}")
    n_search = con.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    n_obs = con.execute("SELECT COUNT(*) FROM fare_observations").fetchone()[0]
    p("")
    p(f"Searches stored:           {n_search}")
    p(f"Fare observations stored:  {n_obs:,}")
    if n_search:
        p(f"Observations per search:   {n_obs / n_search:.1f}")
    p("Cost so far: $0 (free plan)")
    p("")


def coverage(con, origin, p):
    obs = con.execute("SELECT * FROM fare_observations WHERE origin = ?", (origin,)).fetchall()
    p(LINE); p(f"{origin} COVERAGE"); p(LINE)
    if not obs:
        p("No observations yet.\n")
        return
    dests = {}
    for r in obs:
        dests.setdefault(r["destination"], r)
    dom = [d for d, r in dests.items() if r["country"] == "United States"]
    intl = [d for d, r in dests.items() if r["country"] != "United States"]
    countries = defaultdict(int)
    for d in intl:
        countries[dests[d]["country"]] += 1
    days = sorted({local_day(r["observed_at"]) for r in obs})
    p(f"Destination airports discovered: {len(dests)}")
    p(f"  Domestic:      {len(dom)}")
    p(f"  International: {len(intl)} in {len(countries)} countries")
    if countries:
        p("    " + ", ".join(f"{c} ({n})" for c, n in sorted(countries.items(), key=lambda kv: -kv[1])[:15]))
    regions = con.execute("""SELECT region, COUNT(DISTINCT destination) n FROM fare_observations
                             WHERE origin = ? GROUP BY region""", (origin,)).fetchall()
    p("  By search region: " + ", ".join(f"{r['region']} ({r['n']})" for r in regions))
    p(f"Fare observations: {len(obs):,}   Days with data: {len(days)} ({days[0]} .. {days[-1]})")
    stops = defaultdict(int)
    for r in obs:
        stops[stops_txt(r["stops"])] += 1
    p("Stops mix: " + ", ".join(f"{k}: {v}" for k, v in sorted(stops.items())))
    p("")


def history_deals(con, origin, p):
    """Our own statistics: today's price vs this destination's price history."""
    obs = con.execute("""SELECT * FROM fare_observations WHERE origin = ? AND source = 'google_travel_explore'
                         ORDER BY observed_at""", (origin,)).fetchall()
    series = defaultdict(list)
    for r in obs:  # a weekend trip is never compared with a 1-week one
        series[(r["destination"], r["trip_kind"] or "1 week")].append(r)
    ready, flagged = 0, []
    for dest, rs in series.items():
        by_day = {}
        for r in rs:  # one price per day (lowest seen that day)
            d = local_day(r["observed_at"])
            if d not in by_day or r["price"] < by_day[d]["price"]:
                by_day[d] = r
        if len(by_day) < MIN_HISTORY_DAYS + 1:
            continue
        ready += 1
        days = sorted(by_day)
        latest = by_day[days[-1]]
        history = [by_day[d]["price"] for d in days[:-1]]
        med = statistics.median(history)
        pct = 100 * sum(h <= latest["price"] for h in history) / len(history)
        if latest["price"] <= HISTORY_DEAL_RATIO * med or latest["price"] < min(history):
            flagged.append((latest["price"] / med, latest, med, pct, len(history), latest["price"] < min(history)))
    p(f"-- {origin}: EXPERIMENTAL deals vs OUR OWN price history --")
    p(f"   Destinations with >= {MIN_HISTORY_DAYS} prior days of history: {ready} of {len(series)}")
    if not ready:
        need = MIN_HISTORY_DAYS + 1 - len({local_day(r['observed_at']) for r in obs})
        p(f"   Not enough history yet - about {max(need, 1)} more daily run(s) needed.")
    for ratio, r, med, pct, n, record in sorted(flagged, key=lambda x: x[0])[:10]:
        p(f"   {fare_line(r)}")
        p(f"      {1 - ratio:.0%} below its median ${med:,.0f} over {n} days; percentile {pct:.0f}"
          f"{'; NEW RECORD LOW' if record else ''}")
        p(f"      {r['google_link']}")
    if ready and not flagged:
        p("   Nothing unusual today.")
    p("")


def google_deals(con, origin, p):
    """Google's own judgment, shown for comparison only."""
    last = con.execute("""SELECT MAX(id) FROM searches WHERE origin = ? AND engine = 'google_flights_deals'""",
                       (origin,)).fetchone()[0]
    p(f"-- {origin}: Google Flights 'Deals' (GOOGLE's opinion, for comparison) --")
    if not last:
        p("   No deals search yet.\n")
        return
    rows = con.execute("""SELECT * FROM fare_observations WHERE search_row_id = ?
                          ORDER BY discount_pct DESC""", (last,)).fetchall()
    for r in rows[:10]:
        p(f"   {fare_line(r)}")
        p(f"      usually ${r['typical_price']:,.0f} -> {r['discount_pct']:.0f}% off")
    p("")


def snapshot_cheapest(con, origin, p):
    """Latest price per destination; cheapest few per region. Context, not deal detection."""
    rows = con.execute("""SELECT f.* FROM fare_observations f
                          JOIN (SELECT destination, MAX(observed_at) m FROM fare_observations
                                WHERE origin = ? AND source = 'google_travel_explore'
                                  AND COALESCE(trip_kind, '1 week') = '1 week' GROUP BY destination) x
                          ON f.destination = x.destination AND f.observed_at = x.m
                          WHERE f.origin = ? AND f.source = 'google_travel_explore'
                            AND COALESCE(f.trip_kind, '1 week') = '1 week'""", (origin, origin)).fetchall()
    by_region = defaultdict(list)
    for r in rows:
        if r["region"] != "default":
            group = r["region"]
        else:
            group = "Domestic" if r["country"] == "United States" else "Canada/Mexico/other"
        by_region[group].append(r)
    p(f"-- {origin}: latest cheapest fares by region (1-week round trips, next ~6 months) --")
    for region, rs in sorted(by_region.items()):
        p(f"   {region}:")
        for r in sorted(rs, key=lambda r: r["price"])[:4]:
            p(f"     {fare_line(r)}")
    p("")


def export_csv(con):
    path = config.SERPAPI_DIR / "fare_observations.csv"
    rows = con.execute("SELECT * FROM fare_observations ORDER BY observed_at, origin, destination").fetchall()
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(rows[0].keys() if rows else [])
        w.writerows([tuple(r) for r in rows])
    return path


def run_report(con=None):
    sys.stdout.reconfigure(errors="replace")  # Windows console can't print names like Kaua'i (okina)
    con = con or sc.connect()
    out = [f"SERPAPI FLIGHT DATA POC - SLC / PVU   (generated {datetime.now(config.LOCAL_TZ):%Y-%m-%d %H:%M})", ""]
    p = out.append
    for origin in config.ORIGINS:
        coverage(con, origin, p)
    for origin in config.ORIGINS:
        snapshot_cheapest(con, origin, p)
        history_deals(con, origin, p)
        google_deals(con, origin, p)
    efficiency(con, p)
    report = "\n".join(out)
    (config.SERPAPI_DIR / "report.txt").write_text(report, encoding="utf-8")
    csv_path = export_csv(con)
    print(report)
    print(f"Report: {config.SERPAPI_DIR / 'report.txt'}\nCSV:    {csv_path}\nDB:     {config.SERPAPI_DB}")


if __name__ == "__main__":
    run_report()
