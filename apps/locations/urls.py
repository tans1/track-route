from django.urls import path

from .views import resolve_locations

app_name = "locations"
urlpatterns = [path("resolve/", resolve_locations, name="resolve")]
