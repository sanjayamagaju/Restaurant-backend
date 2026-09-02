"""Server-side order pricing and customization validation.

The client sends only menu item ids, quantities and selected option ids -
never a price. This module is the single source of truth: it reloads every
menu item and option from the database, validates the selection against
each option group's rules, and computes prices from scratch. Nothing the
client sends about price is ever trusted.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import MenuItem

SPECIAL_INSTRUCTIONS_MAX_LENGTH = 500


def build_order_items(items_input):
    """Validate `items_input` (already schema-validated) and price it.

    Returns (prepared_kwargs_list, total_amount). `prepared_kwargs_list`
    entries are ready to pass straight into `OrderItem(order=order, **kwargs)`.
    Raises `serializers.ValidationError` on the first invalid item - the
    whole order is rejected rather than silently dropping bad lines.
    """
    menu_item_ids = {entry['menuItemId'] for entry in items_input}
    menu_items = MenuItem.objects.filter(id__in=menu_item_ids).prefetch_related('option_groups__options')
    menu_items_by_id = {item.id: item for item in menu_items}

    prepared = []
    total = Decimal('0.00')

    for entry in items_input:
        menu_item = menu_items_by_id.get(entry['menuItemId'])
        if menu_item is None:
            raise serializers.ValidationError(f"Menu item {entry['menuItemId']} does not exist.")
        if not menu_item.available:
            raise serializers.ValidationError(
                f'{menu_item.name} is no longer available. Please update your cart.'
            )

        quantity = entry['quantity']
        selected_option_ids = set(entry.get('selectedOptionIds') or [])

        active_groups = [group for group in menu_item.option_groups.all() if group.active]
        valid_options_by_id = {}
        options_by_group_id = {}
        for group in active_groups:
            active_options = [option for option in group.options.all() if option.active]
            options_by_group_id[group.id] = active_options
            for option in active_options:
                valid_options_by_id[option.id] = (group, option)

        unknown_ids = selected_option_ids - set(valid_options_by_id.keys())
        if unknown_ids:
            raise serializers.ValidationError(
                f'One or more selected options for {menu_item.name} are no longer available.'
            )

        selected_by_group_id = {}
        for option_id in selected_option_ids:
            group, option = valid_options_by_id[option_id]
            selected_by_group_id.setdefault(group.id, []).append(option)

        for group in active_groups:
            count = len(selected_by_group_id.get(group.id, []))
            if group.required and count < 1:
                raise serializers.ValidationError(
                    f'Please choose an option for "{group.name}" on {menu_item.name}.'
                )
            if count < group.min_selection:
                raise serializers.ValidationError(
                    f'Please select at least {group.min_selection} option(s) for '
                    f'"{group.name}" on {menu_item.name}.'
                )
            if count > group.max_selection:
                raise serializers.ValidationError(
                    f'Please select at most {group.max_selection} option(s) for '
                    f'"{group.name}" on {menu_item.name}.'
                )

        ordered_selections = []
        for group in sorted(active_groups, key=lambda g: (g.display_order, g.id)):
            for option in sorted(selected_by_group_id.get(group.id, []), key=lambda o: (o.display_order, o.id)):
                ordered_selections.append((group, option))

        option_total = Decimal('0.00')
        snapshot = []
        for group, option in ordered_selections:
            option_total += option.additional_price
            snapshot.append({
                'groupName': group.name,
                'optionName': option.name,
                'additionalPrice': str(option.additional_price),
            })

        unit_price = menu_item.price + option_total
        total += unit_price * quantity

        special_instructions = (entry.get('specialInstructions') or '')[:SPECIAL_INSTRUCTIONS_MAX_LENGTH]

        prepared.append({
            'menu_item': menu_item,
            'quantity': quantity,
            'special_instructions': special_instructions,
            'item_name_snapshot': menu_item.name,
            'base_price_snapshot': menu_item.price,
            'unit_price_snapshot': unit_price,
            'selected_options_snapshot': snapshot,
        })

    return prepared, total
