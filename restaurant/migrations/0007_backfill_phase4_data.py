from django.db import migrations


def backfill_data(apps, schema_editor):
    MenuCategory = apps.get_model('restaurant', 'MenuCategory')
    MenuItem = apps.get_model('restaurant', 'MenuItem')
    OrderItem = apps.get_model('restaurant', 'OrderItem')

    if MenuItem.objects.filter(category__isnull=True).exists():
        uncategorized, _ = MenuCategory.objects.get_or_create(
            name='Uncategorized',
            defaults={'description': 'Items not yet assigned to a category.', 'display_order': 0},
        )
        MenuItem.objects.filter(category__isnull=True).update(category=uncategorized)

    # Pre-Phase-4 order items have no pricing/customization snapshot; best-effort
    # backfill from the live menu item so old orders still display sensibly.
    for order_item in OrderItem.objects.select_related('menu_item').filter(item_name_snapshot=''):
        order_item.item_name_snapshot = order_item.menu_item.name
        order_item.base_price_snapshot = order_item.menu_item.price
        order_item.unit_price_snapshot = order_item.menu_item.price
        order_item.selected_options_snapshot = []
        order_item.save(update_fields=[
            'item_name_snapshot', 'base_price_snapshot', 'unit_price_snapshot', 'selected_options_snapshot',
        ])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0006_menucategory_alter_menuitem_options_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_data, noop_reverse),
    ]
