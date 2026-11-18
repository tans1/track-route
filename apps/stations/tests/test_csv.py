"""Station CSV export, catalog loading, and resuming after an interrupted batch."""

import csv
import io
import json
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from zipfile import ZipFile

from apps.stations.services.catalog import CatalogIntegrityError, load_station_catalog
from apps.stations.services.census_batch import run_batch_export
from apps.stations.services.csv_data import SOURCE_COLUMNS
from scripts import geocode_census


class StationCsvTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "input.csv"
        self.output = self.directory / "output.csv"
        self.cache = self.directory / "cache.json"
        self.report = self.directory / "report.json"
        self.gazetteer = self.directory / "places.zip"

    def write_input(self, rows):
        with self.source.open("w", newline="") as stream:
            csv.writer(stream).writerows([SOURCE_COLUMNS, *rows])

    def export(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_batch_export(
                self.source, self.output, self.cache, self.report, **kwargs
            )

    def run_script(self, *extra):
        args = [
            "geocode_census",
            str(self.source),
            "--output",
            str(self.output),
            "--cache",
            str(self.cache),
            "--report",
            str(self.report),
            "--gazetteer",
            str(self.gazetteer),
            *extra,
        ]
        with (
            patch("sys.argv", args),
            patch.object(geocode_census, "CACHE", self.directory),
            redirect_stdout(io.StringIO()),
        ):
            geocode_census.main()

    def test_export_preserves_rows_geocodes_and_selects_the_lowest_station_price(self):
        rows = [
            ["001", " Fuel, Inc. ", "12 Main St", "Austin", "TX", "007", "4.0000"],
            ["001", " Fuel, Inc. ", "12 Main St", "Austin", "TX", "007", "3.1000"],
            ["002", "Highway Stop", "I-35 Exit 1", "Austin", "TX", "008", "3.5000"],
            ["003", "Canada Stop", "1 Main St", "Calgary", "AB", "009", "4.5000"],
        ]
        self.write_input(rows)
        original = self.source.read_bytes()
        with ZipFile(self.gazetteer, "w") as archive:
            archive.writestr(
                "places.txt",
                "USPS|GEOID|NAME|INTPTLAT|INTPTLONG\nTX|4805000|Austin city|30.26|-97.74\n",
            )
        response = io.StringIO(newline="")
        csv.writer(response).writerows(
            [
                ["2", "Highway", "No_Match"],
                [
                    "1",
                    "12 Main St",
                    "Match",
                    "Exact",
                    "12 MAIN ST, AUSTIN, TX, 12345",
                    "-97.75,30.25",
                    "123",
                    "L",
                ],
            ]
        )
        with patch(
            "apps.stations.services.census_batch.urlopen",
            return_value=io.BytesIO(response.getvalue().encode()),
        ) as http:
            self.run_script()
            saved = self.output.read_bytes()
            http.side_effect = AssertionError("Offline export must use the cache")
            self.run_script("--offline")
            http.assert_called_once()
        self.assertEqual(self.output.read_bytes(), saved)
        self.assertEqual(self.source.read_bytes(), original)
        with self.output.open(newline="") as stream:
            exported = list(csv.reader(stream))
        self.assertEqual(exported[0], [*SOURCE_COLUMNS, "latitude", "longitude"])
        self.assertEqual([row[:-2] for row in exported[1:]], rows)
        self.assertEqual(
            [row[-2:] for row in exported[1:]],
            [["30.25", "-97.75"], ["30.25", "-97.75"], ["30.26", "-97.74"], ["", ""]],
        )
        report = json.loads(self.report.read_text())
        self.assertTrue(report["complete"])
        self.assertEqual(
            report["results"][-1]["reason"], "unsupported_country_or_state"
        )
        catalog = load_station_catalog(self.output, self.report, raw_path=self.source)
        self.assertEqual(catalog.eligible_count, 2)
        self.assertEqual(catalog.stations[0].price_per_gallon, Decimal("3.1000"))
        self.assertEqual(catalog.stations[0].source_csv_record, 3)
        self.output.write_text(self.output.read_text().replace("3.1000", "9.9999"))
        with self.assertRaises(CatalogIntegrityError):
            load_station_catalog(self.output, self.report, raw_path=self.source)

    def test_interrupted_export_resumes_only_the_unfinished_batch(self):
        self.write_input(
            [
                ["1", "First", "12 Main St", "Austin", "TX", "1", "3.00"],
                ["2", "Second", "99 Main St", "Austin", "TX", "2", "4.00"],
            ]
        )
        matched = {
            "latitude": "30.25",
            "longitude": "-97.75",
            "status": "address_match",
        }
        submit = Mock(side_effect=[{"1": matched}, OSError("interrupted")])
        with self.assertRaises(OSError):
            self.export(batch_size=1, submit=submit)
        partial = json.loads(self.report.read_text())
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["counts"], {"address_match": 1, "pending": 1})
        resumed = Mock(
            return_value={"1": {"status": "unresolved", "reason": "No_Match"}}
        )
        result = self.export(batch_size=1, submit=resumed)
        resumed.assert_called_once()
        self.assertEqual(resumed.call_args.args[0]["1"][0], "99 MAIN ST")
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"], {"address_match": 1, "unresolved": 1})
