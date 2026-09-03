from django.db import migrations


def backfill_category_snapshot(apps, schema_editor):
    """Give pre-existing order lines a category snapshot.

    Orders placed before this field existed have no record of the category
    their item belonged to at the time, so the live category is the best
    available signal. Items with no category stay blank rather than being
    guessed at, and analytics reports those as "Uncategorised".
    """
    OrderItem = apps.get_model('restaurant', 'OrderItem')

    order_items = (
        OrderItem.objects
        .select_related('menu_item__category')
        .filter(category_name_snapshot='', menu_item__category__isnull=False)
    )
    for order_item in order_items:
        order_item.category_name_snapshot = order_item.menu_item.category.name
        order_item.save(update_fields=['category_name_snapshot'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0008_order_cancellation_reason_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_category_snapshot, noop_reverse),
    ]
