# are-we-fishing

`fishcast.py` answers "is it good fishing?" for a spot (or a list of spots).
It pulls tide predictions from NOAA and the weather forecast from Open-Meteo,
scores each hour from 0 to 100, and lists the best time windows to go.

Both data sources are free and need no API key.

## Setup

You need Python 3.9 or newer plus `requests` and `pyyaml`:

```bash
pip install -r requirements.txt   # or use a conda env that already has both
```

## Usage

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

## Reports

Each run prints to the terminal and also writes the same text to a file:

```
reports/<spot name or spots-file name>_<YYYYmmdd-HHMMSS>.txt
```

The first two lines of the file record when the report was generated and the
command that made it. Because each file name includes the run time, older
reports are never overwritten. `reports/` and all `*.txt` files except
`requirements.txt` are gitignored.

## How to read a report

```
=== Pillar Point  (37.4955, -122.4856) ===
  Tide station: 9414131 Pillar Point Harbor, Half Moon Bay CA (0.8 km away)

  At Fri Nov 06 12:00 PST:
  POOR (30/100)
  Tide:  falling 2.4 ft (-1.3 ft/hr), 3h51m after high (5.9 ft @ 08:09); next low 0.1 ft @ 14:55
  Wind:  8 mph WNW (gusts 8)   Pressure: 1020.0 hPa, -1.0 hPa/3h
  Sky:   42% cloud, 0% rain, 67°F   Waves: 8.6 ft
  + strong tidal flow   + light wind   + pressure falling slowly   - big swell (8.6 ft)

  Best windows in the next 12 h (score ≥ 60):
    none — nothing scores well in this window
```

- **Header**: the spot and the NOAA tide station used for it. A line starting
  with `!` is a warning, for example when the nearest station is more than
  `max_station_km` away or a data source failed.
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
`max_gust_mph` at 35.

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
    station: "9414290"    # optional: force a NOAA station
    notes: High pier      # optional: printed under the header
    max_wave_ft: 8        # optional: override any setting for this spot only
```

## Code layout

Everything is in `fishcast.py`, in this order:

| Section | Functions | Purpose |
|---|---|---|
| Config | `DEFAULTS` | Scoring thresholds and weights |
| Data classes | `Spot`, `TideEvent`, `HourScore` | A location, one high or low tide, one scored hour |
| NOAA CO-OPS | `load_stations`, `pick_station`, `fetch_tide_extremes`, `tide_state` | Station list (cached for 30 days in `~/.cache/fishcast/`), nearest station, high/low predictions, interpolated tide state |
| Open-Meteo | `fetch_weather`, `fetch_waves` | Hourly weather, sunrise and sunset, wave height |
| Scoring | `score_hour`, `verdict` | Turns one hour of data into an `HourScore` |
| Running a spot | `evaluate_spot`, `best_windows` | Fetches everything for a spot, scores each hour, finds the good windows |
| Output | `describe_hour`, `print_spot_report`, `print_summary`, `Tee` | Text formatting; `Tee` sends output to both the terminal and the report file |
| CLI | `main`, `load_spots_yaml`, `report_path` | Parses arguments and decides where the report is saved |

All times are handled in UTC inside the code and converted to the spot's local
time zone only for display.

## Data sources

- [NOAA CO-OPS Data API](https://api.tidesandcurrents.noaa.gov/api/prod/): tide predictions (`product=predictions`, `interval=hilo`)
- [NOAA CO-OPS Metadata API](https://api.tidesandcurrents.noaa.gov/mdapi/prod/): the list of tide-prediction stations
- [Open-Meteo Forecast API](https://open-meteo.com/en/docs): wind, gusts, pressure, rain, cloud, and sunrise and sunset
- [Open-Meteo Marine API](https://open-meteo.com/en/docs/marine-weather-api): wave height (skipped if there is no data for the spot)

NOAA tide stations cover the US only. Outside the US, the report will still
show the weather but warn that the nearest tide station is far away.
