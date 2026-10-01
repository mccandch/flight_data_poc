"""Thin Travelpayouts Data API client + a normalizer that maps every endpoint's
response into one common fare-observation row."""
import json
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from urllib.parse import quote

import requests

import config

API_ROOT = "https://api.travelpayouts.com"


class CallBudgetExceeded(RuntimeError):
    pass


class TravelpayoutsClient:
    def __init__(self, token: str):
        if not token:
            raise SystemExit("TRAVELPAYOUTS_TOKEN is missing. Copy .env.example to .env and add your token.")
        self.session = requests.Session()
        # Token goes in a header, never the URL, so it never lands in saved files or logs.
        self.session.headers.update({"X-Access-Token": token, "Accept-Encoding": "gzip, deflate"})
        self.calls = 0
        self.calls_by_endpoint = Counter()
        self.errors = defaultdict(list)
        config.RAW_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, path: str, params: dict) -> dict | None:
        if self.calls >= config.MAX_API_CALLS_PER_RUN:
            raise CallBudgetExceeded(f"Hit MAX_API_CALLS_PER_RUN={config.MAX_API_CALLS_PER_RUN}")
        for attempt in range(3):
            time.sleep(config.REQUEST_DELAY_SECONDS)
            self.calls += 1  # every HTTP request counts, retries included
            self.calls_by_endpoint[path] += 1
            try:
                resp = self.session.get(API_ROOT + path, params=params, timeout=45)
            except requests.RequestException as exc:
                self.errors[path].append(f"network: {exc}")
                return None
            if resp.status_code == 429:  # rate limited: back off, then retry
                self.errors[path].append("HTTP 429 (rate limited)")
                time.sleep(20 * (attempt + 1))
                continue
            break

        record = {
            "requested_at": now_utc().isoformat(),
            "path": path,
            "params": params,
            "status": resp.status_code,
        }
        try:
            body = resp.json()
        except ValueError:
            body = None
            record["text"] = resp.text[:2000]
        record["body"] = body
        self._save_raw(path, params, record)

        if resp.status_code != 200 or not isinstance(body, dict) or body.get("success") is False:
            msg = (body or {}).get("error") if isinstance(body, dict) else resp.text[:200]
            self.errors[path].append(f"HTTP {resp.status_code}: {msg}")
            return None
        return body

    def _save_raw(self, path, params, record):
        name = path.strip("/").replace("/", "_")
        tag = "_".join(f"{k}-{v}" for k, v in sorted(params.items()) if k not in ("currency",))
        tag = re.sub(r"[^A-Za-z0-9_.-]", "", tag)[:120]
        fname = config.RAW_DIR / f"{self.calls:04d}_{name}_{tag}.json"
        fname.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")


def fetch_reference_data() -> dict:
    """Airports / cities / countries (public, no token, cached locally)."""
    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in ("airports", "cities", "countries"):
        path = config.REFERENCE_DIR / f"{name}.json"
        if not path.exists():
            resp = requests.get(f"{API_ROOT}/data/en/{name}.json", timeout=60)
            resp.raise_for_status()
            path.write_text(resp.text, encoding="utf-8")
        out[name] = json.loads(path.read_text(encoding="utf-8"))
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
NORMALIZED_FIELDS = [
    "observed_at", "source", "market", "origin", "destination", "origin_airport",
    "destination_airport", "trip_type", "departure_date", "return_date", "trip_length",
    "price", "currency", "airline", "flight_number", "stops", "return_stops",
    "duration_min", "source_last_updated", "expires_at", "actual", "link",
    "google_flights_url", "extra",
]

_KNOWN_KEYS = {
    "origin", "origin_code", "destination", "destination_code", "origin_airport",
    "destination_airport", "price", "value", "departure_at", "depart_date", "return_at",
    "return_date", "airline", "flight_number", "transfers", "number_of_changes",
    "return_transfers", "duration", "found_at", "expires_at", "actual", "link", "currency",
}
_IATA = re.compile(r"^[A-Z]{3}$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)  # API says times are UTC


def _date_part(value) -> str:
    return value[:10] if isinstance(value, str) and len(value) >= 10 else ""


def _looks_like_fare(d) -> bool:
    return isinstance(d, dict) and ("price" in d or "value" in d)


def _iter_fares(data):
    """Yield (keys, fare_dict). Endpoints nest fares differently:
    list of fares (v3, v2) | {DEST: fare} (city-directions) | {DATE: fare} (grouped_prices)
    | {DEST: {stops: fare}} (v1/prices/cheap)."""
    if isinstance(data, list):
        for item in data:
            if _looks_like_fare(item):
                yield (), item
    elif isinstance(data, dict):
        for k, v in data.items():
            if _looks_like_fare(v):
                yield (k,), v
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    if _looks_like_fare(v2):
                        yield (k, k2), v2


def google_flights_url(origin, destination, dep, ret) -> str:
    if not (origin and destination and dep):
        return ""
    q = f"Flights to {destination} from {origin} on {dep}"
    q += f" through {ret}" if ret else " oneway"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def normalize(body: dict, source: str, params: dict, observed_at: datetime) -> list[dict]:
    if not body:
        return []
    currency = body.get("currency") or params.get("currency", "")
    rows = []
    for keys, f in _iter_fares(body.get("data")):
        origin = f.get("origin") or f.get("origin_code") or params.get("origin", "")
        dest = f.get("destination") or f.get("destination_code") or ""
        if not dest and keys and _IATA.match(str(keys[0])):
            dest = keys[0]
        stops = f.get("transfers", f.get("number_of_changes"))
        if stops is None and len(keys) == 2 and str(keys[1]).isdigit():
            stops = int(keys[1])

        dep = _date_part(f.get("departure_at") or f.get("depart_date"))
        ret = _date_part(f.get("return_at") or f.get("return_date"))
        trip_length = ""
        if dep and ret:
            try:
                trip_length = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
            except ValueError:
                pass

        link = f.get("link") or ""
        if link.startswith("/"):
            link = "https://www.aviasales.com" + link
        extra = {k: v for k, v in f.items() if k not in _KNOWN_KEYS}
        if keys:
            extra["_group_keys"] = list(keys)

        rows.append({
            "observed_at": observed_at.isoformat(),
            "source": source,
            "market": params.get("market", ""),
            "origin": origin,
            "destination": dest,
            "origin_airport": f.get("origin_airport", ""),
            "destination_airport": f.get("destination_airport", ""),
            "trip_type": "RT" if ret else "OW",
            "departure_date": dep,
            "return_date": ret,
            "trip_length": trip_length,
            "price": f.get("price", f.get("value")),
            "currency": f.get("currency") or currency,
            "airline": f.get("airline", ""),
            "flight_number": f.get("flight_number", ""),
            "stops": "" if stops is None else stops,
            "return_stops": f.get("return_transfers", ""),
            "duration_min": f.get("duration", ""),
            "source_last_updated": f.get("found_at", ""),
            "expires_at": f.get("expires_at", ""),
            "actual": f.get("actual", ""),
            "link": link,
            "google_flights_url": google_flights_url(origin, dest, dep, ret),
            "extra": json.dumps(extra, ensure_ascii=False) if extra else "",
        })
    return rows
