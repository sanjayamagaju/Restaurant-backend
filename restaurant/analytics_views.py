"""Read-only analytics endpoints for staff.

Every endpoint takes the same range parameters (`range`, plus `from`/`to` for
`range=custom`) and is restricted to authenticated users - the app has no role
system yet, so "authenticated" is the existing definition of staff (the same
rule the order/table management endpoints use). Nothing here is public: the
figures describe the business, not a customer's own order.
"""

import csv

from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from . import analytics


def _csv_response(filename, header, rows):
    """Stream aggregated rows as CSV.

    A UTF-8 BOM keeps Excel from mangling non-ASCII names, and numbers are
    written unformatted (no currency prefix or thousands separator) so they
    stay usable as numbers in a spreadsheet.
    """
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('﻿')
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


def _wants_csv(request):
    # Deliberately not `format`: DRF reserves that query param for renderer
    # negotiation and would 404 on an unknown value.
    return request.query_params.get('export', '').lower() == 'csv'


class AnalyticsView(APIView):
    """Base class resolving the shared date range once per request."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        start, end, meta = analytics.resolve_range_from_request(request)
        return self.build(request, start, end, meta)

    def build(self, request, start, end, meta):  # pragma: no cover - overridden
        raise NotImplementedError


class OverviewAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        return Response(analytics.overview(start, end, meta))


class TrendsAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        granularity = analytics.resolve_granularity(
            start, end, request.query_params.get('granularity'),
        )
        return Response({
            'meta': {**meta, 'granularity': granularity},
            'points': analytics.trends(start, end, granularity),
        })


class MenuPerformanceAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        params = request.query_params
        limit = params.get('limit')
        rows = analytics.menu_performance(
            start, end,
            sort=params.get('sort', 'revenue'),
            descending=params.get('order', 'desc') != 'asc',
            category_id=params.get('category') or None,
            search=params.get('search') or None,
            limit=int(limit) if limit and limit.isdigit() else None,
        )

        if _wants_csv(request):
            return _csv_response(
                f'menu-performance-{meta["from"]}-to-{meta["to"]}.csv',
                ['Item', 'Category', 'Units Sold', 'Revenue', 'Orders', 'Availability'],
                [
                    [
                        row['name'], row['categoryName'], row['unitsSold'], row['revenue'],
                        row['orders'], 'Available' if row['available'] else 'Unavailable',
                    ]
                    for row in rows
                ],
            )

        return Response({'meta': meta, 'items': rows})


class CategoryAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        rows = analytics.category_performance(start, end)

        if _wants_csv(request):
            return _csv_response(
                f'category-performance-{meta["from"]}-to-{meta["to"]}.csv',
                ['Category', 'Units Sold', 'Orders', 'Revenue', '% of Revenue'],
                [
                    [row['category'], row['unitsSold'], row['orders'], row['revenue'], row['pctOfRevenue']]
                    for row in rows
                ],
            )

        return Response({'meta': meta, 'categories': rows})


class TimePatternsAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        return Response({'meta': meta, **analytics.time_patterns(start, end)})


class TableAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        rows = analytics.table_performance(start, end)

        if _wants_csv(request):
            return _csv_response(
                f'table-performance-{meta["from"]}-to-{meta["to"]}.csv',
                ['Table', 'Orders', 'Revenue', 'Average Order Value'],
                [
                    [row['tableNumber'], row['orders'], row['revenue'], row['averageOrderValue']]
                    for row in rows
                ],
            )

        return Response({'meta': meta, 'tables': rows})


class FeedbackAnalyticsView(AnalyticsView):
    def build(self, request, start, end, meta):
        return Response({'meta': meta, **analytics.feedback_summary(start, end)})
