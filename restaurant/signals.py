import json

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.renderers import JSONRenderer

from .consumers import ADMIN_GROUP, order_group_name
from .models import Order, OrderItem
from .serializers import OrderSerializer


def serialize_order_for_broadcast(order):
    """JSON-safe dict for an Order, suitable for Channels' send_json.

    `.data` on a serializer holds raw Decimal/datetime objects (the renderer
    normally converts those on the way out of an HTTP response). Channels'
    send_json uses the plain json module, which can't encode either - so
    render through DRF's JSONRenderer first to get JSON-safe primitives.
    """
    return json.loads(JSONRenderer().render(OrderSerializer(order).data))


def broadcast_order(order, event_type):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {
        'type': event_type,
        'order': serialize_order_for_broadcast(order),
    }
    async_to_sync(channel_layer.group_send)(
        ADMIN_GROUP, {'type': 'order.event', 'payload': payload}
    )
    async_to_sync(channel_layer.group_send)(
        order_group_name(order.id), {'type': 'order.event', 'payload': payload}
    )


@receiver(post_save, sender=Order)
def on_order_saved(sender, instance, created, **kwargs):
    broadcast_order(instance, 'order_created' if created else 'order_updated')


@receiver(post_save, sender=OrderItem)
def on_order_item_saved(sender, instance, created, **kwargs):
    broadcast_order(instance.order, 'order_updated')
