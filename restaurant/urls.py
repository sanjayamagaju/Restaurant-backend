from django.urls import path
from rest_framework.routers import DefaultRouter

from .analytics_views import (
    CategoryAnalyticsView, FeedbackAnalyticsView, MenuPerformanceAnalyticsView, OverviewAnalyticsView,
    TableAnalyticsView, TimePatternsAnalyticsView, TrendsAnalyticsView,
)
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

urlpatterns = router.urls + [
    path('analytics/overview/', OverviewAnalyticsView.as_view(), name='analytics-overview'),
    path('analytics/trends/', TrendsAnalyticsView.as_view(), name='analytics-trends'),
    path('analytics/menu-performance/', MenuPerformanceAnalyticsView.as_view(), name='analytics-menu-performance'),
    path('analytics/categories/', CategoryAnalyticsView.as_view(), name='analytics-categories'),
    path('analytics/time-patterns/', TimePatternsAnalyticsView.as_view(), name='analytics-time-patterns'),
    path('analytics/tables/', TableAnalyticsView.as_view(), name='analytics-tables'),
    path('analytics/feedback/', FeedbackAnalyticsView.as_view(), name='analytics-feedback'),
]
