# Generated manually to migrate legacy pending/completed status values.

from django.db import migrations


def forwards(apps, schema_editor):
    Order = apps.get_model('restaurant', 'Order')
    Order.objects.filter(status='pending').update(status='NEW')
    completed = Order.objects.filter(status='completed')
    for order in completed:
        order.status = 'COMPLETED'
        order.completed_at = order.created_at
        order.save(update_fields=['status', 'completed_at'])


def backwards(apps, schema_editor):
    Order = apps.get_model('restaurant', 'Order')
    Order.objects.exclude(status='CANCELLED').filter(
        status__in=['NEW', 'ACCEPTED', 'PREPARING', 'READY', 'SERVED']
    ).update(status='pending')
    Order.objects.filter(status='COMPLETED').update(status='completed')


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0002_order_lifecycle'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
