"""Enrich the station CSV through the Census addressbatch endpoint."""

import argparse
import csv
import fcntl
import os
import shutil
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from apps.fuel_prices.services.census_batch import run_batch_export
from apps.fuel_prices.services.census_places import CensusPlaces
from apps.fuel_prices.services.csv_data import read_input


ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data/processed"
CACHE = ROOT / "data/cache/census"
GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip"


def load_gazetteer(path, offline):
    if not path.exists():
        if offline:
            raise ValueError("City gazetteer is missing")

        path.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(path) + ".part")

        try:
            with urlopen(
                Request(
                    GAZETTEER_URL, headers={"User-Agent": "TrackRouteCensusBatch/1.0"}
                ),
                timeout=60,
            ) as response:
                with partial.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
            if not zipfile.is_zipfile(partial):
                raise ValueError("Census city download is not a ZIP archive")
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
    return CensusPlaces(path)


def validate_paths(args, lock_path):
    paths = [
        args.path,
        args.output,
        args.cache,
        args.report,
        args.gazetteer,
        Path(str(args.gazetteer) + ".part"),
        lock_path,
    ]
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if first.resolve() == second.resolve() or (
                first.exists() and second.exists() and first.samefile(second)
            ):
                raise ValueError(
                    "Input, outputs, cache, gazetteer and lock paths must be distinct"
                )
    for path in paths[1:]:
        if path.resolve().is_relative_to((ROOT / "data/raw").resolve()):
            raise ValueError("Generated files must not be written into data/raw")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=ROOT / "data/raw/fuel-prices-for-be-assessment.csv",
    )
    parser.add_argument(
        "--output", type=Path, default=PROCESSED / "stations_geocoded_census.csv"
    )
    parser.add_argument("--cache", type=Path, default=CACHE / "queries.json")
    parser.add_argument(
        "--report", type=Path, default=PROCESSED / "census_batch_report.json"
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--benchmark", default="Public_AR_Current")
    parser.add_argument(
        "--gazetteer", type=Path, default=CACHE / "2025_Gaz_place_national.zip"
    )
    parser.add_argument(
        "--no-city-fallback", action="store_true", help="Use addressbatch only"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require cached batches and local city data",
    )
    args = parser.parse_args()

    try:
        if not 1 <= args.batch_size <= 10000:
            raise ValueError("batch-size must be between 1 and 10,000")

        if not args.benchmark or any(c in args.benchmark for c in "\r\n"):
            raise ValueError("Provide a valid benchmark")

        lock_path = CACHE / ".lock"

        validate_paths(args, lock_path)

        if not args.path.is_file():
            raise ValueError(f"Input CSV not found: {args.path}")
        read_input(args.path)

        CACHE.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gazetteer = (
                None
                if args.no_city_fallback
                else load_gazetteer(args.gazetteer, args.offline)
            )
            report = run_batch_export(
                args.path,
                args.output,
                args.cache,
                args.report,
                batch_size=args.batch_size,
                benchmark=args.benchmark,
                gazetteer=gazetteer,
                offline=args.offline,
            )

        print(f"Wrote {report['row_count']} rows: {report['counts']}")
        print(f"CSV: {args.output}\nReport: {args.report}")

    except (OSError, ValueError, KeyError, csv.Error, zipfile.BadZipFile) as exc:
        parser.exit(
            1,
            f"Census export stopped: {exc}\nCompleted batches remain cached for resuming.\n",
        )


if __name__ == "__main__":
    main()
