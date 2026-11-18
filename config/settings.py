"""Configuration for the CSV-backed, database-free trip API."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-track-route-local")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "apps.stations.apps.StationsConfig",
    "apps.fuel_planning.apps.FuelPlanningConfig",
    "apps.locations.apps.LocationsConfig",
    "apps.routes.apps.RoutesConfig",
    "django.contrib.staticfiles",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": ["django.template.context_processors.request"]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {}  # CSV files and bounded in-memory caches are the only stores.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"

# Request-location geocoding. Public Nominatim is supported in one process only.
NOMINATIM_SEARCH_URL = os.environ.get(
    "NOMINATIM_SEARCH_URL", "https://nominatim.openstreetmap.org/search"
)
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "TrackRoute/0.1 (start and destination lookup)"
)
NOMINATIM_TIMEOUT = 10
LOCATION_CACHE_TTL = 24 * 60 * 60
LOCATION_MISS_CACHE_TTL = 5 * 60
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_DIRECTIONS_URL = os.environ.get(
    "ORS_DIRECTIONS_URL", "https://api.heigit.org/openrouteservice/v2/directions"
)
ORS_TIMEOUT = 30
ROUTE_CACHE_TTL = 60 * 60
FUEL_STATIONS_RAW_PATH = BASE_DIR / "data/raw/fuel-prices-for-be-assessment.csv"
FUEL_STATIONS_CSV_PATH = BASE_DIR / "data/processed/stations_geocoded_census.csv"
FUEL_STATIONS_REPORT_PATH = BASE_DIR / "data/processed/census_batch_report.json"
FUEL_PRICE_POLICY = "lowest_listed"
FUEL_MOVEMENT_MODEL_VERSION = "route-excursions-v1"
FUEL_MODEL_CACHE_TTL = 60 * 60
FUEL_SEARCH_TIMEOUT_SECONDS = 5
FUEL_SEARCH_MAX_LABELS = 200000
FUEL_PLAN_CACHE_TTL = 60 * 60
FUEL_MAP_CACHE_TTL = 60 * 60
FUEL_SOLVER_VERSION = "continuous-fuel-v1"
MAP_TILE_URL = os.environ.get(
    "MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
)
MAP_TILE_ATTRIBUTION = os.environ.get(
    "MAP_TILE_ATTRIBUTION",
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
)
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "locations": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "request-locations",
        "OPTIONS": {"MAX_ENTRIES": 1000},
    },
    "routes": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "driving-routes",
        "OPTIONS": {"MAX_ENTRIES": 64},
    },
    "station_catalogs": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "station-catalogs",
        "OPTIONS": {"MAX_ENTRIES": 2},
    },
    "station_attachments": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "station-attachments",
        "OPTIONS": {"MAX_ENTRIES": 8},
    },
    "route_contexts": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "route-contexts",
        "OPTIONS": {"MAX_ENTRIES": 8},
    },
    "fuel_plans": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-plans",
        "OPTIONS": {"MAX_ENTRIES": 64},
    },
    "fuel_maps": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-maps",
        "OPTIONS": {"MAX_ENTRIES": 64},
    },
}
