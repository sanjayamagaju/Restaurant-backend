import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import (
    MenuCategory, MenuItem, MenuItemOption, MenuItemOptionGroup, Order, OrderItem, Table,
)
from .signals import serialize_order_for_broadcast


class OrderLifecycleTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)
        self.menu_item = MenuItem.objects.create(name='Burger', price='9.99')

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def create_order_payload(self):
        return {
            'tableNumber': '5',
            'totalAmount': '9.99',
            'status': 'pending',  # legacy/forged value must be ignored
            'items': [{'menuItemId': self.menu_item.id, 'quantity': 1}],
        }

    def test_customer_can_create_order_without_auth(self):
        response = self.client.post('/api/orders/', self.create_order_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data['id'])
        self.assertEqual(order.status, Order.STATUS_NEW)
        self.assertEqual(order.items.count(), 1)

    def test_customer_can_retrieve_own_order_without_auth(self):
        order = Order.objects.create(table_number='5', total_amount='9.99')
        response = self.client.get(f'/api/orders/{order.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'NEW')

    def test_anonymous_cannot_list_orders(self):
        response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 401)

    def test_anonymous_cannot_change_status(self):
        order = Order.objects.create(table_number='5', total_amount='9.99')
        response = self.client.patch(f'/api/orders/{order.id}/', {'status': 'ACCEPTED'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_valid_status_transition_sets_timestamp(self):
        self.auth()
        order = Order.objects.create(table_number='5', total_amount='9.99')
        response = self.client.patch(f'/api/orders/{order.id}/', {'status': 'ACCEPTED'}, format='json')
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'ACCEPTED')
        self.assertIsNotNone(order.accepted_at)

    def test_full_lifecycle_transition_chain(self):
        self.auth()
        order = Order.objects.create(table_number='5', total_amount='9.99')
        for status in ['ACCEPTED', 'PREPARING', 'READY', 'SERVED', 'COMPLETED']:
            response = self.client.patch(f'/api/orders/{order.id}/', {'status': status}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            order.refresh_from_db()
            self.assertEqual(order.status, status)

    def test_invalid_status_transition_rejected(self):
        self.auth()
        order = Order.objects.create(table_number='5', total_amount='9.99')
        response = self.client.patch(f'/api/orders/{order.id}/', {'status': 'PREPARING'}, format='json')
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'NEW')

    def test_completed_order_cannot_transition_further(self):
        self.auth()
        order = Order.objects.create(table_number='5', total_amount='9.99', status=Order.STATUS_COMPLETED)
        response = self.client.patch(f'/api/orders/{order.id}/', {'status': 'PREPARING'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_cancel_from_new(self):
        self.auth()
        order = Order.objects.create(table_number='5', total_amount='9.99')
        response = self.client.patch(f'/api/orders/{order.id}/', {'status': 'CANCELLED'}, format='json')
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'CANCELLED')
        self.assertIsNotNone(order.cancelled_at)

    def test_model_transition_helper_rejects_invalid(self):
        order = Order(table_number='5', total_amount='9.99', status=Order.STATUS_READY)
        self.assertFalse(order.can_transition_to(Order.STATUS_NEW))
        with self.assertRaises(Exception):
            order.transition_to(Order.STATUS_NEW)


class TableTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_table_auto_generates_a_unique_code_on_save(self):
        table_a = Table.objects.create(number='5')
        table_b = Table.objects.create(number='6')
        self.assertTrue(table_a.code)
        self.assertNotEqual(table_a.code, table_b.code)

    def test_staff_can_create_a_table(self):
        self.auth()
        response = self.client.post('/api/tables/', {'number': '12'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['number'], '12')
        self.assertTrue(response.data['code'])

    def test_anonymous_cannot_create_or_list_tables(self):
        self.assertEqual(self.client.post('/api/tables/', {'number': '12'}, format='json').status_code, 401)
        self.assertEqual(self.client.get('/api/tables/').status_code, 401)

    def test_duplicate_table_number_rejected(self):
        self.auth()
        Table.objects.create(number='7')
        response = self.client.post('/api/tables/', {'number': '7'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_anonymous_can_resolve_a_valid_table_code(self):
        table = Table.objects.create(number='9')
        response = self.client.get(f'/api/tables/resolve/{table.code}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['number'], '9')

    def test_resolving_an_unknown_code_returns_404(self):
        response = self.client.get('/api/tables/resolve/does-not-exist/')
        self.assertEqual(response.status_code, 404)


class OrderBroadcastTestCase(TestCase):
    def test_broadcast_payload_is_json_safe(self):
        """Regression: OrderSerializer(order).data holds raw Decimal/datetime
        values. Channels' send_json calls the plain json module, which can't
        encode either - previously this crashed the consumer (WS close code
        1011) the instant any order changed, silently dropping every
        real-time update. serialize_order_for_broadcast must hand back
        something plain json.dumps can actually encode.
        """
        order = Order.objects.create(table_number='5', total_amount='12.50')
        order.transition_to(Order.STATUS_ACCEPTED)
        order.save()

        data = serialize_order_for_broadcast(order)

        json.dumps(data)  # must not raise
        self.assertEqual(data['tableNumber'], '5')
        self.assertEqual(data['status'], 'ACCEPTED')
        self.assertIsInstance(data['createdAt'], str)
        self.assertIsInstance(data['acceptedAt'], str)
        self.assertIsInstance(data['totalAmount'], (str, int, float))


class MenuCategoryTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_anyone_can_list_active_categories(self):
        MenuCategory.objects.create(name='Starters', display_order=1)
        MenuCategory.objects.create(name='Hidden', active=False, display_order=2)
        response = self.client.get('/api/menu-categories/')
        self.assertEqual(response.status_code, 200)
        names = [c['name'] for c in response.data]
        self.assertIn('Starters', names)
        self.assertNotIn('Hidden', names)

    def test_staff_sees_inactive_categories_too(self):
        self.auth()
        MenuCategory.objects.create(name='Starters', display_order=1)
        MenuCategory.objects.create(name='Hidden', active=False, display_order=2)
        response = self.client.get('/api/menu-categories/')
        self.assertEqual(response.status_code, 200)
        names = [c['name'] for c in response.data]
        self.assertIn('Hidden', names)

    def test_anonymous_cannot_create_category(self):
        response = self.client.post('/api/menu-categories/', {'name': 'Drinks'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_staff_can_create_and_update_category(self):
        self.auth()
        create = self.client.post('/api/menu-categories/', {'name': 'Drinks', 'displayOrder': 3}, format='json')
        self.assertEqual(create.status_code, 201, create.data)
        category_id = create.data['id']

        update = self.client.patch(
            f'/api/menu-categories/{category_id}/', {'active': False}, format='json'
        )
        self.assertEqual(update.status_code, 200)
        self.assertFalse(update.data['active'])

    def test_cannot_delete_category_with_menu_items(self):
        self.auth()
        category = MenuCategory.objects.create(name='Burgers')
        MenuItem.objects.create(name='Cheeseburger', price='5.00', category=category)
        response = self.client.delete(f'/api/menu-categories/{category.id}/')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(MenuCategory.objects.filter(id=category.id).exists())

    def test_can_delete_empty_category(self):
        self.auth()
        category = MenuCategory.objects.create(name='Empty')
        response = self.client.delete(f'/api/menu-categories/{category.id}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(MenuCategory.objects.filter(id=category.id).exists())


class MenuItemAvailabilityTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_menu_item_includes_category_and_availability_fields(self):
        category = MenuCategory.objects.create(name='Burgers')
        item = MenuItem.objects.create(
            name='Cheeseburger', price='5.00', category=category, preparation_time=10,
        )
        response = self.client.get(f'/api/menu-items/{item.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['category'], category.id)
        self.assertEqual(response.data['categoryName'], 'Burgers')
        self.assertTrue(response.data['available'])
        self.assertEqual(response.data['preparationTime'], 10)

    def test_staff_can_toggle_availability(self):
        self.auth()
        item = MenuItem.objects.create(name='Cheeseburger', price='5.00')
        response = self.client.post(f'/api/menu-items/{item.id}/toggle-availability/')
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.available)

    def test_anonymous_cannot_toggle_availability(self):
        item = MenuItem.objects.create(name='Cheeseburger', price='5.00')
        response = self.client.post(f'/api/menu-items/{item.id}/toggle-availability/')
        self.assertEqual(response.status_code, 401)

    def test_ordering_sold_out_item_is_rejected(self):
        item = MenuItem.objects.create(name='Cheeseburger', price='5.00', available=False)
        payload = {
            'tableNumber': '5',
            'items': [{'menuItemId': item.id, 'quantity': 1}],
        }
        response = self.client.post('/api/orders/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_negative_price_rejected(self):
        self.auth()
        response = self.client.post(
            '/api/menu-items/', {'name': 'Bad Item', 'price': '-5.00', 'description': ''}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_negative_preparation_time_rejected(self):
        self.auth()
        response = self.client.post(
            '/api/menu-items/',
            {'name': 'Item', 'price': '5.00', 'description': '', 'preparationTime': -5},
            format='json',
        )
        self.assertEqual(response.status_code, 400)


class MenuItemOptionOrderingTestCase(TestCase):
    """Covers option-group selection rules and server-side pricing (section 20/38)."""

    def setUp(self):
        self.client = APIClient()
        self.menu_item = MenuItem.objects.create(name='Chicken Burger', price=Decimal('650.00'))

        self.size_group = MenuItemOptionGroup.objects.create(
            menu_item=self.menu_item, name='Choose Size', required=True,
            min_selection=1, max_selection=1, display_order=1,
        )
        self.regular = MenuItemOption.objects.create(
            option_group=self.size_group, name='Regular', additional_price=Decimal('0.00'), display_order=1,
        )
        self.large = MenuItemOption.objects.create(
            option_group=self.size_group, name='Large', additional_price=Decimal('150.00'), display_order=2,
        )

        self.extras_group = MenuItemOptionGroup.objects.create(
            menu_item=self.menu_item, name='Extras', required=False,
            min_selection=0, max_selection=4, display_order=2,
        )
        self.cheese = MenuItemOption.objects.create(
            option_group=self.extras_group, name='Extra Cheese', additional_price=Decimal('80.00'), display_order=1,
        )
        self.patty = MenuItemOption.objects.create(
            option_group=self.extras_group, name='Extra Patty', additional_price=Decimal('250.00'), display_order=2,
        )

    def place_order(self, selected_option_ids, quantity=1, special_instructions=''):
        payload = {
            'tableNumber': 'A8',
            'items': [{
                'menuItemId': self.menu_item.id,
                'quantity': quantity,
                'selectedOptionIds': selected_option_ids,
                'specialInstructions': special_instructions,
            }],
        }
        return self.client.post('/api/orders/', payload, format='json')

    def test_required_group_missing_selection_is_rejected(self):
        response = self.place_order([])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_single_select_group_over_max_is_rejected(self):
        response = self.place_order([self.regular.id, self.large.id])
        self.assertEqual(response.status_code, 400)

    def test_multi_select_extras_within_max_is_accepted(self):
        response = self.place_order([self.regular.id, self.cheese.id, self.patty.id])
        self.assertEqual(response.status_code, 201, response.data)

    def test_invalid_option_id_rejected(self):
        response = self.place_order([self.regular.id, 999999])
        self.assertEqual(response.status_code, 400)

    def test_inactive_option_rejected(self):
        self.large.active = False
        self.large.save(update_fields=['active'])
        response = self.place_order([self.large.id])
        self.assertEqual(response.status_code, 400)

    def test_option_from_inactive_group_rejected(self):
        self.extras_group.active = False
        self.extras_group.save(update_fields=['active'])
        response = self.place_order([self.regular.id, self.cheese.id])
        self.assertEqual(response.status_code, 400)

    def test_pricing_is_base_plus_options_times_quantity(self):
        response = self.place_order([self.large.id, self.cheese.id, self.patty.id], quantity=2)
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(pk=response.data['id'])
        # unit price = 650 + 150 + 80 + 250 = 1130; total = 1130 * 2 = 2260
        self.assertEqual(order.total_amount, Decimal('2260.00'))
        item = order.items.first()
        self.assertEqual(item.unit_price_snapshot, Decimal('1130.00'))

    def test_client_supplied_total_is_ignored(self):
        payload = {
            'tableNumber': 'A8',
            'totalAmount': '1.00',
            'items': [{
                'menuItemId': self.menu_item.id,
                'quantity': 1,
                'selectedOptionIds': [self.regular.id],
            }],
        }
        response = self.client.post('/api/orders/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(pk=response.data['id'])
        self.assertEqual(order.total_amount, Decimal('650.00'))

    def test_special_instructions_persisted_per_item(self):
        response = self.place_order([self.regular.id], special_instructions='Less spicy please')
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(pk=response.data['id'])
        self.assertEqual(order.items.first().special_instructions, 'Less spicy please')

    def test_special_instructions_truncated_to_max_length(self):
        response = self.place_order([self.regular.id], special_instructions='x' * 600)
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(pk=response.data['id'])
        self.assertEqual(len(order.items.first().special_instructions), 500)

    def test_whole_order_rejected_if_any_item_invalid(self):
        other_item = MenuItem.objects.create(name='Fries', price=Decimal('100.00'))
        payload = {
            'tableNumber': 'A8',
            'items': [
                {'menuItemId': other_item.id, 'quantity': 1},
                {'menuItemId': self.menu_item.id, 'quantity': 1, 'selectedOptionIds': []},  # missing required size
            ],
        }
        response = self.client.post('/api/orders/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_option_selected_from_wrong_menu_item_rejected(self):
        other_item = MenuItem.objects.create(name='Pizza', price=Decimal('300.00'))
        payload = {
            'tableNumber': 'A8',
            'items': [{
                'menuItemId': other_item.id,
                'quantity': 1,
                'selectedOptionIds': [self.regular.id],
            }],
        }
        response = self.client.post('/api/orders/', payload, format='json')
        self.assertEqual(response.status_code, 400)


class OrderSnapshotIntegrityTestCase(TestCase):
    """Historical orders must not change when the menu is edited later (section 21/38)."""

    def setUp(self):
        self.client = APIClient()
        self.menu_item = MenuItem.objects.create(name='Chicken Burger', price=Decimal('650.00'))
        self.group = MenuItemOptionGroup.objects.create(
            menu_item=self.menu_item, name='Choose Size', required=True, min_selection=1, max_selection=1,
        )
        self.large = MenuItemOption.objects.create(
            option_group=self.group, name='Large', additional_price=Decimal('150.00'),
        )

        payload = {
            'tableNumber': 'A8',
            'items': [{
                'menuItemId': self.menu_item.id,
                'quantity': 1,
                'selectedOptionIds': [self.large.id],
            }],
        }
        response = self.client.post('/api/orders/', payload, format='json')
        self.order_id = response.data['id']

    def test_snapshot_survives_menu_item_rename_and_repricing(self):
        self.menu_item.name = 'Renamed Burger'
        self.menu_item.price = Decimal('999.00')
        self.menu_item.save()

        response = self.client.get(f'/api/orders/{self.order_id}/')
        item = response.data['items'][0]
        self.assertEqual(item['unitPrice'], Decimal('800.00'))
        self.assertEqual(item['itemTotal'], Decimal('800.00'))
        self.assertEqual(item['selectedOptions'][0]['optionName'], 'Large')
        self.assertEqual(item['selectedOptions'][0]['additionalPrice'], '150.00')

    def test_snapshot_survives_option_rename_and_deletion(self):
        self.large.name = 'Renamed Option'
        self.large.additional_price = Decimal('999.00')
        self.large.save()
        self.large.delete()

        response = self.client.get(f'/api/orders/{self.order_id}/')
        item = response.data['items'][0]
        # Snapshot keeps the name/price as they were at order time, unaffected
        # by the later rename/deletion of the live MenuItemOption.
        self.assertEqual(item['selectedOptions'][0]['optionName'], 'Large')
        self.assertEqual(item['selectedOptions'][0]['additionalPrice'], '150.00')
        self.assertEqual(item['unitPrice'], Decimal('800.00'))


class MenuItemOptionGroupAdminApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='staff', password='pass1234')
        self.token = Token.objects.create(user=self.user)
        self.menu_item = MenuItem.objects.create(name='Chicken Burger', price=Decimal('650.00'))

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_anonymous_cannot_manage_option_groups(self):
        response = self.client.post(
            '/api/menu-option-groups/',
            {'menuItemId': self.menu_item.id, 'name': 'Choose Size', 'minSelection': 1, 'maxSelection': 1},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    def test_staff_can_create_option_group_and_option(self):
        self.auth()
        group_response = self.client.post(
            '/api/menu-option-groups/',
            {
                'menuItemId': self.menu_item.id, 'name': 'Choose Size', 'required': True,
                'minSelection': 1, 'maxSelection': 1,
            },
            format='json',
        )
        self.assertEqual(group_response.status_code, 201, group_response.data)
        group_id = group_response.data['id']

        option_response = self.client.post(
            '/api/menu-item-options/',
            {'optionGroupId': group_id, 'name': 'Large', 'additionalPrice': '150.00'},
            format='json',
        )
        self.assertEqual(option_response.status_code, 201, option_response.data)

        menu_item_response = self.client.get(f'/api/menu-items/{self.menu_item.id}/')
        groups = menu_item_response.data['optionGroups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['options'][0]['name'], 'Large')

    def test_required_group_with_zero_min_selection_rejected(self):
        self.auth()
        response = self.client.post(
            '/api/menu-option-groups/',
            {
                'menuItemId': self.menu_item.id, 'name': 'Choose Size', 'required': True,
                'minSelection': 0, 'maxSelection': 1,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_max_below_min_rejected(self):
        self.auth()
        response = self.client.post(
            '/api/menu-option-groups/',
            {'menuItemId': self.menu_item.id, 'name': 'Extras', 'minSelection': 3, 'maxSelection': 1},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
