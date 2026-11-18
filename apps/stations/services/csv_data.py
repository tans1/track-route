"""Input validation and atomic file writes for the Census CSV workflow."""

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path

SOURCE_COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)


def read_input(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        headers = next(reader, [])
        if (
            not headers
            or len(set(headers)) != len(headers)
            or any(not h.strip() for h in headers)
        ):
            raise ValueError("Input requires unique nonblank column names")
        if set(SOURCE_COLUMNS) - set(headers):
            raise ValueError("Input is missing station columns")
        if {"latitude", "longitude"} & set(headers):
            raise ValueError(
                "Input already has coordinate columns; use the original raw CSV"
            )
        rows = []
        for record, values in enumerate(reader, 2):
            if len(values) != len(headers):
                raise ValueError(
                    f"CSV record {record}: field count differs from header"
                )
            rows.append(dict(zip(headers, values, strict=True)))
    return headers, rows


def fingerprint(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def atomic_write(path, writer):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            writer(stream)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    def write_json(stream):
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")

    atomic_write(path, write_json)
