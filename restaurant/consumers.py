from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from rest_framework.authtoken.models import Token

ADMIN_GROUP = 'orders_admin'


def order_group_name(order_id):
    return f'order_{order_id}'


class AdminOrdersConsumer(AsyncJsonWebsocketConsumer):
    """Staff-facing socket: receives every order create/update event."""

    async def connect(self):
        query = parse_qs(self.scope['query_string'].decode())
        token_key = query.get('token', [None])[0]
        user = await self._get_user_for_token(token_key)
        if user is None:
            await self.close(code=4001)
            return
        await self.channel_layer.group_add(ADMIN_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(ADMIN_GROUP, self.channel_name)

    async def order_event(self, event):
        await self.send_json(event['payload'])

    @database_sync_to_async
    def _get_user_for_token(self, token_key):
        if not token_key:
            return None
        try:
            return Token.objects.select_related('user').get(key=token_key).user
        except Token.DoesNotExist:
            return None


class CustomerOrderConsumer(AsyncJsonWebsocketConsumer):
    """Public socket: receives updates for a single order only."""

    async def connect(self):
        self.order_id = self.scope['url_route']['kwargs']['order_id']
        self.group_name = order_group_name(self.order_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def order_event(self, event):
        await self.send_json(event['payload'])
