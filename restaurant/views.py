from django.shortcuts import get_object_or_404
from rest_framework import viewsets, mixins, permissions
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import MenuCategory, MenuItem, MenuItemOption, MenuItemOptionGroup, Order, OrderItem, Table
from .serializers import (
    MenuCategorySerializer, MenuItemOptionGroupSerializer, MenuItemOptionSerializer, MenuItemSerializer,
    OrderSerializer, OrderItemSerializer, TableSerializer,
)


class MenuCategoryViewSet(viewsets.ModelViewSet):
    serializer_class = MenuCategorySerializer

    def get_queryset(self):
        queryset = MenuCategory.objects.all().order_by('display_order', 'name')
        if not (self.request.user and self.request.user.is_authenticated):
            queryset = queryset.filter(active=True)
        return queryset

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def destroy(self, request, *args, **kwargs):
        category = self.get_object()
        if category.menu_items.exists():
            return Response(
                {'detail': 'This category still has menu items. Reassign or deactivate it instead of deleting it.'},
                status=400,
            )
        return super().destroy(request, *args, **kwargs)


class MenuItemViewSet(viewsets.ModelViewSet):
    serializer_class = MenuItemSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        return (
            MenuItem.objects.select_related('category')
            .prefetch_related('option_groups__options')
            .order_by('display_order', 'id')
        )

    @action(detail=True, methods=['post'], url_path='toggle-availability', permission_classes=[permissions.IsAuthenticated])
    def toggle_availability(self, request, pk=None):
        item = self.get_object()
        item.available = not item.available
        item.save(update_fields=['available'])
        return Response(self.get_serializer(item).data)


class MenuItemOptionGroupViewSet(viewsets.ModelViewSet):
    serializer_class = MenuItemOptionGroupSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = MenuItemOptionGroup.objects.all().order_by('display_order', 'id')
        menu_item_id = self.request.query_params.get('menu_item')
        if menu_item_id:
            queryset = queryset.filter(menu_item_id=menu_item_id)
        return queryset


class MenuItemOptionViewSet(viewsets.ModelViewSet):
    serializer_class = MenuItemOptionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = MenuItemOption.objects.all().order_by('display_order', 'id')
        option_group_id = self.request.query_params.get('option_group')
        if option_group_id:
            queryset = queryset.filter(option_group_id=option_group_id)
        return queryset


class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all().order_by('-created_at').prefetch_related(
        'items__menu_item__category',
    )
    serializer_class = OrderSerializer

    def get_permissions(self):
        if self.action in ('create', 'retrieve'):
            # Customers place orders anonymously and track their own order by id.
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]


class OrderItemViewSet(mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = OrderItem.objects.all()
    serializer_class = OrderItemSerializer
    permission_classes = [permissions.IsAuthenticated]


class TableViewSet(viewsets.ModelViewSet):
    queryset = Table.objects.all().order_by('number')
    serializer_class = TableSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['get'], url_path='resolve/(?P<code>[^/]+)', permission_classes=[permissions.AllowAny])
    def resolve(self, request, code=None):
        # Public: a customer's scanned QR resolves its opaque code to a table number.
        table = get_object_or_404(Table, code=code)
        return Response({'number': table.number, 'code': table.code})


class LoginView(ObtainAuthToken):
    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        token, _ = Token.objects.get_or_create(user=user)
        return Response({'token': token.key, 'username': user.username})
