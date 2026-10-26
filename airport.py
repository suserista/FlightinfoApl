#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Swedavia FlightInfo terminal client - modernized rebuild.

APL-project 2026 -- "Reverse Engineer a Result".

This is a modernized rebuild of the original ``airport.py`` that was
observed in the screen recording: a Swedish-language terminal client
talking to Swedavia's FlightInfo API v2.

Preserved from the original (evidence from the recording):
  * Swedish menu with six features plus exit
  * flight cards: flightId, airline, route, status, terminal, gate,
    baggage, scheduled/actual times, D/I marker (inrikes/utrikes)
  * pagination 50 rows at a time ("Visar 50/217", "Visar nasta sida? [j/n]")
  * UTC -> Swedish local time conversion (CET/CEST, DST-safe)
  * destination overview: "Frankfurt (5st) [Germany]"

Modernizations:
  * typed dataclasses + adapter pattern (API JSON -> domain model)
  * retrying requests.Session with timeouts
  * automatic offline fallback to bundled mock data when no API key
  * zoneinfo-based time zones (stdlib, no pytz)
  * ANSI colours, auto-disabled for pipes / NO_COLOR / --no-color

Usage:
    python airport.py               # live mode (needs SWEDAVIA_API_KEY)
    python airport.py --offline     # force offline mode with sample data
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from destinationer import DestinationMapper, render_overview

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
MOCK_FILE = APP_DIR / "mock_flights.json"
CITY_COUNTRY_FILE = APP_DIR / "city_country.json"

API_BASE_URL = "https://api.swedavia.se/flightinfo/v2"
REQUEST_TIMEOUT = 15.0  # seconds
PAGE_SIZE = 50          # same page size as the original ("Visar 50/217")

SWEDAVIA_AIRPORTS: dict[str, str] = {
    "ARN": "Stockholm Arlanda",
    "BMA": "Stockholm Bromma",
    "GOT": "Göteborg Landvetter",
    "MMX": "Malmö Sturup",
    "LLA": "Luleå Kallax",
    "UME": "Umeå",
    "OSD": "Åre Östersund",
    "KRN": "Kiruna",
    "RNB": "Ronneby",
    "SKE": "Skellefteå",
    "VHM": "Vilhelmina",
    "LYC": "Lycksele",
    "HMV": "Hemavan",
    "VBY": "Visby",
    "VXO": "Växjö",
}
SWEDISH_IATA = set(SWEDAVIA_AIRPORTS)

try:
    LOCAL_TZ = ZoneInfo("Europe/Stockholm")
except ZoneInfoNotFoundError:  # e.g. Windows without the tzdata package
    LOCAL_TZ = timezone(timedelta(hours=1))  # CET approximation, no DST

# Status codes -> the Swedish labels seen in the recording.
# Two code families: the offline mock uses full words, the live FlightInfo
# v2 API answers with 3-letter flightLegStatus codes (SCH/SEQ/ACT/LAN/DEL/CAN).
STATUS_SV: dict[str, str] = {
    "SCHEDULED": "Enligt plan",
    "CHECKIN": "Incheckning",
    "CHECKEDIN": "Incheckad",
    "GATEOPEN": "Gate öppen",
    "BOARDING": "Ombordstigning",
    "GATECLOSED": "Gate stängd",
    "FINALCALL": "Sista incheckning",
    "DEPARTED": "Avgick",
    "LANDED": "Landat",
    "DELAYED": "Försenat",
    "CANCELLED": "Inställt",
    "DIVERTED": "Omdirigerat",
    "UNKNOWN": "Okänd status",
    "SCH": "Enligt plan",
    "SEQ": "Beräknad",
    "ACT": "Startat",
    "LAN": "Landat",
    "DEL": "Borttagen",
    "CAN": "Inställt",
}

# --------------------------------------------------------------------------
# ANSI colour handling (auto-disabled when not a TTY)
# --------------------------------------------------------------------------


class Palette:
    """Small ANSI wrapper. Degrades to plain text when colours are off."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def _colours_supported(no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        os.system("")  # enable VT processing on modern Windows terminals
    return sys.stdout.isatty()


# --------------------------------------------------------------------------
# Domain model
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Flight:
    """One flight leg, normalized away from the API's nested JSON."""

    flight_id: str
    airline: str
    origin_iata: str
    origin_city: str
    dest_iata: str
    dest_city: str
    status_en: str
    terminal: str | None
    gate: str | None
    baggage: str | None
    scheduled_utc: datetime | None
    actual_utc: datetime | None
    domestic: bool

    @property
    def status_sv(self) -> str:
        return STATUS_SV.get(self.status_en, self.status_en)

    @property
    def direction_marker(self) -> str:
        return "D" if self.domestic else "I"

    @property
    def sort_key(self) -> datetime:
        return self.scheduled_utc or datetime.max.replace(tzinfo=timezone.utc)

    def to_cet(self, utc: datetime | None) -> str:
        """Format a UTC instant in Swedish local time (CET/CEST)."""
        if utc is None:
            return "-"
        local = utc.astimezone(LOCAL_TZ)
        offset = local.utcoffset() or timedelta(0)
        label = "CEST" if offset >= timedelta(hours=2) else "CET"
        return f"{local:%Y-%m-%d %H:%M} ({label})"


# --------------------------------------------------------------------------
# Adapter: Swedavia v2 JSON -> Flight
# --------------------------------------------------------------------------


def _parse_utc(value: Any) -> datetime | None:
    """Parse ISO-8601 timestamps like 2026-10-26T13:35:00Z."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_time(leg: dict[str, Any], keys: Sequence[str]) -> datetime | None:
    """Pull the first present timestamp out of a leg's time sub-dict."""
    time_obj = leg.get("time") or {}
    for key in keys:
        stamp = (time_obj.get(key) or {}).get("timeUtc")
        parsed = _parse_utc(stamp)
        if parsed:
            return parsed
    return None


def _dig(node: Any, *keys: str) -> Any:
    """Depth-first search for the first of *keys anywhere in nested JSON.

    The live FlightInfo v2 payload nests fields in combinations that vary
    between releases; digging keeps the adapter resilient when the exact
    path is unknown or changes.
    """
    if isinstance(node, dict):
        for key in keys:
            if key in node and node[key] is not None:
                return node[key]
        for value in node.values():
            found = _dig(value, *keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _dig(child, *keys)
            if found is not None:
                return found
    return None


def _as_text(node: Any) -> str | None:
    """Coerce an airport/airline field that may be a str, int or dict."""
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, dict):
        for key in ("name", "city", "airportName", "iata", "code"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def flight_from_api(item: dict[str, Any], leg_kind: str) -> Flight:
    """Normalize one flight record, live schema or mock schema.

    The live API (routes verified against the gateway) answers with
    records built around ``flightLegIdentifier`` / ``locationAndStatus``
    / ``departureTime`` / ``arrivalTime``; the bundled mock data uses a
    nested ``departure``/``arrival`` shape. Both normalize to the same
    ``Flight`` dataclass.

    ``leg_kind`` is "arrival" or "departure" and decides which leg's
    times, gate and baggage belt are shown - exactly like airport
    departure/arrival boards do.
    """
    if "arrival" in item and "departure" not in item and isinstance(item["arrival"], dict):
        return flight_from_api(item["arrival"], "arrival")
    if "departure" in item and "arrival" not in item and isinstance(item["departure"], dict):
        return flight_from_api(item["departure"], "departure")

    if any(key in item for key in ("flightLegIdentifier", "locationAndStatus",
                                   "flightLegStatus", "departureTime", "arrivalTime")):
        return _flight_from_live(item, leg_kind)
    return _flight_from_mock(item, leg_kind)


def _flight_from_live(item: dict[str, Any], leg_kind: str) -> Flight:
    """Adapter for the live FlightInfo v2 record layout."""
    ident = item.get("flightLegIdentifier")
    ident = ident if isinstance(ident, dict) else {}
    flight_id = str(ident.get("flightId") or _dig(item, "flightId") or "????")

    operator = item.get("airlineOperator") or ident.get("airlineOperator") or _dig(item, "airlineOperator")
    if isinstance(operator, str) and operator.strip():
        airline = operator.strip()
    elif isinstance(operator, dict):
        airline = _as_text(operator) or str(
            operator.get("flightAirlineCode") or operator.get("airlineCode") or ""
        ).strip() or None
    else:
        airline = None

    if not airline:
        airline = _as_text(_dig(item, "airlineName", "airline", "airlineCode", "flightAirlineCode")) or "Okänt bolag"

    leg_status = item.get("flightLegStatus")
    if leg_status is None:
        leg_status = _dig(item, "flightLegStatus", "flightStatus", "status")
    if isinstance(leg_status, dict):
        code = (leg_status.get("flightLegStatus") or leg_status.get("code")
                or leg_status.get("status") or "")
    else:
        code = leg_status or ""
    status_en = str(code).upper() or "UNKNOWN"

    dep_raw = (item.get("departureAirportSwedish")
               or _dig(item, "departureAirportEnglish", "departureAirport"))
    arr_raw = (item.get("arrivalAirportSwedish")
               or _dig(item, "arrivalAirportEnglish", "arrivalAirport"))
    dep_iata = str(_as_text(_dig(item, "departureAirportIata", "departureAirportCode"))
                   or (dep_raw.get("iata") if isinstance(dep_raw, dict) else None) or "?")
    arr_iata = str(_as_text(_dig(item, "arrivalAirportIata", "arrivalAirportCode"))
                   or (arr_raw.get("iata") if isinstance(arr_raw, dict) else None) or "?")
    dep_city = _as_text(dep_raw) or SWEDAVIA_AIRPORTS.get(dep_iata) or "?"
    arr_city = _as_text(arr_raw) or SWEDAVIA_AIRPORTS.get(arr_iata) or "?"

    shown_times = (item.get("arrivalTime") if leg_kind == "arrival"
                   else item.get("departureTime")) or {}
    if not isinstance(shown_times, dict):
        shown_times = {}
    scheduled = _parse_utc(shown_times.get("scheduledUtc") or shown_times.get("estimatedUtc"))
    actual = _parse_utc(shown_times.get("actualUtc") or shown_times.get("estimatedUtc"))

    loc = item.get("locationAndStatus")
    loc = loc if isinstance(loc, dict) else {}
    leg_loc = loc.get(leg_kind)
    leg_loc = leg_loc if isinstance(leg_loc, dict) else loc
    terminal = _as_text(_dig(leg_loc, "terminal"))
    gate = _as_text(_dig(leg_loc, "gate"))
    baggage = _as_text(_dig(item, "baggageClaimUnit", "baggageClaim", "baggage"))

    di = str(item.get("diIndicator") or _dig(item, "diIndicator") or "").upper()
    if di == "D":
        domestic = True
    elif di in {"S", "I"}:
        domestic = False
    else:  # fall back to geography: both endpoints in Sweden = domestic
        domestic = dep_iata in SWEDISH_IATA and arr_iata in SWEDISH_IATA

    return Flight(
        flight_id=flight_id,
        airline=airline,
        origin_iata=dep_iata,
        origin_city=dep_city,
        dest_iata=arr_iata,
        dest_city=arr_city,
        status_en=status_en,
        terminal=terminal,
        gate=gate,
        baggage=baggage,
        scheduled_utc=scheduled,
        actual_utc=actual,
        domestic=domestic,
    )


def _flight_from_mock(item: dict[str, Any], leg_kind: str) -> Flight:
    """Adapter for the bundled mock_flights.json record layout."""
    dep = item.get("departure") or {}
    arr = item.get("arrival") or {}

    dep_ap = dep.get("airport") or {}
    arr_ap = arr.get("airport") or {}
    dep_loc = dep.get("location") or {}
    arr_loc = arr.get("location") or {}

    shown = arr if leg_kind == "arrival" else dep
    shown_loc = arr_loc if leg_kind == "arrival" else dep_loc
    other_loc = dep_loc if leg_kind == "arrival" else arr_loc

    status = item.get("status")
    if isinstance(status, dict):
        status_en = str(status.get("code") or "UNKNOWN").upper()
    else:
        status_en = str(status or "UNKNOWN").upper()

    dom_int = str(item.get("domesticInternational") or "").upper()
    if dom_int in {"DOMESTIC", "D"}:
        domestic = True
    elif dom_int in {"INTERNATIONAL", "I"}:
        domestic = False
    else:  # fall back to geography: both endpoints in Sweden = domestic
        domestic = dep_ap.get("iata") in SWEDISH_IATA and arr_ap.get("iata") in SWEDISH_IATA

    return Flight(
        flight_id=str(item.get("flightId") or "????"),
        airline=str((item.get("airline") or {}).get("name") or "Okänt bolag"),
        origin_iata=str(dep_ap.get("iata") or "?"),
        origin_city=str(dep_ap.get("city") or dep_ap.get("name") or "?"),
        dest_iata=str(arr_ap.get("iata") or "?"),
        dest_city=str(arr_ap.get("city") or arr_ap.get("name") or "?"),
        status_en=status_en,
        terminal=shown_loc.get("terminal"),
        gate=shown_loc.get("gate") or other_loc.get("gate"),
        baggage=shown_loc.get("baggage"),
        scheduled_utc=_first_time(shown, ("scheduled", "estimated", "actual")),
        actual_utc=_first_time(shown, ("actual", "estimated")),
        domestic=domestic,
    )


# --------------------------------------------------------------------------
# Data sources: live API + offline mock
# --------------------------------------------------------------------------


class SwedaviaApiError(RuntimeError):
    """Raised when the live API answers with an unrecoverable error."""


class SwedaviaClient:
    """Thin, retrying client for Swedavia FlightInfo API v2."""

    def __init__(self, api_key: str) -> None:
        try:
            from requests.adapters import HTTPAdapter, Retry
        except ImportError:  # pragma: no cover - very old requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "Ocp-Apim-Subscription-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "apl2026-flightinfo-rebuild/1.0",
            }
        )

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{API_BASE_URL}{path}"
        try:
            response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise SwedaviaApiError(f"Nätverksfel mot {url}: {exc}") from exc
        if response.status_code == 401:
            raise SwedaviaApiError("API-nyckeln accepterades inte (HTTP 401).")
        if response.status_code == 404:
            raise SwedaviaApiError(f"Hittade ingen data (HTTP 404) för {path}.")
        if response.status_code != 200:
            raise SwedaviaApiError(f"Oväntat svar {response.status_code} från {path}.")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise SwedaviaApiError(f"Ogiltig JSON från {path}.") from exc

    @staticmethod
    def _flights(payload: Any, leg_kind: str) -> list[Flight]:
        if isinstance(payload, dict):
            items = payload.get("flights") or payload.get("value") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        return [flight_from_api(item, leg_kind) for item in items]

    # -- public API --------------------------------------------------------

    def arrivals(self, airport: str, day: date) -> list[Flight]:
        # Route verified against the live gateway: /{airport}/arrivals/{date}
        data = self._get(f"/{airport}/arrivals/{day:%Y-%m-%d}")
        return self._flights(data, "arrival")

    def departures(self, airport: str, day: date) -> list[Flight]:
        data = self._get(f"/{airport}/departures/{day:%Y-%m-%d}")
        return self._flights(data, "departure")

    def query(self, airport: str, day: date, filter_expr: str) -> list[Flight]:
        pool = self.departures(airport, day) + self.arrivals(airport, day)
        return local_filter(pool, filter_expr)

    def heartbeat(self) -> tuple[bool, int]:
        """Ping the heartbeat endpoint. Returns (ok, latency_ms)."""
        started = time.perf_counter()
        self._get("/heartbeat")
        return True, int((time.perf_counter() - started) * 1000)


class MockSource:
    """Offline data source backed by mock_flights.json.

    The mock file stores records in a nested ``departure``/``arrival``
    shape that ``flight_from_api`` normalizes alongside the live
    FlightInfo v2 layout. Only ARN / 2026-10-26 exists in the file;
    other dates are served by shifting the timestamps so the
    pagination demo works any day.
    """

    def __init__(self, path: Path = MOCK_FILE) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.meta = raw.get("meta", {})
        self.base_date = date.fromisoformat(self.meta["date"])
        self.base_iata = self.meta["airport"]
        self._arrivals = raw.get("arrivals", {}).get("flights", [])
        self._departures = raw.get("departures", {}).get("flights", [])

    def _shift(self, items: list[dict[str, Any]], airport: str, day: date,
               leg_kind: str) -> list[Flight]:
        if airport != self.base_iata:
            return []
        delta = day - self.base_date
        shifted = _shift_items(items, delta) if delta else items
        return [flight_from_api(item, leg_kind) for item in shifted]

    def arrivals(self, airport: str, day: date) -> list[Flight]:
        return self._shift(self._arrivals, airport, day, "arrival")

    def departures(self, airport: str, day: date) -> list[Flight]:
        return self._shift(self._departures, airport, day, "departure")

    def query(self, airport: str, day: date, filter_expr: str) -> list[Flight]:
        pool = self.departures(airport, day) + self.arrivals(airport, day)
        return local_filter(pool, filter_expr)

    def heartbeat(self) -> tuple[bool, int]:
        return True, 42  # simulated latency; printed as simulated


def _shift_items(items: list[dict[str, Any]], delta: timedelta) -> list[dict[str, Any]]:
    """Shift every timestamp in the mock data by ``delta`` days."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "timeUtc" and isinstance(value, str):
                    parsed = _parse_utc(value)
                    out[key] = (parsed + delta).strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else value
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(child) for child in node]
        return node

    return [walk(item) for item in items]


# --------------------------------------------------------------------------
# Tiny local OData-style filter engine (offline mode)
# --------------------------------------------------------------------------

FILTER_FIELDS: dict[str, str] = {
    "airline": "airline",
    "flygbolag": "airline",
    "status": "status",
    "destination": "dest_city",
    "dest": "dest_city",
    "origin": "origin_city",
    "ursprung": "origin_city",
    "gate": "gate",
}

FIELD_GETTERS = {
    "airline": lambda f: f.airline,
    "status": lambda f: f.status_sv,
    "dest_city": lambda f: f.dest_city,
    "origin_city": lambda f: f.origin_city,
    "gate": lambda f: f.gate or "",
}

_CLAUSE_RE = re.compile(r"^(\w+)\s+(eq|ne|gt|lt)\s+'?(.*?)'?$", re.IGNORECASE)


def _tokenize(expr: str) -> list[str]:
    """Split a filter on whitespace but keep quoted values together."""
    parts: list[str] = []
    token = ""
    quote = False
    for ch in expr:
        if ch == "'":
            quote = not quote
            token += ch
        elif ch.isspace() and not quote:
            if token:
                parts.append(token)
                token = ""
        else:
            token += ch
    if token:
        parts.append(token)
    return parts


def _clauses_and_ops(expr: str) -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    ops: list[str] = []
    current: list[str] = []
    for token in _tokenize(expr):
        if token.upper() in {"AND", "OR"} and current:
            ops.append(token.upper())
            clauses.append(" ".join(current))
            current = []
        else:
            current.append(token)
    if current:
        clauses.append(" ".join(current))
    return clauses, ops


def _eval_clause(flight: Flight, clause: str) -> bool:
    match = _CLAUSE_RE.match(clause.strip())
    if not match:
        return False
    field, op, value = match.groups()
    attr = FILTER_FIELDS.get(field.lower(), field.lower())
    getter = FIELD_GETTERS.get(attr)
    actual = str(getter(flight) if getter else "").strip().lower()
    expected = value.strip().strip("'").lower()
    if op.lower() == "eq":
        return expected in actual
    if op.lower() == "ne":
        return expected not in actual
    try:
        left, right = float(actual), float(expected)
    except ValueError:
        left, right = actual, expected
    return left > right if op.lower() == "gt" else left < right


def _eval_expr(flight: Flight, expr: str) -> bool:
    clauses, ops = _clauses_and_ops(expr)
    if not clauses:
        return True
    verdicts = [_eval_clause(flight, clause) for clause in clauses]
    verdict = verdicts[0]
    for op, value in zip(ops, verdicts[1:]):
        verdict = (verdict and value) if op == "AND" else (verdict or value)
    return verdict


def local_filter(flights: Iterable[Flight], expr: str) -> list[Flight]:
    """Evaluate a small subset of OData locally: field op 'value' [and/or]."""
    return [f for f in flights if _eval_expr(f, expr)]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_card(flight: Flight, pal: Palette) -> str:
    """One flight card, mirroring the layout seen in the recording."""
    line = "-" * 68
    kind = "Inrikes (D)" if flight.domestic else "Utrikes (I)"

    status = flight.status_sv
    if flight.status_en in {"LANDED", "DEPARTED", "LAN", "ACT"}:
        status = pal.green(status)
    elif flight.status_en in {"DELAYED", "SEQ"}:
        status = pal.yellow(status)
    elif flight.status_en in {"CANCELLED", "CAN", "DEL"}:
        status = pal.red(status)

    # Pad on plain text first so ANSI escapes never skew the alignment.
    head = f" {flight.flight_id}  {flight.airline}".ljust(45)
    route = f" {flight.origin_city} ({flight.origin_iata}) -> {flight.dest_city} ({flight.dest_iata})"
    details = f" Terminal {flight.terminal or '-'} | Gate {flight.gate or '-'} | Bagage {flight.baggage or '-'}"
    times = (
        f" Planerad {flight.to_cet(flight.scheduled_utc)}"
        f" | Faktisk {flight.to_cet(flight.actual_utc)} | {kind}"
    )
    return "\n".join(
        [line, pal.bold(head) + status.rjust(23), route, pal.dim(details), pal.dim(times), line]
    )


def paginate(flights: Sequence[Flight], pal: Palette, page_size: int = PAGE_SIZE) -> None:
    """Print flights PAGE_SIZE at a time, exactly like the original."""
    total = len(flights)
    if total == 0:
        print(pal.yellow("\nInga flyg hittades för detta urval.\n"))
        return
    start = 0
    while start < total:
        for flight in flights[start : start + page_size]:
            print(render_card(flight, pal))
        shown = min(start + page_size, total)
        print(f"\nVisar {shown}/{total}")
        if shown >= total:
            break
        answer = input("Visar nästa sida? [j/n]: ").strip().lower()
        if answer not in {"j", "ja", "y", "yes"}:
            break
        start += page_size
    print()


# --------------------------------------------------------------------------
# Interactive helpers
# --------------------------------------------------------------------------


def ask_airport() -> str:
    codes = list(SWEDAVIA_AIRPORTS)
    print("\nSwedavias flygplatser:")
    for index, code in enumerate(codes, start=1):
        print(f"  [{index:2}] {code}  {SWEDAVIA_AIRPORTS[code]}")
    while True:
        raw = input(f"Välj flygplats [1-{len(codes)}, Enter = ARN]: ").strip().lower()
        if not raw:
            return "ARN"
        if raw.upper() in SWEDAVIA_AIRPORTS:
            return raw.upper()
        if raw.isdigit() and 1 <= int(raw) <= len(codes):
            return codes[int(raw) - 1]
        print("Ogiltigt val, forsok igen.")


def ask_date() -> date:
    while True:
        raw = input("Datum (ÅÅÅÅ-MM-DD, Enter = idag): ").strip()
        if not raw:
            return date.today()
        try:
            chosen = date.fromisoformat(raw)
        except ValueError:
            print("Ogiltigt datum, anvand formatet ÅÅÅÅ-MM-DD.")
            continue
        if chosen.year < 2000 or chosen.year > 2100:
            print("Datumet ligger utanfor rimligt intervall (2000-2100).")
            continue
        return chosen


def ask_menu_choice() -> str:
    return input("Välj [1-7]: ").strip()


def menu(pal: Palette, offline: bool) -> None:
    mode = pal.yellow("[OFFLINE - exempeldata]") if offline else pal.green("[LIVE - Swedavia API]")
    print()
    print(pal.bold("=" * 62))
    print(pal.bold("   SWEDAVIA FLYGINFORMATION - terminalklient"))
    print(f"   Läge: {mode}")
    print(pal.bold("=" * 62))
    print("  [1] Ankomster")
    print("  [2] Avgångar")
    print("  [3] Sök flyg")
    print("  [4] OData-fråga")
    print("  [5] Hälsokontroll (heartbeat)")
    print("  [6] Destinationer")
    print("  [7] Avsluta")


# --------------------------------------------------------------------------
# Features (one function per menu entry)
# --------------------------------------------------------------------------


def show_board(source: Any, airport: str, day: date, kind: str, pal: Palette) -> None:
    if kind == "arrival":
        title = f"ANKOMSTER - {SWEDAVIA_AIRPORTS.get(airport, airport)} {day:%Y-%m-%d}"
        fetch = source.arrivals
    else:
        title = f"AVGÅNGAR - {SWEDAVIA_AIRPORTS.get(airport, airport)} {day:%Y-%m-%d}"
        fetch = source.departures
    print(f"\n{pal.bold(pal.cyan(title))}\n")
    try:
        flights = fetch(airport, day)
    except SwedaviaApiError as exc:
        print(pal.red(f"Kunde inte hämta tavlan: {exc}\n"))
        return
    flights.sort(key=lambda f: f.sort_key)
    paginate(flights, pal)


def search_flights(source: Any, airport: str, day: date, pal: Palette) -> None:
    needle = input("Flygnummer (eller del av, t.ex. SK): ").strip().upper()
    if not needle:
        print(pal.yellow("Tomt sökord."))
        return
    try:
        pool = source.arrivals(airport, day) + source.departures(airport, day)
    except SwedaviaApiError as exc:
        print(pal.red(f"Kunde inte hämta flyg: {exc}\n"))
        return
    hits = [f for f in pool if needle in f.flight_id.upper()]
    print(f"\n{pal.bold(pal.cyan(f'SÖKRESULTAT - {needle}'))} ({len(hits)} träffar)\n")
    hits.sort(key=lambda f: f.sort_key)
    paginate(hits, pal)


ODATA_HELP = """Bygg en OData-fråga, exempel:  airline eq 'SAS' and status eq 'Landat'
Tillgängliga fält: airline, status, destination, origin, gate
Operatorer: eq, ne, gt, lt   Kombinera med: and, or"""


def run_odata_query(source: Any, airport: str, day: date, pal: Palette) -> None:
    print(f"\n{pal.dim(ODATA_HELP)}")
    expr = input("Filter: ").strip()
    if not expr:
        print(pal.yellow("Tomt filter."))
        return
    print(f"\n{pal.bold(pal.cyan(f'ODATA-FRÅGA - {expr}'))}\n")
    try:
        hits = source.query(airport, day, expr)
    except SwedaviaApiError as exc:
        print(pal.red(f"Fel vid frågekörning: {exc}"))
        return
    hits.sort(key=lambda f: f.sort_key)
    paginate(hits, pal)


def check_heartbeat(source: Any, offline: bool, pal: Palette) -> None:
    print("\nHälsokontroll av API:et ...")
    try:
        ok, latency = source.heartbeat()
    except SwedaviaApiError as exc:
        print(pal.red(f"MISSLYCKADES - {exc}"))
        return
    if ok:
        note = " (simulerad i offline-läge)" if offline else ""
        print(pal.green(f"OK - API:et svarar. Svarstid: {latency} ms{note}"))
    else:
        print(pal.red("MISSLYCKADES - API:et svarar inte."))


def show_destinations(source: Any, airport: str, day: date, pal: Palette) -> None:
    mapper = DestinationMapper(CITY_COUNTRY_FILE)
    try:
        arrivals = source.arrivals(airport, day)
        departures = source.departures(airport, day)
    except SwedaviaApiError as exc:
        print(pal.red(f"Kunde inte hämta flyg: {exc}\n"))
        return
    print(f"\n{pal.bold(pal.cyan('DESTINATIONER'))} - "
          f"{SWEDAVIA_AIRPORTS.get(airport, airport)} {day:%Y-%m-%d}")
    print(f"(baserat på {len(departures)} avgångar och {len(arrivals)} ankomster)\n")
    render_overview(mapper, arrivals, departures)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


def load_env_file() -> None:
    """Read a .env file next to airport.py, if present.

    Supports KEY=VALUE lines; '#' starts a comment. Values already set in
    the real environment always win, so an exported variable overrides .env.
    This keeps the API key out of the command line and out of git
    (.gitignore excludes .env).
    """
    env_path = APP_DIR / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_source(args: argparse.Namespace, pal: Palette) -> tuple[Any, bool]:
    """Live client with automatic fallback to the offline mock."""
    api_key = os.environ.get("SWEDAVIA_API_KEY", "").strip()
    if args.offline or not api_key:
        if not args.offline and not api_key:
            print(pal.yellow("\nIngen SWEDAVIA_API_KEY hittades - byter till offline-läge."))
        return MockSource(), True
    return SwedaviaClient(api_key), False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="airport.py",
        description="Swedavia FlightInfo terminalklient (moderniserad återbyggnad).",
    )
    parser.add_argument("--offline", action="store_true",
                        help="tvinga offline-läge med exempeldata")
    parser.add_argument("--no-color", action="store_true",
                        help="stäng av ANSI-färger")
    args = parser.parse_args(argv)

    # Windows consoles often default to a legacy code page; force UTF-8 so
    # the Swedish characters (å, ä, ö) render correctly in PowerShell/CMD.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    pal = Palette(_colours_supported(args.no_color))
    load_env_file()
    source, offline = build_source(args, pal)

    while True:
        menu(pal, offline)
        try:
            choice = ask_menu_choice()
        except (EOFError, KeyboardInterrupt):
            choice = "7"
        if choice == "1":
            airport, day = ask_airport(), ask_date()
            show_board(source, airport, day, "arrival", pal)
        elif choice == "2":
            airport, day = ask_airport(), ask_date()
            show_board(source, airport, day, "departure", pal)
        elif choice == "3":
            airport, day = ask_airport(), ask_date()
            search_flights(source, airport, day, pal)
        elif choice == "4":
            airport, day = ask_airport(), ask_date()
            run_odata_query(source, airport, day, pal)
        elif choice == "5":
            check_heartbeat(source, offline, pal)
        elif choice == "6":
            airport, day = ask_airport(), ask_date()
            show_destinations(source, airport, day, pal)
        elif choice == "7":
            print("\nHej då!")
            return 0
        else:
            print(pal.yellow("Ogiltigt val - välj 1-7."))


if __name__ == "__main__":
    sys.exit(main())
