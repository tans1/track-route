"""Enrich the station CSV through the Census addressbatch endpoint."""

import argparse
import csv
import fcntl
import zipfile
from pathlib import Path

from apps.stations.services.census_batch import run_batch_export
from apps.stations.services.census_places import load_census_places
from apps.stations.services.csv_data import read_input

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data/processed"
CACHE = ROOT / "data/cache/census"


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
                else load_census_places(args.gazetteer, args.offline)
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
