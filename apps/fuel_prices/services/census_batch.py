"""Census multipart batch geocoding with keyed responses and resumable exports."""

import csv
import io
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apps.fuel_prices.services.csv_data import atomic_json, atomic_write, fingerprint, read_input
from apps.fuel_prices.services.geo_utils import US_CODES, city_key, coordinates


ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"


def address_parts(row):
    return tuple(" ".join(value.split()).upper() for value in (
        row["Address"], row["City"], row["State"], row.get("ZIP", ""),
    ))


def batch_csv(addresses):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for query_id, parts in addresses.items():
        writer.writerow([query_id, *parts])
    return stream.getvalue()


def parse_response(text, addresses):
    """Census coordinates are longitude,latitude; response order is not input order."""
    results = {}
    for fields in csv.reader(io.StringIO(text), strict=True):
        if not fields:
            continue
        if len(fields) < 3 or fields[0] not in addresses or fields[0] in results:
            raise ValueError("Unexpected, duplicate, or malformed Census response ID")
        query_id, _, indicator = fields[:3]
        result = {"latitude": "", "longitude": "", "status": "unresolved",
                  "provider": "us_census_batch", "reason": indicator,
                  "geocoded_at": datetime.now(timezone.utc).isoformat()}
        if indicator == "Match":
            if len(fields) != 8:
                raise ValueError(f"Malformed Census match for ID {query_id}")
            point = fields[5].split(",")
            if len(point) != 2 or not coordinates(point[1], point[0]):
                raise ValueError(f"Invalid Census coordinates for ID {query_id}")
            parts = fields[4].rsplit(",", 3)
            expected = addresses[query_id]
            result.update({"matched_address": fields[4], "match_type": fields[3],
                           "tiger_line_id": fields[6], "side": fields[7]})
            if (len(parts) != 4 or parts[2].strip().upper() != expected[2]
                    or (expected[1] and city_key(parts[1]) != city_key(expected[1]))):
                result["reason"] = "matched_locality_differs"
            else:
                result.update({"latitude": point[1].strip(), "longitude": point[0].strip(),
                               "status": "address_match", "reason": "census_address_match",
                               "coordinate_kind": "interpolated_address"})
        elif indicator not in {"No_Match", "Tie"}:
            raise ValueError(f"Unknown Census match indicator: {indicator}")
        results[query_id] = result
    if set(results) != set(addresses):
        raise ValueError("Census response is incomplete; batch was not cached")
    return results


def submit_batch(addresses, benchmark):
    if not 1 <= len(addresses) <= 10000:
        raise ValueError("Census batches must contain 1–10,000 unique addresses")
    boundary = "TrackRoute" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\n"
        f"{benchmark}\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="addressFile"; filename="addresses.csv"\r\n'
        'Content-Type: text/csv; charset=utf-8\r\n\r\n'
        f"{batch_csv(addresses)}\r\n--{boundary}--\r\n"
    ).encode("utf-8")
    request = Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "TrackRouteCensusBatch/1.0",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=180) as response:
                text = response.read().decode("utf-8-sig")
            return parse_response(text, addresses)
        except HTTPError as exc:
            if attempt == 2 or (exc.code != 429 and exc.code < 500):
                raise
            retry = exc.headers.get("Retry-After", "")
            time.sleep(max(2 ** (attempt + 1), int(retry) if retry.isdigit() else 0))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def run_batch_export(source, output, cache_path, report_path, *, batch_size=1000,
                     benchmark="Public_AR_Current", gazetteer=None, offline=False,
                     submit=submit_batch):
    if not 1 <= batch_size <= 10000:
        raise ValueError("batch-size must be between 1 and 10,000")
    if not benchmark or any(c in benchmark for c in "\r\n"):
        raise ValueError("Provide a valid benchmark")
    source_hash = fingerprint(source)
    headers, rows = read_input(source)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {
        "version": 1, "endpoint": ENDPOINT, "queries": {},
    }
    if (not isinstance(cache, dict) or cache.get("version") != 1
            or cache.get("endpoint") != ENDPOINT or not isinstance(cache.get("queries"), dict)):
        raise ValueError("Invalid Census batch cache")
    queries = cache["queries"]

    def key(row):
        return json.dumps([benchmark, *address_parts(row)])

    pending = {}
    for row in rows:
        street, city, state, zipcode = address_parts(row)
        if state in US_CODES and street and key(row) not in queries:
            pending.setdefault(key(row), (street, city, state, zipcode))
    city_cache = {}

    def snapshot():
        results = []
        for row in rows:
            street, city, state, _ = address_parts(row)
            if state not in US_CODES:
                result = {"status": "unresolved", "reason": "unsupported_country_or_state"}
            elif not street:
                result = {"status": "unresolved", "reason": "missing_street_address"}
            else:
                result = queries.get(key(row), {"status": "pending", "reason": "batch_not_completed"})
            if state in US_CODES and gazetteer is not None and result["status"] == "unresolved":
                lookup_key = (city, state)
                if lookup_key not in city_cache:
                    city_cache[lookup_key] = gazetteer.lookup(city, state)
                city_result = city_cache[lookup_key]
                result = {**city_result, "address_reason": result["reason"]}
            lat = result.get("latitude", "") if result["status"] in {"address_match", "city_fallback"} else ""
            lon = result.get("longitude", "") if result["status"] in {"address_match", "city_fallback"} else ""
            if result["status"] in {"address_match", "city_fallback"} and not coordinates(lat, lon):
                raise ValueError("Invalid coordinates in cached or city result")
            results.append({**result, "latitude": lat, "longitude": lon})
        if fingerprint(source) != source_hash:
            raise ValueError("Input changed during processing; rerun with a stable CSV")
        def write_csv(stream):
            writer = csv.DictWriter(stream, fieldnames=[*headers, "latitude", "longitude"])
            writer.writeheader()
            for row, result in zip(rows, results):
                writer.writerow({**row, "latitude": result["latitude"], "longitude": result["longitude"]})
        atomic_write(output, write_csv)
        report = {
            "complete": not any(r["status"] == "pending" for r in results),
            "row_count": len(rows), "counts": dict(Counter(r["status"] for r in results)),
            "source_sha256": source_hash, "output_sha256": fingerprint(output),
            "endpoint": ENDPOINT, "benchmark": benchmark,
            "attribution": "US Census Bureau: interpolated address coordinates and representative place points",
            "results": [{"csv_record": n, **result} for n, result in enumerate(results, 2)],
        }
        atomic_json(report_path, report)
        return report

    items = list(pending.items())
    if offline and items:
        raise ValueError(f"Offline cache lacks {len(items)} address lookups; run online first")
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        addresses = {str(i): parts for i, (_, parts) in enumerate(batch, 1)}
        print(f"Submitting {len(addresses)} addresses ({start + len(batch)}/{len(items)} uncached)", flush=True)
        try:
            returned = submit(addresses, benchmark)
            if set(returned) != set(addresses):
                raise ValueError("Incomplete batch response; refusing to cache it")
            for i, (query_key, _) in enumerate(batch, 1):
                queries[query_key] = returned[str(i)]
            atomic_json(cache_path, cache)
        finally:
            snapshot()
    return snapshot()
