from django.urls import re_path

from .consumers import AdminOrdersConsumer, CustomerOrderConsumer

websocket_urlpatterns = [
    re_path(r'^ws/orders/admin/$', AdminOrdersConsumer.as_asgi()),
    re_path(r'^ws/orders/(?P<order_id>\d+)/$', CustomerOrderConsumer.as_asgi()),
]
