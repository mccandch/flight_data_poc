"""Email today's big fare drops, split by category. Runs in the daily cloud scan after the collector.

    python alerts.py              # send (needs GMAIL_USER + GMAIL_APP_PASSWORD) and record what was sent
    python alerts.py --dry-run    # write output/alerts_preview.html; send nothing, record nothing

Categories (thresholds in config.py):
  1. At least 50% below OUR typical price (median of the destination's previous days)
  2. At least 50% below Google's "usual price" (from Google's Deals list)
A fare that qualifies for both is listed in both sections.
"""
import argparse
import html
import os
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd

import config
import fares

CATEGORIES = [
    ("typical", "vs_typical", config.ALERT_MAX_VS_TYPICAL,
     "At least {pct}% below OUR typical price",
     "Compared with this destination's median price over the days we've tracked it."),
    ("google", "vs_google", config.ALERT_MAX_VS_GOOGLE,
     "At least {pct}% below Google's \"usual\" price",
     "Compared with the 'usual price' Google shows on its Deals list. Google's 'usual' can run high, "
     "so treat this list as a second opinion."),
]

SENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY, sent_at TEXT, category TEXT, origin TEXT, destination TEXT,
    departure_date TEXT, return_date TEXT, price REAL, trip_kind TEXT
)"""
KEY = ["origin", "destination", "trip_kind"]


def _sent_columns(con) -> set:
    return {r[1] for r in con.execute("PRAGMA table_info(alerts_sent)")}


def find_alerts(scan_day=None) -> dict[str, pd.DataFrame]:
    obs, _ = fares.load()
    latest = fares.latest_fares(obs)
    day = scan_day or obs["day"].max()
    todays = latest[latest["day"] == day]
    out = {}
    for key, col, limit, *_ in CATEGORIES:
        out[key] = todays[todays[col] <= limit].sort_values(col)
    return out, day


def drop_already_sent(alerts: dict[str, pd.DataFrame], con) -> dict[str, pd.DataFrame]:
    """Skip fares alerted recently, unless the price has dropped meaningfully since."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'alerts_sent'").fetchone():
        return alerts  # nothing sent yet
    since = (datetime.now(timezone.utc) - timedelta(days=config.ALERT_REPEAT_DAYS)).isoformat()
    kind = "COALESCE(trip_kind, '1 week')" if "trip_kind" in _sent_columns(con) else "'1 week'"
    sent = pd.read_sql(f"SELECT category, origin, destination, {kind} AS trip_kind, MIN(price) AS last_price "
                       f"FROM alerts_sent WHERE sent_at >= ? GROUP BY 1, 2, 3, 4", con, params=(since,))
    out = {}
    for key, df in alerts.items():
        prev = sent[sent["category"] == key].set_index(KEY)["last_price"]
        last = df.set_index(KEY).index.map(lambda k: prev.get(k))
        keep = [lp is None or pd.isna(lp) or p <= lp * (1 - config.ALERT_REPEAT_MIN_DROP)
                for p, lp in zip(df["price"], last)]
        out[key] = df[keep]
    return out


def record_sent(alerts: dict[str, pd.DataFrame], con):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    con.execute(SENT_SCHEMA)
    if "trip_kind" not in _sent_columns(con):  # table created before trip lengths were tracked
        con.execute("ALTER TABLE alerts_sent ADD COLUMN trip_kind TEXT")
        con.execute("UPDATE alerts_sent SET trip_kind = '1 week' WHERE trip_kind IS NULL")
    con.executemany(
        "INSERT INTO alerts_sent (sent_at, category, origin, destination, departure_date, return_date, price,"
        " trip_kind) VALUES (?,?,?,?,?,?,?,?)",
        [(now, key, r.origin, r.destination, r.departure_date, r.return_date, float(r.price), r.trip_kind)
         for key, df in alerts.items() for r in df.itertuples()])
    con.commit()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _pct(v) -> str:
    return "" if pd.isna(v) else f"{v:+.0%}"


def _money(v) -> str:
    return "" if pd.isna(v) else f"${v:,.0f}"


def _dates(r) -> str:
    try:
        d = datetime.fromisoformat(r.departure_date).strftime("%a %b %d")
        rt = datetime.fromisoformat(r.return_date).strftime("%a %b %d")
        return f"{d} – {rt} ({int(r.nights)} nights)"
    except (TypeError, ValueError):
        return f"{r.departure_date} – {r.return_date}"


def build_email(alerts, day) -> tuple[str, str, str]:
    n = {k: len(df) for k, df in alerts.items()}
    subject = (f"✈️ {sum(n.values())} flight deal{'s' if sum(n.values()) != 1 else ''} from SLC/PVU "
               f"({n['typical']} vs our typical · {n['google']} vs Google) – {day:%b %d}")
    th = 'style="text-align:left;padding:6px 8px;border-bottom:2px solid #ccc;font-size:13px"'
    td = 'style="padding:6px 8px;border-bottom:1px solid #eee;font-size:14px;vertical-align:top"'
    parts_html = [f'<div style="font-family:Arial,sans-serif;max-width:900px">'
                  f'<h2 style="margin:0 0 4px">SLC / PVU fare alerts – {day:%A, %B %d}</h2>'
                  f'<p style="color:#555;margin:0 0 16px">Cheapest round trips found by today\'s scan '
                  f'(weekend, 1-week and 2-week trip searches). '
                  f'Prices change quickly – confirm on Google Flights before booking.</p>']
    parts_text = [f"SLC / PVU fare alerts – {day:%A, %B %d}\n"]
    for key, col, limit, title, note in CATEGORIES:
        df = alerts[key]
        heading = title.format(pct=round(-limit * 100))
        parts_html.append(f'<h3 style="margin:20px 0 2px">{html.escape(heading)} ({len(df)})</h3>'
                          f'<p style="color:#666;font-size:13px;margin:0 0 8px">{html.escape(note)}</p>')
        parts_text.append(f"\n== {heading} ({len(df)}) ==")
        if df.empty:
            parts_html.append('<p style="color:#888">None today.</p>')
            parts_text.append("None today.")
            continue
        rows = []
        for r in df.itertuples():
            route = f"{r.origin} → {html.escape(r.city)} ({r.destination}), {html.escape(r.country)}"
            ours = f"{_money(r.median_price)} ({_pct(r.vs_typical)})" if pd.notna(r.vs_typical) else "–"
            goog = f"{_money(r.google_usual)} ({_pct(r.vs_google)})" if pd.notna(r.vs_google) else "–"
            rows.append(
                f"<tr><td {td}><b>{route}</b><br><span style='color:#666;font-size:12px'>"
                f"{html.escape(r.region_group or '')} · {html.escape(r.trip_kind)} trip</span></td>"
                f"<td {td}><b style='font-size:16px'>{_money(r.price)}</b></td>"
                f"<td {td}>{_dates(r)}<br><span style='color:#666;font-size:12px'>"
                f"{fares.stops_label(r.stops)} · {html.escape(r.airline or '')}</span></td>"
                f"<td {td}>{ours}</td><td {td}>{goog}</td>"
                f"<td {td}><a href='{html.escape(r.gf_link)}'>Google Flights</a></td></tr>")
            parts_text.append(
                f"- {r.origin} -> {r.city} ({r.destination}), {r.trip_kind} trip: {_money(r.price)}, {_dates(r)}, "
                f"{fares.stops_label(r.stops)}, {r.airline} | our typical {ours} | Google usual {goog}\n"
                f"  {r.gf_link}")
        parts_html.append(
            f"<table style='border-collapse:collapse;width:100%'><tr><th {th}>Route</th><th {th}>Price</th>"
            f"<th {th}>Dates</th><th {th}>Our typical</th><th {th}>Google 'usual'</th><th {th}></th></tr>"
            + "".join(rows) + "</table>")
    parts_html.append('<p style="color:#888;font-size:12px;margin-top:24px">Sent by the flight_data_poc daily '
                      'scan. A fare already emailed is not repeated for '
                      f'{config.ALERT_REPEAT_DAYS} days unless its price drops further.</p></div>')
    return subject, "\n".join(parts_text), "".join(parts_html)


def send_email(subject, text, html_body):
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    to = os.environ.get("ALERT_EMAIL_TO", "") or user
    if not (user and password):
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Flight deals <{user}>", to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write a preview; send and record nothing")
    ap.add_argument("--test-email", action="store_true",
                    help="send today's alerts even if already sent, without recording them")
    args = ap.parse_args()

    alerts, day = find_alerts()
    today = datetime.now(config.LOCAL_TZ).date()
    print(f"Latest scan day: {day}. Matches before de-duplication: "
          + ", ".join(f"{k}={len(v)}" for k, v in alerts.items()))
    if day != today and not (args.dry_run or args.test_email):
        print(f"No scan data for today ({today}); not sending stale alerts.")
        return

    # read-only unless we may record sends, so previews never change the database
    con = (sqlite3.connect(f"file:{config.SERPAPI_DB}?mode=ro", uri=True) if args.dry_run or args.test_email
           else sqlite3.connect(config.SERPAPI_DB))
    if not args.test_email:
        alerts = drop_already_sent(alerts, con)
    total = sum(len(v) for v in alerts.values())
    print("New to send: " + ", ".join(f"{k}={len(v)}" for k, v in alerts.items()))

    subject, text, html_body = build_email(alerts, day)
    if args.dry_run:
        path = config.OUTPUT_DIR / "alerts_preview.html"
        path.write_text(html_body, encoding="utf-8")
        print(f"Subject: {subject}\nPreview written to {path}")
        return
    if total == 0:
        print("Nothing new to email today.")
        return
    try:
        send_email(subject, text, html_body)
    except Exception as exc:  # noqa: BLE001 - never fail the scan because of email
        print(f"!! Email not sent: {exc}")
        return
    print(f"Emailed: {subject}")
    if not args.test_email:
        record_sent(alerts, con)


if __name__ == "__main__":
    main()
