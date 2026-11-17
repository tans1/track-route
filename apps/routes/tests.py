import copy
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.core.cache import caches
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from apps.locations.services.geocoding import LocationNotFound, ProviderError

from .services.directions import DirectionsClient, RoutingError, route_cache_key

START = [-118.2427676, 34.0536923]
DESTINATION = [-122.4075201, 37.7879363]


def geojson():
    coordinates = [
        START.copy(),
        [-118.25, 34.07],
        [-119.4, 35.8],
        [-121.2, 37.2],
        DESTINATION.copy(),
    ]
    return {
        "type": "FeatureCollection",
        "bbox": [-122.4075201, 34.0536923, -118.2427676, 37.7879363],
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": {
                    "summary": {"distance": 615123.4, "duration": 21800.5},
                    "segments": [
                        {
                            "distance": 615123.4,
                            "duration": 21800.5,
                            "steps": [
                                {"instruction": "Head north", "way_points": [0, 2]}
                            ],
                        }
                    ],
                    "way_points": [0, 4],
                    "warnings": [{"code": 1, "message": "Example warning"}],
                    "extras": {"surface": {"values": [[0, 4, 3]]}},
                },
            }
        ],
        "metadata": {
            "attribution": "openrouteservice.org | OpenStreetMap contributors",
            "service": "routing",
            "timestamp": 1794900000,
            "query": {"coordinates": [START.copy(), DESTINATION.copy()], "units": "m"},
            "engine": {"version": "9.10.0", "graph_date": "2026-11-01"},
        },
    }


def response(payload):
    result = MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


def http_error(status, payload=None, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    return HTTPError(
        "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson",
        status,
        "Example error",
        headers,
        io.BytesIO(json.dumps(payload or {}).encode()),
    )


class RoutingTestCase(SimpleTestCase):
    def setUp(self):
        config = override_settings(ORS_API_KEY="test-ors-key")
        config.enable()
        self.addCleanup(config.disable)
        caches["routes"].clear()
        self.addCleanup(caches["routes"].clear)
        self.now = 100.0
        self.directions = DirectionsClient(clock=lambda: self.now)
        self.payload = geojson()
        patcher = patch(
            "apps.routes.services.directions.urlopen",
            return_value=response(self.payload),
        )
        self.http = patcher.start()
        self.addCleanup(patcher.stop)


class DirectionsTests(RoutingTestCase):
    def test_uses_current_host_post_auth_and_ordered_longitude_latitude(self):
        self.directions.route(
            START, DESTINATION, options={"avoid_features": ["ferries"]}
        )
        request = self.http.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "test-ors-key")
        body = json.loads(request.data)
        self.assertEqual(body["coordinates"], [START, DESTINATION])
        self.assertEqual(body["options"], {"avoid_features": ["ferries"]})
        self.assertEqual(body["units"], "m")
        self.assertFalse(body["geometry_simplify"])
        self.assertTrue(body["instructions"])
        self.assertNotIn("test-ors-key", request.full_url)
        self.assertNotIn("test-ors-key", request.data.decode())

    def test_preserves_entire_geojson_and_intermediate_geometry(self):
        result = self.directions.route(START, DESTINATION)
        self.assertEqual(result, self.payload)
        self.assertEqual(len(result["features"][0]["geometry"]["coordinates"]), 5)
        self.assertEqual(
            result["features"][0]["properties"]["summary"]["distance"], 615123.4
        )
        self.assertEqual(result["metadata"]["engine"]["graph_date"], "2026-11-01")

    def test_reverse_trip_is_distinct_and_never_sorts_waypoints(self):
        self.directions.route(START, DESTINATION)
        self.directions.route(DESTINATION, START)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(
            json.loads(self.http.call_args.args[0].data)["coordinates"],
            [DESTINATION, START],
        )

    def test_cache_keeps_sub_meter_coordinate_differences(self):
        first = [START[0], 34.053692301]
        second = [START[0], 34.053692302]
        self.directions.route(first, DESTINATION)
        self.directions.route(second, DESTINATION)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(
            json.loads(self.http.call_args.args[0].data)["coordinates"][0], second
        )

    def test_equivalent_numeric_coordinates_and_signed_zero_share_cache(self):
        self.directions.route([-118, 0], [-122, 37])
        self.directions.route([-118.0, -0.0], [-122.0, 37.0])
        self.http.assert_called_once()

    def test_option_key_order_does_not_change_cache_identity(self):
        self.directions.route(
            START,
            DESTINATION,
            options={"avoid_borders": "all", "avoid_features": ["ferries"]},
        )
        self.directions.route(
            START,
            DESTINATION,
            options={"avoid_features": ["ferries"], "avoid_borders": "all"},
        )
        self.http.assert_called_once()

    def test_changed_options_and_provider_get_separate_entries(self):
        self.directions.route(START, DESTINATION)
        self.directions.route(
            START, DESTINATION, options={"avoid_features": ["ferries"]}
        )
        with override_settings(ORS_DIRECTIONS_URL="https://example.test/v2/directions"):
            self.directions.route(START, DESTINATION)
        self.assertEqual(self.http.call_count, 3)

    def test_profile_is_part_of_cache_identity(self):
        body = {"coordinates": [START, DESTINATION], "options": {}}
        self.assertNotEqual(
            route_cache_key("https://example.test", "driving-car", body),
            route_cache_key("https://example.test", "driving-hgv", body),
        )

    def test_default_and_empty_options_share_cache(self):
        self.directions.route(START, DESTINATION)
        self.directions.route(START, DESTINATION, options={})
        self.http.assert_called_once()

    def test_route_cache_expires(self):
        with (
            override_settings(ROUTE_CACHE_TTL=10),
            patch(
                "django.core.cache.backends.locmem.time.time", return_value=1000
            ) as clock,
        ):
            self.directions.route(START, DESTINATION)
            self.directions.route(START, DESTINATION)
            self.http.assert_called_once()
            clock.return_value = 1011
            self.directions.route(START, DESTINATION)
            self.assertEqual(self.http.call_count, 2)

    def test_modifying_returned_route_does_not_mutate_cached_geometry(self):
        first = self.directions.route(START, DESTINATION)
        first["features"][0]["geometry"]["coordinates"].clear()
        second = self.directions.route(START, DESTINATION)
        self.assertEqual(second, self.payload)
        self.http.assert_called_once()

    def test_concurrent_identical_routes_issue_one_request(self):
        barrier = threading.Barrier(4)

        def lookup(_):
            barrier.wait(timeout=5)
            return self.directions.route(START, DESTINATION)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lookup, range(4)))
        self.assertTrue(all(result == self.payload for result in results))
        self.http.assert_called_once()

    def test_missing_key_and_invalid_coordinates_never_send_requests(self):
        with override_settings(ORS_API_KEY=""):
            with self.assertRaises(RoutingError) as error:
                self.directions.route(START, DESTINATION)
            self.assertEqual(error.exception.reason, "provider_not_configured")
        for position in (
            [True, 34],
            [float("nan"), 34],
            [0, 91],
            [181, 0],
            [0],
            "invalid",
        ):
            with (
                self.subTest(position=position),
                self.assertRaises((ValueError, TypeError)),
            ):
                self.directions.route(position, DESTINATION)
        self.http.assert_not_called()

    def test_no_route_and_unsnappable_point_are_explicit_and_not_cached(self):
        for code in (2009, 2010):
            with self.subTest(code=code):
                self.http.side_effect = http_error(404, {"error": {"code": code}})
                with self.assertRaises(RoutingError) as error:
                    self.directions.route(START, DESTINATION)
                self.assertEqual(error.exception.status, 422)
                self.assertEqual(error.exception.reason, "route_not_found")
        self.http.side_effect = None
        self.assertEqual(self.directions.route(START, DESTINATION), self.payload)
        self.assertEqual(self.http.call_count, 3)

    def test_invalid_geojson_is_rejected_and_not_cached(self):
        invalid = []
        for mutate in (
            lambda p: p.update(features=[]),
            lambda p: p["features"][0].update(geometry=None),
            lambda p: p["features"][0]["geometry"].update(type="Point"),
            lambda p: p["features"][0]["geometry"].update(coordinates=[START]),
            lambda p: p["features"][0]["geometry"]["coordinates"].append([0, 100]),
            lambda p: p["features"][0]["properties"]["summary"].update(duration=-1),
            lambda p: p["features"][0]["properties"].update(way_points=[4, 0]),
            lambda p: p.update(metadata=None),
        ):
            payload = copy.deepcopy(self.payload)
            mutate(payload)
            invalid.append(payload)
        for payload in [None, [], {"type": "FeatureCollection"}, *invalid]:
            with self.subTest(payload=payload):
                self.http.return_value = response(payload)
                with self.assertRaises(RoutingError) as error:
                    self.directions.route(START, DESTINATION)
                self.assertEqual(error.exception.reason, "provider_invalid_response")
                self.assertEqual(error.exception.status, 502)
        self.http.return_value = response(self.payload)
        self.assertEqual(self.directions.route(START, DESTINATION), self.payload)

    def test_rate_limit_honors_retry_after_without_caching_failure(self):
        self.http.side_effect = http_error(429, retry_after=30)
        with self.assertRaises(RoutingError) as error:
            self.directions.route(START, DESTINATION)
        self.assertEqual(error.exception.retry_after, 30)
        with self.assertRaises(RoutingError) as error:
            self.directions.route(START, DESTINATION)
        self.assertEqual(error.exception.reason, "provider_cooldown")
        self.http.assert_called_once()
        self.now += 31
        self.http.side_effect = None
        self.assertEqual(self.directions.route(START, DESTINATION), self.payload)

    def test_timeout_and_connection_failure_are_explained(self):
        for failure, reason in (
            (TimeoutError(), "provider_timeout"),
            (URLError("unavailable"), "provider_connection_error"),
        ):
            with self.subTest(reason=reason):
                self.http.side_effect = failure
                with self.assertRaises(RoutingError) as error:
                    self.directions.route(START, DESTINATION)
                self.assertEqual(error.exception.reason, reason)
                self.now += 6

    def test_html_provider_error_is_not_mistaken_for_no_route(self):
        self.http.return_value.__enter__.return_value.read.return_value = (
            b"<html>Unavailable</html>"
        )
        with self.assertRaises(RoutingError) as error:
            self.directions.route(START, DESTINATION)
        self.assertEqual(error.exception.status, 502)


class RouteEndpointTests(RoutingTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse("routes:fetch")
        self.input = {"start": "Los Angeles, CA", "destination": "San Francisco, CA"}
        patcher = patch("apps.routes.views.directions", self.directions)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("apps.routes.views.resolver.resolve")
        self.geocode = patcher.start()
        self.addCleanup(patcher.stop)

        def resolve(query):
            point = START if query.startswith("Los") else DESTINATION
            return {
                "query": query,
                "display_name": query,
                "country_code": "us",
                "longitude": point[0],
                "latitude": point[1],
            }

        self.geocode.side_effect = resolve

    def post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )

    def test_resolves_names_and_returns_full_geojson_with_summaries(self):
        result = self.post(self.input)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["route"], self.payload)
        self.assertEqual(result.json()["start"]["longitude"], START[0])
        self.assertEqual(result.json()["destination"]["latitude"], DESTINATION[1])
        self.assertEqual(result.json()["profile"], "driving-car")
        self.assertEqual(
            [call.args[0] for call in self.geocode.call_args_list],
            list(self.input.values()),
        )
        self.assertEqual(
            json.loads(self.http.call_args.args[0].data)["coordinates"],
            [START, DESTINATION],
        )

    def test_different_location_labels_share_route_for_same_resolved_coordinates(self):
        first = self.post(self.input)
        second = self.post(
            {
                "start": "Los Angeles, California",
                "destination": "San Francisco, California",
            }
        )
        self.assertEqual(first.json()["route"], second.json()["route"])
        self.http.assert_called_once()

    def test_missing_key_fails_before_geocoding(self):
        with override_settings(ORS_API_KEY=""):
            result = self.post(self.input)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["reason"], "provider_not_configured")
        self.geocode.assert_not_called()
        self.http.assert_not_called()

    def test_invalid_input_and_options_fail_before_geocoding(self):
        payloads = [
            None,
            [],
            {},
            {"start": "LA"},
            {**self.input, "end": "SF"},
            {**self.input, "destination": None},
            {**self.input, "profile": "foot-walking"},
        ]
        for options in (
            None,
            [],
            {"coordinates": []},
            {"round_trip": {}},
            {"avoid_features": "ferries"},
            {"avoid_features": ["steps"]},
            {"avoid_countries": [True]},
            {"avoid_borders": "invalid"},
            {"avoid_polygons": {"type": "Point"}},
        ):
            payloads.append({**self.input, "options": options})
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
        self.geocode.assert_not_called()
        self.http.assert_not_called()

    def test_json_method_content_type_and_body_limits(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(
            self.client.post(
                self.url, data="{", content_type="application/json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                self.url, data="{}", content_type="text/plain"
            ).status_code,
            415,
        )
        self.assertEqual(
            self.client.post(
                self.url, data=" " * 8193, content_type="application/json"
            ).status_code,
            413,
        )
        self.geocode.assert_not_called()
        self.http.assert_not_called()

    def test_unresolved_location_stops_routing(self):
        self.geocode.side_effect = LocationNotFound("No US location found.")
        result = self.post(self.input)
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["error"]["field"], "start")
        self.http.assert_not_called()

    def test_geocoding_failure_retains_reason_and_retry_after(self):
        self.geocode.side_effect = ProviderError(
            "Try later", reason="provider_timeout", retry_after=5
        )
        result = self.post(self.input)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "geocoding_unavailable")
        self.assertEqual(result["Retry-After"], "5")
        self.http.assert_not_called()

    def test_no_route_returns_422_and_rate_limit_returns_503(self):
        self.http.side_effect = http_error(404, {"error": {"code": 2009}})
        result = self.post(self.input)
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["error"]["code"], "route_not_found")
        self.http.side_effect = http_error(429, retry_after=20)
        result = self.post(self.input)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result["Retry-After"], "20")

    def test_authentication_failure_does_not_expose_key_or_provider_body(self):
        self.http.side_effect = http_error(
            403, {"error": {"message": "Secret test-ors-key"}}
        )
        with self.assertLogs(
            "apps.routes.services.directions", level="WARNING"
        ) as logs:
            result = self.post(self.input)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["reason"], "provider_auth_error")
        self.assertEqual(result.json()["error"]["provider_status"], 403)
        self.assertNotIn(
            "test-ors-key", result.content.decode() + " ".join(logs.output)
        )
