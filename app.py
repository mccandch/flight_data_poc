"""SLC / PVU flight browser (read-only view of the SerpApi collection).

    run_app.bat        or        .venv\\Scripts\\streamlit run app.py
"""

import altair as alt
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

import config
import fares
from fares import MIN_HISTORY_DAYS, daily_min, latest_fares, stops_label

st.set_page_config(page_title="SLC/PVU Flight Browser", page_icon="✈️", layout="wide")

ORIGIN_COORDS = {"SLC": (40.7856, -111.9807), "PVU": (40.2181, -111.7222)}


@st.cache_data(ttl=300)
def load():
    return fares.load()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
if not config.SERPAPI_DB.exists():
    st.error(f"No data yet: {config.SERPAPI_DB} not found. Run serpapi_collect.py first.")
    st.stop()

obs, searches = load()
latest = latest_fares(obs)

st.title("✈️ SLC / PVU Flight Browser")
last_run = searches["run"].max()
n_days = obs["day"].nunique()
st.caption(f"Cheapest round trips found by the daily Google Explore / Deals collection · "
           f"last collection {last_run:%b %d, %I:%M %p} · {n_days} day(s) of history · "
           f"{len(obs):,} observations")

# ---- Sidebar filters -------------------------------------------------------
with st.sidebar:
    st.header("Filters")
    origins = st.multiselect("From", ["SLC", "PVU"], default=["SLC", "PVU"])
    region_order = ["Domestic", "Hawaii", "Alaska", "Canada & Mexico", "Caribbean", "Europe", "Asia",
                    "South America", "Oceania", "Other international"]
    available = [r for r in region_order if r in set(latest["region_group"])]
    regions = st.multiselect("Regions", available, default=available)
    kinds = [k for k in ("weekend", "1 week", "2 weeks") if k in set(latest["trip_kind"])]
    trip_kinds = st.multiselect("Trip length", kinds, default=kinds,
                                help="Google's presets: weekend, ~1 week, ~2 weeks. Each is tracked separately.")
    max_possible = int(latest["price"].max()) + 1 if len(latest) else 1000
    max_price = st.slider("Max round-trip price ($)", 50, max(max_possible, 100), max_possible, step=25)
    stops = st.radio("Stops", ["Any", "Nonstop only", "1 stop or fewer"], horizontal=False)
    dep_min, dep_max = latest["dep"].min().date(), latest["dep"].max().date()
    dep_range = st.date_input("Departing between", (dep_min, dep_max), min_value=dep_min, max_value=dep_max)
    text = st.text_input("Destination contains", placeholder="e.g. Rome, HNL, Italy")
    if st.button("Reload data"):
        st.cache_data.clear()
        st.rerun()

view = latest[latest["origin"].isin(origins) & latest["region_group"].isin(regions)
              & latest["trip_kind"].isin(trip_kinds) & (latest["price"] <= max_price)]
if stops == "Nonstop only":
    view = view[view["stops"] == 0]
elif stops == "1 stop or fewer":
    view = view[view["stops"] <= 1]
if isinstance(dep_range, tuple) and len(dep_range) == 2:
    view = view[(view["dep"].dt.date >= dep_range[0]) & (view["dep"].dt.date <= dep_range[1])]
if text:
    t = text.lower()
    view = view[view["city"].str.lower().str.contains(t, regex=False)
                | view["destination"].str.lower().str.contains(t, regex=False)
                | view["country"].str.lower().str.contains(t, regex=False)
                | view["destination_name"].str.lower().str.contains(t, regex=False)]
view = view.sort_values("price")


def show_history(origin: str, dest: str, kind: str):
    rows = obs[(obs["origin"] == origin) & (obs["destination"] == dest)
               & (obs["trip_kind"] == kind)].sort_values("observed")
    if rows.empty:
        st.info("No observations.")
        return
    head = rows.iloc[-1]
    st.subheader(f"{origin} → {head['city']} ({dest}), {head['country']} · {kind} trips")
    dm = daily_min(rows).sort_values("day")
    c1, c2, c3, c4 = st.columns(4)
    cur = dm.iloc[-1]
    c1.metric("Latest cheapest", f"${cur['price']:,.0f}")
    c2.metric("Lowest seen", f"${dm['price'].min():,.0f}")
    c3.metric("Median", f"${dm['price'].median():,.0f}")
    c4.metric("Days tracked", len(dm))
    if len(dm) < MIN_HISTORY_DAYS + 1:
        st.caption(f"Deal scoring starts once a destination has {MIN_HISTORY_DAYS + 1} days of history.")
    chart = alt.Chart(dm.assign(date=pd.to_datetime(dm["day"]))).mark_line(point=alt.OverlayMarkDef(size=80)).encode(
        x=alt.X("date:T", title="Collected on"),
        y=alt.Y("price:Q", title="Cheapest round trip ($)", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="Collected"), alt.Tooltip("price:Q", format="$,.0f"),
                 alt.Tooltip("departure_date:N", title="Depart"), alt.Tooltip("return_date:N", title="Return"),
                 alt.Tooltip("airline:N")])
    st.altair_chart(chart, width="stretch")
    st.dataframe(
        rows.sort_values("observed", ascending=False)[
            ["observed", "price", "departure_date", "return_date", "stops", "airline", "search_label", "gf_link"]],
        hide_index=True, width="stretch",
        column_config={
            "observed": st.column_config.DatetimeColumn("Collected", format="MMM D, h:mm a"),
            "price": st.column_config.NumberColumn("Price", format="$%d"),
            "departure_date": "Depart", "return_date": "Return", "stops": "Stops", "airline": "Airline",
            "search_label": "Found by search",
            "gf_link": st.column_config.LinkColumn("Google Flights", display_text="Open ↗"),
        })


MONEY_FMT = JsCode("function(p){return p.value==null?'':'$'+Math.round(p.value).toLocaleString();}")
PCT_FMT = JsCode("function(p){return p.value==null?'':(p.value>0?'+':'')+Math.round(p.value*100)+'%';}")
STOPS_FMT = JsCode("function(p){return p.value==null?'?':(p.value==0?'Nonstop':p.value+(p.value==1?' stop':' stops'));}")
LINK_RENDERER = JsCode("""
class LinkRenderer {
  init(p) {
    this.eGui = document.createElement('a');
    if (p.value) { this.eGui.href = p.value; this.eGui.target = '_blank';
                   this.eGui.rel = 'noopener'; this.eGui.innerText = 'Open ↗'; }
  }
  getGui() { return this.eGui; }
}""")
# Blank cells sort last in both directions (AG Grid puts them first by default)
NULLS_LAST = JsCode("""
function(a, b, nodeA, nodeB, isDesc) {
  if (a == null && b == null) return 0;
  if (a == null) return isDesc ? -1 : 1;
  if (b == null) return isDesc ? 1 : -1;
  return a - b;
}""")
# ISO "YYYY-MM-DD" strings -> date filter ("before", "after", "between")
ISO_DATE_FILTER = {"comparator": JsCode("""
function(filterDate, cell) {
  if (!cell) return -1;
  const [y, m, d] = cell.split('-').map(Number);
  const c = new Date(y, m - 1, d);
  return c < filterDate ? -1 : (c > filterDate ? 1 : 0);
}""")}


def fares_grid(table: pd.DataFrame):
    gb = GridOptionsBuilder.from_dataframe(table)
    gb.configure_default_column(filter=True, floatingFilter=True, sortable=True, resizable=True,
                                suppressHeaderMenuButton=False, minWidth=90)
    text, num, date = "agTextColumnFilter", "agNumberColumnFilter", "agDateColumnFilter"
    gb.configure_column("origin", "From", filter=text, width=90, pinned="left")
    gb.configure_column("city", "Destination", filter=text, width=170, pinned="left")
    gb.configure_column("trip_kind", "Trip", filter=text, width=105,
                        headerTooltip="Google's trip-length preset: weekend, 1 week or 2 weeks.")
    gb.configure_column("destination", "Airport", filter=text, width=95)
    gb.configure_column("country", "Country", filter=text, width=140)
    gb.configure_column("region_group", "Region", filter=text, width=140)
    gb.configure_column("price", "Price", filter=num, valueFormatter=MONEY_FMT, width=105, sort="asc")
    gb.configure_column("departure_date", "Depart", filter=date, filterParams=ISO_DATE_FILTER, width=130)
    gb.configure_column("return_date", "Return", filter=date, filterParams=ISO_DATE_FILTER, width=130)
    gb.configure_column("nights", "Nights", filter=num, width=95)
    gb.configure_column("stops", "Stops", filter=num, valueFormatter=STOPS_FMT, width=100,
                        headerTooltip="Filter with numbers: 0 = nonstop, 1 = one stop...")
    gb.configure_column("airline", "Airline", filter=text, width=170)
    gb.configure_column("lowest_seen", "Lowest seen", filter=num, valueFormatter=MONEY_FMT, width=120)
    gb.configure_column("days_tracked", "Days tracked", filter=num, width=120)
    gb.configure_column("vs_typical", "vs our typical", filter=num, comparator=NULLS_LAST, valueFormatter=PCT_FMT, width=130,
                        headerTooltip=f"Price vs this destination's median over its previous days. Blank until "
                                      f"it has {MIN_HISTORY_DAYS} previous days of history.")
    gb.configure_column("google_usual", "Google 'usual'", filter=num, comparator=NULLS_LAST, valueFormatter=MONEY_FMT, width=130,
                        headerTooltip="Google's 'usual price' from the latest Deals list that included this "
                                      "destination. Blank if Google never listed it as a deal.")
    gb.configure_column("vs_google", "vs Google", filter=num, comparator=NULLS_LAST, valueFormatter=PCT_FMT, width=115,
                        headerTooltip="Price vs Google's 'usual price'. -50% = half of what Google says is usual.")
    gb.configure_column("gf_link", "Google Flights", filter=False, sortable=False,
                        cellRenderer=LINK_RENDERER, width=125)
    opts = gb.build()
    # AG Grid 34 row-selection API (the builder still emits the deprecated string form)
    opts["rowSelection"] = {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True}
    opts.pop("autoSizeStrategy", None)  # keep column widths; scroll sideways instead of squashing 16 columns
    return AgGrid(table, gridOptions=opts, height=560, theme="streamlit",
                  update_on=["selectionChanged"], allow_unsafe_jscode=True,
                  key="fares_grid", show_search=True, show_download_button=True)


tab_fares, tab_map, tab_hist, tab_deals, tab_status = st.tabs(
    ["Fares", "Map", "Price history", "Google Deals", "Collection status"])

# ---- Fares -----------------------------------------------------------------
with tab_fares:
    dom = view[view["region_group"] == "Domestic"]
    intl = view[~view["region_group"].isin(["Domestic", "Hawaii", "Alaska"])]
    m1, m2, m3 = st.columns(3)
    m1.metric("Destinations shown", len(view))
    if len(dom):
        m2.metric("Cheapest domestic", f"${dom['price'].min():,.0f}",
                  f"{dom.iloc[0]['origin']} → {dom.iloc[0]['city']}", delta_color="off")
    if len(intl):
        m3.metric("Cheapest international", f"${intl['price'].min():,.0f}",
                  f"{intl.iloc[0]['origin']} → {intl.iloc[0]['city']}", delta_color="off")

    table = view[["origin", "city", "destination", "country", "region_group", "trip_kind", "price",
                  "departure_date",
                  "return_date", "nights", "stops", "airline", "lowest_seen", "days_tracked", "vs_typical",
                  "google_usual", "vs_google", "gf_link"]].reset_index(drop=True)
    table = table.astype(object).where(table.notna(), None)  # NaN -> blank cells in the grid
    grid = fares_grid(table)
    st.caption("Filter any column by typing in the box under its header, or click the filter icon next to "
               "the box for options like 'less than' or date ranges. "
               "Prices are the cheapest round trip Google found in the next ~6 months for each trip length "
               "(weekend, 1 week, 2 weeks), any number of stops, as of the last collection; each trip length is "
               "compared only with itself. Confirm on Google Flights before booking. "
               "Click a row to see its price history.")
    sel = grid.selected_rows
    if sel is not None and len(sel):
        r = sel.iloc[0] if isinstance(sel, pd.DataFrame) else sel[0]
        st.divider()
        show_history(r["origin"], r["destination"], r["trip_kind"])

# ---- Map -------------------------------------------------------------------
with tab_map:
    pts = view.dropna(subset=["lat", "lon"]).copy()
    if pts.empty:
        st.info("Nothing to map with the current filters.")
    else:
        lo, hi = pts["price"].min(), pts["price"].max()
        frac = (pts["price"] - lo) / max(hi - lo, 1)
        pts["color"] = [[int(40 + 200 * f), int(170 - 120 * f), 90, 210] for f in frac]  # green -> red
        pts["label"] = pts.apply(lambda r: f"{r['origin']} → {r['city']} ({r['destination']}): "
                                           f"${r['price']:,.0f}, {r['departure_date']} – {r['return_date']}, "
                                           f"{stops_label(r['stops'])}", axis=1)
        origins_df = pd.DataFrame([{"lat": ORIGIN_COORDS[o][0], "lon": ORIGIN_COORDS[o][1], "label": o}
                                   for o in origins])
        st.pydeck_chart(pdk.Deck(
            initial_view_state=pdk.ViewState(latitude=38, longitude=-100, zoom=2.2),
            layers=[
                pdk.Layer("ScatterplotLayer", pts, get_position=["lon", "lat"], get_fill_color="color",
                          get_radius=60000, radius_min_pixels=4, radius_max_pixels=14, pickable=True),
                pdk.Layer("ScatterplotLayer", origins_df, get_position=["lon", "lat"],
                          get_fill_color=[30, 90, 220, 255], get_radius=80000, radius_min_pixels=6,
                          pickable=True),
            ],
            tooltip={"text": "{label}"},
        ), width="stretch", height=620)
        st.caption("Green = cheaper, red = pricier (relative to what's shown). Hover a dot for details.")

# ---- Price history ---------------------------------------------------------
with tab_hist:
    opts = view.sort_values(["origin", "city", "trip_kind"])
    if opts.empty:
        st.info("No destinations match the filters.")
    else:
        labels = [f"{o} → {c} ({d}) · {k}" for o, c, d, k in
                  zip(opts["origin"], opts["city"], opts["destination"], opts["trip_kind"])]
        choice = st.selectbox("Destination", range(len(labels)), format_func=lambda i: labels[i])
        sel_row = opts.iloc[choice]
        show_history(sel_row["origin"], sel_row["destination"], sel_row["trip_kind"])

# ---- Google Deals ----------------------------------------------------------
with tab_deals:
    st.caption("Google's own 'deals' list and its 'usual price' — shown for comparison only; "
               "our deal scoring will use our own price history.")
    for origin in origins:
        ds = searches[(searches["origin"] == origin) & (searches["engine"] == "google_flights_deals")]
        if ds.empty:
            continue
        last = ds.sort_values("run").iloc[-1]
        d = obs[obs["search_row_id"] == last["id"]].sort_values("discount_pct", ascending=False)
        st.subheader(f"{origin} — {last['run']:%b %d}")
        st.dataframe(
            d[["city", "destination", "country", "price", "typical_price", "discount_pct", "departure_date",
               "return_date", "stops", "airline", "gf_link"]],
            hide_index=True, width="stretch",
            column_config={
                "city": "Destination", "destination": "Airport", "country": "Country",
                "price": st.column_config.NumberColumn("Price", format="$%d"),
                "typical_price": st.column_config.NumberColumn("Google 'usual'", format="$%d"),
                "discount_pct": st.column_config.NumberColumn("% off", format="%d%%"),
                "departure_date": "Depart", "return_date": "Return", "stops": "Stops", "airline": "Airline",
                "gf_link": st.column_config.LinkColumn("Google Flights", display_text="Open ↗"),
            })

# ---- Collection status -----------------------------------------------------
with tab_status:
    per_day = (searches.assign(day=searches["run"].dt.date)
               .groupby("day").agg(searches=("id", "count"), airports=("priced_airports", "sum"))
               .sort_index(ascending=False))
    c1, c2, c3 = st.columns(3)
    c1.metric("Searches stored", len(searches))
    c2.metric("Observations", f"{len(obs):,}")
    c3.metric("Destination airports", obs.groupby("origin")["destination"].nunique().sum())
    st.write("**Per day**")
    st.dataframe(per_day, width="stretch")
    if st.button("Check SerpApi balance (free, uses no search)"):
        try:
            a = requests.get("https://serpapi.com/account.json",
                             params={"api_key": config.SERPAPI_API_KEY}, timeout=20).json()
            st.success(f"{a.get('total_searches_left')} of {a.get('searches_per_month')} searches left · "
                       f"renews {a.get('plan_renewal_date')}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't reach SerpApi: {exc}")
    st.write("**Recent searches**")
    st.dataframe(searches.sort_values("run", ascending=False).head(30)[
                     ["run", "label", "priced_airports", "items", "error"]],
                 hide_index=True, width="stretch",
                 column_config={"run": st.column_config.DatetimeColumn("Run", format="MMM D, h:mm a"),
                                "label": "Search", "priced_airports": "Priced airports",
                                "items": "Places returned", "error": "Error"})
