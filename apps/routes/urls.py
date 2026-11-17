from django.urls import path

from .views import fetch_route

app_name = "routes"
urlpatterns = [path("", fetch_route, name="fetch")]
