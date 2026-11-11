"""用内存 SQLite 和合成接口响应验证闲鱼改价同步，不访问业务数据库或闲鱼。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes.chat_customer_order import router as customer_router
from app.api.routes.orders import router as order_router
from common.models.agent_order import AgentOrder
from common.models.ticket_registration import TicketRegistration
from common.models.user import User, UserRole, UserStatus
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.order_service import OrderDetailService, OrderService
from common.utils.order_amount import extract_order_detail_amount, parse_order_amount


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def sold_item(value="8.88"):
    price = {"buyNum": "1"}
    if value is not None:
        price["totalPrice"] = value
    return {"commonData": {"orderId": "SYNTH-REPRICE-1", "orderStatus": "待发货"}, "priceVO": price}


def detail_response(value="8.88", quantity="1"):
    data = {"itemInfo": {"price": "888.00", "buyAmount": quantity}}
    if value is not None:
        # 来自脱敏的真实接口结构；成交价与商品标价不同，billList 不作为原价来源。
        data["priceInfo"] = {
            "amount": {"title": "成交价", "value": value, "expanded": True},
            "billList": [{"code": "ITEM_TOTAL_FEE", "title": "商品总价", "value": value},
                         {"code": "POST_FEE", "title": "运费", "value": "0.00"}],
        }
    return {"data": {"components": [{"render": "orderInfoVO", "data": data}]}}


class OrderAmountSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (XYAccount, XYOrder, AgentOrder, TicketRegistration):
            model.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.account = XYAccount(id=1, owner_id=1, account_id="synthetic-account",
                                 cookie="synthetic-cookie", login_method="cookie")
        self.order = XYOrder(id=1, owner_id=1, account_id="synthetic-account", order_no="SYNTH-REPRICE-1",
                             status="pending_ship", buyer_id="synthetic-buyer", quantity=1, amount=Decimal("888.00"))
        self.session.add_all([self.account, self.order])
        self.session.commit()
        self.adapter = AsyncSessionAdapter(self.session)
        self.service = OrderService(self.adapter)
        self.service.get_delivery_log_status_map = AsyncMock(return_value={})
        self.app = FastAPI()
        self.app.include_router(order_router, prefix="/orders")
        self.app.include_router(customer_router)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(
            id=1, username="synthetic-user", role=UserRole.MEMBER, status=UserStatus.ACTIVE,
        )
        self.app.dependency_overrides[deps.get_account_service] = lambda: object()
        self.app.dependency_overrides[deps.get_order_service] = lambda: self.service
        self.app.dependency_overrides[deps.get_db_session] = lambda: self.adapter
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session.close()
        self.engine.dispose()

    async def sync(self, value):
        parsed = self.service._parse_sold_order_item(sold_item(value))
        return await self.service._upsert_order(parsed, self.account)

    async def test_reprice_updates_existing_amount_and_both_read_endpoints(self):
        self.assertEqual(await self.sync("8.88"), "updated")
        self.session.refresh(self.order)
        self.assertEqual(self.order.amount, Decimal("8.88"))
        orders = (await self.client.get("/orders")).json()
        customer_orders = (await self.client.get("/chat-new/customer-orders/synthetic-account/synthetic-buyer")).json()
        self.assertEqual(orders["data"][0]["amount"], "8.88")
        self.assertEqual(customer_orders["data"][0]["amount"], "8.88")

    async def test_unchanged_price_is_idempotent_and_integer_yuan_is_not_divided(self):
        self.assertEqual(await self.sync("888.00"), "skipped")
        self.assertEqual(await self.sync("888"), "skipped")
        self.assertEqual(await self.sync("8.88"), "updated")
        self.assertEqual(await self.sync("8.88"), "skipped")
        self.assertEqual(await self.sync("1000"), "updated")
        self.session.refresh(self.order)
        self.assertEqual(self.order.amount, Decimal("1000.00"))

    async def test_missing_or_invalid_prices_preserve_stored_amount(self):
        for value in (None, "", "not-money", "NaN", "Infinity", "-1", True, "0.001", "10000000000"):
            with self.subTest(value=value):
                self.assertEqual(await self.sync(value), "skipped")
                self.session.refresh(self.order)
                self.assertEqual(self.order.amount, Decimal("888.00"))

    async def test_explicit_zero_is_distinct_from_missing(self):
        self.assertEqual(await self.sync("0.00"), "updated")
        self.session.refresh(self.order)
        self.assertEqual(self.order.amount, Decimal("0.00"))
        self.assertEqual(await self.sync(None), "skipped")

    async def test_refund_catalog_price_does_not_overwrite_repriced_total(self):
        await self.sync("8.88")
        parsed = self.service._parse_refund_item({
            "commonData": {"orderId": self.order.order_no},
            "priceVO": {"auctionPrice": "888", "buyNum": "2"},
        }, "refunding")
        self.assertEqual(parsed["amount"], Decimal("1776.00"))
        await self.service._upsert_order(parsed, self.account)
        self.session.refresh(self.order)
        self.assertEqual(self.order.status, "refunding")
        self.assertEqual(self.order.amount, Decimal("8.88"))

    async def test_refund_estimate_can_fill_missing_amount(self):
        self.order.amount = None
        self.session.commit()
        parsed = self.service._parse_refund_item({
            "commonData": {"orderId": self.order.order_no},
            "priceVO": {"auctionPrice": "8.88", "buyNum": "2"},
        }, "refunding")
        await self.service._upsert_order(parsed, self.account)
        self.session.refresh(self.order)
        self.assertEqual(self.order.amount, Decimal("17.76"))

    async def update_detail(self, response):
        detail = OrderDetailService("synthetic-account", "synthetic-cookie")
        parsed = detail._parse_order_detail_response(self.order.order_no, response)
        with patch.object(detail, "_fetch_order_detail", AsyncMock(return_value=parsed)), \
             patch("common.db.session.async_session_maker", return_value=self.adapter):
            self.assertTrue(await detail.fetch_and_update_order_detail(self.order.order_no))
        self.session.refresh(self.order)
        return parsed

    async def test_detail_refresh_uses_real_transaction_price_then_list_remains_consistent(self):
        parsed = await self.update_detail(detail_response())
        self.assertEqual(parsed["amount"], "8.88")
        self.assertFalse(parsed["amount_is_estimate"])
        self.assertEqual(self.order.amount, Decimal("8.88"))
        self.assertEqual(await self.sync("8.88"), "skipped")
        self.assertEqual(self.order.amount, Decimal("8.88"))

    async def test_detail_without_valid_transaction_price_cannot_restore_catalog_price(self):
        await self.sync("8.88")
        for value in (None, "bad", "NaN", "-3", "8.888"):
            with self.subTest(value=value):
                parsed = await self.update_detail(detail_response(value))
                self.assertTrue(parsed["amount_is_estimate"])
                self.assertEqual(self.order.amount, Decimal("8.88"))

    async def test_transaction_total_is_not_multiplied_by_quantity(self):
        await self.update_detail(detail_response("17.76", quantity="2"))
        self.assertEqual(self.order.amount, Decimal("17.76"))
        self.assertEqual(self.order.quantity, 2)

    async def test_detail_explicit_zero_can_replace_previous_amount(self):
        await self.update_detail(detail_response("0.00"))
        self.assertEqual(self.order.amount, Decimal("0.00"))
        await self.update_detail(detail_response(None))
        self.assertEqual(self.order.amount, Decimal("0.00"))


class MoneyParserTests(unittest.TestCase):
    def test_decimal_conversion_preserves_yuan_and_precision(self):
        for value in ("8.88", 8.88, Decimal("8.88")):
            self.assertEqual(parse_order_amount(value), Decimal("8.88"))
        self.assertEqual(parse_order_amount("888"), Decimal("888.00"))
        self.assertEqual(parse_order_amount("0.00"), Decimal("0.00"))
        self.assertIsNone(parse_order_amount("8.888"))

    def test_detail_catalog_fallback_is_explicit_estimate(self):
        amount, estimate = extract_order_detail_amount(detail_response(None, quantity="2")["data"]["components"])
        self.assertEqual(amount, Decimal("1776.00"))
        self.assertTrue(estimate)
        self.assertEqual(extract_order_detail_amount([None, {}, {"render": "other", "data": {"price": "9"}}]), (None, False))


if __name__ == "__main__":
    unittest.main()
