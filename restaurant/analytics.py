"""Analytics aggregation for the admin dashboard.

METRIC DEFINITIONS
==================

Every analytics endpoint uses these definitions - they are deliberately shared
so no two widgets can disagree about what "revenue" or "an order" means.

Total Orders
    Count of orders whose `created_at` falls in the selected range, regardless
    of status. This is the demand signal: an order the kitchen later cancelled
    still represents a customer who ordered.

Revenue
    Sum of `Order.total_amount` for orders in range whose status is COMPLETED.
    Cancelled orders never contribute revenue, and in-flight orders (NEW ..
    SERVED) are not counted as earned yet.

Completed Orders / Cancelled Orders
    Count of orders in range currently in that terminal status.

Average Order Value
    Revenue / number of COMPLETED orders in range. Zero when there are no
    completed orders (never a division error, never Infinity).

Items Sold
    Sum of `OrderItem.quantity` across COMPLETED orders in range.

Per-item / per-category revenue
    Sum of `unit_price_snapshot * quantity` from the order line, NOT the menu
    item's current price - so repricing an item never rewrites past reports.
    Categories group by `OrderItem.category_name_snapshot` for the same reason.

Order-volume trend
    Counts orders of any status per time bucket (matches Total Orders).

Revenue trend
    Completed-only per time bucket (matches Revenue).

DATE RANGES
===========

Ranges are half-open: `created_at >= start AND created_at < end`, where `end`
is midnight at the start of the day *after* the requested end date. A request
for 2026-09-01 through 2026-09-07 therefore includes all of 2026-09-07.

Boundaries are computed in the server's configured local timezone
(`settings.TIME_ZONE`, read via `timezone.get_current_timezone()`), as is the
hour/day-of-week bucketing for peak-hour analysis. Changing `TIME_ZONE` in
settings changes how days and hours are grouped here - that is intentional, so
a restaurant operating outside UTC reports on its own calendar rather than
UTC's.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, F, Q, Sum
from django.db.models.functions import (
    Coalesce, ExtractHour, ExtractWeekDay, TruncDay, TruncHour, TruncMonth, TruncWeek,
)
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Order, OrderFeedback, OrderItem

RANGE_TODAY = 'today'
RANGE_YESTERDAY = 'yesterday'
RANGE_LAST_7 = 'last7'
RANGE_LAST_30 = 'last30'
RANGE_THIS_MONTH = 'this_month'
RANGE_PREV_MONTH = 'prev_month'
RANGE_CUSTOM = 'custom'

RANGE_LABELS = {
    RANGE_TODAY: 'Today',
    RANGE_YESTERDAY: 'Yesterday',
    RANGE_LAST_7: 'Last 7 Days',
    RANGE_LAST_30: 'Last 30 Days',
    RANGE_THIS_MONTH: 'This Month',
    RANGE_PREV_MONTH: 'Previous Month',
    RANGE_CUSTOM: 'Custom Range',
}

# Human wording for the "vs previous ..." comparison caption.
COMPARISON_LABELS = {
    RANGE_TODAY: 'vs yesterday',
    RANGE_YESTERDAY: 'vs previous day',
    RANGE_LAST_7: 'vs previous 7 days',
    RANGE_LAST_30: 'vs previous 30 days',
    RANGE_THIS_MONTH: 'vs previous period',
    RANGE_PREV_MONTH: 'vs previous month',
    RANGE_CUSTOM: 'vs previous period',
}

GRANULARITY_HOUR = 'hour'
GRANULARITY_DAY = 'day'
GRANULARITY_WEEK = 'week'
GRANULARITY_MONTH = 'month'

_TRUNC_FOR_GRANULARITY = {
    GRANULARITY_HOUR: TruncHour,
    GRANULARITY_DAY: TruncDay,
    GRANULARITY_WEEK: TruncWeek,
    GRANULARITY_MONTH: TruncMonth,
}

# Django normalises ExtractWeekDay to 1=Sunday .. 7=Saturday on every backend.
WEEKDAY_NAMES = {
    1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday',
    5: 'Thursday', 6: 'Friday', 7: 'Saturday',
}
# Monday-first display order, matching how the dashboard lists the week.
WEEKDAY_DISPLAY_ORDER = [2, 3, 4, 5, 6, 7, 1]

MONEY = DecimalField(max_digits=12, decimal_places=2)
ZERO = Decimal('0.00')


def _local_tz():
    return timezone.get_current_timezone()


def _start_of_day(day):
    """Midnight at the start of `day`, as an aware datetime in local time."""
    return timezone.make_aware(datetime.combine(day, time.min), _local_tz())


def _parse_date_param(raw, field_name):
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValidationError({field_name: 'Expected a date in YYYY-MM-DD format.'})


def resolve_range(preset, from_raw=None, to_raw=None, today=None):
    """Resolve a preset (plus optional custom dates) to `(start, end, meta)`.

    `start`/`end` are aware datetimes forming a half-open interval; `end` is
    midnight following the last included day. `meta` carries the display label,
    the inclusive calendar dates that were used, and the comparison caption.
    """
    preset = preset or RANGE_LAST_7
    if preset not in RANGE_LABELS:
        raise ValidationError({
            'range': f'Unsupported range. Choose one of: {", ".join(sorted(RANGE_LABELS))}.',
        })

    today = today or timezone.localdate()

    if preset == RANGE_CUSTOM:
        if not from_raw or not to_raw:
            raise ValidationError({'range': 'A custom range requires both "from" and "to" dates.'})
        first_day = _parse_date_param(from_raw, 'from')
        last_day = _parse_date_param(to_raw, 'to')
        if last_day < first_day:
            raise ValidationError({'to': 'The end date cannot be before the start date.'})
    elif preset == RANGE_TODAY:
        first_day = last_day = today
    elif preset == RANGE_YESTERDAY:
        first_day = last_day = today - timedelta(days=1)
    elif preset == RANGE_LAST_7:
        # Inclusive of today, so "Last 7 Days" spans today and the 6 days before it.
        first_day, last_day = today - timedelta(days=6), today
    elif preset == RANGE_LAST_30:
        first_day, last_day = today - timedelta(days=29), today
    elif preset == RANGE_THIS_MONTH:
        first_day, last_day = today.replace(day=1), today
    else:  # RANGE_PREV_MONTH
        this_month_first = today.replace(day=1)
        last_day = this_month_first - timedelta(days=1)
        first_day = last_day.replace(day=1)

    start = _start_of_day(first_day)
    end = _start_of_day(last_day + timedelta(days=1))
    meta = {
        'range': preset,
        'label': RANGE_LABELS[preset],
        'from': first_day.isoformat(),
        'to': last_day.isoformat(),
        'comparisonLabel': COMPARISON_LABELS[preset],
    }
    return start, end, meta


def resolve_range_from_request(request):
    params = request.query_params
    return resolve_range(params.get('range'), params.get('from'), params.get('to'))


def previous_period(start, end):
    """The equally-long window immediately preceding `[start, end)`."""
    span = end - start
    return start - span, start


def pct_change(current, previous):
    """Percentage change, with a safe label when there is no baseline.

    Returns `{'value': float|None, 'label': str}`. A missing baseline yields
    'New' (grew from nothing) or '-' (nothing either side) rather than
    dividing by zero.
    """
    current = Decimal(str(current or 0))
    previous = Decimal(str(previous or 0))

    if previous == 0:
        if current == 0:
            return {'value': None, 'label': '—'}
        return {'value': None, 'label': 'New'}

    change = (current - previous) / previous * 100
    return {'value': float(round(change, 1)), 'label': None}


def resolve_granularity(start, end, override=None):
    if override:
        if override not in _TRUNC_FOR_GRANULARITY:
            raise ValidationError({
                'granularity': f'Unsupported granularity. Choose one of: '
                               f'{", ".join(sorted(_TRUNC_FOR_GRANULARITY))}.',
            })
        return override

    days = max((end - start).days, 1)
    if days <= 1:
        return GRANULARITY_HOUR
    if days <= 31:
        return GRANULARITY_DAY
    if days <= 180:
        return GRANULARITY_WEEK
    return GRANULARITY_MONTH


# --- Query building blocks -------------------------------------------------

def orders_in_range(start, end):
    return Order.objects.filter(created_at__gte=start, created_at__lt=end)


def completed_order_items(start, end):
    """Order lines belonging to completed orders created in the range."""
    return OrderItem.objects.filter(
        order__created_at__gte=start,
        order__created_at__lt=end,
        order__status=Order.STATUS_COMPLETED,
    )


_LINE_REVENUE = Sum(F('unit_price_snapshot') * F('quantity'), output_field=MONEY)


def order_totals(start, end):
    """Headline order/revenue aggregates in a single query."""
    completed = Q(status=Order.STATUS_COMPLETED)
    aggregates = orders_in_range(start, end).aggregate(
        total_orders=Count('id'),
        completed_orders=Count('id', filter=completed),
        cancelled_orders=Count('id', filter=Q(status=Order.STATUS_CANCELLED)),
        revenue=Coalesce(Sum('total_amount', filter=completed), ZERO, output_field=MONEY),
    )
    items_sold = completed_order_items(start, end).aggregate(
        units=Coalesce(Sum('quantity'), 0),
    )['units']

    revenue = aggregates['revenue'] or ZERO
    completed_orders = aggregates['completed_orders']
    return {
        'totalOrders': aggregates['total_orders'],
        'completedOrders': completed_orders,
        'cancelledOrders': aggregates['cancelled_orders'],
        'revenue': revenue,
        'itemsSold': items_sold,
        # Revenue-oriented, so the divisor is completed orders only (see module docstring).
        'averageOrderValue': (revenue / completed_orders).quantize(ZERO) if completed_orders else ZERO,
    }


def status_breakdown(start, end):
    counts = {
        row['status']: row['count']
        for row in orders_in_range(start, end).values('status').annotate(count=Count('id'))
    }
    # Emit every status in lifecycle order so the UI can render a stable list.
    return [
        {'status': status, 'label': label, 'count': counts.get(status, 0)}
        for status, label in Order.STATUS_CHOICES
    ]


def cancellation_breakdown(start, end):
    in_range = orders_in_range(start, end)
    totals = in_range.aggregate(
        total=Count('id'),
        cancelled=Count('id', filter=Q(status=Order.STATUS_CANCELLED)),
    )
    total, cancelled = totals['total'], totals['cancelled']

    reason_counts = {
        row['cancellation_reason']: row['count']
        for row in in_range.filter(status=Order.STATUS_CANCELLED)
        .values('cancellation_reason')
        .annotate(count=Count('id'))
    }
    reasons = [
        {'reason': reason, 'label': label, 'count': reason_counts.get(reason, 0)}
        for reason, label in Order.CANCELLATION_REASON_CHOICES
        if reason_counts.get(reason, 0)
    ]
    # Cancellations recorded before reasons were captured stay honestly unlabelled.
    unspecified = reason_counts.get('', 0)
    if unspecified:
        reasons.append({'reason': '', 'label': 'Not recorded', 'count': unspecified})

    return {
        'cancelledOrders': cancelled,
        'totalOrders': total,
        'rate': float(round(cancelled / total * 100, 1)) if total else 0.0,
        'reasons': reasons,
    }


def overview(start, end, meta):
    current = order_totals(start, end)
    prev_start, prev_end = previous_period(start, end)
    previous = order_totals(prev_start, prev_end)

    comparison = {
        key: pct_change(current[key], previous[key])
        for key in ('totalOrders', 'revenue', 'averageOrderValue', 'completedOrders',
                    'cancelledOrders', 'itemsSold')
    }

    return {
        'meta': meta,
        'kpis': current,
        'previous': previous,
        'comparison': comparison,
        'statusBreakdown': status_breakdown(start, end),
        'cancellation': cancellation_breakdown(start, end),
    }


def trends(start, end, granularity):
    """Revenue (completed-only) and order counts (all statuses) per bucket."""
    trunc = _TRUNC_FOR_GRANULARITY[granularity]
    rows = (
        orders_in_range(start, end)
        .annotate(bucket=trunc('created_at', tzinfo=_local_tz()))
        .values('bucket')
        .annotate(
            orders=Count('id'),
            revenue=Coalesce(
                Sum('total_amount', filter=Q(status=Order.STATUS_COMPLETED)), ZERO, output_field=MONEY,
            ),
        )
        .order_by('bucket')
    )
    return [
        {
            'bucket': row['bucket'].isoformat() if row['bucket'] else None,
            'orders': row['orders'],
            'revenue': row['revenue'],
        }
        for row in rows
    ]


def menu_performance(start, end, sort='revenue', descending=True, category_id=None, search=None, limit=None):
    """Per-menu-item units/revenue/order-count for completed orders in range.

    Revenue uses the frozen `unit_price_snapshot` on each line, so changing a
    menu item's price today never alters past figures. Item name, category and
    availability come from the live menu item - this table describes the
    current menu, priced by history.
    """
    queryset = completed_order_items(start, end)
    if category_id:
        queryset = queryset.filter(menu_item__category_id=category_id)
    if search:
        queryset = queryset.filter(menu_item__name__icontains=search)

    rows = (
        queryset
        .values(
            'menu_item_id',
            'menu_item__name',
            'menu_item__available',
            'menu_item__category_id',
            'menu_item__category__name',
        )
        .annotate(
            unitsSold=Coalesce(Sum('quantity'), 0),
            revenue=Coalesce(_LINE_REVENUE, ZERO, output_field=MONEY),
            orders=Count('order_id', distinct=True),
        )
    )

    sort_fields = {'revenue': 'revenue', 'units': 'unitsSold', 'orders': 'orders', 'name': 'menu_item__name'}
    if sort not in sort_fields:
        raise ValidationError({
            'sort': f'Unsupported sort. Choose one of: {", ".join(sorted(sort_fields))}.',
        })
    order_by = sort_fields[sort]
    rows = rows.order_by(f'-{order_by}' if descending else order_by, 'menu_item__name')

    if limit:
        rows = rows[:limit]

    return [
        {
            'menuItemId': row['menu_item_id'],
            'name': row['menu_item__name'],
            'categoryId': row['menu_item__category_id'],
            'categoryName': row['menu_item__category__name'] or 'Uncategorised',
            'available': row['menu_item__available'],
            'unitsSold': row['unitsSold'],
            'revenue': row['revenue'],
            'orders': row['orders'],
        }
        for row in rows
    ]


def category_performance(start, end):
    """Revenue/units per category, grouped by the snapshot taken at order time."""
    rows = (
        completed_order_items(start, end)
        .values('category_name_snapshot')
        .annotate(
            unitsSold=Coalesce(Sum('quantity'), 0),
            revenue=Coalesce(_LINE_REVENUE, ZERO, output_field=MONEY),
            orders=Count('order_id', distinct=True),
        )
        .order_by('-revenue')
    )
    rows = list(rows)
    total_revenue = sum((row['revenue'] for row in rows), ZERO)

    return [
        {
            'category': row['category_name_snapshot'] or 'Uncategorised',
            'unitsSold': row['unitsSold'],
            'revenue': row['revenue'],
            'orders': row['orders'],
            'pctOfRevenue': (
                float(round(row['revenue'] / total_revenue * 100, 1)) if total_revenue else 0.0
            ),
        }
        for row in rows
    ]


def time_patterns(start, end):
    """Order counts and revenue by hour of day and by weekday, in local time."""
    tz = _local_tz()
    completed = Q(status=Order.STATUS_COMPLETED)
    base = orders_in_range(start, end)

    hour_rows = {
        row['hour']: row
        for row in base
        .annotate(hour=ExtractHour('created_at', tzinfo=tz))
        .values('hour')
        .annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=completed), ZERO, output_field=MONEY),
        )
    }
    hours = [
        {
            'hour': hour,
            'orders': hour_rows.get(hour, {}).get('orders', 0),
            'revenue': hour_rows.get(hour, {}).get('revenue', ZERO),
        }
        for hour in range(24)
    ]

    weekday_rows = {
        row['weekday']: row
        for row in base
        .annotate(weekday=ExtractWeekDay('created_at', tzinfo=tz))
        .values('weekday')
        .annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount', filter=completed), ZERO, output_field=MONEY),
        )
    }
    weekdays = [
        {
            'weekday': WEEKDAY_NAMES[key],
            'orders': weekday_rows.get(key, {}).get('orders', 0),
            'revenue': weekday_rows.get(key, {}).get('revenue', ZERO),
        }
        for key in WEEKDAY_DISPLAY_ORDER
    ]

    return {'hours': hours, 'weekdays': weekdays}


def table_performance(start, end):
    """Per-table orders/revenue. `Order.table_number` is already a point-in-time
    string on the order, so renaming a table never rewrites past reports."""
    rows = (
        orders_in_range(start, end)
        .filter(status=Order.STATUS_COMPLETED)
        .values('table_number')
        .annotate(
            orders=Count('id'),
            revenue=Coalesce(Sum('total_amount'), ZERO, output_field=MONEY),
            averageOrderValue=Coalesce(Avg('total_amount'), ZERO, output_field=MONEY),
        )
        .order_by('-revenue')
    )
    return [
        {
            'tableNumber': row['table_number'],
            'orders': row['orders'],
            'revenue': row['revenue'],
            'averageOrderValue': row['averageOrderValue'].quantize(ZERO),
        }
        for row in rows
    ]


def feedback_summary(start, end, recent_limit=8):
    """Rating average, star distribution and a few recent comments.

    Filtered on when the feedback was submitted, not when its order was placed.
    """
    in_range = OrderFeedback.objects.filter(created_at__gte=start, created_at__lt=end)

    totals = in_range.aggregate(count=Count('id'), average=Avg('rating'))
    distribution_counts = {
        row['rating']: row['count']
        for row in in_range.values('rating').annotate(count=Count('id'))
    }
    total = totals['count']
    distribution = [
        {
            'rating': rating,
            'count': distribution_counts.get(rating, 0),
            'pct': float(round(distribution_counts.get(rating, 0) / total * 100, 1)) if total else 0.0,
        }
        for rating in range(OrderFeedback.RATING_MAX, OrderFeedback.RATING_MIN - 1, -1)
    ]

    recent = [
        {
            'orderId': row.order_id,
            'rating': row.rating,
            'comment': row.comment,
            'createdAt': row.created_at.isoformat(),
        }
        for row in in_range.order_by('-created_at')[:recent_limit]
    ]

    return {
        'count': total,
        'averageRating': float(round(totals['average'], 2)) if totals['average'] is not None else None,
        'distribution': distribution,
        'recent': recent,
    }
