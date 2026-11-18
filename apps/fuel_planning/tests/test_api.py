"""Main trip workflows with real local planning and mocked provider HTTP."""

import json
from dataclasses import replace
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.core.cache import caches
from django.test import Client, SimpleTestCase, override_settings

from apps.fuel_planning.services.optimizer import plan_fuel_stops
from apps.locations.services.geocoding import LocationResolver
from apps.routes.services.directions import DirectionsClient

from .fixtures import catalog, route_payload


@override_settings(ORS_API_KEY="synthetic-key")
class TripApiTests(SimpleTestCase):
    url = "/api/trips/plan/"
    queries = {"start": "West, CA", "destination": "East, TX"}

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        for cache in caches.all():
            cache.clear()
            self.addCleanup(cache.clear)
        self.route = route_payload()
        self.catalog = catalog()
        self.country = "us"
        self.route_http = self.patch(
            "apps.routes.services.directions.urlopen",
            side_effect=lambda *args, **kwargs: self.response(self.route),
        )
        self.location_http = self.patch(
            "apps.locations.services.geocoding.urlopen", side_effect=self.location
        )
        self.patch(
            "apps.fuel_planning.services.trips.load_configured_catalog",
            side_effect=lambda: self.catalog,
        )
        self.patch("apps.fuel_planning.services.trips.directions", DirectionsClient())
        self.patch(
            "apps.fuel_planning.services.trips.resolver",
            LocationResolver(sleep=lambda _: None),
        )

    def patch(self, target, *args, **kwargs):
        context = patch(target, *args, **kwargs)
        self.addCleanup(context.stop)
        return context.start()

    @staticmethod
    def response(payload):
        result = MagicMock()
        result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return result

    def location(self, request, **kwargs):
        query = parse_qs(urlparse(request.full_url).query)["q"][0]
        return self.response(
            [
                {
                    "lon": "-120" if query.casefold().startswith("west") else "-108",
                    "lat": "35",
                    "display_name": query,
                    "address": {"country_code": self.country},
                }
            ]
        )

    def post(self, **kwargs):
        return self.client.post(
            self.url, json.dumps(self.queries | kwargs), content_type="application/json"
        )

    def test_trip_returns_stops_and_map_and_reuses_cached_results(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        result = first.json()
        self.assertEqual(
            set(result),
            {
                "start",
                "destination",
                "path",
                "stops",
                "totals",
                "search_complete",
                "map_url",
            },
        )
        self.assertEqual(
            result["path"], self.route["features"][0]["geometry"]["coordinates"]
        )
        self.assertEqual([stop["station_id"] for stop in result["stops"]], ["B", "C"])
        self.assertEqual(result["totals"]["distance_miles"], "1200.00")
        self.assertTrue(result["search_complete"])
        self.assertEqual(
            (first["X-Geocoding-Requests"], first["X-ORS-Requests"]), ("2", "1")
        )
        self.assertEqual(
            json.loads(self.route_http.call_args.args[0].data)["coordinates"],
            [result["start"]["coordinates"], result["destination"]["coordinates"]],
        )
        for call in self.location_http.call_args_list:
            params = parse_qs(urlparse(call.args[0].full_url).query)
            self.assertEqual(params["countrycodes"], ["us"])
            self.assertEqual(params["addressdetails"], ["1"])
        with patch("apps.fuel_planning.services.trips.plan_trip") as planner:
            page = self.client.get(result["map_url"])
            self.assertContains(page, 'id="trip-plan"')
            planner.assert_not_called()
        with patch("apps.fuel_planning.services.planner.plan_fuel_stops") as solver:
            repeated = self.post()
            self.assertEqual(repeated.status_code, 200, repeated.content)
            self.assertEqual(repeated.json()["totals"], result["totals"])
            self.assertEqual(repeated["X-Plan-Cache"], "hit")
            self.assertEqual(
                (repeated["X-Geocoding-Requests"], repeated["X-ORS-Requests"]),
                ("0", "0"),
            )
            solver.assert_not_called()
        self.route_http.assert_called_once()
        self.assertEqual(self.location_http.call_count, 2)

    def test_short_trip_needs_no_fuel_purchase(self):
        self.route = route_payload(100)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["stops"], [])
        self.assertEqual(result["totals"]["fuel_purchase_cost"], "0.00")
        self.assertEqual(result["totals"]["gallons_purchased"], "0.000000")
        self.assertEqual(result["totals"]["gallons_consumed"], "10.000000")

    def test_invalid_input_is_rejected_before_provider_requests(self):
        for payload in (
            {},
            self.queries | {"destination": ""},
            self.queries | {"initial_fuel_gallons": -1},
            self.queries | {"initial_fuel_gallons": 51},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.url, json.dumps(payload), content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
        self.route_http.assert_not_called()
        self.location_http.assert_not_called()

    def test_non_us_result_is_rejected_before_routing(self):
        self.country = "ca"
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["field"], "start")
        self.route_http.assert_not_called()

    def test_geocoding_failure_returns_an_explicit_retryable_error(self):
        self.location_http.side_effect = URLError("synthetic unavailable")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "geocoding_unavailable")
        self.assertEqual(response["Retry-After"], "5")
        self.route_http.assert_not_called()

    def test_routing_failure_makes_only_one_attempt(self):
        self.route_http.side_effect = URLError("synthetic unavailable")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "routing_unavailable")
        self.assertEqual(response["X-ORS-Requests"], "1")
        self.route_http.assert_called_once()

    def test_search_limits_are_distinct_from_an_infeasible_trip(self):
        cases = (
            (catalog(()), 100, 422, "model_infeasible"),
            (catalog(), 1, 503, "search_budget_exhausted"),
            (catalog(), 4, 200, None),
        )
        for stations, limit, status, code in cases:
            with (
                self.subTest(status=status),
                override_settings(FUEL_SEARCH_MAX_LABELS=limit),
            ):
                self.catalog = stations
                response = self.post()
                self.assertEqual(response.status_code, status, response.content)
                if code:
                    self.assertEqual(response.json()["error"]["code"], code)
                else:
                    self.assertFalse(response.json()["search_complete"])
                    self.assertEqual(self.post()["X-Plan-Cache"], "miss")
        self.route_http.assert_called_once()

    def test_changed_fuel_options_and_data_invalidate_the_plan_cache(self):
        self.post()
        with patch(
            "apps.fuel_planning.services.planner.plan_fuel_stops", wraps=plan_fuel_stops
        ) as solver:
            fuel = self.post(initial_fuel_gallons=49)
            options = self.post(options={"avoid_features": ["ferries"]})
            self.catalog = replace(self.catalog, fingerprint="changed-dataset")
            dataset = self.post()
        for result in (fuel, options, dataset):
            self.assertEqual(result.status_code, 200, result.content)
            self.assertEqual(result["X-Plan-Cache"], "miss")
        self.assertEqual(solver.call_count, 3)
        self.assertEqual(self.route_http.call_count, 2)
