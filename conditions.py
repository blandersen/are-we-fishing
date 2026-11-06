#!/usr/bin/env python3
"""
conditions — the raw ocean and weather conditions at a fishing spot.

fishcast.py turns conditions into a score; this script shows them directly:
  tide     NOAA predicted tide curve (text chart), highs/lows, live gauge vs prediction
  swell    Open-Meteo marine model: total waves, primary/secondary swell, wind waves, water temp
  buoy     latest measurements from the nearest NOAA NDBC buoy that reports waves
  wind     hourly wind speed, gusts and direction (text chart + table)
  weather  hourly sky/temp/rain/visibility and a daily summary
  nws      NWS marine zone forecast and active alerts (US only)

Examples
--------
  python conditions.py --lat 37.6330 --lon -122.4960 --name "Pacifica Pier"
  python conditions.py --spots spots.yaml --spot pacifica --hours 72 --plot
  python conditions.py --spots spots.yaml --spot juno --spot "palm beach" --only tide,swell

Each run saves its text to reports/conditions_<spot-or-file>_<YYYYmmdd-HHMMSS>.txt;
--plot also saves a PNG with the same name.
"""
from __future__ import annotations

import argparse
import json
import math
import textwrap
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import fishcast as fc
from fishcast import CACHE_DIR, SESSION, UTC, Spot, compass, fmt_t, haversine_km

NDBC_STATIONS = "https://www.ndbc.noaa.gov/activestations.xml"
NDBC_REALTIME = "https://www.ndbc.noaa.gov/data/realtime2/{id}.txt"
NWS = "https://api.weather.gov"

SECTIONS = ("tide", "swell", "buoy", "wind", "weather", "nws")
MAX_BUOY_KM = 250

MARINE_VARS = ["wave_height", "wave_period", "wave_direction",
               "swell_wave_height", "swell_wave_period", "swell_wave_direction",
               "secondary_swell_wave_height", "secondary_swell_wave_period",
               "secondary_swell_wave_direction",
               "wind_wave_height", "wind_wave_period", "wind_wave_direction",
               "sea_surface_temperature"]
WX_VARS = [*fc.HOURLY_VARS, "visibility"]
DAILY_VARS = ["weather_code", "temperature_2m_max", "temperature_2m_min",
              "precipitation_probability_max", "precipitation_sum",
              "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant"]

# WMO weather codes used by Open-Meteo
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
       56: "frz drizzle", 57: "frz drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
       85: "snow showers", 86: "snow showers", 95: "thunderstorm", 96: "t-storm, hail",
       99: "t-storm, hail"}

M_TO_FT, MS_TO_MPH, FT_TO_MI = 3.28084, 2.23694, 1 / 5280


def c_to_f(c):
    return c * 9 / 5 + 32


def fv(v, spec=".0f", unit=""):
    """Format a possibly-missing value."""
    return "—" if v is None else f"{v:{spec}}{unit}"


# ----------------------------------------------------------------------------
# Tide (NOAA CO-OPS)
# ----------------------------------------------------------------------------
def fetch_tide_series(station_id: str, start: datetime, end: datetime, product: str):
    """6-minute predicted ("predictions") or observed ("water_level") heights, ft above MLLW."""
    params = {"product": product, "application": fc.APP_NAME,
              "begin_date": start.astimezone(UTC).strftime("%Y%m%d %H:%M"),
              "end_date": end.astimezone(UTC).strftime("%Y%m%d %H:%M"),
              "datum": "MLLW", "station": station_id, "time_zone": "gmt",
              "units": "english", "format": "json"}
    if product == "predictions":
        params["interval"] = "6"
    r = SESSION.get(fc.NOAA_DATA, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"].get("message"))
    rows = j.get("predictions" if product == "predictions" else "data", [])
    return [(datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC), float(p["v"]))
            for p in rows if p.get("v") not in (None, "")]


def gather_tide(spot, start, end, stations):
    station, dist = fc.pick_station(spot, stations)
    d = {"station": station, "dist_km": dist, "notes": [], "obs": []}
    d["extremes"] = fc.fetch_tide_extremes(station["id"], start - timedelta(hours=14),
                                           end + timedelta(hours=14))
    try:
        d["pred"] = fetch_tide_series(station["id"], start, end, "predictions")
    except Exception:
        # Subordinate stations publish only highs/lows; rebuild the curve from them.
        n = int((end - start).total_seconds() // 360) + 1
        d["pred"] = [(t, s["height"]) for t in (start + timedelta(minutes=6 * i) for i in range(n))
                     if (s := fc.tide_state(d["extremes"], t))]
        d["notes"].append("curve interpolated from predicted highs/lows (subordinate station)")
    now = datetime.now(UTC)
    if start < now:
        try:
            d["obs"] = fetch_tide_series(station["id"], start, min(end, now), "water_level")
        except Exception:
            d["notes"].append("no real-time water level gauge at this station")
    return d


# ----------------------------------------------------------------------------
# Swell (Open-Meteo marine)
# ----------------------------------------------------------------------------
def gather_marine(spot):
    if spot.overrides.get("waves", True) is False:     # `waves: off` in spots.yaml
        return {"off": True}
    m = fc.fetch_marine(spot.lat, spot.lon, MARINE_VARS, past_days=1, forecast_days=8)
    m["dist_km"] = haversine_km(spot.lat, spot.lon, m["lat"], m["lon"])
    return m


# ----------------------------------------------------------------------------
# Buoys (NOAA NDBC)
# ----------------------------------------------------------------------------
def load_buoys(refresh=False):
    """All active NDBC stations, cached on disk for 7 days."""
    cache = CACHE_DIR / "ndbc_stations.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 7 * 86400:
        cached = json.loads(cache.read_text())
        if cached:
            return cached
    r = SESSION.get(NDBC_STATIONS, timeout=60)
    r.raise_for_status()
    buoys = [{"id": s.get("id"), "name": s.get("name", ""), "type": s.get("type", ""),
              "lat": float(s.get("lat")), "lon": float(s.get("lon")), "met": s.get("met") == "y"}
             for s in ET.fromstring(r.content).iter("station")]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(buoys))
    return buoys


def fetch_buoy_obs(buoy_id: str) -> list[dict]:
    """Last ~45 days of NDBC realtime observations, newest first, in ft / mph / °F."""
    r = SESSION.get(NDBC_REALTIME.format(id=buoy_id.upper()), timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    lines = r.text.splitlines()
    cols = lines[0].lstrip("#").split()
    obs = []
    for ln in lines[2:]:                      # line 1 holds units
        v = ln.split()
        if len(v) != len(cols):
            continue
        row = dict(zip(cols, v))
        t = datetime(int(row["YY"]), int(row["MM"]), int(row["DD"]), int(row["hh"]), int(row["mm"]),
                     tzinfo=UTC)
        x = {k: None if row.get(k, "MM") == "MM" else float(row[k])
             for k in ("WDIR", "WSPD", "GST", "WVHT", "DPD", "APD", "MWD", "PRES", "ATMP", "WTMP", "PTDY")}
        obs.append({
            "t": t,
            "waves_ft": x["WVHT"] * M_TO_FT if x["WVHT"] is not None else None,
            "dom_period": x["DPD"], "avg_period": x["APD"], "wave_dir": x["MWD"],
            "wind_mph": x["WSPD"] * MS_TO_MPH if x["WSPD"] is not None else None,
            "gust_mph": x["GST"] * MS_TO_MPH if x["GST"] is not None else None,
            "wind_dir": x["WDIR"], "pressure": x["PRES"], "ptdy": x["PTDY"],
            "air_f": c_to_f(x["ATMP"]) if x["ATMP"] is not None else None,
            "water_f": c_to_f(x["WTMP"]) if x["WTMP"] is not None else None,
        })
    return obs


def gather_buoy(spot, buoys):
    """Nearest buoy with waves measured in the last 6 h (falls back to any recent data)."""
    if spot.buoy:
        match = [b for b in buoys if b["id"].upper() == spot.buoy.upper()]
        cands = match or [{"id": spot.buoy, "name": "(user-specified)", "lat": spot.lat, "lon": spot.lon}]
    else:
        cands = sorted((b for b in buoys if b["met"]),
                       key=lambda b: haversine_km(spot.lat, spot.lon, b["lat"], b["lon"]))[:8]
    now, fallback = datetime.now(UTC), None
    for b in cands:
        dist = haversine_km(spot.lat, spot.lon, b["lat"], b["lon"])
        if not spot.buoy and dist > MAX_BUOY_KM:
            break
        try:
            obs = fetch_buoy_obs(b["id"])
        except Exception:
            continue
        recent = [o for o in obs if now - o["t"] < timedelta(hours=6)]
        if not recent:
            continue
        res = {"buoy": b, "dist_km": dist, "obs": obs, "no_waves": False}
        if any(o["waves_ft"] is not None for o in recent):
            return res
        fallback = fallback or {**res, "no_waves": True}
    if fallback:
        return fallback
    raise RuntimeError(f"no buoy with recent data within {MAX_BUOY_KM} km")


# ----------------------------------------------------------------------------
# NWS marine forecast + alerts (api.weather.gov)
# ----------------------------------------------------------------------------
def nws_get(path, **params):
    r = SESSION.get(f"{NWS}{path}", params=params, timeout=30,
                    headers={"Accept": "application/geo+json"})
    r.raise_for_status()
    return r.json()


def find_marine_zone(lat, lon):
    """NWS marine zone at the spot, else the first one found on rings 2–20 km out.

    Shoreline coordinates often fall on land, outside every marine zone polygon.
    Results are cached per coordinate in ~/.cache/fishcast/marine_zones.json.
    """
    cache = CACHE_DIR / "marine_zones.json"
    zones = json.loads(cache.read_text()) if cache.exists() else {}
    key = f"{lat:.4f},{lon:.4f}"
    if key in zones:
        return zones[key]
    found = None
    for radius in (0, 2, 5, 10, 20):
        for bearing in ([0] if radius == 0 else range(0, 360, 45)):
            la = lat + radius / 111.0 * math.cos(math.radians(bearing))
            lo = lon + radius / (111.0 * math.cos(math.radians(lat))) * math.sin(math.radians(bearing))
            feats = nws_get("/zones", type="marine", point=f"{la:.4f},{lo:.4f}").get("features", [])
            if feats:
                p = feats[0]["properties"]
                found = {"id": p["id"], "name": p.get("name", ""), "offset_km": radius}
                break
        if found:
            break
    zones[key] = found
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(zones))
    return found


def gather_nws(spot):
    d = {"zone": None, "periods": [], "alerts": [], "notes": []}
    if spot.marine_zone:
        zid = spot.marine_zone.upper()
        try:
            name = nws_get(f"/zones/marine/{zid}")["properties"].get("name", "")
        except Exception:
            name = ""
        d["zone"] = {"id": zid, "name": name, "offset_km": 0}
    else:
        d["zone"] = find_marine_zone(spot.lat, spot.lon)
    if d["zone"]:
        try:
            d["periods"] = nws_get(f"/zones/forecast/{d['zone']['id']}/forecast")["properties"]["periods"]
        except Exception as e:
            d["notes"].append(f"marine forecast unavailable ({e})")
    feats = nws_get("/alerts/active", point=f"{spot.lat:.4f},{spot.lon:.4f}").get("features", [])
    if d["zone"]:
        feats += nws_get(f"/alerts/active/zone/{d['zone']['id']}").get("features", [])
    seen = set()
    for f in feats:
        p = f["properties"]
        if p["id"] not in seen:
            seen.add(p["id"])
            d["alerts"].append(p)
    return d


# ----------------------------------------------------------------------------
# Gather everything for a spot
# ----------------------------------------------------------------------------
def gather(spot, when_naive, hours, past, sections, stations, buoys):
    d = {"spot": spot}
    try:
        d["wx"] = fc.fetch_weather(spot.lat, spot.lon, WX_VARS, DAILY_VARS)
        tz = d["wx"]["tz"]
    except Exception as e:
        d["wx"] = {"error": str(e)}
        tz = datetime.now().astimezone().tzinfo
    d["tz"] = tz
    t0 = (when_naive.replace(tzinfo=tz) if when_naive else datetime.now(tz)).astimezone(UTC)
    d["t0"] = t0.replace(minute=0, second=0, microsecond=0)
    d["t1"] = d["t0"] + timedelta(hours=hours)
    d["t_past"] = d["t0"] - timedelta(hours=past)

    jobs = {"tide": lambda: gather_tide(spot, d["t_past"], d["t1"], stations),
            "swell": lambda: gather_marine(spot),
            "buoy": lambda: gather_buoy(spot, buoys),
            "nws": lambda: gather_nws(spot)}
    jobs = {k: v for k, v in jobs.items() if k in sections}
    with ThreadPoolExecutor(max_workers=len(jobs) or 1) as ex:
        futures = {k: ex.submit(job) for k, job in jobs.items()}
    for k, fut in futures.items():
        try:
            d[k] = fut.result()
        except Exception as e:
            d[k] = {"error": str(e)}
    return d


# ----------------------------------------------------------------------------
# Text output
# ----------------------------------------------------------------------------
def ascii_chart(series, t_start, t_end, tz, width=72, height=10, yfmt="{:5.1f}"):
    """Text line chart. series = [(marker, [(utc_time, value), ...]), ...]; later series draw on top."""
    vals = [v for _, pts in series for t, v in pts if v is not None and t_start <= t <= t_end]
    if not vals:
        return ["    (no data in this window)"]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    span = (t_end - t_start).total_seconds()

    def col(t):
        return min(width - 1, int((t - t_start).total_seconds() / span * width))

    def row(v):
        return height - 1 - round((v - lo) / (hi - lo) * (height - 1))

    grid = [[" "] * width for _ in range(height)]
    for mark, pts in series:
        bins = {}
        for t, v in pts:
            if v is not None and t_start <= t <= t_end:
                bins.setdefault(col(t), []).append(v)
        prev = None
        for c in sorted(bins):
            r = row(sum(bins[c]) / len(bins[c]))
            if prev and c - prev[0] == 1:          # fill steep segments so the line stays connected
                for rr in range(min(prev[1], r) + 1, max(prev[1], r)):
                    grid[rr][c] = mark
            grid[r][c] = mark
            prev = (c, r)

    pad = len(yfmt.format(hi))
    lines = []
    for i, cells in enumerate(grid):
        labeled = i % 3 == 0 or i == height - 1
        label = yfmt.format(hi - i * (hi - lo) / (height - 1)) if labeled else " " * pad
        lines.append(f"    {label} {'┤' if labeled else '│'}{''.join(cells)}")

    # Time axis: ticks every 6/12/24 h of local time, "now" marked with ▲.
    hrs = span / 3600
    every = 6 if hrs <= 54 else 12 if hrs <= 108 else 24
    axis, labels = ["─"] * width, [" "] * (width + 4)
    t = t_start.replace(minute=0, second=0, microsecond=0)
    while t <= t_end:
        loc = t.astimezone(tz)
        if t >= t_start and loc.hour % every == 0:
            c = col(t)
            axis[c] = "┬"
            text = loc.strftime("%a") if loc.hour == 0 else f"{loc.hour:02d}"
            if all(ch == " " for ch in labels[max(0, c - 1):c + len(text) + 1]):
                labels[c:c + len(text)] = list(text)
        t += timedelta(hours=1)
    now = datetime.now(UTC)
    if t_start <= now <= t_end:
        axis[col(now)] = "▲"
    lines.append(f"    {' ' * pad} └{''.join(axis)}")
    lines.append(f"    {' ' * pad}  {''.join(labels).rstrip()}")
    return lines


def now_mark(start, end):
    """Legend entry for the ▲ marker, only when now falls inside the chart."""
    return "   ▲ now" if start <= datetime.now(UTC) <= end else ""


def header(out, title):
    print(f"\n  {title}\n  {'─' * len(title)}", file=out)


def table_times(d, step):
    return [d["t0"] + timedelta(hours=h) for h in range(0, int((d["t1"] - d["t0"]).total_seconds() // 3600) + 1, step)]


def print_tide(d, step, width, out):
    x = d["tide"]
    if "error" in x:
        header(out, "TIDE")
        print(f"  unavailable: {x['error']}", file=out)
        return
    st, tz = x["station"], d["tz"]
    header(out, f"TIDE — NOAA {st['id']} {st['name']} {st['state']} ({x['dist_km']:.1f} km away)".replace("  ", " "))
    if x["dist_km"] > fc.DEFAULTS["max_station_km"]:
        print(f"  ! station is {x['dist_km']:.0f} km away; tides may not match your spot", file=out)
    for n in x["notes"]:
        print(f"  ! {n}", file=out)
    ext = [e for e in x["extremes"] if d["t_past"] <= e.t <= d["t1"]]
    print("  Highs and lows (ft above MLLW):", file=out)
    cells = [f"{fmt_t(e.t, tz, '%a %H:%M')} {'high' if e.kind == 'H' else 'low ':4s} {e.h:5.1f}" for e in ext]
    for i in range(0, len(cells), 3):
        print("    " + "     ".join(cells[i:i + 3]), file=out)

    print("", file=out)
    series = [("•", x["pred"])] + ([("o", x["obs"])] if x["obs"] else [])
    print("\n".join(ascii_chart(series, d["t_past"], d["t1"], tz, width)), file=out)
    print("    • predicted" + ("   o observed" if x["obs"] else "") + now_mark(d["t_past"], d["t1"]), file=out)

    if x["obs"]:
        t_obs, h_obs = x["obs"][-1]
        pred = dict(x["pred"])
        p = pred.get(t_obs)
        diff = f", {h_obs - p:+.2f} ft vs predicted" if p is not None else ""
        print(f"  Latest gauge reading: {h_obs:.2f} ft at {fmt_t(t_obs, tz, '%a %H:%M')}{diff}", file=out)


def print_swell(d, step, width, out):
    m = d["swell"]
    if "off" in m:
        header(out, "SWELL")
        print("  off for this spot (waves: off in spots.yaml). The ocean wave model doesn't cover\n"
              "  sheltered water; check WIND for chop and BUOY for any nearby measurements.", file=out)
        return
    if "error" in m:
        header(out, "SWELL")
        print(f"  unavailable: {m['error']}", file=out)
        return
    tz = d["tz"]
    header(out, f"SWELL — Open-Meteo marine model, grid point ({m['lat']:.3f}, {m['lon']:.3f}), "
                f"{m['dist_km']:.1f} km away")
    rows = [(t, m["hourly"].get(t)) for t in table_times(d, step)]
    if all(r is None or r["wave_height"] is None for _, r in rows):
        print("  ! no wave data for this spot/time", file=out)
        return

    def comp(r, pre):
        h, p, dr = r[f"{pre}_height"], r[f"{pre}_period"], r[f"{pre}_direction"]
        return "—" if h is None else f"{h:4.1f} ft {fv(p, '2.0f')} s {compass(dr):3s}"

    print(f"  {'Time':10s}  {'Total':>7s}   {'Primary swell':16s}  {'Secondary swell':16s}  "
          f"{'Wind waves':16s}  Water", file=out)
    for t, r in rows:
        if r is None:
            continue
        print(f"  {fmt_t(t, tz):10s}  {fv(r['wave_height'], '.1f', ' ft'):>7s}   "
              f"{comp(r, 'swell_wave'):16s}  {comp(r, 'secondary_swell_wave'):16s}  "
              f"{comp(r, 'wind_wave'):16s}  {fv(r['sea_surface_temperature'], '.0f', '°F')}", file=out)
    print("  (period in seconds; direction is where the waves come FROM)", file=out)

    print("", file=out)
    hourly = sorted(m["hourly"].items())
    series = [("+", [(t, r["swell_wave_height"]) for t, r in hourly]),
              ("•", [(t, r["wave_height"]) for t, r in hourly])]
    print("\n".join(ascii_chart(series, d["t0"], d["t1"], tz, width, height=8)), file=out)
    print("    • total wave height (ft)   + primary swell (ft)" + now_mark(d["t0"], d["t1"]), file=out)


def print_buoy(d, step, width, out):
    b = d["buoy"]
    if "error" in b:
        header(out, "BUOY")
        print(f"  unavailable: {b['error']}", file=out)
        return
    tz = d["tz"]
    name = " ".join(b["buoy"]["name"].split())
    header(out, f"BUOY — NDBC {b['buoy']['id'].upper()} {name} ({b['dist_km']:.0f} km away)")
    if b["no_waves"]:
        print("  ! no wave sensor reporting at the nearby buoys; showing the nearest station with data",
              file=out)
    o = b["obs"][0]
    age = (datetime.now(UTC) - o["t"]).total_seconds() / 3600
    print(f"  Latest: {fmt_t(o['t'], tz, '%a %H:%M')} ({age:.1f} h ago) — measured now, "
          f"not forecast for --time", file=out)

    # One reading per hour (the first in each hour), last 12 hours, oldest first.
    seen, hourly = set(), []
    for x in b["obs"]:
        key = x["t"].replace(minute=0)
        if key not in seen and o["t"] - x["t"] < timedelta(hours=12):
            seen.add(key)
            hourly.append(x)

    def num(v, spec, unit=""):
        return None if v is None else f"{v:{spec}}{unit}"

    def per(x):
        if x["dom_period"] is None and x["avg_period"] is None:
            return None
        return f"{fv(x['dom_period'])}/{fv(x['avg_period'])} s"

    # (header, width, row -> text or None); a column with no data in any row is dropped
    cols = [("Waves", 7, lambda x: num(x["waves_ft"], ".1f", " ft")),
            ("Dom/avg per", 11, per),
            ("From", 4, lambda x: None if x["wave_dir"] is None else compass(x["wave_dir"])),
            ("Wind", 7, lambda x: num(x["wind_mph"], ".0f", " mph")),
            ("Gust", 4, lambda x: num(x["gust_mph"], ".0f")),
            ("From", 4, lambda x: None if x["wind_dir"] is None else compass(x["wind_dir"])),
            ("Water", 5, lambda x: num(x["water_f"], ".0f", "°F")),
            ("Air", 4, lambda x: num(x["air_f"], ".0f", "°F")),
            ("Pressure", 10, lambda x: num(x["pressure"], ".1f", " hPa"))]
    hourly = list(reversed(hourly[:12]))
    cols = [c for c in cols if any(c[2](x) is not None for x in hourly)]
    print(f"  {'Time':10s}" + "".join(f"  {h:>{w}s}" for h, w, _ in cols), file=out)
    for x in hourly:
        print(f"  {fmt_t(x['t'], tz):10s}" + "".join(f"  {g(x) or '—':>{w}s}" for _, w, g in cols), file=out)
    if not any(x["wind_mph"] is not None for x in hourly):
        print("  (this buoy has no wind sensor; see WIND below for the forecast)", file=out)


def wx_or_error(d, title, out):
    if "error" in d["wx"]:
        header(out, title)
        print(f"  unavailable: {d['wx']['error']}", file=out)
        return None
    return d["wx"]


def print_wind(d, step, width, out):
    wx = wx_or_error(d, "WIND", out)
    if not wx:
        return
    tz = d["tz"]
    header(out, "WIND — Open-Meteo forecast, 10 m above ground (mph)")
    hourly = sorted(wx["hourly"].items())
    series = [("+", [(t, r["wind_gusts_10m"]) for t, r in hourly]),
              ("•", [(t, r["wind_speed_10m"]) for t, r in hourly])]
    print("\n".join(ascii_chart(series, d["t0"], d["t1"], tz, width, height=8, yfmt="{:3.0f}")), file=out)
    print("    • sustained wind   + gusts" + now_mark(d["t0"], d["t1"]), file=out)
    print(f"\n  {'Time':10s}  {'Wind':>7s}  {'Gust':>4s}  {'From':4s}  Pressure (3 h change)", file=out)
    for t in table_times(d, step):
        r = wx["hourly"].get(t)
        if not r:
            continue
        prev = wx["hourly"].get(t - timedelta(hours=3))
        dp = (r["pressure_msl"] - prev["pressure_msl"]
              if prev and None not in (r["pressure_msl"], prev["pressure_msl"]) else None)
        print(f"  {fmt_t(t, tz):10s}  {fv(r['wind_speed_10m'], '3.0f', ' mph'):>7s}  "
              f"{fv(r['wind_gusts_10m'], '3.0f'):>4s}  {compass(r['wind_direction_10m']):4s}  "
              f"{fv(r['pressure_msl'], '.1f', ' hPa')} ({fv(dp, '+.1f')})", file=out)


def print_weather(d, step, width, out):
    wx = wx_or_error(d, "WEATHER", out)
    if not wx:
        return
    tz = d["tz"]
    header(out, "WEATHER — Open-Meteo forecast")
    print(f"  {'Time':10s}  {'Sky':14s}  {'Temp':>4s}  {'Rain':>4s}  {'Amount':>7s}  {'Cloud':>5s}  Visibility",
          file=out)
    for t in table_times(d, step):
        r = wx["hourly"].get(t)
        if not r:
            continue
        # Open-Meteo reports visibility in ft when imperial units are requested
        vis = r["visibility"] * FT_TO_MI if r["visibility"] is not None else None
        print(f"  {fmt_t(t, tz):10s}  {WMO.get(r['weather_code'], '—'):14s}  "
              f"{fv(r['temperature_2m'], '.0f', '°F'):>4s}  {fv(r['precipitation_probability'], '.0f', '%'):>4s}  "
              f"{fv(r['precipitation'], '.2f', ' in'):>7s}  {fv(r['cloud_cover'], '.0f', '%'):>5s}  "
              f"{fv(vis, '.1f', ' mi')}", file=out)

    days = sorted({t.astimezone(tz).strftime("%Y-%m-%d") for t in table_times(d, 1)})
    print(f"\n  {'Day':10s}  {'Sky':14s}  {'Hi/Lo':>9s}  {'Rain':>4s}  {'Total':>7s}  "
          f"{'Max wind/gust':>13s}  {'From':4s}  Sunrise  Sunset", file=out)
    for day in days:
        x = wx["daily"].get(day)
        if not x:
            continue
        hilo = f"{fv(x['temperature_2m_max'])}/{fv(x['temperature_2m_min'])}°F"
        wind = f"{fv(x['wind_speed_10m_max'])}/{fv(x['wind_gusts_10m_max'])} mph"
        print(f"  {datetime.fromisoformat(day):%a %b %d}  {WMO.get(x['weather_code'], '—'):14s}  {hilo:>9s}  "
              f"{fv(x['precipitation_probability_max'], '.0f', '%'):>4s}  "
              f"{fv(x['precipitation_sum'], '.2f', ' in'):>7s}  {wind:>13s}  "
              f"{compass(x['wind_direction_10m_dominant']):4s}  "
              f"{(x['sunrise'] or '—')[11:16]:7s}  {(x['sunset'] or '—')[11:16]}", file=out)


def print_nws(d, step, width, out):
    n = d["nws"]
    if "error" in n:
        header(out, "NWS")
        print(f"  unavailable: {n['error']}", file=out)
        return
    z = n["zone"]
    if z:
        where = "at the spot" if z["offset_km"] == 0 else f"found {z['offset_km']} km from the spot"
        header(out, f"NWS MARINE FORECAST — {z['id']} {' '.join(z['name'].split())} ({where})")
    else:
        header(out, "NWS MARINE FORECAST")
        print("  no NWS marine zone within 20 km", file=out)
    for note in n["notes"]:
        print(f"  ! {note}", file=out)
    for p in n["periods"][:4]:
        wrapped = textwrap.wrap(p["detailedForecast"], width=86)
        print(f"  {p['name']}:", file=out)
        print("\n".join(f"    {ln}" for ln in wrapped), file=out)

    print("\n  Active alerts:", file=out)
    if not n["alerts"]:
        print("    none", file=out)
    for a in n["alerts"]:
        ends = a.get("ends") or a.get("expires")
        until = f" until {fmt_t(datetime.fromisoformat(ends), d['tz'], '%a %H:%M')}" if ends else ""
        print(f"    ! {a['event']}{until} — {a.get('headline') or ''}".rstrip(" —"), file=out)


PRINTERS = {"tide": print_tide, "swell": print_swell, "buoy": print_buoy,
            "wind": print_wind, "weather": print_weather, "nws": print_nws}


def print_conditions(d, sections, step, width, out):
    s, tz = d["spot"], d["tz"]
    print(f"\n=== {s.name}  ({s.lat:.4f}, {s.lon:.4f}) ===", file=out)
    if s.notes:
        print(f"  {s.notes}", file=out)
    print(f"  {fmt_t(d['t0'], tz, '%a %b %d %H:%M')} to {fmt_t(d['t1'], tz, '%a %b %d %H:%M %Z')}", file=out)
    for k in SECTIONS:
        if k in sections:
            PRINTERS[k](d, step, width, out)


# ----------------------------------------------------------------------------
# PNG plot (optional, needs matplotlib)
# ----------------------------------------------------------------------------
def plot_png(d, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    wx = "error" not in d["wx"]
    panels = [k for k in ("tide", "swell", "wind", "weather")
              if (k in ("wind", "weather") and wx)
              or (k in d and "error" not in d[k] and "off" not in d[k])]
    if not panels:
        return False
    # Plain-text rendering regardless of the user's rc (usetex would treat "%" as a comment)
    with plt.rc_context({"text.usetex": False}):
        return _plot_panels(d, path, panels, plt, mdates)


def _plot_panels(d, path, panels, plt, mdates):
    tz, t0, t1 = d["tz"], d["t0"], d["t1"]
    wx = d["wx"] if "error" not in d["wx"] else None
    fig, axes = plt.subplots(len(panels), 1, figsize=(11, 2.4 * len(panels)), sharex=True, squeeze=False)

    def win(series):
        pts = [(t, v) for t, v in series if v is not None and d["t_past"] <= t <= t1]
        return [t for t, _ in pts], [v for _, v in pts]

    for ax, k in zip(axes[:, 0], panels):
        if k == "tide":
            x = d["tide"]
            ax.plot(*zip(*x["pred"]), label="predicted")
            if x["obs"]:
                ax.plot(*zip(*x["obs"]), ".", ms=2, label="observed")
            for e in x["extremes"]:
                if d["t_past"] <= e.t <= t1:
                    ax.annotate(f"{e.h:.1f}", (e.t, e.h), textcoords="offset points",
                                xytext=(0, 6 if e.kind == "H" else -12), ha="center", fontsize=8)
            ax.set_ylabel("tide (ft MLLW)")
        elif k == "swell":
            h = sorted(d["swell"]["hourly"].items())
            for var, lab in (("wave_height", "total"), ("swell_wave_height", "primary swell"),
                             ("wind_wave_height", "wind waves")):
                ts, vs = win([(t, r[var]) for t, r in h])
                ax.plot(ts, vs, label=lab)
            ax.set_ylabel("waves (ft)")
            ax2 = ax.twinx()
            ts, vs = win([(t, r["swell_wave_period"]) for t, r in h])
            ax2.plot(ts, vs, "k:", lw=1, label="swell period")
            ax2.set_ylabel("period (s)")
        elif k == "wind":
            h = sorted(wx["hourly"].items())
            for var, lab in (("wind_speed_10m", "wind"), ("wind_gusts_10m", "gusts")):
                ts, vs = win([(t, r[var]) for t, r in h])
                ax.plot(ts, vs, label=lab)
            ax.set_ylabel("wind (mph)")
            for t in table_times(d, max(1, int((t1 - t0).total_seconds() // 3600 // 16))):
                r = wx["hourly"].get(t)
                if r and r["wind_direction_10m"] is not None:
                    ax.annotate(compass(r["wind_direction_10m"]), (t, 1.0), xycoords=("data", "axes fraction"),
                                ha="center", va="bottom", fontsize=7)
        elif k == "weather":
            h = sorted(wx["hourly"].items())
            ts, vs = win([(t, r["temperature_2m"]) for t, r in h])
            ax.plot(ts, vs, color="tab:red", label="temp")
            ax.set_ylabel("temp (°F)")
            ax2 = ax.twinx()
            ts, vs = win([(t, r["precipitation_probability"]) for t, r in h])
            ax2.bar(ts, vs, width=1 / 24, alpha=0.3, label="rain chance")
            ax2.set_ylim(0, 100)
            ax2.set_ylabel("rain (%)")
        if wx:                                   # shade night
            for (sr, ss), (sr2, _) in zip(wx["sun"], wx["sun"][1:]):
                ax.axvspan(ss, sr2, color="0.85", zorder=0)
        now = datetime.now(UTC)
        if d["t_past"] <= now <= t1:
            ax.axvline(now, color="k", lw=0.8, ls="--")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8)
    ax = axes[-1, 0]
    ax.set_xlim(d["t_past"], t1)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=tz))
    fig.suptitle(f"{d['spot'].name} — {fmt_t(t0, tz, '%a %b %d %H:%M %Z')}")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def select_spots(spots, terms):
    if not terms:
        return spots
    picked = []
    for term in terms:
        hits = [s for s in spots if term.lower() in s.name.lower()]
        if not hits:
            raise SystemExit(f"no spot matches '{term}'. Spots: {', '.join(s.name for s in spots)}")
        picked += [s for s in hits if s not in picked]
    return picked


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Tide chart, swell, buoy, wind, weather and NWS marine forecast for a spot")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--name", default="My spot")
    ap.add_argument("--station", help="force a NOAA tide station ID")
    ap.add_argument("--buoy", help="force an NDBC buoy ID, e.g. 46012")
    ap.add_argument("--marine-zone", help="force an NWS marine zone, e.g. PZZ545")
    ap.add_argument("--spots", help="YAML file of spots")
    ap.add_argument("--spot", action="append",
                    help="spot from --spots (case-insensitive name fragment); repeatable; default: all")
    ap.add_argument("--time", help='local start time, e.g. "2026-11-08 06:00" (default: now)')
    ap.add_argument("--hours", type=int, default=48, help="hours ahead to show (default 48)")
    ap.add_argument("--past", type=int, default=6, help="hours of tide history before the start (default 6)")
    ap.add_argument("--step", type=int, default=3, help="hours between table rows (default 3)")
    ap.add_argument("--width", type=int, default=72, help="text chart width in characters (default 72)")
    ap.add_argument("--only", help=f"comma-separated sections to show: {','.join(SECTIONS)}")
    ap.add_argument("--plot", action="store_true", help="also save a PNG chart (needs matplotlib)")
    ap.add_argument("--refresh-stations", action="store_true", help="re-download NOAA/NDBC station lists")
    fc.add_output_args(ap)
    a = ap.parse_args(argv)

    sections = set(SECTIONS)
    if a.only:
        sections = {s.strip() for s in a.only.split(",") if s.strip()}
        bad = sections - set(SECTIONS)
        if bad:
            ap.error(f"unknown section(s) {', '.join(sorted(bad))}; choose from {', '.join(SECTIONS)}")

    if a.spots:
        spots = select_spots(fc.load_spots_yaml(a.spots)[0], a.spot)
        label = spots[0].name if len(spots) == 1 else Path(a.spots).stem
    elif a.lat is not None and a.lon is not None:
        spots = [Spot(a.name, a.lat, a.lon, station=a.station, buoy=a.buoy, marine_zone=a.marine_zone)]
        label = a.name
    else:
        ap.error("give --lat/--lon or --spots")

    stations = fc.load_stations(a.refresh_stations) if "tide" in sections else []
    buoys = load_buoys(a.refresh_stations) if "buoy" in sections else []
    when = fc.parse_time(a.time)
    step = max(1, a.step)

    path = fc.report_path(a, label, prefix="conditions_")
    with fc.report_output(path, __file__, argv) as out:
        for s in spots:
            try:
                d = gather(s, when, a.hours, a.past, sections, stations, buoys)
                print_conditions(d, sections, step, a.width, out)
            except Exception as e:
                print(f"\n=== {s.name} === failed: {e}", file=out)
                continue
            if a.plot:
                png_dir = path.parent if path else Path(a.outdir)
                png = png_dir / f"conditions_{fc.slugify(s.name)}_{datetime.now():%Y%m%d-%H%M%S}.png"
                try:
                    if plot_png(d, png):
                        print(f"  Plot saved to {png}", file=out)
                except ImportError:
                    print("  ! --plot needs matplotlib (not installed in this environment)", file=out)


if __name__ == "__main__":
    main()
