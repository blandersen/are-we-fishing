# are-we-fishing

Two scripts for deciding when and where to fish:

- `fishcast.py` answers "is it good fishing?" for a spot (or a list of spots).
  It pulls tide predictions from NOAA and the weather forecast from Open-Meteo,
  scores each hour from 0 to 100, and lists the best time windows to go.
- `conditions.py` shows the raw conditions behind that score: a tide chart,
  swell, the nearest buoy, wind, weather, and the NWS marine forecast and alerts.

The score only flags which spots and hours look promising. It can't judge
things like bay chop, water clarity, bait, or crowds, so look at the
conditions and decide for yourself.

All data sources are free and need no API key.

## Setup

You need Python 3.9 or newer plus `requests` and `pyyaml`:

```bash
pip install -r requirements.txt   # or use a conda env that already has both
```

`conditions.py --plot` also needs `matplotlib`. Everything else works without it.

## fishcast.py usage

```bash
# One spot, right now
python fishcast.py --lat 37.4955 --lon -122.4856 --name "Pillar Point"

# One spot at a given local time, with the hour-by-hour table
python fishcast.py --lat 26.894 --lon -80.056 --time "2026-11-27 06:00" --hourly

# Every spot in spots.yaml, ranked in a summary table
python fishcast.py --spots spots.yaml --time "2026-11-08 05:00" --hours 18
```

| Flag | Meaning |
|---|---|
| `--lat`, `--lon`, `--name` | A single spot (use these or `--spots`) |
| `--spots FILE` | YAML file of spots and settings |
| `--station ID` | Use this NOAA tide station instead of the nearest one |
| `--time "YYYY-MM-DD HH:MM"` | Start time, in the spot's local time zone (default: now) |
| `--hours N` | How many hours ahead to score (default: `window_hours`, 24) |
| `--hourly` | Print the hour-by-hour table |
| `--outdir DIR` | Where to save reports (default: `reports/` next to the script) |
| `--out FILE` | Save the report to this exact file |
| `--no-save` | Print to the terminal only |
| `--refresh-stations` | Download the NOAA station list again instead of using the cache |

## conditions.py usage

```bash
# Everything for one spot, the next 48 h
python conditions.py --lat 37.6330 --lon -122.4960 --name "Pacifica Pier"

# Spots from spots.yaml by name (case-insensitive, any part of the name), 72 h, with a PNG chart
python conditions.py --spots spots.yaml --spot pacifica --hours 72 --plot

# Only some sections, for two spots, starting at a given local time
python conditions.py --spots spots.yaml --spot juno --spot "palm beach" --only tide,swell --time "2026-11-08 05:00"
```

| Flag | Meaning |
|---|---|
| `--lat`, `--lon`, `--name` | A single spot (use these or `--spots`) |
| `--spots FILE`, `--spot NAME` | Spots from a YAML file; `--spot` can be repeated, and without it every spot is shown |
| `--station`, `--buoy`, `--marine-zone` | Use this NOAA tide station, NDBC buoy, or NWS marine zone instead of finding the nearest one |
| `--time`, `--hours N` | Start time (local) and how many hours ahead to show (default 48) |
| `--past N` | Hours of tide history to show before the start time (default 6) |
| `--step N` | Hours between table rows (default 3) |
| `--only LIST` | Comma-separated sections: `tide,swell,buoy,wind,weather,nws` |
| `--plot` | Also save a PNG with tide, waves, wind, and temperature/rain panels |
| `--width N` | Text chart width in characters (default 72) |
| `--outdir`, `--out`, `--no-save`, `--refresh-stations` | Same as for `fishcast.py` |

### What each section shows

- **TIDE**: the NOAA predicted highs and lows, plus a text chart of the tide
  curve (`•`). If the station has a real-time gauge, measured water levels are
  plotted as `o`, and the report gives how far the latest reading is above or
  below the prediction. A positive difference usually means storm surge, wind
  setup, or swell. Some stations publish only highs and lows; for those the
  curve is interpolated, and the report says so.
- **SWELL**: Open-Meteo marine model output. **Total** is the combined sea
  state. It is split into the **primary swell**, the **secondary swell**, and
  short-period **wind waves** (height, period in seconds, and the direction the
  waves come *from*), plus the water temperature. Long-period swell (about 13 s
  or more) breaks noticeably bigger than its height suggests. The header gives
  the model grid point used and its distance from the spot; the model snaps to
  its nearest sea cell, which can be several km offshore or inside a bay.
- **BUOY**: measured data from the nearest NOAA NDBC station that has reported
  waves in the last 6 hours: wave height, dominant/average period, direction,
  and water temperature, plus wind and pressure when the buoy has those sensors.
  Columns a buoy doesn't measure are left out. These are current measurements,
  so they don't change with `--time`.
- **WIND**: hourly forecast of sustained wind (`•`) and gusts (`+`) as a text
  chart, then a table with direction and the 3-hour pressure change.
- **WEATHER**: hourly sky, temperature, chance of rain, rain amount, cloud
  cover, and visibility, then a daily summary with high/low temperatures,
  maximum wind, sunrise, and sunset.
- **NWS MARINE FORECAST**: the National Weather Service forecast for the marine
  zone at the spot, and active alerts such as Small Craft Advisory, High Surf
  Advisory, or Coastal Flood Advisory. Shoreline coordinates often fall just on
  land, so if the spot isn't inside a marine zone the script searches up to
  20 km out. The zone it finds is cached in `~/.cache/fishcast/marine_zones.json`.
  US only.

The `▲` under a chart marks the current time.

## Reports

Each run prints to the terminal and also writes the same text to a file:

```
reports/<spot name or spots-file name>_<YYYYmmdd-HHMMSS>.txt              # fishcast.py
reports/conditions_<spot name or spots-file name>_<YYYYmmdd-HHMMSS>.txt   # conditions.py
reports/conditions_<spot name>_<YYYYmmdd-HHMMSS>.png                      # conditions.py --plot
```

The first two lines of each text file record when it was generated and the
command that made it. Because each file name includes the run time, older
reports are never overwritten. `reports/`, all `*.png` files, and all `*.txt`
files except `requirements.txt` are gitignored.

## How to read a fishcast report

```
=== Pillar Point  (37.4955, -122.4856) ===
  Tide station: 9414131 Pillar Point Harbor, Half Moon Bay CA (0.8 km away)
  Wave model:   grid point (37.458, -122.458), 4.8 km away

  At Fri Nov 06 12:00 PST:
  POOR (30/100)
  Tide:  falling 2.4 ft (-1.3 ft/hr), 3h51m after high (5.9 ft @ 08:09); next low 0.1 ft @ 14:55
  Wind:  8 mph WNW (gusts 8)   Pressure: 1020.0 hPa, -1.0 hPa/3h
  Sky:   42% cloud, 0% rain, 67°F   Waves: 8.6 ft
  + strong tidal flow   + light wind   + pressure falling slowly   - big swell (8.6 ft)

  Best windows in the next 12 h (score ≥ 60):
    none — nothing scores well in this window
```

- **Header**: the spot, the NOAA tide station used for it, and the wave model
  grid point. A line starting with `!` is a warning. Warnings appear when the
  tide station is more than `max_station_km` away, when the wave grid point is
  more than `max_wave_grid_km` away, when a data source failed, or when there
  is no wave data. Without wave data the swell limit can't be applied.
- **At ...**: conditions at the start time.
  - **Verdict**: GREAT (75 or more), GOOD (60 to 74), FAIR (45 to 59), or POOR (under 45).
  - **Tide**: height above MLLW, the rate it is rising or falling, and the
    previous and next high or low.
  - **Wind**: speed, direction, and gusts. **Pressure** is shown with its change
    over the last 3 hours.
  - **Sky** and **Waves**: cloud cover, chance of rain, air temperature, and
    wave height from the Open-Meteo marine model.
  - `+` and `-` tags list what raised or lowered the score.
- **Best windows**: runs of consecutive hours scoring at least `good_score`,
  best first. Each shows its peak hour and why that hour scored well.
- **Hourly table** (with `--hourly`): one row per hour. The bar is the score in
  tens. `↑`/`↓` means the tide is rising or falling, and wind is shown as
  `speed/gust`.
- **Summary** (only when you give more than one spot): the spots ranked by
  their best hourly score, with the verdict at the start time and the best window.

## How the score works

Each hour gets five sub-scores between 0 and 1. The score is their weighted
average, times 100:

| Factor | Default weight | What scores high |
|---|---|---|
| tide | 40 | Moving water: highest at mid-tide, lowest at slack, and higher for bigger tidal ranges. `prefer_tide` reduces the score for the other direction. |
| wind | 25 | At or below `calm_wind_mph`, falling to 0 at `max_wind_mph` |
| pressure | 15 | Pressure falling slowly over 3 h; a fast rise after a front scores low |
| light | 10 | Within 1.5 h of sunrise or sunset; at midday, cloudy scores better than clear |
| precip | 10 | Low chance of rain |

Some conditions cap the score no matter what the other factors say:
thunderstorms cap it at 15, waves above `max_wave_ft` at 30, and gusts above
`max_gust_mph` at 35. The wave cap is skipped for spots set to `waves: off`.

Tide height between NOAA's predicted highs and lows is filled in with a
half-cosine curve. The scoring is a rule of thumb, so tune it to your spots.

## spots.yaml

```yaml
settings:              # global defaults (any key from DEFAULTS in fishcast.py)
  max_wind_mph: 20
  weights: {tide: 40, wind: 25, pressure: 15, light: 10, precip: 10}

spots:
  - name: Pacifica Pier   # required: name, lat, lon
    lat: 37.6330
    lon: -122.4960
    station: "9414131"    # optional: force a NOAA tide station
    buoy: "46237"         # optional: force an NDBC buoy (conditions.py)
    marine_zone: PZZ545   # optional: force an NWS marine zone (conditions.py)
    notes: High pier      # optional: printed under the header
    max_wave_ft: 8        # optional: override any setting for this spot only
    waves: off            # optional: ignore the ocean wave model here (see below)
```

**Check the tide station each spot gets.** By default the scripts use the
closest station in a straight line, which can be the wrong body of water.
Pacifica and Mussel Rock are closest to stations on the San Francisco Bay side
of the Peninsula, and Juno Beach Pier is closest to one on the Intracoastal
Waterway. Bay and waterway tides are later and bigger or smaller than on the
open coast. `spots.yaml` sets `station:` for those three spots to open-coast
stations. When you add a spot, check the `Tide station:` line and set
`station:` if it's on the wrong side.

**Use `waves: off` for sheltered water.** The Open-Meteo wave model is an
ocean model on a coarse grid. Inside a bay it can pick an open-ocean grid
cell, so a calm spot gets capped for swell it never sees. It can also have no
data at all. With `waves: off`, `fishcast.py` skips the wave cap and
`conditions.py` skips the SWELL section. Chop in sheltered water comes mostly
from local wind, and the wind factor in the score is the only signal for it.
Whether a given wind makes your spot choppy depends on its direction and how
much open water it blows across, so that part is your call. `spots.yaml` sets
`waves: off` for Coyote Point, Dumbarton Pier, and Mission Bay. Check the `Wave model:` line
for other inside spots.

## Code layout

`fishcast.py` holds the data fetching, scoring, and report writing, in this order:

| Section | Functions | Purpose |
|---|---|---|
| Config | `DEFAULTS` | Scoring thresholds and weights |
| Data classes | `Spot`, `TideEvent`, `HourScore` | A location, one high or low tide, one scored hour |
| NOAA CO-OPS | `load_stations`, `pick_station`, `fetch_tide_extremes`, `tide_state` | Station list (cached for 30 days in `~/.cache/fishcast/`), nearest station, high/low predictions, interpolated tide state |
| Open-Meteo | `fetch_weather`, `fetch_marine`, `fetch_waves` | Hourly and daily weather, sunrise and sunset, marine model (waves, swell, water temperature) |
| Scoring | `score_hour`, `verdict` | Turns one hour of data into an `HourScore` |
| Running a spot | `evaluate_spot`, `best_windows` | Fetches everything for a spot, scores each hour, finds the good windows |
| Output | `describe_hour`, `print_spot_report`, `print_summary`, `Tee` | Text formatting; `Tee` sends output to both the terminal and the report file |
| CLI | `main`, `load_spots_yaml`, `add_output_args`, `report_path`, `report_output` | Parses arguments and saves the report; the last three are shared with `conditions.py` |

`conditions.py` imports those helpers and adds:

| Section | Functions | Purpose |
|---|---|---|
| Tide | `fetch_tide_series`, `gather_tide` | 6-minute predicted and observed water levels |
| Swell | `gather_marine` | Marine model output plus the grid point's distance from the spot |
| Buoys | `load_buoys`, `fetch_buoy_obs`, `gather_buoy` | NDBC station list (cached for 7 days), realtime observations, nearest buoy that reports waves |
| NWS | `find_marine_zone`, `gather_nws` | Marine zone lookup (cached), zone forecast, active alerts |
| Gather | `gather` | Fetches all requested sections for a spot, in parallel threads |
| Text output | `ascii_chart`, `print_tide`, `print_swell`, `print_buoy`, `print_wind`, `print_weather`, `print_nws` | One printer per section |
| Plot | `plot_png` | Optional matplotlib figure |

All times are handled in UTC inside the code and converted to the spot's local
time zone only for display.

## Data sources

- [NOAA CO-OPS Data API](https://api.tidesandcurrents.noaa.gov/api/prod/): tide predictions (highs and lows, and 6-minute values) and observed water levels
- [NOAA CO-OPS Metadata API](https://api.tidesandcurrents.noaa.gov/mdapi/prod/): the list of tide-prediction stations
- [NOAA NDBC](https://www.ndbc.noaa.gov/): buoy station list and realtime observations
- [NWS API](https://www.weather.gov/documentation/services-web-api): marine zones, zone forecasts, and active alerts
- [Open-Meteo Forecast API](https://open-meteo.com/en/docs): wind, gusts, pressure, rain, cloud, visibility, and sunrise and sunset
- [Open-Meteo Marine API](https://open-meteo.com/en/docs/marine-weather-api): waves, swell, wind waves, and water temperature

NOAA, NDBC, and NWS cover the US only. Outside the US, both scripts still show
the Open-Meteo weather and waves but warn that the nearest tide station is far
away. In `conditions.py`, the buoy and NWS sections report that nothing was found.
