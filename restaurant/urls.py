from rest_framework.routers import DefaultRouter

from .views import MenuItemViewSet, OrderViewSet, OrderItemViewSet

router = DefaultRouter()
router.register('menu-items', MenuItemViewSet, basename='menu-item')
router.register('orders', OrderViewSet, basename='order')
router.register('order-items', OrderItemViewSet, basename='order-item')

urlpatterns = router.urls
