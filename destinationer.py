#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Destination statistics for the Swedavia terminal client.

The original project shipped a hand-built ``city_country.json``
(263 cities across 59 countries, according to the narration in the
recording). This module is the modernized equivalent: it loads the
mapping, counts how many flights touch each city, and renders the
"Frankfurt (5st) [Germany]" overview seen in the recording.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # avoid a circular import at runtime
    from airport import Flight

DEFAULT_MAPPING_FILE = Path(__file__).resolve().parent / "city_country.json"

# Live Swedish boards often append the IATA code to the city name,
# e.g. "London LHR", "Rom FCO" - strip that suffix before lookup.
_IATA_SUFFIX = re.compile(r"\s+[A-Z]{3}$")


def _normalize_city(city: str) -> str:
    """Normalize a board name: 'London LHR' -> 'London'."""
    return _IATA_SUFFIX.sub("", city.strip()).strip()


class DestinationMapper:
    """City -> country lookup with graceful handling of unknown cities."""

    def __init__(self, path: Path | str = DEFAULT_MAPPING_FILE) -> None:
        self.path = Path(path)
        self.city_country: dict[str, str] = {}
        self._by_lower: dict[str, str] = {}
        self.misses: Counter[str] = Counter()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit(
                f"Saknar {self.path.name} - filen ingar i projektet och maste ligga "
                "brevid airport.py."
            )
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Ogiltig JSON i {self.path.name}: {exc}")
        if isinstance(raw, dict):
            self.city_country = {
                str(k).strip(): str(v).strip()
                for k, v in raw.items()
                if not str(k).startswith("_")  # allow _comment-style metadata keys
            }
            # Case-insensitive fallback index (live boards capitalize freely).
            self._by_lower = {key.lower(): key for key in self.city_country}

    @property
    def city_count(self) -> int:
        return len(self.city_country)

    @property
    def country_count(self) -> int:
        return len(set(self.city_country.values()))

    def country_of(self, city: str) -> str:
        """Return the country for a city, or 'Okänt land' when missing.

        Tries the raw name, then the IATA-suffix-stripped name, then a
        case-insensitive match - live boards show e.g. "London LHR",
        "Helsingfors" or "KÖPENHAMN" depending on airport and locale.
        """
        base = _normalize_city(city)
        for candidate in (city.strip(), base):
            if candidate and candidate in self.city_country:
                return self.city_country[candidate]
        hit = self._by_lower.get(base.lower() or city.strip().lower())
        if hit:
            return self.city_country[hit]
        self.misses[base or city.strip()] += 1
        return "Okänt land"


def overview_rows(
    mapper: DestinationMapper,
    arrivals: Sequence["Flight"],
    departures: Sequence["Flight"],
) -> list[tuple[str, int, str]]:
    """Count flights per city and attach the country from the mapping.

    Arrivals contribute their origin cities, departures their
    destination cities - together they describe every city this
    airport exchanges traffic with on the given day.
    """
    counts: Counter[str] = Counter()
    for flight in arrivals:
        counts[flight.origin_city] += 1
    for flight in departures:
        counts[flight.dest_city] += 1

    rows = [
        (city, count, mapper.country_of(city))
        for city, count in counts.items()
    ]
    # Sort by count (desc), then city name for stable, readable output.
    rows.sort(key=lambda row: (-row[1], row[0].lower()))
    return rows


def render_overview(
    mapper: DestinationMapper,
    arrivals: Sequence["Flight"],
    departures: Sequence["Flight"],
) -> None:
    """Print the destination overview: 'Frankfurt (5st) [Germany]'."""
    rows = overview_rows(mapper, arrivals, departures)
    if not rows:
        print("Inga destinationer att visa.\n")
        return

    print("=" * 46)
    for city, count, country in rows:
        print(f"{city} ({count}st) [{country}]")
    print("=" * 46)

    total_flights = sum(count for _, count, _ in rows)
    countries = {country for _, _, country in rows if country != "Okänt land"}
    print(
        f"{len(rows)} städer | {len(countries)} länder | {total_flights} flyg "
        f"(kartläggning: {mapper.city_count} städer i {mapper.country_count} länder)"
    )
    if mapper.misses:
        missing = ", ".join(f"{city} (x{n})" for city, n in mapper.misses.most_common(5))
        print(f"OBS: saknar landsdata för: {missing}")
    print()
