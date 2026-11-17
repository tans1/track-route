import json
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from django.core.cache import caches
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from .services.geocoding import (
    LocationNotFound,
    LocationResolver,
    ProviderError,
    retry_delay,
)


def place(
    label="Los Angeles, California, United States",
    country="us",
    lat="34.05",
    lon="-118.24",
):
    return {
        "display_name": label,
        "address": {"country_code": country},
        "lat": lat,
        "lon": lon,
    }


def response(payload):
    result = MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


class ResolverTestCase(SimpleTestCase):
    def setUp(self):
        caches["locations"].clear()
        self.addCleanup(caches["locations"].clear)
        self.now = 100.0
        self.delays = []
        self.resolver = LocationResolver(clock=lambda: self.now, sleep=self.advance)
        patcher = patch(
            "apps.locations.services.geocoding.urlopen",
            return_value=response([place()]),
        )
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class LocationResolverTests(ResolverTestCase):
    def test_search_parameters_identification_and_coordinate_order(self):
        result = self.resolver.resolve("Los Angeles, CA")
        request = self.http.call_args.args[0]
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(
            params,
            {
                "q": ["Los Angeles, CA"],
                "countrycodes": ["us"],
                "format": ["jsonv2"],
                "addressdetails": ["1"],
                "limit": ["3"],
                "accept-language": ["en"],
            },
        )
        self.assertEqual(urlsplit(request.full_url).path, "/search")
        self.assertIn("TrackRoute", request.get_header("User-agent"))
        self.assertEqual(self.http.call_args.kwargs["timeout"], 10)
        self.assertEqual((result["latitude"], result["longitude"]), (34.05, -118.24))
        self.assertEqual(result["country_code"], "us")

    def test_country_filter_is_verified_on_returned_results(self):
        for country in ("ca", "gb", None, 123, ""):
            with self.subTest(country=country):
                self.http.return_value = response([place(country=country)])
                with self.assertRaises(LocationNotFound):
                    self.resolver.resolve(f"Foreign location {country}")

    def test_missing_country_is_not_accepted(self):
        self.http.return_value = response(
            [{"lat": "34", "lon": "-118", "display_name": "Unverified"}]
        )
        with self.assertRaises(LocationNotFound):
            self.resolver.resolve("Unverified")

    def test_selects_first_verified_us_result(self):
        self.http.return_value = response(
            [place(country="ca"), place(country="US"), place(lat="40")]
        )
        self.assertEqual(self.resolver.resolve("Los Angeles")["latitude"], 34.05)

    def test_normalized_queries_share_cache_but_preserve_request_text(self):
        self.resolver.resolve("  Los   Angeles, CA ")
        result = self.resolver.resolve("los angeles, ca")
        self.assertEqual(result["query"], "los angeles, ca")
        self.http.assert_called_once()

    def test_different_states_have_separate_cache_entries(self):
        self.resolver.resolve("Springfield, IL")
        self.resolver.resolve("Springfield, MA")
        self.assertEqual(self.http.call_count, 2)

    def test_switching_provider_does_not_reuse_other_provider_cache(self):
        self.resolver.resolve("Los Angeles, CA")
        with override_settings(NOMINATIM_SEARCH_URL="https://example.test/search"):
            self.resolver.resolve("Los Angeles, CA")
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(
            urlsplit(self.http.call_args.args[0].full_url).netloc, "example.test"
        )

    def test_empty_results_are_cached(self):
        self.http.return_value = response([])
        for _ in range(2):
            with self.assertRaises(LocationNotFound):
                self.resolver.resolve("Missing place")
        self.http.assert_called_once()

    def test_success_and_miss_cache_expire(self):
        with override_settings(LOCATION_CACHE_TTL=10, LOCATION_MISS_CACHE_TTL=3):
            with patch(
                "django.core.cache.backends.locmem.time.time", return_value=1000
            ) as clock:
                self.resolver.resolve("Los Angeles")
                self.http.return_value = response([])
                with self.assertRaises(LocationNotFound):
                    self.resolver.resolve("Missing")
                clock.return_value = 1004
                self.http.return_value = response([place()])
                self.resolver.resolve("Missing")
                self.resolver.resolve("Los Angeles")
                self.assertEqual(self.http.call_count, 3)
                clock.return_value = 1011
                self.resolver.resolve("Los Angeles")
                self.assertEqual(self.http.call_count, 4)

    def test_provider_requests_are_spaced_but_cache_hits_do_not_wait(self):
        starts = []

        def fetch(*args, **kwargs):
            starts.append(self.now)
            return response([place()])

        self.http.side_effect = fetch
        self.resolver.resolve("Los Angeles")
        self.resolver.resolve("San Francisco")
        self.resolver.resolve("Los Angeles")
        self.assertGreaterEqual(starts[1] - starts[0], 1)
        self.assertEqual(len(self.delays), 1)

    def test_concurrent_identical_queries_issue_one_request(self):
        barrier = threading.Barrier(4)

        def lookup(_):
            barrier.wait(timeout=5)
            return self.resolver.resolve("Los Angeles")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lookup, range(4)))
        self.assertEqual(len(results), 4)
        self.http.assert_called_once()

    def test_throttling_honors_retry_after_and_recovers(self):
        self.http.side_effect = HTTPError(
            "https://example.test/search", 429, "Slow down", {"Retry-After": "30"}, None
        )
        with self.assertRaises(ProviderError) as first:
            self.resolver.resolve("Los Angeles")
        self.assertEqual(first.exception.retry_after, 30)
        with self.assertRaises(ProviderError):
            self.resolver.resolve("Another place")
        self.http.assert_called_once()
        self.now += 31
        self.http.side_effect = None
        self.resolver.resolve("Los Angeles")
        self.assertEqual(self.http.call_count, 2)

    def test_transient_failure_is_not_cached_as_no_match(self):
        self.http.side_effect = URLError("network unavailable")
        with self.assertRaises(ProviderError):
            self.resolver.resolve("Los Angeles")
        self.now += 6
        self.http.side_effect = None
        self.assertEqual(self.resolver.resolve("Los Angeles")["country_code"], "us")
        self.assertEqual(self.http.call_count, 2)

    def test_malformed_provider_responses_are_rejected(self):
        for payload in (
            {"error": "upstream error"},
            [None],
            [place(lat="nan")],
            [place(lon="181")],
            [place(lat=None)],
            [place(lat=True)],
            [place(label="")],
        ):
            with self.subTest(payload=payload):
                self.http.return_value = response(payload)
                with self.assertRaises(ProviderError) as error:
                    self.resolver.resolve("Malformed")
                self.assertEqual(error.exception.status, 502)
                self.now += 6

    def test_invalid_json_response_is_a_provider_error(self):
        self.http.return_value.__enter__.return_value.read.return_value = (
            b"<html>Unavailable</html>"
        )
        with self.assertRaises(ProviderError) as error:
            self.resolver.resolve("Los Angeles")
        self.assertEqual(error.exception.status, 502)

    def test_retry_after_accepts_http_date_and_falls_back_for_invalid_values(self):
        fixed = datetime(2026, 11, 17, tzinfo=timezone.utc)
        with patch("apps.locations.services.geocoding.datetime") as clock:
            clock.now.return_value = fixed
            later = fixed.replace(minute=1)
            self.assertEqual(retry_delay(format_datetime(later), 5), 60)
        self.assertEqual(retry_delay("invalid", 5), 5)
        self.assertEqual(retry_delay(None, 5), 5)


class ResolveEndpointTests(ResolverTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("locations:resolve")
        patcher = patch("apps.locations.views.resolver", self.resolver)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client(enforce_csrf_checks=True)

    def post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )

    def test_resolves_both_locations_and_reuses_cache_on_next_request(self):
        self.http.side_effect = [
            response([place()]),
            response(
                [
                    place(
                        "San Francisco, California, United States",
                        lat="37.77",
                        lon="-122.42",
                    )
                ]
            ),
        ]
        payload = {"start": "Los Angeles, CA", "destination": "San Francisco, CA"}
        first = self.post(payload)
        self.assertEqual(first.status_code, 200)
        result = first.json()
        self.assertEqual(result["start"]["latitude"], 34.05)
        self.assertEqual(result["destination"]["longitude"], -122.42)
        self.assertEqual(result["provider"], "nominatim")
        self.assertIn("OpenStreetMap", result["attribution"])
        second = self.post(payload)
        self.assertEqual(second.json(), result)
        self.assertEqual(self.http.call_count, 2)

    def test_same_start_and_destination_only_need_one_lookup(self):
        result = self.post({"start": "Los Angeles", "destination": "los angeles"})
        self.assertEqual(result.status_code, 200)
        self.http.assert_called_once()

    def test_validates_entire_payload_before_any_requests(self):
        for payload in (
            None,
            [],
            {},
            {"start": "Los Angeles"},
            {"start": "Los Angeles", "destination": "", "other": "x"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
        for value in (None, 4, True, [], {}, " ", "x" * 301, "bad\x00place"):
            with self.subTest(value=value):
                result = self.post({"start": "Los Angeles", "destination": value})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json()["error"]["field"], "destination")
        self.http.assert_not_called()

    def test_rejects_malformed_json_wrong_method_media_type_and_oversize_body(self):
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
        get = self.client.get(self.url)
        self.assertEqual(get.status_code, 405)
        self.assertEqual(get["Allow"], "POST")
        self.http.assert_not_called()

    def test_reports_which_location_cannot_be_resolved(self):
        self.http.side_effect = [response([place()]), response([place(country="ca")])]
        result = self.post({"start": "Los Angeles", "destination": "Toronto, Canada"})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["error"]["field"], "destination")
        self.assertEqual(result.json()["error"]["code"], "location_not_found")

    def test_provider_throttling_returns_retry_header(self):
        self.http.side_effect = HTTPError(
            "https://example.test/search", 429, "Slow down", {"Retry-After": "20"}, None
        )
        result = self.post({"start": "Los Angeles", "destination": "San Francisco"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result["Retry-After"], "20")
        self.assertEqual(result.json()["error"]["field"], "start")
        self.assertEqual(result.json()["error"]["reason"], "provider_rate_limited")
        self.assertEqual(result.json()["error"]["provider_status"], 429)
        self.assertEqual(result.json()["error"]["retry_after"], 20)

    def test_reports_connection_timeout_dns_and_certificate_errors(self):
        failures = [
            (TimeoutError("timed out"), "provider_timeout"),
            (URLError(TimeoutError("timed out")), "provider_timeout"),
            (
                URLError(socket.gaierror(-2, "hostname lookup failed")),
                "provider_dns_error",
            ),
            (
                URLError(ssl.SSLCertVerificationError("certificate verify failed")),
                "provider_tls_error",
            ),
            (URLError("connection unavailable"), "provider_connection_error"),
        ]
        for failure, reason in failures:
            with self.subTest(reason=reason):
                self.http.side_effect = failure
                with self.assertLogs(
                    "apps.locations.services.geocoding", level="WARNING"
                ) as logs:
                    result = self.post(
                        {"start": "123 Private Street", "destination": "San Francisco"}
                    )
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()["error"]["reason"], reason)
                self.assertEqual(result.json()["error"]["retry_after"], 5)
                self.assertNotIn("provider_status", result.json()["error"])
                self.assertIn(reason, logs.output[0])
                self.assertNotIn("123 Private Street", " ".join(logs.output))
                self.now += 6

    def test_reports_upstream_http_status_and_explains_cooldown(self):
        self.http.side_effect = HTTPError(
            "https://example.test/search",
            503,
            "Unavailable",
            {"Retry-After": "15"},
            None,
        )
        payload = {"start": "Los Angeles", "destination": "San Francisco"}
        first = self.post(payload)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json()["error"]["reason"], "provider_http_error")
        self.assertEqual(first.json()["error"]["provider_status"], 503)
        self.now += 4
        second = self.post(payload)
        self.assertEqual(second.json()["error"]["reason"], "provider_cooldown")
        self.assertEqual(second.json()["error"]["retry_after"], 11)
        self.assertEqual(second["Retry-After"], "11")
        self.http.assert_called_once()

    def test_invalid_provider_response_returns_bad_gateway(self):
        self.http.return_value = response({"error": "invalid"})
        result = self.post({"start": "Los Angeles", "destination": "San Francisco"})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["error"]["reason"], "provider_invalid_response")
