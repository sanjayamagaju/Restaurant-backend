from django.apps import AppConfig


class RestaurantConfig(AppConfig):
    name = 'restaurant'

    def ready(self):
        from . import signals  # noqa: F401
