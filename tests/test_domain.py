"""Money must be exact, and orders must total correctly."""

from __future__ import annotations

import pytest

from conftest import dollars, make_order
from storefront_agent.domain import (
    Channel,
    Money,
    OrderLine,
    OrderState,
    POLine,
    PurchaseOrder,
    Storefront,
)


class TestMoney:
    def test_from_dollars_rounds_half_up(self):
        assert Money.from_dollars("0.005").cents == 1
        assert Money.from_dollars(19.99).cents == 1999
        assert Money.from_dollars("1234.567").cents == 123457

    def test_float_input_does_not_lose_cents(self):
        # 0.1 + 0.2 in binary float is 0.30000000000000004.
        assert (Money.from_dollars(0.1) + Money.from_dollars(0.2)) == Money.from_dollars(0.3)

    def test_hundred_additions_stay_exact(self):
        total = Money.zero()
        for _ in range(100):
            total = total + Money.from_dollars("0.07")
        assert total.cents == 700

    def test_rejects_non_integer_cents(self):
        with pytest.raises(TypeError, match="minor units"):
            Money(19.99)

    def test_rejects_bad_currency(self):
        with pytest.raises(ValueError, match="3-letter"):
            Money(100, "DOLLARS")

    def test_refuses_to_mix_currencies(self):
        with pytest.raises(ValueError, match="cannot combine"):
            Money(100, "USD") + Money(100, "GBP")

    def test_multiplication_rounds_half_up(self):
        assert (Money(1999) * 0.029).cents == 58

    def test_ratio_to_zero_is_zero_not_a_crash(self):
        assert Money(500).ratio_to(Money(0)) == 0.0

    def test_ordering_and_negation(self):
        assert Money(100) < Money(200)
        assert (-Money(250)).cents == -250

    def test_formats_with_separators_and_symbol(self):
        assert str(Money(123456)) == "$1,234.56"
        assert str(Money(-500)) == "-$5.00"
        assert str(Money(100, "EUR")).startswith("€")


class TestOrder:
    def test_revenue_includes_shipping(self):
        order = make_order(quantity=2, price=10.00)
        order.shipping_paid = dollars(4.95)
        assert order.revenue == dollars(24.95)

    def test_margin_is_revenue_less_cost(self):
        order = make_order(quantity=2, price=10.00, cost=3.00)
        assert order.cost == dollars(6.00)
        assert order.margin == dollars(14.00)

    def test_units_sums_lines(self):
        order = make_order(quantity=3)
        assert order.units == 3

    def test_line_quantity_must_be_positive(self):
        with pytest.raises(ValueError, match="positive"):
            OrderLine("SKU", 0, dollars(1), dollars(1))

    def test_terminal_states(self):
        assert OrderState.DELIVERED.is_terminal
        assert OrderState.CANCELLED.is_terminal
        assert not OrderState.PLACED.is_terminal


class TestPurchaseOrder:
    def test_total_sums_lines(self):
        po = PurchaseOrder(
            "PO-1", "sup-1",
            (POLine("A", 2, dollars(5), "o1"), POLine("B", 3, dollars(4), "o2")),
            created_at=None,
        )
        assert po.total == dollars(22)

    def test_order_ids_deduplicate_preserving_order(self):
        po = PurchaseOrder(
            "PO-1", "sup-1",
            (
                POLine("A", 1, dollars(5), "o2"),
                POLine("B", 1, dollars(5), "o1"),
                POLine("C", 1, dollars(5), "o2"),
            ),
            created_at=None,
        )
        assert po.order_ids == ("o2", "o1")


class TestStorefront:
    def test_fee_is_rate_plus_fixed(self):
        shop = Storefront(Channel.SHOPIFY, "Shop", fee_rate=0.029, fee_fixed=Money(30))
        assert shop.fee_on(dollars(100)) == dollars(3.20)

    def test_channel_labels_are_human(self):
        assert Channel.EBAY.label == "eBay"
        assert Channel.AMAZON.label == "Amazon"

    def test_for_channel_gives_each_marketplace_its_own_fee_structure(self):
        rates = {c: Storefront.for_channel(c).fee_rate for c in Channel}
        assert rates[Channel.AMAZON] > rates[Channel.EBAY] > rates[Channel.SHOPIFY]

    def test_amazon_has_no_fixed_fee_but_shopify_does(self):
        assert Storefront.for_channel(Channel.AMAZON).fee_fixed == Money(0)
        assert Storefront.for_channel(Channel.SHOPIFY).fee_fixed == Money(30)

    def test_the_same_sale_nets_less_on_a_higher_fee_channel(self):
        sale = dollars(50)
        amazon = Storefront.for_channel(Channel.AMAZON).fee_on(sale)
        shopify = Storefront.for_channel(Channel.SHOPIFY).fee_on(sale)
        assert amazon > shopify
