from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import (
    MenuCategory, MenuItem, MenuItemOption, MenuItemOptionGroup, Order, OrderItem, Table,
)
from .pricing import build_order_items


class MenuCategorySerializer(serializers.ModelSerializer):
    displayOrder = serializers.IntegerField(source='display_order', required=False, min_value=0)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    itemCount = serializers.SerializerMethodField()

    class Meta:
        model = MenuCategory
        fields = [
            'id', 'name', 'description', 'displayOrder', 'active', 'itemCount', 'createdAt', 'updatedAt',
        ]

    def get_itemCount(self, obj):
        return obj.menu_items.count()


# --- Option groups/options: nested read-only shape, used inside MenuItemSerializer ---

class NestedMenuItemOptionSerializer(serializers.ModelSerializer):
    additionalPrice = serializers.DecimalField(source='additional_price', max_digits=8, decimal_places=2)
    displayOrder = serializers.IntegerField(source='display_order')

    class Meta:
        model = MenuItemOption
        fields = ['id', 'name', 'additionalPrice', 'displayOrder', 'active']


class NestedMenuItemOptionGroupSerializer(serializers.ModelSerializer):
    options = serializers.SerializerMethodField()
    minSelection = serializers.IntegerField(source='min_selection')
    maxSelection = serializers.IntegerField(source='max_selection')
    displayOrder = serializers.IntegerField(source='display_order')

    class Meta:
        model = MenuItemOptionGroup
        fields = ['id', 'name', 'required', 'minSelection', 'maxSelection', 'displayOrder', 'active', 'options']

    def get_options(self, obj):
        options = obj.options.all()
        if not self._is_staff_request():
            options = [o for o in options if o.active]
        return NestedMenuItemOptionSerializer(options, many=True).data

    def _is_staff_request(self):
        request = self.context.get('request')
        return bool(request and request.user and request.user.is_authenticated)


# --- Option groups/options: standalone CRUD serializers used by admin management endpoints ---

class MenuItemOptionGroupSerializer(serializers.ModelSerializer):
    menuItemId = serializers.PrimaryKeyRelatedField(source='menu_item', queryset=MenuItem.objects.all())
    minSelection = serializers.IntegerField(source='min_selection', required=False, min_value=0)
    maxSelection = serializers.IntegerField(source='max_selection', required=False, min_value=1)
    displayOrder = serializers.IntegerField(source='display_order', required=False, min_value=0)

    class Meta:
        model = MenuItemOptionGroup
        fields = ['id', 'menuItemId', 'name', 'required', 'minSelection', 'maxSelection', 'displayOrder', 'active']

    def validate(self, attrs):
        min_selection = attrs.get('min_selection', getattr(self.instance, 'min_selection', 0))
        max_selection = attrs.get('max_selection', getattr(self.instance, 'max_selection', 1))
        required = attrs.get('required', getattr(self.instance, 'required', False))
        if max_selection < min_selection:
            raise serializers.ValidationError('Maximum selection cannot be less than minimum selection.')
        if required and min_selection < 1:
            raise serializers.ValidationError('A required option group must require at least 1 selection.')
        return attrs


class MenuItemOptionSerializer(serializers.ModelSerializer):
    optionGroupId = serializers.PrimaryKeyRelatedField(
        source='option_group', queryset=MenuItemOptionGroup.objects.all(),
    )
    additionalPrice = serializers.DecimalField(
        source='additional_price', max_digits=8, decimal_places=2, min_value=Decimal('0'),
    )
    displayOrder = serializers.IntegerField(source='display_order', required=False, min_value=0)

    class Meta:
        model = MenuItemOption
        fields = ['id', 'optionGroupId', 'name', 'additionalPrice', 'displayOrder', 'active']


class MenuItemSerializer(serializers.ModelSerializer):
    isSpecial = serializers.BooleanField(source='is_special')
    imageUrl = serializers.CharField(source='image_url', required=False, allow_blank=True)
    category = serializers.PrimaryKeyRelatedField(
        queryset=MenuCategory.objects.all(), required=False, allow_null=True,
    )
    categoryName = serializers.CharField(source='category.name', read_only=True, default=None)
    preparationTime = serializers.IntegerField(source='preparation_time', required=False, allow_null=True)
    displayOrder = serializers.IntegerField(source='display_order', required=False, min_value=0)
    optionGroups = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = MenuItem
        fields = [
            'id', 'name', 'price', 'description', 'isSpecial', 'imageUrl',
            'category', 'categoryName', 'available', 'preparationTime', 'displayOrder',
            'optionGroups', 'createdAt', 'updatedAt',
        ]

    def get_optionGroups(self, obj):
        request = self.context.get('request')
        is_staff_request = bool(request and request.user and request.user.is_authenticated)
        groups = obj.option_groups.all() if is_staff_request else obj.option_groups.filter(active=True)
        return NestedMenuItemOptionGroupSerializer(
            sorted(groups, key=lambda g: (g.display_order, g.id)), many=True, context=self.context,
        ).data

    def validate_price(self, value):
        if value < 0:
            raise serializers.ValidationError('Price cannot be negative.')
        return value

    def validate_preparationTime(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError('Preparation time cannot be negative.')
        return value


class TableSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Table
        fields = ['id', 'number', 'code', 'createdAt']
        read_only_fields = ['code', 'createdAt']


class OrderItemSerializer(serializers.ModelSerializer):
    """Read-only representation of an order line, including its price/customization
    snapshot. Writes are handled entirely by `OrderSerializer.create` (via
    `pricing.build_order_items`) - this serializer only allows `completed` to be
    patched by kitchen staff.
    """

    menuItem = MenuItemSerializer(source='menu_item', read_only=True)
    specialInstructions = serializers.CharField(source='special_instructions', read_only=True)
    unitPrice = serializers.DecimalField(source='unit_price_snapshot', max_digits=8, decimal_places=2, read_only=True)
    itemTotal = serializers.SerializerMethodField()
    selectedOptions = serializers.JSONField(source='selected_options_snapshot', read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            'id', 'menuItem', 'quantity', 'completed',
            'specialInstructions', 'unitPrice', 'itemTotal', 'selectedOptions',
        ]
        read_only_fields = ['menuItem', 'quantity', 'specialInstructions', 'unitPrice', 'itemTotal', 'selectedOptions']

    def get_itemTotal(self, obj):
        return obj.unit_price_snapshot * obj.quantity


class OrderItemInputSerializer(serializers.Serializer):
    menuItemId = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    selectedOptionIds = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )
    # No max_length here: pricing.build_order_items truncates rather than rejects,
    # so a slightly-too-long note doesn't fail the whole order.
    specialInstructions = serializers.CharField(required=False, allow_blank=True, default='')


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    tableNumber = serializers.CharField(source='table_number')
    totalAmount = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    specialInstructions = serializers.CharField(
        source='special_instructions', required=False, allow_blank=True
    )
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    acceptedAt = serializers.DateTimeField(source='accepted_at', read_only=True)
    preparingAt = serializers.DateTimeField(source='preparing_at', read_only=True)
    readyAt = serializers.DateTimeField(source='ready_at', read_only=True)
    servedAt = serializers.DateTimeField(source='served_at', read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)
    cancelledAt = serializers.DateTimeField(source='cancelled_at', read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'tableNumber', 'items', 'totalAmount', 'specialInstructions', 'status', 'createdAt',
            'acceptedAt', 'preparingAt', 'readyAt', 'servedAt', 'completedAt', 'cancelledAt',
        ]

    def to_internal_value(self, data):
        # Orders always start life as NEW; ignore any client-supplied status on create
        # instead of failing choice validation on legacy/forged values.
        if self.instance is None and hasattr(data, 'copy'):
            data = data.copy()
            data.pop('status', None)
        return super().to_internal_value(data)

    def validate(self, attrs):
        if self.instance is None:
            items_raw = self.initial_data.get('items') or []
            if not items_raw:
                raise serializers.ValidationError({'items': 'At least one item is required.'})
            input_serializer = OrderItemInputSerializer(data=items_raw, many=True)
            input_serializer.is_valid(raise_exception=True)
            attrs['_items_input'] = input_serializer.validated_data
        return attrs

    def create(self, validated_data):
        items_input = validated_data.pop('_items_input')
        validated_data['status'] = Order.STATUS_NEW
        with transaction.atomic():
            prepared_items, total = build_order_items(items_input)
            order = Order.objects.create(total_amount=total, **validated_data)
            OrderItem.objects.bulk_create([
                OrderItem(order=order, **item_kwargs) for item_kwargs in prepared_items
            ])
        return order

    def update(self, instance, validated_data):
        validated_data.pop('items', None)
        validated_data.pop('_items_input', None)
        new_status = validated_data.pop('status', None)
        if new_status and new_status != instance.status:
            try:
                instance.transition_to(new_status)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'status': exc.messages})
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
