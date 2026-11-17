import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from apps.fuel_prices.services.census_batch import (
    ENDPOINT,
    batch_csv,
    parse_response,
    run_batch_export,
    submit_batch,
)
from scripts import geocode_census


HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]


def row(station="001", street="12 Main St", city="Example City", state="TX"):
    return [station, " Fuel, Inc. ", street, city, state, "007", "3.12345678"]


def response_csv(records):
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(records)
    return stream.getvalue()


def response_row(query_id="1", city="EXAMPLE CITY", state="TX", point="-97.25,32.5"):
    return [
        query_id,
        "12 Main St, Example City, TX, ",
        "Match",
        "Exact",
        f"12 MAIN ST, {city}, {state}, 12345",
        point,
        "123456",
        "L",
    ]


def miss():
    return {
        "latitude": "",
        "longitude": "",
        "status": "unresolved",
        "reason": "No_Match",
    }


def matched():
    return {
        "latitude": "32.5",
        "longitude": "-97.25",
        "status": "address_match",
        "reason": "census_address_match",
    }


class CensusBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "input.csv"
        self.output = self.directory / "output.csv"
        self.cache = self.directory / "cache.json"
        self.report = self.directory / "report.json"
        self.addresses = {"1": ("12 MAIN ST", "EXAMPLE CITY", "TX", "")}

    def write_input(self, rows):
        with self.source.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADERS)
            writer.writerows(rows)

    def export(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_batch_export(
                self.source, self.output, self.cache, self.report, **kwargs
            )

    def test_headerless_csv_and_multipart_request_preserve_quoted_commas(self):
        addresses = {"1": ("I-44, EXIT 283", "EXAMPLE CITY", "TX", "")}
        encoded = batch_csv(addresses)
        self.assertEqual(
            list(csv.reader(io.StringIO(encoded))), [["1", *addresses["1"]]]
        )
        self.assertIn('"I-44, EXIT 283"', encoded)
        with patch(
            "apps.fuel_prices.services.census_batch.urlopen",
            return_value=io.BytesIO(b'"1","input","No_Match"\n'),
        ) as opened:
            result = submit_batch(addresses, "Public_AR_Current")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.get_header('Content-type')}\r\n\r\n".encode()
            + request.data
        )
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        self.assertEqual(
            parts["benchmark"].get_payload(decode=True), b"Public_AR_Current"
        )
        self.assertEqual(parts["addressFile"].get_filename(), "addresses.csv")
        uploaded = parts["addressFile"].get_payload(decode=True).decode()
        self.assertEqual(
            list(csv.reader(io.StringIO(uploaded))), [["1", *addresses["1"]]]
        )
        self.assertEqual(result["1"]["reason"], "No_Match")

    def test_response_ids_control_order_and_lon_lat_are_swapped_for_output(self):
        addresses = {**self.addresses, "2": ("BAD ROAD", "EXAMPLE CITY", "TX", "")}
        result = parse_response(
            response_csv(
                [
                    ["2", "Bad Road", "No_Match"],
                    response_row(),
                ]
            ),
            addresses,
        )
        self.assertEqual(result["1"]["latitude"], "32.5")
        self.assertEqual(result["1"]["longitude"], "-97.25")
        self.assertEqual(result["1"]["tiger_line_id"], "123456")
        self.assertEqual(result["2"]["status"], "unresolved")
        self.assertEqual(result["2"]["latitude"], "")

    def test_invalid_duplicate_unknown_missing_and_malformed_responses_fail(self):
        cases = [
            [],
            [response_row(), response_row()],
            [response_row(query_id="unexpected")],
            [["1", "input", "Match"]],
            [response_row(point="NaN,32")],
            [response_row(point="-97,91")],
            [["1", "input", "Error"]],
        ]
        for records in cases:
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    parse_response(response_csv(records), self.addresses)
        with self.assertRaises(csv.Error):
            parse_response('"unterminated', self.addresses)

    def test_ties_and_wrong_localities_are_unresolved(self):
        tie = parse_response(response_csv([["1", "input", "Tie"]]), self.addresses)["1"]
        self.assertEqual(tie["status"], "unresolved")
        for city, state in (("OTHER CITY", "TX"), ("EXAMPLE CITY", "OK")):
            with self.subTest(city=city, state=state):
                result = parse_response(
                    response_csv([response_row(city=city, state=state)]), self.addresses
                )["1"]
                self.assertEqual(result["reason"], "matched_locality_differs")
                self.assertEqual((result["latitude"], result["longitude"]), ("", ""))

    def test_cli_preserves_all_rows_and_fields_while_only_us_queries_are_submitted(
        self,
    ):
        rows = [
            row(street="I-44, EXIT 283", city="Example City   "),
            row(station="002", state="AB", city="Calgary"),
            row(street="I-44, EXIT 283", city="Example City   "),
        ]
        self.write_input(rows)
        original = self.source.read_bytes()
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
            str(self.directory / "city.zip"),
            "--no-city-fallback",
        ]
        calls = []

        def request(addresses, benchmark):
            calls.append(addresses)
            return {key: matched() for key in addresses}

        real_export = run_batch_export

        def export_with_stub(*args, **kwargs):
            return real_export(*args, **kwargs, submit=request)

        with (
            patch("sys.argv", args),
            patch.object(geocode_census, "PROCESSED", self.directory),
            patch.object(geocode_census, "CACHE", self.directory),
        ):
            with patch.object(
                geocode_census, "run_batch_export", side_effect=export_with_stub
            ):
                with redirect_stdout(io.StringIO()):
                    geocode_census.main()
        with self.output.open(newline="") as stream:
            output = list(csv.reader(stream))
        self.assertEqual(output[0], [*HEADERS, "latitude", "longitude"])
        self.assertEqual([record[:-2] for record in output[1:]], rows)
        self.assertEqual(output[1][-2:], ["32.5", "-97.25"])
        self.assertEqual(output[2][-2:], ["", ""])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 1)
        report = json.loads(self.report.read_text())
        self.assertTrue(report["complete"])
        self.assertEqual(report["results"][1]["reason"], "unsupported_country_or_state")
        self.assertEqual(self.source.read_bytes(), original)

    def test_us_city_fallback_is_cached_locally_and_never_applied_to_canada(self):
        self.write_input(
            [
                row(),
                row(station="2", street="Other Rd"),
                row(station="3", city="Calgary", state="AB"),
            ]
        )
        gazetteer = Mock()
        gazetteer.lookup.return_value = {
            "status": "city_fallback",
            "latitude": "33",
            "longitude": "-98",
            "reason": "unique_census_place",
            "source_id": "001",
        }
        result = self.export(
            gazetteer=gazetteer,
            submit=lambda addresses, benchmark: {k: miss() for k in addresses},
        )
        self.assertEqual(result["counts"], {"city_fallback": 2, "unresolved": 1})
        gazetteer.lookup.assert_called_once_with("EXAMPLE CITY", "TX")
        self.assertEqual(result["results"][0]["address_reason"], "No_Match")

    def test_cli_enables_city_fallback_by_default(self):
        self.write_input([row()])
        gazetteer = Mock()
        gazetteer.lookup.return_value = {
            "status": "city_fallback",
            "latitude": "33",
            "longitude": "-98",
            "reason": "unique_census_place",
        }

        def export_with_miss(*args, **kwargs):
            return run_batch_export(
                *args,
                **kwargs,
                submit=lambda addresses, benchmark: {k: miss() for k in addresses},
            )

        args = [
            "geocode_census",
            str(self.source),
            "--output",
            str(self.output),
            "--cache",
            str(self.cache),
            "--report",
            str(self.report),
        ]
        with (
            patch("sys.argv", args),
            patch.object(geocode_census, "PROCESSED", self.directory),
            patch.object(geocode_census, "CACHE", self.directory),
        ):
            with patch.object(
                geocode_census, "load_gazetteer", return_value=gazetteer
            ) as load:
                with patch.object(
                    geocode_census, "run_batch_export", side_effect=export_with_miss
                ):
                    with redirect_stdout(io.StringIO()):
                        geocode_census.main()
            load.assert_called_once_with(
                self.directory / "2025_Gaz_place_national.zip", False
            )
        self.assertEqual(
            json.loads(self.report.read_text())["counts"], {"city_fallback": 1}
        )

    def test_batches_deduplicate_queries_then_offline_cache_reuses_matches_and_misses(
        self,
    ):
        self.write_input(
            [row(street=f"{i} Main St") for i in range(5)] + [row(street="0 Main St")]
        )
        calls = []

        def submit(addresses, benchmark):
            calls.append(addresses)
            return {
                k: matched() if int(parts[0][0]) % 2 == 0 else miss()
                for k, parts in addresses.items()
            }

        first = self.export(batch_size=2, submit=submit)
        self.assertEqual([len(c) for c in calls], [2, 2, 1])
        self.assertTrue(first["complete"])
        before = self.output.read_bytes()
        unused = Mock(
            side_effect=AssertionError("Offline execution must not submit batches")
        )
        second = self.export(batch_size=2, offline=True, submit=unused)
        unused.assert_not_called()
        self.assertEqual(second["counts"], first["counts"])
        self.assertEqual(self.output.read_bytes(), before)

    def test_failed_batch_preserves_completed_cache_and_pending_rows_for_resume(self):
        self.write_input([row(street="12 Main St"), row(street="99 Main St")])
        calls = []

        def fail_second(addresses, benchmark):
            calls.append(addresses)
            if len(calls) == 2:
                raise OSError("Provider temporarily unavailable")
            return {key: matched() for key in addresses}

        with self.assertRaisesRegex(OSError, "temporarily unavailable"):
            self.export(batch_size=1, submit=fail_second)
        self.assertEqual(len(json.loads(self.cache.read_text())["queries"]), 1)
        partial = json.loads(self.report.read_text())
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["counts"], {"address_match": 1, "pending": 1})
        resumed = Mock(
            side_effect=lambda addresses, benchmark: {k: miss() for k in addresses}
        )
        result = self.export(batch_size=1, submit=resumed)
        resumed.assert_called_once()
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"], {"address_match": 1, "unresolved": 1})

    def test_incomplete_batch_is_not_cached_and_offline_requires_all_queries(self):
        self.write_input([row(), row(street="99 Main St")])
        with self.assertRaisesRegex(ValueError, "Incomplete batch"):
            self.export(submit=lambda addresses, benchmark: {"1": matched()})
        self.assertFalse(self.cache.exists())
        self.assertEqual(json.loads(self.report.read_text())["counts"], {"pending": 2})
        unused = Mock()
        with self.assertRaisesRegex(ValueError, "Offline cache lacks"):
            self.export(offline=True, submit=unused)
        unused.assert_not_called()

    def test_batch_size_bounds_fail_before_transport(self):
        self.write_input([row()])
        for size in (0, -1, 10001):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.export(batch_size=size)
        with patch("apps.fuel_prices.services.census_batch.urlopen") as network:
            for addresses in ({}, {str(i): self.addresses["1"] for i in range(10001)}):
                with self.assertRaises(ValueError):
                    submit_batch(addresses, "Public_AR_Current")
            network.assert_not_called()

    def test_generated_paths_cannot_overwrite_input_gazetteer_or_download_partial(self):
        self.write_input([row()])
        args = SimpleNamespace(
            path=self.source,
            output=self.output,
            cache=self.cache,
            report=self.report,
            gazetteer=self.directory / "city.zip",
        )
        for field, collision in (
            ("output", self.source),
            ("cache", args.gazetteer),
            ("report", Path(str(args.gazetteer) + ".part")),
        ):
            modified = SimpleNamespace(**vars(args))
            setattr(modified, field, collision)
            with self.subTest(field=field), self.assertRaises(ValueError):
                geocode_census.validate_paths(modified, self.directory / "lock")
        before = self.source.read_bytes()
        cli = ["geocode_census", str(self.source), "--output", str(self.source)]
        with (
            patch("sys.argv", cli),
            patch.object(geocode_census, "load_gazetteer") as download,
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                geocode_census.main()
            download.assert_not_called()
        self.assertEqual(self.source.read_bytes(), before)
