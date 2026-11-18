"""Public API and map URLs."""

from django.urls import include, path

urlpatterns = [
    path("", include("apps.fuel_planning.urls")),
]
