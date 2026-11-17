import tempfile
import unittest
import zipfile
from pathlib import Path

from apps.fuel_prices.services.census_places import CensusPlaces, place_name


class CensusPlacesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "places.zip"

    def gazetteer(self, rows, delimiter="|"):
        header = ["USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"]
        text = "\n".join(delimiter.join(row) for row in [header, *rows]) + "\n"
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("places.txt", text)
        return CensusPlaces(self.path)

    def test_matches_city_state_and_preserves_coordinate_precision(self):
        gazetteer = self.gazetteer([
            ["TX", "4800001", "Paris city", "+33.660938", "-95.555513"],
            ["IL", "1700001", "Paris city", "39.6111", "-87.6961"],
        ])
        result = gazetteer.lookup("PARIS", " tx ")
        self.assertEqual(result["source_id"], "4800001")
        self.assertEqual(result["latitude"], "+33.660938")
        self.assertEqual(result["status"], "city_fallback")
        self.assertEqual(result["provider"], "us_census_gazetteer")
        self.assertEqual(result["matched_name"], "Paris city")
        self.assertEqual(gazetteer.lookup("Paris", "IL")["source_id"], "1700001")

    def test_strips_only_terminal_municipal_suffix(self):
        cases = {
            "Kansas City city": "Kansas City",
            "Dodge City city": "Dodge City",
            "Carson City": "Carson City",
            "Juneau city and borough": "Juneau",
            "Milford city (balance)": "Milford",
            "Athens-Clarke County unified government (balance)": "Athens-Clarke County",
            "Big Cabin town": "Big Cabin",
            "New Town CDP": "New Town",
            "Macon-Bibb County": "Macon-Bibb County",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(place_name(name), expected)

    def test_keeps_ambiguous_city_and_cdp_unresolved(self):
        gazetteer = self.gazetteer([
            ["NY", "1", "Example city", "40", "-74"],
            ["NY", "2", "Example CDP", "43", "-77"],
        ])
        result = gazetteer.lookup("Example", "NY")
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason"], "ambiguous_city")
        self.assertEqual(result["longitude"], "")
        self.assertEqual(len(result["candidates"]), 2)

    def test_does_not_apply_us_locations_to_canadian_province(self):
        gazetteer = self.gazetteer([["TX", "1", "London town", "30", "-97"]])
        self.assertEqual(gazetteer.lookup("London", "ON")["reason"], "unknown_state")
        self.assertEqual(gazetteer.lookup("", "TX")["reason"], "missing_city")
        self.assertEqual(gazetteer.lookup("Other", "TX")["reason"], "city_not_found")

    def test_handles_tab_delimited_gazetteer(self):
        gazetteer = self.gazetteer([["TX", "1", "Example town", "30", "-97"]], delimiter="\t")
        self.assertEqual(gazetteer.lookup("Example", "TX")["latitude"], "30")

    def test_renamed_archive_finds_the_official_national_member(self):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("2025_Gaz_place_national.txt",
                             "USPS|GEOID|NAME|INTPTLAT|INTPTLONG\nTX|1|Example town|30|-97\n")
        self.assertEqual(CensusPlaces(self.path).lookup("Example", "TX")["latitude"], "30")

    def test_rejects_bad_coordinate_and_missing_record_fields(self):
        for row in (["TX", "1", "Example town", "nan", "-97"],
                    ["TX", "1", "Example town", "30"]):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    self.gazetteer([row])
