from rest_framework.routers import DefaultRouter

from .views import (
    MenuCategoryViewSet, MenuItemOptionGroupViewSet, MenuItemOptionViewSet, MenuItemViewSet,
    OrderViewSet, OrderItemViewSet, TableViewSet,
)

router = DefaultRouter()
router.register('menu-categories', MenuCategoryViewSet, basename='menu-category')
router.register('menu-items', MenuItemViewSet, basename='menu-item')
router.register('menu-option-groups', MenuItemOptionGroupViewSet, basename='menu-option-group')
router.register('menu-item-options', MenuItemOptionViewSet, basename='menu-item-option')
router.register('orders', OrderViewSet, basename='order')
router.register('order-items', OrderItemViewSet, basename='order-item')
router.register('tables', TableViewSet, basename='table')

urlpatterns = router.urls
