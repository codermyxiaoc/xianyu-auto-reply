"""订单身份信息状态/筛选的隔离测试，仅使用内存 SQLite 合成数据。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes.orders import router
from common.models.agent_order import AgentOrder
from common.models.ticket_registration import TicketRegistration
from common.models.user import User, UserRole, UserStatus
from common.models.xy_order import XYOrder
from common.services.order_filters import build_order_filter_conditions
from common.services.order_service import OrderService


class AsyncSessionAdapter:
    """运行真实 SQL 查询，但不引入异步 SQLite 驱动或业务数据库。"""

    def __init__(self, session: Session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)


class TicketOrderStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (XYOrder, TicketRegistration, AgentOrder):
            model.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.exported_at = datetime(2026, 11, 7, 12, 0)
        for identifier in range(1, 6):
            self.session.add(XYOrder(
                id=identifier, owner_id=1 if identifier < 5 else 2,
                account_id="synthetic-account", order_no=f"SYNTH-STATUS-{identifier}",
                status="pending_ship", amount="100.00", quantity=1,
                placed_at=datetime(2026, 11, 1) + timedelta(hours=identifier),
            ))
            if identifier > 1:
                self.session.add(TicketRegistration(
                    id=identifier, order_id=identifier,
                    token_hash=f"synthetic-token-{identifier}", token_ciphertext="synthetic-ciphertext",
                    visit_date=date(2026, 12, 1), visitors=[], version=1,
                    submitted_at=None if identifier == 2 else datetime(2026, 11, 6),
                    exported_at=self.exported_at if identifier in (4, 5) else None,
                ))
        self.session.commit()
        self.adapter = AsyncSessionAdapter(self.session)
        self.service = OrderService(self.adapter)
        self.service.get_delivery_log_status_map = AsyncMock(return_value={})
        self.app = FastAPI()
        self.app.include_router(router, prefix="/orders")
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(
            id=1, username="synthetic-admin", role=UserRole.ADMIN, status=UserStatus.ACTIVE,
        )
        self.app.dependency_overrides[deps.get_account_service] = lambda: object()
        self.app.dependency_overrides[deps.get_order_service] = lambda: self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session.close()
        self.engine.dispose()

    def matching_ids(self, owner_id=None, *, joined=False, **filters):
        query = select(XYOrder.id)
        if joined:
            query = query.join(TicketRegistration, TicketRegistration.order_id == XYOrder.id)
        return set(self.session.execute(query.where(*build_order_filter_conditions(owner_id, **filters))).scalars())

    async def test_collection_export_and_combined_filters(self):
        cases = [
            ({"ticket_collection_status": "uncreated"}, {1}),
            ({"ticket_collection_status": "pending"}, {2}),
            ({"ticket_collection_status": "collected"}, {3, 4, 5}),
            ({"ticket_export_status": "unexported"}, {2, 3}),
            ({"ticket_export_status": "exported"}, {4, 5}),
            ({"ticket_collection_status": "collected", "ticket_export_status": "unexported"}, {3}),
            ({"ticket_collection_status": "uncreated", "ticket_export_status": "unexported"}, set()),
            ({"ticket_collection_status": "pending", "ticket_export_status": "exported"}, set()),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(self.matching_ids(**filters), expected)
                # 导出 JOIN 已存在时相关子查询仍须正确，并自动排除未创建的订单。
                self.assertEqual(self.matching_ids(joined=True, **filters), expected - {1})
        self.assertEqual(self.matching_ids(1, ticket_export_status="exported"), {4})
        self.assertEqual(self.matching_ids(2, ticket_collection_status="collected"), {5})

    async def test_list_count_and_pagination_use_same_filters(self):
        first, total, _ = await self.service.list_orders(1, ticket_collection_status="collected", page_size=1)
        second, second_total, _ = await self.service.list_orders(1, ticket_collection_status="collected", page=2, page_size=1)
        self.assertEqual(total, 2)
        self.assertEqual(second_total, 2)
        self.assertEqual([order.id for order in first], [4])
        self.assertEqual([order.id for order in second], [3])

    async def test_batch_status_query_excludes_identity_and_phone_columns(self):
        states = await self.service.get_ticket_status_map([1, 2, 3, 4])
        self.assertEqual(len(self.adapter.statements), 1)
        query = str(self.adapter.statements[0])
        self.assertNotIn("identity_ciphertext", query)
        self.assertNotIn("phone_ciphertext", query)
        self.assertNotIn("token_ciphertext", query)
        self.assertEqual(states[1], {"ticket_collection_status": "uncreated", "ticket_export_status": None, "ticket_exported_at": None})
        self.assertEqual(states[2]["ticket_collection_status"], "pending")
        self.assertEqual(states[2]["ticket_export_status"], "unexported")
        self.assertEqual(states[3]["ticket_collection_status"], "collected")
        self.assertEqual(states[3]["ticket_export_status"], "unexported")
        self.assertEqual(states[4]["ticket_export_status"], "exported")
        self.assertEqual(states[4]["ticket_exported_at"], self.exported_at)
        self.assertEqual(await self.service.get_ticket_status_map([]), {})
        self.assertEqual(len(self.adapter.statements), 1)

    async def test_http_list_and_detail_include_consistent_statuses(self):
        response = await self.client.get("/orders", params={"ticket_export_status": "unexported"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual({order["id"] for order in data["data"]}, {"2", "3"})
        for row in data["data"]:
            self.assertEqual(row["ticket_export_status"], "unexported")
        plain = (await self.client.get("/orders", params={"ticket_collection_status": "uncreated"})).json()["data"][0]
        self.assertEqual(plain["ticket_collection_status"], "uncreated")
        self.assertIsNone(plain["ticket_export_status"])
        self.assertIsNone(plain["ticket_exported_at"])
        detail = (await self.client.get("/orders/SYNTH-STATUS-4")).json()["data"]
        self.assertEqual(detail["ticket_collection_status"], "collected")
        self.assertEqual(detail["ticket_export_status"], "exported")
        self.assertEqual(detail["ticket_exported_at"], self.exported_at.isoformat())

    async def test_invalid_status_filters_are_rejected(self):
        for key in ("ticket_collection_status", "ticket_export_status"):
            with self.subTest(key=key):
                response = await self.client.get("/orders", params={key: "invalid"})
                self.assertEqual(response.status_code, 422)
                with self.assertRaises(ValueError):
                    build_order_filter_conditions(None, **{key: "invalid"})


if __name__ == "__main__":
    unittest.main()
