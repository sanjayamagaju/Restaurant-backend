import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class MenuCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name_plural = 'menu categories'

    def __str__(self):
        return self.name


class MenuItem(models.Model):
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    description = models.TextField(blank=True, default='')
    is_special = models.BooleanField(default=False)
    image_url = models.TextField(blank=True, default='')
    category = models.ForeignKey(
        MenuCategory, related_name='menu_items', on_delete=models.SET_NULL, null=True, blank=True,
    )
    available = models.BooleanField(default=True)
    preparation_time = models.PositiveIntegerField(
        null=True, blank=True, help_text='Estimated preparation time, in minutes.',
    )
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.name


class MenuItemOptionGroup(models.Model):
    """A customization choice for a menu item, e.g. "Choose Size" or "Extras".

    `min_selection`/`max_selection` express both single-select (1/1) and
    multi-select (0/N) groups uniformly, so order validation doesn't need to
    special-case "radio vs checkbox" - it just enforces a selection count range.
    """

    menu_item = models.ForeignKey(MenuItem, related_name='option_groups', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    required = models.BooleanField(default=False)
    min_selection = models.PositiveIntegerField(default=0)
    max_selection = models.PositiveIntegerField(default=1)
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return f'{self.menu_item.name} - {self.name}'

    def clean(self):
        if self.max_selection < 1:
            raise ValidationError('Maximum selection must be at least 1.')
        if self.max_selection < self.min_selection:
            raise ValidationError('Maximum selection cannot be less than minimum selection.')
        if self.required and self.min_selection < 1:
            raise ValidationError('A required option group must require at least 1 selection.')


class MenuItemOption(models.Model):
    option_group = models.ForeignKey(MenuItemOptionGroup, related_name='options', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    additional_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return f'{self.option_group.name} - {self.name}'


class Table(models.Model):
    """A physical table, provisioned by staff and identified to customers via a QR code.

    `code` is the opaque token embedded in the QR URL - it never changes, so a
    printed QR code stays valid even if `number` (the staff-visible label) is edited.
    """

    number = models.CharField(max_length=50, unique=True)
    code = models.CharField(max_length=32, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = uuid.uuid4().hex[:12]
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Table {self.number}'


class Order(models.Model):
    STATUS_NEW = 'NEW'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_PREPARING = 'PREPARING'
    STATUS_READY = 'READY'
    STATUS_SERVED = 'SERVED'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (STATUS_NEW, 'New'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_PREPARING, 'Preparing'),
        (STATUS_READY, 'Ready'),
        (STATUS_SERVED, 'Served'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    # Maps a status to the set of statuses it may transition to.
    ALLOWED_TRANSITIONS = {
        STATUS_NEW: {STATUS_ACCEPTED, STATUS_CANCELLED},
        STATUS_ACCEPTED: {STATUS_PREPARING, STATUS_CANCELLED},
        STATUS_PREPARING: {STATUS_READY, STATUS_CANCELLED},
        STATUS_READY: {STATUS_SERVED},
        STATUS_SERVED: {STATUS_COMPLETED},
        STATUS_COMPLETED: set(),
        STATUS_CANCELLED: set(),
    }

    REASON_CUSTOMER_REQUEST = 'CUSTOMER_REQUEST'
    REASON_ITEM_UNAVAILABLE = 'ITEM_UNAVAILABLE'
    REASON_OTHER = 'OTHER'

    CANCELLATION_REASON_CHOICES = [
        (REASON_CUSTOMER_REQUEST, 'Customer request'),
        (REASON_ITEM_UNAVAILABLE, 'Unavailable item'),
        (REASON_OTHER, 'Other'),
    ]

    # Maps a status to the timestamp field that should be stamped when it is entered.
    STATUS_TIMESTAMP_FIELD = {
        STATUS_ACCEPTED: 'accepted_at',
        STATUS_PREPARING: 'preparing_at',
        STATUS_READY: 'ready_at',
        STATUS_SERVED: 'served_at',
        STATUS_COMPLETED: 'completed_at',
        STATUS_CANCELLED: 'cancelled_at',
    }

    table_number = models.CharField(max_length=50)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    special_instructions = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)
    # Only meaningful once an order reaches CANCELLED; blank for orders cancelled
    # before reasons were captured, so historical rows are never back-filled with a guess.
    cancellation_reason = models.CharField(
        max_length=20, choices=CANCELLATION_REASON_CHOICES, blank=True, default='',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    preparing_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'Order #{self.pk} - Table {self.table_number}'

    def can_transition_to(self, new_status):
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, new_status):
        """Validate and apply a status transition, stamping the relevant timestamp.

        Does not save the instance; caller is responsible for persisting it.
        """
        if new_status == self.status:
            return
        if not self.can_transition_to(new_status):
            raise ValidationError(
                f'Cannot transition order from {self.status} to {new_status}.'
            )
        from django.utils import timezone

        self.status = new_status
        timestamp_field = self.STATUS_TIMESTAMP_FIELD.get(new_status)
        if timestamp_field:
            setattr(self, timestamp_field, timezone.now())


class OrderItem(models.Model):
    """A line item on an order.

    `*_snapshot` fields freeze the menu item's name/price and chosen
    customizations at order time, so a historical order's receipt/kitchen
    ticket stays accurate even if the menu item or its options are later
    renamed, repriced, or deleted.
    """

    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    menu_item = models.ForeignKey(MenuItem, related_name='+', on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    completed = models.BooleanField(default=False)
    special_instructions = models.CharField(max_length=500, blank=True, default='')
    item_name_snapshot = models.CharField(max_length=200, default='')
    base_price_snapshot = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    unit_price_snapshot = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    selected_options_snapshot = models.JSONField(default=list, blank=True)
    # Frozen so category analytics stay historically correct if an item is later
    # moved to a different category, or its category is renamed/deleted.
    category_name_snapshot = models.CharField(max_length=100, blank=True, default='')

    def __str__(self):
        return f'{self.quantity} x {self.menu_item.name}'

    @property
    def item_total(self):
        return self.unit_price_snapshot * self.quantity


class OrderFeedback(models.Model):
    """A customer's rating of a completed order.

    Anonymous by design - the only link back to the diner is the order itself,
    so analytics can surface ratings and comments without exposing identity.
    """

    RATING_MIN = 1
    RATING_MAX = 5

    order = models.OneToOneField(Order, related_name='feedback', on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(RATING_MIN), MaxValueValidator(RATING_MAX)],
    )
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'order feedback'

    def __str__(self):
        return f'Order #{self.order_id} - {self.rating}/5'
