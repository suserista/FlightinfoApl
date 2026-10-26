# Swedavia FlightInfo — Terminal Client (Modernized Rebuild)

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Swedavia FlightInfo v2](https://img.shields.io/badge/API-Swedavia_FlightInfo_v2-1B6B7A)
![Offline mode](https://img.shields.io/badge/offline_mode-mock_data-4C7A34)
![Live-tested](https://img.shields.io/badge/live--tested-2026--10-brightgreen)
![GenAI-assisted](https://img.shields.io/badge/GenAI--assisted-documented_in_report-8A2BE2)
![APL 2026](https://img.shields.io/badge/project-APL2026-162235)

APL-project 2026 — **"Reverse Engineer a Result"**

A modernized rebuild of the Swedish-language terminal client that was
observed in the screen recording: a Python CLI app that talks to
[Swedavia's FlightInfo API v2](https://developer.swedavia.se/) and shows
arrivals, departures, flight search, OData queries, an API health check
and a destination overview.

## Files

| File                | Purpose                                                                 |
|---------------------|-------------------------------------------------------------------------|
| `airport.py`        | Main app: menu loop, API client, offline fallback, rendering, pagination |
| `destinationer.py`  | Destination statistics: counts cities, maps cities to countries         |
| `city_country.json` | Hand-built mapping of 250 cities to 67 countries (English + Swedish board names) |
| `mock_flights.json` | Offline sample dataset (ARN, 2026-10-26, **217 arrivals** + 190 departures) |
| `requirements.txt`  | Python dependencies                                                     |

## Quick start

```bash
pip install -r requirements.txt

# Offline mode (no API key needed) — uses bundled sample data
python airport.py --offline
```

### Live mode (optional)

1. Create an account at [apideveloper.swedavia.se](https://apideveloper.swedavia.se/)
   (Sign up, top right).
2. Go to **Products**, open the product containing **FlightInfo – v2**
   (free tier: 10 000 requests/month) and press **Subscribe**.
3. Go to your **Profile** page, find the subscription, click **Show**
   to reveal the key, and copy it.
4. Create a file named `.env` next to `airport.py`:

   ```
   SWEDAVIA_API_KEY=paste-your-key-here
   ```

5. Run `python airport.py` — the app loads `.env` automatically and
   starts in LIVE mode.

Alternatively, export the variable in your shell instead of using `.env`
(Linux/macOS: `export SWEDAVIA_API_KEY="..."`; PowerShell:
`$env:SWEDAVIA_API_KEY="..."`). Shell variables override `.env`.

The key is sent as the `Ocp-Apim-Subscription-Key` header on every
request. `.env` is listed in `.gitignore` — never commit your real key.

Without `SWEDAVIA_API_KEY` set, the app automatically falls back to
offline mode and says so at startup. If a live request fails mid-session
(no network, expired key), the error is reported by the feature that hit it.

## Features (the six menu options)

1. **Ankomster** — arrivals for a chosen Swedavia airport and date, sorted
   by time, paginated 50 at a time (`Visar 50/217`, `Visar nästa sida? [j/n]`).
2. **Avgångar** — departures, same board layout with gate info.
3. **Sök flyg** — substring search on flight number across both boards.
4. **OData-fråga** — guided filter builder, e.g. `airline eq 'SAS' and status eq 'Landat'`.
   Both modes evaluate the expression locally against the day's combined
   board (verified live: the gateway's `/query` route accepts but ignores
   `$filter`, so client-side filtering is the reliable approach).
5. **Hälsokontroll** — heartbeat ping with latency in ms.
6. **Destinationer** — per-city flight counts enriched with countries:
   `Frankfurt (5st) [Germany]`.

Flight cards show flightId, airline, route, Swedish status, terminal,
gate, baggage belt, scheduled/actual times converted from UTC to Swedish
local time (CET/CEST, DST-safe via `zoneinfo`), and a D/I marker
(inrikes/utrikes).

## Modernization notes (vs. the original recording)

| Area          | Original                          | Rebuild                                        |
|---------------|-----------------------------------|------------------------------------------------|
| Structure     | `airport.py` + `destinationer.py` | Same files, clearer module boundaries          |
| Data model    | dicts from JSON                   | `@dataclass Flight` + adapter pattern          |
| HTTP          | plain `requests.get`              | retrying `Session`, timeouts, typed errors     |
| Time zones    | manual UTC+1 offset               | `zoneinfo` (handles CET/CEST daylight saving)  |
| Offline demo  | none                              | `--offline` / automatic fallback via mock data |
| CLI           | none                              | `argparse` (`--offline`, `--no-color`)         |
| Colours       | plain text                        | ANSI colours, auto-off for pipes and `NO_COLOR`|
| Pagination    | 50/page                           | identical (`PAGE_SIZE = 50`)                   |

## Known deviations from the original

* The original menu read `Välj [1-6]`; the recording did not show clearly
  how exit was offered. This rebuild uses an explicit `[7] Avsluta`.
* The original `city_country.json` reportedly held 263 cities in 59
  countries; this rebuild ships 250 cities in 67 countries, including
  Swedish board names (Helsingfors, Köpenhamn, Rom, München, Åbo, Florens,
  ...) and airport-name variants (London Heathrow, ...). The live-board
  names were verified the data-driven way: every unique city on one day's
  live boards across 13 Swedavia airports was diffed against this file and
  every missing name was added. Extending it further is a pure data task.
* All four live routes were **probed against the real gateway**
  (`GET /{airport}/arrivals/{date}`, `GET /{airport}/departures/{date}`,
  `GET /query` with `$filter`, `GET /heartbeat` — each answers 401
  "missing subscription key", i.e. the route exists and expects the key).
  The response adapter normalizes both the live v2 layout
  (`flightLegIdentifier`, `locationAndStatus`, `departureTime`/`arrivalTime`,
  `diIndicator`, 3-letter statuses SCH/SEQ/ACT/LAN/DEL/CAN) and the bundled
  mock layout, so offline and live mode render identically.
* **Live-verified quirks handled by the adapter** (found while testing with
  a real key, 2026-10): `/query` wraps every leg in an `{"arrival": ...}` /
  `{"departure": ...}` envelope and ignores `$filter` (option 4 therefore
  filters locally); the gateway omits the name of the airport being
  queried, which the client resolves from its own `SWEDAVIA_AIRPORTS`
  table; `airlineOperator` appears both as a plain string and as a nested
  object depending on route.

## Requirements

Python 3.10+ (uses `zoneinfo`, `dataclass(slots=True)`), `requests`.
On Windows, `tzdata` is also installed automatically (listed in
`requirements.txt`) because `zoneinfo` needs it there; the app also
force-enables UTF-8 console output so å/ä/ö render correctly in
PowerShell/CMD, and falls back to a fixed CET offset if timezone data
is missing.
