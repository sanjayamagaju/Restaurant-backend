from django.contrib import admin

from .models import (
    MenuCategory, MenuItem, MenuItemOption, MenuItemOptionGroup, Order, OrderFeedback, OrderItem, Table,
)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = [
        'item_name_snapshot', 'base_price_snapshot', 'unit_price_snapshot', 'selected_options_snapshot',
        'category_name_snapshot',
    ]


class MenuItemOptionInline(admin.TabularInline):
    model = MenuItemOption
    extra = 0


@admin.register(MenuCategory)
class MenuCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'display_order', 'active', 'item_count']
    list_filter = ['active']
    search_fields = ['name']

    def item_count(self, obj):
        return obj.menu_items.count()


class MenuItemOptionGroupInline(admin.TabularInline):
    model = MenuItemOptionGroup
    extra = 0
    show_change_link = True


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'price', 'available', 'is_special', 'preparation_time']
    list_filter = ['category', 'available', 'is_special']
    search_fields = ['name', 'description']
    inlines = [MenuItemOptionGroupInline]


@admin.register(MenuItemOptionGroup)
class MenuItemOptionGroupAdmin(admin.ModelAdmin):
    list_display = ['name', 'menu_item', 'required', 'min_selection', 'max_selection', 'active']
    list_filter = ['active', 'required']
    search_fields = ['name', 'menu_item__name']
    inlines = [MenuItemOptionInline]


@admin.register(OrderFeedback)
class OrderFeedbackAdmin(admin.ModelAdmin):
    list_display = ['order', 'rating', 'created_at']
    list_filter = ['rating']
    readonly_fields = ['created_at']


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'table_number', 'total_amount', 'status', 'created_at']
    list_filter = ['status', 'cancellation_reason']
    readonly_fields = [
        'accepted_at', 'preparing_at', 'ready_at', 'served_at', 'completed_at', 'cancelled_at',
    ]
    inlines = [OrderItemInline]


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ['number', 'code', 'created_at']
    readonly_fields = ['code']
    search_fields = ['number']
