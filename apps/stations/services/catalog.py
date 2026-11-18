"""Read an auditable, immutable station catalog without changing the CSV export."""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from .csv_data import fingerprint, read_input
from .geo_utils import US_CODES, coordinates

CATALOG_VERSION = "station-catalog-v1"
PRICE_ASSUMPTION = (
    "Prices are USD per US gallon for this assignment; the source CSV does not "
    "establish currency, fuel type, or price units."
)


class CatalogIntegrityError(ValueError):
    """The input, enriched output, and completed report do not agree."""


class PricePolicy(StrEnum):
    LOWEST_LISTED = "lowest_listed"


@dataclass(frozen=True, slots=True)
class PriceQuote:
    csv_record: int
    raw_price: str
    price_per_gallon: Decimal | None
    exclusion_reason: str = ""


@dataclass(frozen=True, slots=True)
class GeocodeProvenance:
    csv_record: int
    status: str
    provider: str
    coordinate_kind: str
    reason: str
    details_json: str

    @property
    def details(self):
        """Return a fresh copy; nested provider metadata cannot mutate the catalog."""
        return json.loads(self.details_json)


@dataclass(frozen=True, slots=True)
class StationRow:
    csv_record: int
    source_values: tuple[tuple[str, str], ...]
    raw_latitude: str
    raw_longitude: str
    latitude: float | None
    longitude: float | None
    quote: PriceQuote
    provenance: GeocodeProvenance
    exclusion_reasons: tuple[str, ...] = ()

    def value(self, column):
        return next(value for key, value in self.source_values if key == column)


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    latitude: float | None
    longitude: float | None
    price_per_gallon: Decimal | None
    source_csv_record: int | None
    coordinate_quality: str
    rows: tuple[StationRow, ...] = ()
    exclusion_reasons: tuple[str, ...] = ()

    @property
    def eligible(self):
        return (
            not self.exclusion_reasons
            and self.price_per_gallon is not None
            and self.latitude is not None
            and self.longitude is not None
        )

    @property
    def quotes(self):
        return tuple(row.quote for row in self.rows)


@dataclass(frozen=True, slots=True)
class CatalogExclusion:
    csv_record: int
    station_id: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StationCatalog:
    stations: tuple[Station, ...]
    rows: tuple[StationRow, ...]
    exclusions: tuple[CatalogExclusion, ...]
    source_columns: tuple[str, ...]
    source_sha256: str
    output_sha256: str
    report_sha256: str
    price_policy: PricePolicy
    fingerprint: str
    price_assumption: str = PRICE_ASSUMPTION

    @property
    def row_count(self):
        return len(self.rows)

    @property
    def eligible_stations(self):
        return tuple(station for station in self.stations if station.eligible)

    @property
    def eligible_count(self):
        return sum(station.eligible for station in self.stations)

    @property
    def excluded_count(self):
        return len(self.stations) - self.eligible_count


def catalog_fingerprint(
    source_sha256, output_sha256, report_sha256, policy=PricePolicy.LOWEST_LISTED
):
    """Include provenance and policy in cache identity, even for identical CSVs."""
    payload = json.dumps(
        [
            CATALOG_VERSION,
            source_sha256,
            output_sha256,
            report_sha256,
            PricePolicy(policy),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def select_station_price(rows, policy=PricePolicy.LOWEST_LISTED):
    """Select a valid quote, retaining its exact value and earliest tied record."""
    PricePolicy(policy)
    quotes = (row.quote for row in rows if row.quote.price_per_gallon is not None)
    return min(
        quotes,
        key=lambda quote: (quote.price_per_gallon, quote.csv_record),
        default=None,
    )


def _price_quote(record, value):
    try:
        price = Decimal(value.strip())
    except InvalidOperation:
        price = None
    if price is None or not price.is_finite() or price < 0:
        return PriceQuote(record, value, None, "invalid_price")
    return PriceQuote(record, value, price)


def _read_report(path, source_sha256, output_sha256, row_count):
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogIntegrityError("Geocoding report is not valid JSON") from exc

    if not isinstance(report, dict) or report.get("complete") is not True:
        raise CatalogIntegrityError("Geocoding report must be complete")

    if report.get("source_sha256") != source_sha256:
        raise CatalogIntegrityError("Raw CSV SHA-256 does not match the report")

    if report.get("output_sha256") != output_sha256:
        raise CatalogIntegrityError("Enriched CSV SHA-256 does not match the report")

    if type(report.get("row_count")) is not int or report["row_count"] != row_count:
        raise CatalogIntegrityError("Report row count does not match the CSV")
    results = report.get("results")

    if not isinstance(results, list) or len(results) != row_count:
        raise CatalogIntegrityError("Report results do not cover every CSV record")
    by_record = {}

    for result in results:
        if not isinstance(result, dict):
            raise CatalogIntegrityError("Report result must be an object")
        record = result.get("csv_record")

        if type(record) is not int or not 2 <= record <= row_count + 1:
            raise CatalogIntegrityError("Report csv_record is outside the CSV records")

        if record in by_record:
            raise CatalogIntegrityError("Report contains duplicate csv_record values")

        if not isinstance(result.get("status"), str) or result["status"] not in {
            "address_match",
            "city_fallback",
            "unresolved",
        }:
            raise CatalogIntegrityError("Completed report has an unsupported status")

        if any(key not in result for key in ("latitude", "longitude")):
            raise CatalogIntegrityError("Report result is missing coordinate fields")

        for key in ("country", "state", "provider", "coordinate_kind", "reason"):
            if key in result and not isinstance(result[key], str):
                raise CatalogIntegrityError(f"Report result {key} must be text")
        by_record[record] = result

    expected_counts = dict(Counter(result["status"] for result in results))
    counts = report.get("counts")
    if (
        not isinstance(counts, dict)
        or any(type(count) is not int for count in counts.values())
        or counts != expected_counts
    ):
        raise CatalogIntegrityError("Report status counts do not match its results")
    return by_record


def _station_row(record, source_values, values, provenance):
    raw_lat, raw_lon = values[-2:]
    if (
        str(provenance["latitude"]) != raw_lat
        or str(provenance["longitude"]) != raw_lon
    ):
        raise CatalogIntegrityError(
            f"CSV record {record}: coordinates do not match report association"
        )
    status = provenance["status"]
    if status == "unresolved" and (raw_lat or raw_lon):
        raise CatalogIntegrityError(
            f"CSV record {record}: unresolved result has coordinates"
        )
    reasons = []
    state = source_values["State"].strip().upper()

    if state not in US_CODES:
        reasons.append("unsupported_country_or_state")

    if provenance.get("country", "US").upper() != "US":
        reasons.append("provider_country_mismatch")

    if provenance.get("state", state).strip().upper() != state:
        reasons.append("provider_state_mismatch")

    if not raw_lat or not raw_lon:
        latitude = longitude = None
        reasons.append("missing_coordinates")

    elif not coordinates(raw_lat, raw_lon):
        latitude = longitude = None
        reasons.append("invalid_coordinates")
    else:
        latitude, longitude = float(raw_lat), float(raw_lon)

    if status == "unresolved":
        reasons.append("unresolved_location")
    elif status == "city_fallback" and "country" not in provenance:
        reasons.append("unverified_country")
    expected_kind = {
        "address_match": "interpolated_address",
        "city_fallback": "representative_place_internal_point",
    }.get(status)

    if expected_kind and provenance.get("coordinate_kind") != expected_kind:
        reasons.append("unsupported_coordinate_kind")
    quote = _price_quote(record, source_values["Retail Price"])
    if quote.exclusion_reason:
        reasons.append(quote.exclusion_reason)

    if not source_values["OPIS Truckstop ID"].strip():
        reasons.append("missing_station_id")
    return StationRow(
        record,
        tuple(source_values.items()),
        raw_lat,
        raw_lon,
        latitude,
        longitude,
        quote,
        GeocodeProvenance(
            record,
            status,
            provenance.get("provider", ""),
            provenance.get("coordinate_kind", ""),
            provenance.get("reason", ""),
            json.dumps(
                provenance, sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
        ),
        tuple(reasons),
    )


def _location_identity(row):
    return tuple(
        " ".join(row.value(column).casefold().split())
        for column in ("Address", "City", "State")
    )


def _build_station(station_id, rows, policy):
    quote = select_station_price(rows, policy)
    selected = next(
        (row for row in rows if quote and row.csv_record == quote.csv_record), None
    )
    reasons = []
    if quote is None:
        reasons.append("no_valid_price")
    if len({_location_identity(row) for row in rows}) > 1:
        reasons.append("conflicting_location_identity")
    known_coordinates = {
        (row.latitude, row.longitude)
        for row in rows
        if row.latitude is not None and row.longitude is not None
    }
    if len(known_coordinates) > 1:
        reasons.append("conflicting_coordinates")
    # Coordinate provenance belongs to the selected quote. Never borrow a resolved
    # position from another quote when this source row was unresolved or excluded.
    location_row = selected or rows[0]
    reasons.extend(
        reason for reason in location_row.exclusion_reasons if reason != "invalid_price"
    )
    return Station(
        station_id=station_id,
        latitude=location_row.latitude,
        longitude=location_row.longitude,
        price_per_gallon=quote.price_per_gallon if quote else None,
        source_csv_record=quote.csv_record if quote else None,
        coordinate_quality=location_row.provenance.coordinate_kind
        or location_row.provenance.status,
        rows=tuple(rows),
        exclusion_reasons=tuple(dict.fromkeys(reasons)),
    )


def load_station_catalog(
    csv_path, report_path, policy=PricePolicy.LOWEST_LISTED, *, raw_path
):
    """Validate all three artifacts and keep every original row and listed quote.

    ``raw_path`` is explicit so a report cannot choose which local source to trust.
    A completed export can contain excluded records; report/file disagreement is
    an integrity error and prevents catalog construction altogether.
    """
    policy = PricePolicy(policy)
    source_sha256 = fingerprint(raw_path)
    output_sha256 = fingerprint(csv_path)
    report_sha256 = fingerprint(report_path)
    headers, original_rows = read_input(raw_path)
    report = _read_report(report_path, source_sha256, output_sha256, len(original_rows))
    rows = []
    try:
        with Path(csv_path).open(encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            if next(reader, []) != [*headers, "latitude", "longitude"]:
                raise CatalogIntegrityError(
                    "Enriched CSV must append only latitude and longitude"
                )
            for record, values in enumerate(reader, 2):
                if len(values) != len(headers) + 2:
                    raise CatalogIntegrityError(
                        f"CSV record {record}: incorrect field count"
                    )
                if record - 2 >= len(original_rows):
                    raise CatalogIntegrityError("Enriched CSV has extra rows")
                original = original_rows[record - 2]
                if values[:-2] != list(original.values()):
                    raise CatalogIntegrityError(
                        f"CSV record {record}: original values or order changed"
                    )
                rows.append(_station_row(record, original, values, report[record]))
    except (csv.Error, UnicodeError) as exc:
        raise CatalogIntegrityError("Enriched CSV cannot be parsed") from exc

    if len(rows) != len(original_rows):
        raise CatalogIntegrityError("Enriched CSV is missing original rows")
    if (
        fingerprint(raw_path) != source_sha256
        or fingerprint(csv_path) != output_sha256
        or fingerprint(report_path) != report_sha256
    ):
        raise CatalogIntegrityError("Catalog artifacts changed while being read")
    grouped = defaultdict(list)

    for row in rows:
        station_id = row.value("OPIS Truckstop ID")
        if station_id.strip():
            grouped[station_id].append(row)
    stations = tuple(
        _build_station(station_id, group, policy)
        for station_id, group in grouped.items()
    )

    station_reasons = {
        station.station_id: station.exclusion_reasons for station in stations
    }

    exclusions = []
    for row in rows:
        station_id = row.value("OPIS Truckstop ID")
        reasons = tuple(
            dict.fromkeys(
                (*row.exclusion_reasons, *station_reasons.get(station_id, ()))
            )
        )
        if reasons:
            exclusions.append(
                CatalogExclusion(
                    row.csv_record, station_id if station_id.strip() else None, reasons
                )
            )

    return StationCatalog(
        stations=stations,
        rows=tuple(rows),
        exclusions=tuple(exclusions),
        source_columns=tuple(headers),
        source_sha256=source_sha256,
        output_sha256=output_sha256,
        report_sha256=report_sha256,
        price_policy=policy,
        fingerprint=catalog_fingerprint(
            source_sha256, output_sha256, report_sha256, policy
        ),
    )
