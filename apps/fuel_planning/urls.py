from django.urls import path

from . import views

app_name = "fuel_planning"

urlpatterns = [
    path("api/trips/plan/", views.create_fuel_plan, name="create"),
    path("trips/<uuid:plan_id>/map/", views.fuel_plan_map, name="map"),
]
