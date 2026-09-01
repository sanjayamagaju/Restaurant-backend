from rest_framework import serializers

from .models import MenuItem, Order, OrderItem


class MenuItemSerializer(serializers.ModelSerializer):
    isSpecial = serializers.BooleanField(source='is_special')
    imageUrl = serializers.CharField(source='image_url', required=False, allow_blank=True)

    class Meta:
        model = MenuItem
        fields = ['id', 'name', 'price', 'description', 'isSpecial', 'imageUrl']


class OrderItemSerializer(serializers.ModelSerializer):
    menuItem = MenuItemSerializer(source='menu_item', read_only=True)
    menuItemId = serializers.PrimaryKeyRelatedField(
        source='menu_item', queryset=MenuItem.objects.all(), write_only=True
    )

    class Meta:
        model = OrderItem
        fields = ['id', 'menuItem', 'menuItemId', 'quantity', 'completed']


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)
    tableNumber = serializers.CharField(source='table_number')
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'tableNumber', 'items', 'totalAmount', 'status', 'createdAt']

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        order = Order.objects.create(**validated_data)
        OrderItem.objects.bulk_create([
            OrderItem(order=order, **item_data) for item_data in items_data
        ])
        return order
