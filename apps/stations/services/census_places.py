"""City fallbacks from the US Census national places gazetteer."""

import csv
import io
import math
import os
import re
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

from apps.stations.services.geo_utils import US_CODES, city_key

GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip"

MUNICIPAL_SUFFIXES = (
    "consolidated government (balance)",
    "unified government (balance)",
    "metro government (balance)",
    "city (balance)",
    "city and borough",
    "metropolitan government",
    "consolidated government",
    "unified government",
    "urban county",
    "municipality",
    "corporation",
    "borough",
    "village",
    "city",
    "town",
    "CDP",
)
SUFFIX = re.compile(
    r"\s+(?:" + "|".join(re.escape(s) for s in MUNICIPAL_SUFFIXES) + r")$"
)


def place_name(name: str) -> str:
    """Remove a terminal Census descriptor, preserving names such as Carson City."""
    return SUFFIX.sub("", name.strip())


class CensusPlaces:
    """Read a national places ZIP and conservatively match city plus US state."""

    def __init__(self, path: Path):
        self._places = defaultdict(dict)
        self._full_names = defaultdict(dict)
        path = Path(path)
        with zipfile.ZipFile(path) as archive:
            member_name = f"{path.stem}.txt"
            if member_name not in archive.namelist():
                candidates = [
                    name
                    for name in archive.namelist()
                    if name.endswith("_Gaz_place_national.txt")
                ]
                if len(candidates) != 1:
                    raise ValueError(
                        "Census places ZIP must contain one national places text file"
                    )
                member_name = candidates[0]
            with archive.open(member_name) as member:
                source = io.TextIOWrapper(member, encoding="utf-8-sig")
                header = source.readline()
                delimiter = "|" if "|" in header else "\t"
                fields = next(csv.reader([header], delimiter=delimiter))
                fields = [name.strip() for name in fields]
                required = {"USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"}
                if not required.issubset(fields):
                    raise ValueError("Census places file is missing required columns")
                reader = csv.DictReader(source, fieldnames=fields, delimiter=delimiter)
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Malformed Census places record")
                    state = row["USPS"].strip().upper()
                    if state not in US_CODES:
                        continue
                    latitude, longitude = (
                        row["INTPTLAT"].strip(),
                        row["INTPTLONG"].strip(),
                    )
                    try:
                        lat, lon = float(latitude), float(longitude)
                    except ValueError as exc:
                        raise ValueError("Invalid Census places coordinates") from exc
                    if not (
                        math.isfinite(lat)
                        and math.isfinite(lon)
                        and -90 <= lat <= 90
                        and -180 <= lon <= 180
                    ):
                        raise ValueError("Census places coordinates out of range")
                    name, source_id = row["NAME"].strip(), row["GEOID"].strip()
                    if not source_id or not name:
                        raise ValueError("Census places record lacks identity")
                    result = {
                        "source_id": source_id,
                        "matched_name": name,
                        "country": "US",
                        "state": state,
                        "latitude": latitude,
                        "longitude": longitude,
                    }
                    self._places[(state, city_key(place_name(name)))][source_id] = (
                        result
                    )
                    self._full_names[(state, city_key(name))][source_id] = result

    def lookup(self, city: str, state: str) -> dict:
        base = {
            "latitude": "",
            "longitude": "",
            "status": "unresolved",
            "provider": "us_census_gazetteer",
            "source_id": "",
            "matched_name": "",
        }
        state = state.strip().upper()
        if state not in US_CODES:
            return {**base, "reason": "unknown_state"}
        if not city_key(city):
            return {**base, "reason": "missing_city"}
        key = (state, city_key(city))
        candidates = self._places.get(key) or self._full_names.get(key)
        if not candidates:
            return {**base, "reason": "city_not_found"}
        if len(candidates) > 1:
            return {
                **base,
                "reason": "ambiguous_city",
                "candidates": [
                    candidates[source_id] for source_id in sorted(candidates)
                ],
            }
        return {
            **base,
            **next(iter(candidates.values())),
            "status": "city_fallback",
            "reason": "unique_census_place",
            "coordinate_kind": "representative_place_internal_point",
        }


def load_census_places(path, offline):
    """Load the local Census Places ZIP, downloading it atomically if allowed."""
    if not path.exists():
        if offline:
            raise ValueError("City gazetteer is missing")

        path.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(path) + ".part")

        try:
            with (
                urlopen(
                    Request(
                        GAZETTEER_URL,
                        headers={"User-Agent": "TrackRouteCensusBatch/1.0"},
                    ),
                    timeout=60,
                ) as response,
                partial.open("wb") as stream,
            ):
                shutil.copyfileobj(response, stream)
            if not zipfile.is_zipfile(partial):
                raise ValueError("Census city download is not a ZIP archive")
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
    return CensusPlaces(path)
