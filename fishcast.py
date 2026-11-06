#!/usr/bin/env python3
"""
fishcast — "is it good fishing?" from tides (NOAA CO-OPS) + weather (Open-Meteo).

Examples
--------
  # one spot, right now
  python fishcast.py --lat 37.4955 --lon -122.4856

  # one spot, a specific local time, show the hour-by-hour table
  python fishcast.py --lat 26.894 --lon -80.056 --time "2026-11-27 06:00" --hourly

  # all of your favourite spots from a YAML file, ranked
  python fishcast.py --spots spots.yaml --time "2026-11-08 05:00" --hours 18

  # print to the terminal only, don't save a report
  python fishcast.py --spots spots.yaml --no-save

Times you type are interpreted in the spot's local time zone.
Each run also saves its output to reports/<spot-or-file>_<YYYYmmdd-HHMMSS>.txt
(override with --outdir or --out; reports/ is gitignored).

Data sources (both free, no API key):
  * NOAA CO-OPS Metadata API  -> list of tide-prediction stations (cached 30 days)
  * NOAA CO-OPS Data API      -> high/low tide predictions (product=predictions, interval=hilo)
  * Open-Meteo forecast API   -> wind, gusts, pressure, rain, cloud, sunrise/sunset
  * Open-Meteo marine API     -> wave height (optional; silently skipped if unavailable)

The scoring is a transparent rule-of-thumb model, not gospel. Tune the
numbers in the `settings:` block of your YAML (or DEFAULTS below).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

NOAA_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
NOAA_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
OM_MARINE = "https://marine-api.open-meteo.com/v1/marine"
APP_NAME = "fishcast"
CACHE_DIR = Path.home() / ".cache" / "fishcast"
UTC = timezone.utc

DEFAULTS = {
    "calm_wind_mph": 8,      # at or below this, wind score is perfect
    "max_wind_mph": 20,      # wind score falls to 0 here
    "max_gust_mph": 28,      # gusts above this -> flagged + capped
    "max_wave_ft": 6,        # waves above this -> flagged + capped
    "max_station_km": 40,    # warn if the nearest tide station is farther than this
    "prefer_tide": "any",    # any | incoming | outgoing
    "good_score": 60,        # threshold for "best windows"
    "window_hours": 24,      # how far ahead to search for best windows
    "weights": {"tide": 40, "wind": 25, "pressure": 15, "light": 10, "precip": 10},
}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = f"{APP_NAME}/1.0 (personal fishing forecast script)"


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------
@dataclass
class Spot:
    name: str
    lat: float
    lon: float
    station: str | None = None          # optional NOAA station override
    notes: str = ""
    overrides: dict = field(default_factory=dict)  # per-spot settings


@dataclass
class TideEvent:
    t: datetime   # UTC
    h: float      # ft above MLLW
    kind: str     # "H" or "L"


@dataclass
class HourScore:
    t: datetime
    score: float
    verdict: str
    parts: dict
    pros: list
    cons: list
    info: dict


# ----------------------------------------------------------------------------
# NOAA CO-OPS
# ----------------------------------------------------------------------------
def load_stations(refresh: bool = False) -> list[dict]:
    """All NOAA tide-prediction stations, cached on disk for 30 days."""
    cache = CACHE_DIR / "tide_stations.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 30 * 86400:
        cached = json.loads(cache.read_text())
        if cached:                      # never trust an empty cache
            return cached

    r = SESSION.get(NOAA_STATIONS, params={"type": "tidepredictions"}, timeout=60)
    r.raise_for_status()
    j = r.json()

    # The station array's key differs by station type ("stationList", "stations", ...),
    # so take the first top-level value that is a list of dicts.
    raw = next((v for v in j.values()
                if isinstance(v, list) and v and isinstance(v[0], dict)), []) if isinstance(j, dict) else j

    def first(d, *keys):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
        raise KeyError(keys[0])

    stations = []
    for s in raw:
        try:
            stations.append({
                "id": str(first(s, "id", "stationId")),
                "name": s.get("name") or s.get("stationName") or "",
                "state": s.get("state") or "",
                "lat": float(first(s, "lat", "latitude")),
                "lon": float(first(s, "lng", "lon", "long", "longitude")),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if not stations:
        keys = list(j.keys()) if isinstance(j, dict) else type(j).__name__
        sample = raw[0] if raw else None
        raise RuntimeError(
            f"Could not parse NOAA station list (top-level keys: {keys}; first row: {sample}). "
            f"URL: {r.url}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stations))
    return stations


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def pick_station(spot: Spot, stations: list[dict]) -> tuple[dict, float]:
    if spot.station:
        for s in stations:
            if s["id"] == str(spot.station):
                return s, haversine_km(spot.lat, spot.lon, s["lat"], s["lon"])
        return {"id": str(spot.station), "name": "(user-specified)", "state": "",
                "lat": spot.lat, "lon": spot.lon}, 0.0
    best = min(stations, key=lambda s: haversine_km(spot.lat, spot.lon, s["lat"], s["lon"]))
    return best, haversine_km(spot.lat, spot.lon, best["lat"], best["lon"])


def fetch_tide_extremes(station_id: str, start: datetime, end: datetime) -> list[TideEvent]:
    """High/low predictions in UTC. hilo works for both harmonic and subordinate stations."""
    params = {
        "product": "predictions",
        "application": APP_NAME,
        "begin_date": start.astimezone(UTC).strftime("%Y%m%d %H:%M"),
        "end_date": end.astimezone(UTC).strftime("%Y%m%d %H:%M"),
        "datum": "MLLW",
        "station": station_id,
        "time_zone": "gmt",
        "units": "english",
        "interval": "hilo",
        "format": "json",
    }
    r = SESSION.get(NOAA_DATA, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"NOAA error for station {station_id}: {j['error'].get('message')}")
    return [
        TideEvent(datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
                  float(p["v"]), p["type"])
        for p in j.get("predictions", [])
    ]


def tide_state(events: list[TideEvent], t: datetime) -> dict | None:
    """Half-cosine interpolation between consecutive extremes."""
    for a, b in zip(events, events[1:]):
        if a.t <= t <= b.t:
            T = (b.t - a.t).total_seconds() / 3600.0
            s = (t - a.t).total_seconds() / 3600.0 / T
            return {
                "height": a.h + (b.h - a.h) * (1 - math.cos(math.pi * s)) / 2,
                "rate": (b.h - a.h) * math.pi / (2 * T) * math.sin(math.pi * s),  # ft/hr
                "rising": b.h > a.h,
                "movement": math.sin(math.pi * s),   # 0 at slack, 1 at mid-tide
                "range": abs(b.h - a.h),
                "prev": a,
                "next": b,
            }
    return None


# ----------------------------------------------------------------------------
# Open-Meteo
# ----------------------------------------------------------------------------
HOURLY_VARS = ["temperature_2m", "precipitation_probability", "precipitation", "weather_code",
               "pressure_msl", "cloud_cover", "wind_speed_10m", "wind_direction_10m",
               "wind_gusts_10m"]


def fetch_weather(lat: float, lon: float) -> dict:
    """~1 week back + 16 days ahead, hourly, keyed by UTC hour. Also returns tz + sun times."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(HOURLY_VARS),
        "daily": "sunrise,sunset",
        "timezone": "auto",
        "past_days": 7, "forecast_days": 16,
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
    }
    r = SESSION.get(OM_FORECAST, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    tz = ZoneInfo(j.get("timezone", "UTC"))

    def to_utc(s):
        return datetime.fromisoformat(s).replace(tzinfo=tz).astimezone(UTC)

    hourly = {}
    h = j["hourly"]
    for i, ts in enumerate(h["time"]):
        hourly[to_utc(ts)] = {v: h[v][i] for v in HOURLY_VARS}
    sun = []
    for sr, ss in zip(j["daily"]["sunrise"], j["daily"]["sunset"]):
        if sr and ss:
            sun.append((to_utc(sr), to_utc(ss)))
    return {"tz": tz, "hourly": hourly, "sun": sun}


def fetch_waves(lat: float, lon: float) -> dict:
    """Wave height (ft) keyed by UTC hour. Empty dict if the marine API has nothing here."""
    try:
        r = SESSION.get(OM_MARINE, params={
            "latitude": lat, "longitude": lon, "hourly": "wave_height",
            "timezone": "GMT", "past_days": 2, "forecast_days": 8}, timeout=30)
        r.raise_for_status()
        h = r.json()["hourly"]
        return {datetime.fromisoformat(ts).replace(tzinfo=UTC): (v * 3.28084 if v is not None else None)
                for ts, v in zip(h["time"], h["wave_height"])}
    except Exception:
        return {}


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def compass(deg):
    if deg is None:
        return "?"
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][int((deg % 360) / 22.5 + 0.5) % 16]


def verdict(score):
    return "GREAT" if score >= 75 else "GOOD" if score >= 60 else "FAIR" if score >= 45 else "POOR"


def score_hour(t: datetime, tides, wx, waves, cfg, max_range) -> HourScore:
    w = cfg["weights"]
    parts, pros, cons, info = {}, [], [], {}
    cap = 100.0

    # --- Tide: moving water is the main driver ---------------------------------
    ts = tide_state(tides, t) if tides else None
    if ts:
        size = 0.6 + 0.4 * (ts["range"] / max_range if max_range else 1)   # bigger swing, more flow
        tide = ts["movement"] * size
        pref = cfg["prefer_tide"]
        if pref in ("incoming", "outgoing") and ts["rising"] != (pref == "incoming"):
            tide *= 0.6
        parts["tide"] = tide
        info["tide"] = ts
        if ts["movement"] > 0.7:
            pros.append("strong tidal flow")
        elif ts["movement"] < 0.35:
            cons.append("near slack tide")
    else:
        parts["tide"] = 0.5
        cons.append("no tide data")

    hr = t.replace(minute=0, second=0, microsecond=0)
    x = wx["hourly"].get(hr) if wx else None
    if x:
        info["wx"] = x
        # --- Wind ------------------------------------------------------------
        ws, gust = x["wind_speed_10m"] or 0, x["wind_gusts_10m"] or 0
        parts["wind"] = clamp(1 - (ws - cfg["calm_wind_mph"]) / (cfg["max_wind_mph"] - cfg["calm_wind_mph"]))
        if ws <= cfg["calm_wind_mph"]:
            pros.append("light wind")
        elif ws >= cfg["max_wind_mph"] * 0.75:
            cons.append(f"windy ({ws:.0f} mph)")
        if gust > cfg["max_gust_mph"]:
            cons.append(f"gusts {gust:.0f} mph")
            cap = min(cap, 35)

        # --- Barometric trend (3 h) ----------------------------------------
        prev = wx["hourly"].get(hr - timedelta(hours=3))
        if prev and x["pressure_msl"] is not None and prev["pressure_msl"] is not None:
            dp = x["pressure_msl"] - prev["pressure_msl"]
            info["dp3"] = dp
            if -3 < dp <= -0.5:
                parts["pressure"] = 1.0; pros.append("pressure falling slowly")
            elif abs(dp) < 0.5:
                parts["pressure"] = 0.7
            elif 0.5 <= dp < 2:
                parts["pressure"] = 0.5
            elif dp >= 2:
                parts["pressure"] = 0.3; cons.append("pressure rising fast (post-front)")
            else:
                parts["pressure"] = 0.5; cons.append("pressure dropping fast (storm?)")
        else:
            parts["pressure"] = 0.6

        # --- Rain / storms --------------------------------------------------
        pp = x["precipitation_probability"] or 0
        parts["precip"] = clamp(1 - 0.7 * max(0, pp - 20) / 80)
        if (x["weather_code"] or 0) >= 95:
            cons.append("THUNDERSTORMS")
            cap = min(cap, 15)
        elif pp >= 70:
            cons.append(f"rain likely ({pp:.0f}%)")
    else:
        for k in ("wind", "pressure", "precip"):
            parts[k] = 0.6
        cons.append("no weather forecast for this time")

    # --- Light: dawn/dusk bite --------------------------------------------
    if wx and wx["sun"]:
        d = min(min(abs((t - sr).total_seconds()), abs((t - ss).total_seconds()))
                for sr, ss in wx["sun"]) / 3600
        daytime = any(sr <= t <= ss for sr, ss in wx["sun"])
        info["sun_dist_h"], info["daytime"] = d, daytime
        if d <= 1.5:
            parts["light"] = 1.0; pros.append("dawn/dusk")
        elif not daytime:
            parts["light"] = 0.6
        else:
            cloud = (x or {}).get("cloud_cover") or 0
            parts["light"] = 0.4 + 0.4 * cloud / 100   # overcast middays fish better than bright sun
    else:
        parts["light"] = 0.6

    # --- Waves (safety) -------------------------------------------------------
    wv = waves.get(hr) if waves else None
    if wv is not None:
        info["waves_ft"] = wv
        if wv > cfg["max_wave_ft"]:
            cons.append(f"big swell ({wv:.1f} ft)")
            cap = min(cap, 30)

    score = sum(w[k] * parts[k] for k in w) / sum(w.values()) * 100
    score = min(score, cap)
    return HourScore(t, score, verdict(score), parts, pros, cons, info)


# ----------------------------------------------------------------------------
# Running a spot
# ----------------------------------------------------------------------------
def evaluate_spot(spot, when_naive, hours, stations, base_cfg):
    cfg = {**base_cfg, **spot.overrides}
    cfg["weights"] = {**base_cfg["weights"], **spot.overrides.get("weights", {})}

    warnings = []
    try:
        wx = fetch_weather(spot.lat, spot.lon)
        tz = wx["tz"]
    except Exception as e:
        wx, tz = None, datetime.now().astimezone().tzinfo
        warnings.append(f"weather unavailable ({e}); scoring on tides only")

    t0 = (when_naive.replace(tzinfo=tz) if when_naive else datetime.now(tz)).astimezone(UTC)
    t0 = t0.replace(minute=0, second=0, microsecond=0)
    t_end = t0 + timedelta(hours=hours)

    station, dist = pick_station(spot, stations)
    if dist > cfg["max_station_km"]:
        warnings.append(f"nearest tide station is {dist:.0f} km away; tides may not match your spot")
    try:
        tides = fetch_tide_extremes(station["id"], t0 - timedelta(hours=14), t_end + timedelta(hours=14))
    except Exception as e:
        tides = []
        warnings.append(f"tides unavailable ({e})")
    waves = fetch_waves(spot.lat, spot.lon)

    ranges = [abs(b.h - a.h) for a, b in zip(tides, tides[1:])]
    max_range = max(ranges) if ranges else 0
    hourly = [score_hour(t0 + timedelta(hours=i), tides, wx, waves, cfg, max_range)
              for i in range(hours + 1)]
    return {"spot": spot, "tz": tz, "station": station, "dist_km": dist, "tides": tides,
            "hourly": hourly, "warnings": warnings, "cfg": cfg}


def best_windows(hourly, threshold):
    wins, cur = [], []
    for h in hourly:
        if h.score >= threshold:
            cur.append(h)
        elif cur:
            wins.append(cur); cur = []
    if cur:
        wins.append(cur)
    return sorted(wins, key=lambda w: -max(h.score for h in w))


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def fmt_t(t, tz, fmt="%a %H:%M"):
    return t.astimezone(tz).strftime(fmt)


def describe_hour(h: HourScore, tz) -> list[str]:
    lines = [f"  {h.verdict} ({h.score:.0f}/100)"]
    ts = h.info.get("tide")
    if ts:
        mins = (h.t - ts["prev"].t).total_seconds() / 60
        lines.append(
            f"  Tide:  {'rising' if ts['rising'] else 'falling'} {ts['height']:.1f} ft "
            f"({ts['rate']:+.1f} ft/hr), {int(mins // 60)}h{int(mins % 60):02d}m after "
            f"{'low' if ts['prev'].kind == 'L' else 'high'} ({ts['prev'].h:.1f} ft @ {fmt_t(ts['prev'].t, tz, '%H:%M')}); "
            f"next {'high' if ts['next'].kind == 'H' else 'low'} {ts['next'].h:.1f} ft @ {fmt_t(ts['next'].t, tz, '%H:%M')}")
    x = h.info.get("wx")
    if x:
        dp = h.info.get("dp3")
        dp_s = f", {dp:+.1f} hPa/3h" if dp is not None else ""
        lines.append(f"  Wind:  {x['wind_speed_10m']:.0f} mph {compass(x['wind_direction_10m'])} "
                     f"(gusts {x['wind_gusts_10m']:.0f})   Pressure: {x['pressure_msl']:.1f} hPa{dp_s}")
        lines.append(f"  Sky:   {x['cloud_cover']:.0f}% cloud, {x['precipitation_probability'] or 0:.0f}% rain, "
                     f"{x['temperature_2m']:.0f}°F" +
                     (f"   Waves: {h.info['waves_ft']:.1f} ft" if "waves_ft" in h.info else ""))
    tags = [f"+ {p}" for p in h.pros] + [f"- {c}" for c in h.cons]
    if tags:
        lines.append("  " + "   ".join(tags))
    return lines


class Tee:
    """File-like object that writes to several streams (terminal + report file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def print_spot_report(res, show_hourly, out=sys.stdout):
    s, tz = res["spot"], res["tz"]
    st = res["station"]
    print(f"\n=== {s.name}  ({s.lat:.4f}, {s.lon:.4f}) ===", file=out)
    if s.notes:
        print(f"  {s.notes}", file=out)
    print(f"  Tide station: {st['id']} {st['name']} {st['state']} ({res['dist_km']:.1f} km away)", file=out)
    for w in res["warnings"]:
        print(f"  ! {w}", file=out)

    now = res["hourly"][0]
    print(f"\n  At {fmt_t(now.t, tz, '%a %b %d %H:%M %Z')}:", file=out)
    print("\n".join(describe_hour(now, tz)), file=out)

    wins = best_windows(res["hourly"], res["cfg"]["good_score"])
    print(f"\n  Best windows in the next {len(res['hourly']) - 1} h (score ≥ {res['cfg']['good_score']}):",
          file=out)
    if not wins:
        print("    none — nothing scores well in this window", file=out)
    for win in wins[:4]:
        peak = max(win, key=lambda h: h.score)
        why = ", ".join(peak.pros) or "decent conditions"
        print(f"    {fmt_t(win[0].t, tz)}–{fmt_t(win[-1].t + timedelta(hours=1), tz, '%H:%M')}"
              f"   peak {peak.score:.0f} @ {fmt_t(peak.t, tz, '%H:%M')}  ({why})", file=out)

    if show_hourly:
        print("\n  Hour        Score  Tide              Wind      Rain  Notes", file=out)
        for h in res["hourly"]:
            ts, x = h.info.get("tide"), h.info.get("wx") or {}
            tide_s = (f"{'↑' if ts['rising'] else '↓'} {ts['height']:4.1f}ft {ts['rate']:+.1f}/h" if ts else "—")
            wind_s = f"{x.get('wind_speed_10m', 0):.0f}/{x.get('wind_gusts_10m', 0):.0f}mph" if x else "—"
            rain_s = f"{x.get('precipitation_probability') or 0:.0f}%" if x else "—"
            bar = "█" * int(h.score / 10)
            print(f"  {fmt_t(h.t, tz):10s}  {h.score:3.0f} {bar:10s} {tide_s:17s} {wind_s:9s} {rain_s:5s} "
                  f"{'; '.join(h.cons)}", file=out)


def print_summary(results, out=sys.stdout):
    print("\n=== Summary ===", file=out)
    print(f"  {'Spot':28s} {'Now':6s} {'Best window':22s} Peak", file=out)
    rows = []
    for r in results:
        wins = best_windows(r["hourly"], r["cfg"]["good_score"])
        peak = max(r["hourly"], key=lambda h: h.score)
        win_s = (f"{fmt_t(wins[0][0].t, r['tz'])}–{fmt_t(wins[0][-1].t + timedelta(hours=1), r['tz'], '%H:%M')}"
                 if wins else "—")
        rows.append((peak.score, r["spot"].name, r["hourly"][0].verdict, win_s))
    for peak, name, now_v, win_s in sorted(rows, reverse=True):
        print(f"  {name[:28]:28s} {now_v:6s} {win_s:22s} {peak:.0f}", file=out)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def load_spots_yaml(path):
    data = yaml.safe_load(Path(path).read_text()) or {}
    settings = data.get("settings", {}) or {}
    spots = []
    for d in data.get("spots", []):
        known = {"name", "lat", "lon", "station", "notes"}
        spots.append(Spot(name=d["name"], lat=float(d["lat"]), lon=float(d["lon"]),
                          station=str(d["station"]) if d.get("station") else None,
                          notes=d.get("notes", ""),
                          overrides={k: v for k, v in d.items() if k not in known}))
    return spots, settings


def parse_time(s):
    if not s or s.lower() == "now":
        return None
    return datetime.fromisoformat(s.replace("T", " "))


def report_path(a) -> Path | None:
    """Where to save this run's report: --out, else <outdir>/<label>_<run time>.txt."""
    if a.no_save:
        return None
    if a.out:
        return Path(a.out)
    label = Path(a.spots).stem if a.spots else a.name
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-") or "spot"
    return Path(a.outdir) / f"{slug}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tide + weather fishing forecast")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--name", default="My spot")
    ap.add_argument("--station", help="force a NOAA station ID instead of nearest")
    ap.add_argument("--spots", help="YAML file of spots")
    ap.add_argument("--time", help='local time at the spot, e.g. "2026-11-08 06:00" (default: now)')
    ap.add_argument("--hours", type=int, help="hours ahead to search for best windows")
    ap.add_argument("--hourly", action="store_true", help="print hour-by-hour table")
    ap.add_argument("--refresh-stations", action="store_true")
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parent / "reports"),
                    help="directory for saved reports (default: reports/ next to this script)")
    ap.add_argument("--out", help="explicit report file path (overrides --outdir)")
    ap.add_argument("--no-save", action="store_true", help="print to terminal only")
    a = ap.parse_args(argv)

    cfg = json.loads(json.dumps(DEFAULTS))
    if a.spots:
        spots, settings = load_spots_yaml(a.spots)
        cfg.update({k: v for k, v in settings.items() if k != "weights"})
        cfg["weights"].update(settings.get("weights", {}))
    elif a.lat is not None and a.lon is not None:
        spots = [Spot(a.name, a.lat, a.lon, a.station)]
    else:
        ap.error("give --lat/--lon or --spots")

    hours = a.hours or cfg["window_hours"]
    stations = load_stations(a.refresh_stations)
    when = parse_time(a.time)

    path = report_path(a)
    fh = None
    out = sys.stdout
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("w", encoding="utf-8")
        out = Tee(sys.stdout, fh)
        # Header goes to the file only; the terminal already shows the command.
        fh.write(f"fishcast report — generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n")
        fh.write(f"command: {' '.join(['fishcast.py', *(argv if argv is not None else sys.argv[1:])])}\n")

    try:
        results = []
        for s in spots:
            try:
                r = evaluate_spot(s, when, hours, stations, cfg)
                results.append(r)
                print_spot_report(r, a.hourly, out)
            except Exception as e:
                msg = f"\n=== {s.name} === failed: {e}"
                print(msg, file=sys.stderr)
                if fh:
                    print(msg, file=fh)
        if len(results) > 1:
            print_summary(results, out)
    finally:
        if fh:
            fh.close()
            sys.stdout.flush()
            print(f"\nReport saved to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
