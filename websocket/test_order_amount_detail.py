"""自动发货详情金额回归：只解析合成响应并写内存 SQLite，不执行发货。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.xianyu.auto_delivery_handler import AutoDeliveryHandler
from common.models.xy_order import XYOrder


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()


class DeliveryOrderAmountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        XYOrder.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.order = XYOrder(id=1, owner_id=1, order_no="SYNTH-DELIVERY-PRICE", status="pending_ship",
                             account_id="synthetic-account", amount=Decimal("888.00"), quantity=1)
        self.session.add(self.order)
        self.session.commit()
        # 不初始化发货依赖，只使用实际类的解析及持久化方法。
        self.handler = object.__new__(AutoDeliveryHandler)
        self.handler.parent = SimpleNamespace(cookie_id="synthetic-account", cookies_str="synthetic-cookie")
        self.adapter = AsyncSessionAdapter(self.session)

    async def asyncTearDown(self):
        self.session.close()
        self.engine.dispose()

    async def update(self, value="8.88", quantity="1"):
        data = {"itemInfo": {"price": "888.00", "buyAmount": quantity}}
        if value is not None:
            data["priceInfo"] = {"amount": {"title": "成交价", "value": value, "expanded": True}}
        parsed = self.handler._parse_order_detail_response(self.order.order_no, {
            "data": {"components": [{"render": "orderInfoVO", "data": data}]},
        })
        await self.handler._update_order_detail_to_db(self.adapter, self.order.order_no, "", "", parsed)
        self.session.refresh(self.order)
        return parsed

    async def test_delivery_uses_transaction_total_instead_of_catalog_price(self):
        parsed = await self.update()
        self.assertEqual(parsed["amount"], "8.88")
        self.assertFalse(parsed["amount_is_estimate"])
        self.assertEqual(self.order.amount, Decimal("8.88"))

    async def test_missing_or_invalid_transaction_total_cannot_revert_reprice(self):
        await self.update()
        for value in (None, "bad", "NaN", "-1"):
            with self.subTest(value=value):
                parsed = await self.update(value)
                self.assertTrue(parsed["amount_is_estimate"])
                self.assertEqual(self.order.amount, Decimal("8.88"))

    async def test_zero_transaction_price_is_preserved_against_catalog_fallback(self):
        await self.update("0.00")
        self.assertEqual(self.order.amount, Decimal("0.00"))
        await self.update(None)
        self.assertEqual(self.order.amount, Decimal("0.00"))

    async def test_multiple_items_do_not_multiply_transaction_total(self):
        await self.update("17.76", quantity="2")
        self.assertEqual(self.order.amount, Decimal("17.76"))
        self.assertEqual(self.order.quantity, 2)

    async def test_catalog_estimate_only_fills_missing_amount(self):
        self.order.amount = None
        self.session.commit()
        parsed = await self.update(None, quantity="2")
        self.assertTrue(parsed["amount_is_estimate"])
        self.assertEqual(self.order.amount, Decimal("1776.00"))


if __name__ == "__main__":
    unittest.main()
