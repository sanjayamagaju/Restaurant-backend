import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from . import analytics
from .models import MenuCategory, MenuItem, Order, OrderFeedback, OrderItem


def local_datetime(year, month, day, hour=0, minute=0):
    return timezone.make_aware(
        datetime(year, month, day, hour, minute), timezone.get_current_timezone(),
    )


class AnalyticsTestMixin:
    """Builds a small, deterministic order history to assert against."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)

        self.burgers = MenuCategory.objects.create(name='Burgers', display_order=1)
        self.drinks = MenuCategory.objects.create(name='Drinks', display_order=2)
        self.burger = MenuItem.objects.create(name='Chicken Burger', price=Decimal('650.00'), category=self.burgers)
        self.coffee = MenuItem.objects.create(name='Cold Coffee', price=Decimal('250.00'), category=self.drinks)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def make_order(self, *, created_at, status=Order.STATUS_COMPLETED, lines=(), table_number='1',
                   cancellation_reason=''):
        """Create an order with explicit line snapshots and a forced timestamp.

        `created_at` is set after the fact because `auto_now_add` ignores any
        value passed to create().
        """
        total = sum((unit_price * quantity for _item, unit_price, quantity in lines), Decimal('0.00'))
        order = Order.objects.create(
            table_number=table_number, total_amount=total, status=status,
            cancellation_reason=cancellation_reason,
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()

        for item, unit_price, quantity in lines:
            OrderItem.objects.create(
                order=order, menu_item=item, quantity=quantity,
                item_name_snapshot=item.name,
                base_price_snapshot=unit_price,
                unit_price_snapshot=unit_price,
                category_name_snapshot=item.category.name if item.category else '',
            )
        return order


class DateRangeTestCase(TestCase):
    def test_range_end_is_exclusive_midnight_after_last_day(self):
        start, end, meta = analytics.resolve_range(
            analytics.RANGE_CUSTOM, '2026-09-01', '2026-09-07',
        )
        self.assertEqual(start, local_datetime(2026, 9, 1, 0, 0))
        # The whole of 2026-09-07 must be included: end is midnight on the 8th.
        self.assertEqual(end, local_datetime(2026, 9, 8, 0, 0))
        self.assertEqual(meta['from'], '2026-09-01')
        self.assertEqual(meta['to'], '2026-09-07')

    def test_an_order_late_on_the_final_day_is_included(self):
        start, end, _meta = analytics.resolve_range(
            analytics.RANGE_CUSTOM, '2026-09-01', '2026-09-07',
        )
        late = local_datetime(2026, 9, 7, 23, 59)
        self.assertGreaterEqual(late, start)
        self.assertLess(late, end)

    def test_today_covers_exactly_one_day(self):
        today = timezone.localdate()
        start, end, meta = analytics.resolve_range(analytics.RANGE_TODAY, today=today)
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual(meta['from'], meta['to'])

    def test_last_7_days_includes_today_and_six_prior_days(self):
        today = timezone.localdate()
        start, end, meta = analytics.resolve_range(analytics.RANGE_LAST_7, today=today)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertEqual(meta['to'], today.isoformat())
        self.assertEqual(meta['from'], (today - timedelta(days=6)).isoformat())

    def test_previous_month_resolves_to_that_whole_calendar_month(self):
        _start, _end, meta = analytics.resolve_range(
            analytics.RANGE_PREV_MONTH, today=date(2026, 3, 15),
        )
        self.assertEqual(meta['from'], '2026-02-01')
        self.assertEqual(meta['to'], '2026-02-28')

    def test_previous_period_is_the_equally_long_preceding_window(self):
        start, end, _meta = analytics.resolve_range(
            analytics.RANGE_CUSTOM, '2026-09-08', '2026-09-14',
        )
        prev_start, prev_end = analytics.previous_period(start, end)
        self.assertEqual(prev_end, start)
        self.assertEqual(prev_end - prev_start, end - start)

    def test_custom_range_requires_both_dates(self):
        with self.assertRaises(Exception):
            analytics.resolve_range(analytics.RANGE_CUSTOM, '2026-09-01', None)

    def test_reversed_custom_range_is_rejected(self):
        with self.assertRaises(Exception):
            analytics.resolve_range(analytics.RANGE_CUSTOM, '2026-09-07', '2026-09-01')

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(Exception):
            analytics.resolve_range('last_century')


class PctChangeTestCase(TestCase):
    def test_growth_and_decline(self):
        self.assertEqual(analytics.pct_change(110, 100)['value'], 10.0)
        self.assertEqual(analytics.pct_change(90, 100)['value'], -10.0)

    def test_zero_baseline_with_growth_reports_new_not_infinity(self):
        result = analytics.pct_change(500, 0)
        self.assertIsNone(result['value'])
        self.assertEqual(result['label'], 'New')

    def test_zero_on_both_sides_reports_a_dash(self):
        result = analytics.pct_change(0, 0)
        self.assertIsNone(result['value'])
        self.assertEqual(result['label'], '—')


class RevenueAndKpiTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(12, 0)), timezone.get_current_timezone(),
        )
        # 2 completed orders: 650*2 + 250 = 1550, and 650 -> revenue 2200, 4 items.
        self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 2),
                                              (self.coffee, Decimal('250.00'), 1)])
        self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 1)])
        # Cancelled and in-flight orders must not count towards revenue.
        self.make_order(created_at=at, status=Order.STATUS_CANCELLED,
                        cancellation_reason=Order.REASON_CUSTOMER_REQUEST,
                        lines=[(self.burger, Decimal('650.00'), 5)])
        self.make_order(created_at=at, status=Order.STATUS_PREPARING,
                        lines=[(self.coffee, Decimal('250.00'), 3)])

    def totals(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        return analytics.order_totals(start, end)

    def test_revenue_counts_only_completed_orders(self):
        self.assertEqual(self.totals()['revenue'], Decimal('2200.00'))

    def test_total_orders_counts_every_status(self):
        self.assertEqual(self.totals()['totalOrders'], 4)

    def test_completed_and_cancelled_counts(self):
        totals = self.totals()
        self.assertEqual(totals['completedOrders'], 2)
        self.assertEqual(totals['cancelledOrders'], 1)

    def test_items_sold_counts_only_completed_order_lines(self):
        self.assertEqual(self.totals()['itemsSold'], 4)

    def test_average_order_value_is_revenue_over_completed_orders(self):
        self.assertEqual(self.totals()['averageOrderValue'], Decimal('1100.00'))

    def test_average_order_value_is_zero_when_nothing_completed(self):
        Order.objects.filter(status=Order.STATUS_COMPLETED).delete()
        totals = self.totals()
        self.assertEqual(totals['completedOrders'], 0)
        self.assertEqual(totals['averageOrderValue'], Decimal('0.00'))

    def test_empty_range_returns_zeroed_totals(self):
        start, end, _meta = analytics.resolve_range(
            analytics.RANGE_CUSTOM, '2000-01-01', '2000-01-31',
        )
        totals = analytics.order_totals(start, end)
        self.assertEqual(totals['totalOrders'], 0)
        self.assertEqual(totals['revenue'], Decimal('0.00'))
        self.assertEqual(totals['averageOrderValue'], Decimal('0.00'))
        self.assertEqual(totals['itemsSold'], 0)

    def test_status_breakdown_lists_every_status_including_zeroes(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        breakdown = {row['status']: row['count'] for row in analytics.status_breakdown(start, end)}
        self.assertEqual(breakdown[Order.STATUS_COMPLETED], 2)
        self.assertEqual(breakdown[Order.STATUS_CANCELLED], 1)
        self.assertEqual(breakdown[Order.STATUS_PREPARING], 1)
        self.assertEqual(breakdown[Order.STATUS_READY], 0)

    def test_cancellation_rate_and_reasons(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        result = analytics.cancellation_breakdown(start, end)
        self.assertEqual(result['cancelledOrders'], 1)
        self.assertEqual(result['totalOrders'], 4)
        self.assertEqual(result['rate'], 25.0)
        self.assertEqual(
            [(row['reason'], row['count']) for row in result['reasons']],
            [(Order.REASON_CUSTOMER_REQUEST, 1)],
        )

    def test_cancellation_rate_is_zero_with_no_orders(self):
        Order.objects.all().delete()
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        self.assertEqual(analytics.cancellation_breakdown(start, end)['rate'], 0.0)


class MenuAndCategoryAnalyticsTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(13, 0)), timezone.get_current_timezone(),
        )
        self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 2)])
        self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 1),
                                              (self.coffee, Decimal('250.00'), 4)])

    def range_today(self):
        return analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)[:2]

    def test_units_and_revenue_per_item(self):
        start, end = self.range_today()
        rows = {row['name']: row for row in analytics.menu_performance(start, end)}
        self.assertEqual(rows['Chicken Burger']['unitsSold'], 3)
        self.assertEqual(rows['Chicken Burger']['revenue'], Decimal('1950.00'))
        self.assertEqual(rows['Chicken Burger']['orders'], 2)
        self.assertEqual(rows['Cold Coffee']['unitsSold'], 4)
        self.assertEqual(rows['Cold Coffee']['revenue'], Decimal('1000.00'))
        self.assertEqual(rows['Cold Coffee']['orders'], 1)

    def test_repricing_a_menu_item_does_not_change_historical_revenue(self):
        start, end = self.range_today()
        before = analytics.menu_performance(start, end)

        self.burger.price = Decimal('800.00')
        self.burger.save(update_fields=['price'])

        after = {row['name']: row for row in analytics.menu_performance(start, end)}
        self.assertEqual(after['Chicken Burger']['revenue'], Decimal('1950.00'))
        self.assertEqual(before, analytics.menu_performance(start, end))

    def test_sorting_by_units_and_revenue(self):
        start, end = self.range_today()
        by_revenue = analytics.menu_performance(start, end, sort='revenue')
        self.assertEqual(by_revenue[0]['name'], 'Chicken Burger')
        by_units = analytics.menu_performance(start, end, sort='units')
        self.assertEqual(by_units[0]['name'], 'Cold Coffee')

    def test_ascending_sort_surfaces_low_sellers_first(self):
        start, end = self.range_today()
        rows = analytics.menu_performance(start, end, sort='units', descending=False)
        self.assertEqual(rows[0]['name'], 'Chicken Burger')

    def test_limit_and_search_and_category_filters(self):
        start, end = self.range_today()
        self.assertEqual(len(analytics.menu_performance(start, end, limit=1)), 1)
        searched = analytics.menu_performance(start, end, search='coffee')
        self.assertEqual([row['name'] for row in searched], ['Cold Coffee'])
        filtered = analytics.menu_performance(start, end, category_id=self.drinks.id)
        self.assertEqual([row['name'] for row in filtered], ['Cold Coffee'])

    def test_category_revenue_and_share_of_total(self):
        start, end = self.range_today()
        rows = {row['category']: row for row in analytics.category_performance(start, end)}
        self.assertEqual(rows['Burgers']['revenue'], Decimal('1950.00'))
        self.assertEqual(rows['Drinks']['revenue'], Decimal('1000.00'))
        self.assertEqual(rows['Burgers']['unitsSold'], 3)
        self.assertAlmostEqual(
            rows['Burgers']['pctOfRevenue'] + rows['Drinks']['pctOfRevenue'], 100.0, places=1,
        )

    def test_category_uses_snapshot_so_moving_an_item_leaves_history_alone(self):
        start, end = self.range_today()
        self.burger.category = self.drinks
        self.burger.save(update_fields=['category'])

        rows = {row['category']: row for row in analytics.category_performance(start, end)}
        # Past burger revenue stays under Burgers, where it was sold.
        self.assertEqual(rows['Burgers']['revenue'], Decimal('1950.00'))
        self.assertEqual(rows['Drinks']['revenue'], Decimal('1000.00'))

    def test_renaming_a_category_leaves_history_alone(self):
        start, end = self.range_today()
        self.burgers.name = 'Sandwiches'
        self.burgers.save(update_fields=['name'])

        categories = [row['category'] for row in analytics.category_performance(start, end)]
        self.assertIn('Burgers', categories)
        self.assertNotIn('Sandwiches', categories)


class TrendAndTimePatternTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        # 2026-09-04 is a Friday; two orders that day, one the next.
        self.make_order(created_at=local_datetime(2026, 9, 4, 12, 30),
                        lines=[(self.burger, Decimal('650.00'), 1)])
        self.make_order(created_at=local_datetime(2026, 9, 4, 19, 15),
                        lines=[(self.burger, Decimal('650.00'), 2)])
        self.make_order(created_at=local_datetime(2026, 9, 5, 19, 45),
                        status=Order.STATUS_CANCELLED,
                        lines=[(self.coffee, Decimal('250.00'), 1)])

    def week_range(self):
        return analytics.resolve_range(analytics.RANGE_CUSTOM, '2026-09-01', '2026-09-07')[:2]

    def test_daily_trend_buckets_orders_and_completed_revenue(self):
        start, end = self.week_range()
        points = analytics.trends(start, end, analytics.GRANULARITY_DAY)
        by_day = {point['bucket'][:10]: point for point in points}
        self.assertEqual(by_day['2026-09-04']['orders'], 2)
        self.assertEqual(by_day['2026-09-04']['revenue'], Decimal('1950.00'))
        # The cancelled order still counts as volume but contributes no revenue.
        self.assertEqual(by_day['2026-09-05']['orders'], 1)
        self.assertEqual(by_day['2026-09-05']['revenue'], Decimal('0.00'))

    def test_granularity_is_chosen_from_the_range_length(self):
        self.assertEqual(
            analytics.resolve_granularity(*analytics.resolve_range(analytics.RANGE_TODAY)[:2]),
            analytics.GRANULARITY_HOUR,
        )
        self.assertEqual(
            analytics.resolve_granularity(*self.week_range()), analytics.GRANULARITY_DAY,
        )
        long_start, long_end, _ = analytics.resolve_range(
            analytics.RANGE_CUSTOM, '2026-01-01', '2026-12-31',
        )
        self.assertEqual(analytics.resolve_granularity(long_start, long_end), analytics.GRANULARITY_MONTH)

    def test_peak_hours_group_in_local_time_and_cover_all_24_hours(self):
        start, end = self.week_range()
        hours = analytics.time_patterns(start, end)['hours']
        self.assertEqual(len(hours), 24)
        by_hour = {row['hour']: row for row in hours}
        self.assertEqual(by_hour[12]['orders'], 1)
        self.assertEqual(by_hour[19]['orders'], 2)
        self.assertEqual(by_hour[3]['orders'], 0)
        # Revenue is completed-only: 19:00 holds one completed and one cancelled order.
        self.assertEqual(by_hour[19]['revenue'], Decimal('1300.00'))

    def test_day_of_week_is_monday_first_and_labels_correctly(self):
        start, end = self.week_range()
        weekdays = analytics.time_patterns(start, end)['weekdays']
        self.assertEqual([row['weekday'] for row in weekdays][0], 'Monday')
        self.assertEqual(len(weekdays), 7)
        by_day = {row['weekday']: row['orders'] for row in weekdays}
        self.assertEqual(by_day['Friday'], 2)
        self.assertEqual(by_day['Saturday'], 1)
        self.assertEqual(by_day['Monday'], 0)


class TablePerformanceTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(14, 0)), timezone.get_current_timezone(),
        )
        self.make_order(created_at=at, table_number='1', lines=[(self.burger, Decimal('650.00'), 2)])
        self.make_order(created_at=at, table_number='1', lines=[(self.coffee, Decimal('250.00'), 2)])
        self.make_order(created_at=at, table_number='2', lines=[(self.burger, Decimal('650.00'), 1)])
        self.make_order(created_at=at, table_number='2', status=Order.STATUS_CANCELLED,
                        lines=[(self.burger, Decimal('650.00'), 4)])

    def test_orders_revenue_and_average_per_table(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        rows = {row['tableNumber']: row for row in analytics.table_performance(start, end)}
        self.assertEqual(rows['1']['orders'], 2)
        self.assertEqual(rows['1']['revenue'], Decimal('1800.00'))
        self.assertEqual(rows['1']['averageOrderValue'], Decimal('900.00'))
        # Table 2's cancelled order is excluded from both count and revenue.
        self.assertEqual(rows['2']['orders'], 1)
        self.assertEqual(rows['2']['revenue'], Decimal('650.00'))


class FeedbackTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(15, 0)), timezone.get_current_timezone(),
        )
        self.orders = [
            self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 1)])
            for _ in range(4)
        ]
        for order, rating in zip(self.orders, [5, 5, 4, 2]):
            OrderFeedback.objects.create(order=order, rating=rating, comment=f'Rated {rating}')

    def test_average_rating_and_distribution(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        summary = analytics.feedback_summary(start, end)
        self.assertEqual(summary['count'], 4)
        self.assertEqual(summary['averageRating'], 4.0)
        distribution = {row['rating']: row for row in summary['distribution']}
        self.assertEqual(distribution[5]['count'], 2)
        self.assertEqual(distribution[5]['pct'], 50.0)
        self.assertEqual(distribution[3]['count'], 0)
        self.assertEqual([row['rating'] for row in summary['distribution']], [5, 4, 3, 2, 1])

    def test_recent_feedback_exposes_no_customer_or_table_information(self):
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        recent = analytics.feedback_summary(start, end)['recent']
        self.assertTrue(recent)
        self.assertEqual(set(recent[0]), {'orderId', 'rating', 'comment', 'createdAt'})

    def test_no_feedback_yields_null_average_not_an_error(self):
        OrderFeedback.objects.all().delete()
        start, end, _meta = analytics.resolve_range(analytics.RANGE_TODAY, today=self.day)
        summary = analytics.feedback_summary(start, end)
        self.assertEqual(summary['count'], 0)
        self.assertIsNone(summary['averageRating'])

    def test_customer_can_rate_a_completed_order_without_auth(self):
        order = self.make_order(
            created_at=timezone.now(), lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.post(
            f'/api/orders/{order.id}/feedback/', {'rating': 5, 'comment': 'Great food.'}, format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(OrderFeedback.objects.get(order=order).rating, 5)

    def test_rating_an_unfinished_order_is_rejected(self):
        order = self.make_order(
            created_at=timezone.now(), status=Order.STATUS_PREPARING,
            lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.post(f'/api/orders/{order.id}/feedback/', {'rating': 5}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_an_order_cannot_be_rated_twice(self):
        response = self.client.post(
            f'/api/orders/{self.orders[0].id}/feedback/', {'rating': 4}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_out_of_range_rating_is_rejected(self):
        order = self.make_order(
            created_at=timezone.now(), lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.post(f'/api/orders/{order.id}/feedback/', {'rating': 9}, format='json')
        self.assertEqual(response.status_code, 400)


class AnalyticsEndpointTestCase(AnalyticsTestMixin, TestCase):
    ENDPOINTS = [
        '/api/analytics/overview/',
        '/api/analytics/trends/',
        '/api/analytics/menu-performance/',
        '/api/analytics/categories/',
        '/api/analytics/time-patterns/',
        '/api/analytics/tables/',
        '/api/analytics/feedback/',
    ]

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(16, 0)), timezone.get_current_timezone(),
        )
        self.make_order(created_at=at, lines=[(self.burger, Decimal('650.00'), 2)])

    def test_every_analytics_endpoint_rejects_anonymous_requests(self):
        for endpoint in self.ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertIn(response.status_code, (401, 403))

    def test_staff_can_read_every_analytics_endpoint(self):
        self.auth()
        for endpoint in self.ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(endpoint).status_code, 200)

    def test_overview_response_shape(self):
        self.auth()
        response = self.client.get('/api/analytics/overview/', {'range': 'today'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['kpis']['completedOrders'], 1)
        self.assertEqual(response.data['meta']['range'], 'today')
        self.assertIn('comparison', response.data)
        self.assertIn('statusBreakdown', response.data)
        self.assertIn('cancellation', response.data)

    def test_all_widgets_resolve_the_same_range_from_identical_params(self):
        self.auth()
        params = {'range': 'custom', 'from': '2026-09-01', 'to': '2026-09-07'}
        metas = []
        for endpoint in self.ENDPOINTS:
            response = self.client.get(endpoint, params)
            self.assertEqual(response.status_code, 200)
            metas.append((response.data['meta']['from'], response.data['meta']['to']))
        self.assertEqual(len(set(metas)), 1)

    def test_invalid_range_parameters_return_400(self):
        self.auth()
        self.assertEqual(self.client.get('/api/analytics/overview/', {'range': 'nope'}).status_code, 400)
        self.assertEqual(
            self.client.get(
                '/api/analytics/overview/', {'range': 'custom', 'from': 'not-a-date', 'to': '2026-09-07'},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get('/api/analytics/overview/', {'range': 'custom', 'from': '2026-09-01'}).status_code,
            400,
        )

    def test_menu_performance_rejects_an_unknown_sort(self):
        self.auth()
        response = self.client.get('/api/analytics/menu-performance/', {'sort': 'sideways'})
        self.assertEqual(response.status_code, 400)


class CsvExportTestCase(AnalyticsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(17, 0)), timezone.get_current_timezone(),
        )
        # A name containing a comma and a quote must survive CSV escaping.
        self.tricky = MenuItem.objects.create(
            name='Momo, "Steamed"', price=Decimal('300.00'), category=self.burgers,
        )
        self.make_order(created_at=at, table_number='7',
                        lines=[(self.tricky, Decimal('300.00'), 3)])

    def read_csv(self, url, params=None):
        self.auth()
        response = self.client.get(url, {'range': 'today', 'export': 'csv', **(params or {})})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename=', response['Content-Disposition'])
        body = response.content.decode('utf-8-sig')
        return list(csv.reader(io.StringIO(body)))

    def test_menu_performance_csv_headers_and_escaping(self):
        rows = self.read_csv('/api/analytics/menu-performance/')
        self.assertEqual(
            rows[0], ['Item', 'Category', 'Units Sold', 'Revenue', 'Orders', 'Availability'],
        )
        row = next(row for row in rows[1:] if row[0] == 'Momo, "Steamed"')
        self.assertEqual(row[1], 'Burgers')
        self.assertEqual(row[2], '3')
        self.assertEqual(Decimal(row[3]), Decimal('900.00'))
        self.assertEqual(row[5], 'Available')

    def test_category_csv_headers_and_rows(self):
        rows = self.read_csv('/api/analytics/categories/')
        self.assertEqual(rows[0], ['Category', 'Units Sold', 'Orders', 'Revenue', '% of Revenue'])
        row = next(row for row in rows[1:] if row[0] == 'Burgers')
        self.assertEqual(Decimal(row[3]), Decimal('900.00'))

    def test_table_csv_headers_and_rows(self):
        rows = self.read_csv('/api/analytics/tables/')
        self.assertEqual(rows[0], ['Table', 'Orders', 'Revenue', 'Average Order Value'])
        row = next(row for row in rows[1:] if row[0] == '7')
        self.assertEqual(row[1], '1')
        self.assertEqual(Decimal(row[2]), Decimal('900.00'))

    def test_csv_is_utf8_with_a_bom_for_spreadsheet_compatibility(self):
        self.auth()
        response = self.client.get(
            '/api/analytics/menu-performance/', {'range': 'today', 'export': 'csv'},
        )
        self.assertTrue(response.content.startswith('﻿'.encode('utf-8')))


class CancellationReasonTestCase(AnalyticsTestMixin, TestCase):
    def test_staff_can_cancel_with_a_reason(self):
        self.auth()
        order = self.make_order(
            created_at=timezone.now(), status=Order.STATUS_NEW,
            lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.patch(
            f'/api/orders/{order.id}/',
            {'status': Order.STATUS_CANCELLED, 'cancellationReason': Order.REASON_ITEM_UNAVAILABLE},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancellation_reason, Order.REASON_ITEM_UNAVAILABLE)

    def test_cancelling_without_a_reason_still_works(self):
        self.auth()
        order = self.make_order(
            created_at=timezone.now(), status=Order.STATUS_NEW,
            lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.patch(
            f'/api/orders/{order.id}/', {'status': Order.STATUS_CANCELLED}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.cancellation_reason, '')

    def test_a_reason_is_ignored_on_a_non_cancelling_transition(self):
        self.auth()
        order = self.make_order(
            created_at=timezone.now(), status=Order.STATUS_NEW,
            lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.patch(
            f'/api/orders/{order.id}/',
            {'status': Order.STATUS_ACCEPTED, 'cancellationReason': Order.REASON_OTHER},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACCEPTED)
        self.assertEqual(order.cancellation_reason, '')

    def test_an_unknown_reason_is_rejected(self):
        self.auth()
        order = self.make_order(
            created_at=timezone.now(), status=Order.STATUS_NEW,
            lines=[(self.burger, Decimal('650.00'), 1)],
        )
        response = self.client.patch(
            f'/api/orders/{order.id}/',
            {'status': Order.STATUS_CANCELLED, 'cancellationReason': 'BECAUSE'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)


class CategorySnapshotTestCase(AnalyticsTestMixin, TestCase):
    def test_placing_an_order_freezes_the_category_name(self):
        response = self.client.post(
            '/api/orders/',
            {
                'tableNumber': '3',
                'items': [{'menuItemId': self.burger.id, 'quantity': 1}],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        line = OrderItem.objects.get(order_id=response.data['id'])
        self.assertEqual(line.category_name_snapshot, 'Burgers')

    def test_an_item_without_a_category_snapshots_blank(self):
        loose = MenuItem.objects.create(name='Mystery Special', price=Decimal('100.00'), category=None)
        response = self.client.post(
            '/api/orders/',
            {'tableNumber': '3', 'items': [{'menuItemId': loose.id, 'quantity': 1}]},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        line = OrderItem.objects.get(order_id=response.data['id'])
        self.assertEqual(line.category_name_snapshot, '')


class AggregationEfficiencyTestCase(AnalyticsTestMixin, TestCase):
    """Guards Part 21/22/38: analytics must aggregate in the database, not in Python."""

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        at = timezone.make_aware(
            datetime.combine(self.day, time(18, 0)), timezone.get_current_timezone(),
        )
        for index in range(40):
            self.make_order(
                created_at=at, table_number=str(index % 5),
                lines=[(self.burger, Decimal('650.00'), 1), (self.coffee, Decimal('250.00'), 2)],
            )

    def test_endpoint_query_count_does_not_grow_with_order_volume(self):
        self.auth()
        # A fixed ceiling: 40 orders / 80 lines must not translate into per-row queries.
        for endpoint, max_queries in [
            ('/api/analytics/overview/', 12),
            ('/api/analytics/trends/', 6),
            ('/api/analytics/menu-performance/', 6),
            ('/api/analytics/categories/', 6),
            ('/api/analytics/time-patterns/', 8),
            ('/api/analytics/tables/', 6),
            ('/api/analytics/feedback/', 8),
        ]:
            with self.subTest(endpoint=endpoint):
                with CaptureQueriesContext(connection) as captured:
                    self.assertEqual(self.client.get(endpoint, {'range': 'today'}).status_code, 200)
                self.assertLessEqual(len(captured.captured_queries), max_queries)
