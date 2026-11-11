"""内部备注使用内存 SQLite / 合成数据验证，不访问业务数据库或闲鱼。"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI, HTTPException
from openpyxl import load_workbook
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes import orders
from app.services import ticket_registration_export_service as exports
from common.db.order_remark_schema import ensure_order_remark_schema
from common.models.agent_order import AgentOrder
from common.models.ticket_registration import TicketRegistration
from common.models.user import User, UserRole, UserStatus
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.services.order_service import OrderService


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def stream(self, statement):
        rows = self.session.execute(statement).all()

        class Stream:
            async def partitions(self, size):
                for offset in range(0, len(rows), size):
                    yield rows[offset:offset + size]

            async def close(self):
                pass

        return Stream()


class OrderRemarkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (XYAccount, XYOrder, AgentOrder, TicketRegistration):
            model.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.order_id = 9123456789012345678
        self.account = XYAccount(id=1, owner_id=1, account_id="mine", cookie="synthetic", login_method="cookie")
        self.order = XYOrder(id=self.order_id, owner_id=1, account_id="mine", order_no="SYNTH-ORDER",
                             status="closed", buyer_id="synthetic-buyer", quantity=1, amount="100.00")
        self.session.add_all([
            self.account, self.order,
            XYAccount(id=2, owner_id=2, account_id="other", cookie="synthetic", login_method="cookie"),
            XYOrder(id=2, owner_id=2, account_id="other", order_no="SYNTH-FOREIGN", status="pending_pay", remark="其他用户备注"),
            XYOrder(id=3, owner_id=1, account_id="other", order_no="SYNTH-ACCOUNT-MISMATCH", status="completed"),
        ])
        self.session.commit()
        self.adapter = AsyncSessionAdapter(self.session)
        self.service = OrderService(self.adapter)
        self.service.get_delivery_log_status_map = AsyncMock(return_value={})
        self.service._get_item_titles = AsyncMock(return_value={})
        self.service.get_item_title = AsyncMock(return_value="")
        self.user = User(id=1, role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app = FastAPI()
        self.app.include_router(orders.router, prefix="/api/v1/orders")
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.user
        self.app.dependency_overrides[deps.get_account_service] = lambda: object()
        self.app.dependency_overrides[deps.get_order_service] = lambda: self.service
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        self.session.close()
        self.engine.dispose()

    async def save(self, remark, order_id=None):
        return await self.http.put(f"/api/v1/orders/{self.order_id if order_id is None else order_id}/remark", json={"remark": remark})

    async def test_long_multiline_round_trip_and_string_id_in_list_detail(self):
        remark = " 开头空格\n" + "测试备注😀" * 1998 + "\n末尾 "
        self.assertLessEqual(len(remark), 10000)
        response = await self.save(remark)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], {"order_id": str(self.order_id), "remark": remark})
        self.session.refresh(self.order)
        self.assertEqual(self.order.remark, remark)
        listing = (await self.http.get("/api/v1/orders")).json()
        item = next(item for item in listing["data"] if item["id"] == str(self.order_id))
        self.assertEqual(item["remark"], remark)
        detail = (await self.http.get("/api/v1/orders/SYNTH-ORDER")).json()
        self.assertEqual(detail["data"]["remark"], remark)

    async def test_clear_and_save_for_every_payment_status(self):
        for order_status in ("pending_pay", "pending_ship", "shipped", "completed", "closed", "refunding"):
            self.order.status = order_status
            self.session.commit()
            self.assertTrue((await self.save("内部备注")).json()["success"])
        response = await self.save("")
        self.assertEqual(response.json()["data"]["remark"], "")
        self.session.refresh(self.order)
        self.assertIsNone(self.order.remark)
        detail = (await self.http.get("/api/v1/orders/SYNTH-ORDER")).json()
        self.assertEqual(detail["data"]["remark"], "")

    async def test_foreign_account_mismatch_and_unknown_orders_are_denied(self):
        for identifier in (2, 3, 999):
            self.assertEqual((await self.save("越权写入", identifier)).status_code, 404)
        self.session.expire_all()
        self.assertEqual(self.session.get(XYOrder, 2).remark, "其他用户备注")
        self.assertIsNone(self.session.get(XYOrder, 3).remark)

    async def test_admin_can_save_other_owners_order(self):
        self.user.role = UserRole.ADMIN
        self.assertTrue((await self.save("管理员备注", 2)).json()["success"])
        self.session.expire_all()
        self.assertEqual(self.session.get(XYOrder, 2).remark, "管理员备注")

    async def test_anonymous_request_cannot_save(self):
        def anonymous():
            raise HTTPException(401, "请先登录")
        self.app.dependency_overrides[deps.get_current_active_user] = anonymous
        self.assertEqual((await self.save("不应写入")).status_code, 401)
        self.session.refresh(self.order)
        self.assertIsNone(self.order.remark)

    async def test_request_is_strict_bounded_and_has_no_other_mutations(self):
        invalid = ({}, {"remark": None}, {"remark": 1}, {"remark": False},
                   {"remark": []}, {"remark": "x" * 10001}, {"remark": "x", "status": "completed"})
        for body in invalid:
            response = await self.http.put(f"/api/v1/orders/{self.order_id}/remark", json=body)
            self.assertEqual(response.status_code, 422, str(body)[:100])
        for identifier in ("0", "01", "-1", "1.5", "9223372036854775808", "9" * 100, "abc"):
            self.assertEqual((await self.save("x", identifier)).status_code, 422)
        self.assertTrue((await self.save("x" * 10000)).json()["success"])
        self.session.refresh(self.order)
        self.assertEqual(self.order.status, "closed")

    async def test_platform_sync_preserves_remark_even_if_payload_contains_it(self):
        await self.save("人工内部备注\n不能覆盖")
        result = await self.service._upsert_order({"order_no": self.order.order_no, "status": "pending_ship",
            "amount": "8.88", "quantity": 2, "remark": "平台不可信内容"}, self.account)
        self.assertEqual(result, "updated")
        self.session.refresh(self.order)
        self.assertEqual(self.order.remark, "人工内部备注\n不能覆盖")
        self.assertEqual(self.order.quantity, 2)

    def registration(self, order_id=None, **changes):
        values = dict(id=1, order_id=order_id or self.order_id, token_hash="synthetic-hash", token_ciphertext="synthetic-token",
            version=7, visit_date=date(2026, 11, 10), visitors=[{"id": "a", "ticket_type_name": "1日成人票"}],
            submitted_at=datetime(2026, 11, 7), identity_ciphertext="synthetic-identity", phone_ciphertext="synthetic-phone",
            exported_at=datetime(2026, 11, 7, 12))
        values.update(changes)
        registration = TicketRegistration(**values)
        self.session.add(registration)
        self.session.commit()
        return registration

    async def test_changed_remark_resets_export_only_and_unchanged_save_is_idempotent(self):
        await self.save("原备注")
        registration = self.registration()
        exported_at = registration.exported_at
        original = (registration.version, registration.submitted_at, registration.identity_ciphertext,
                    registration.phone_ciphertext, registration.token_hash, registration.visitors)
        await self.save("原备注")
        self.session.refresh(registration)
        self.assertEqual(registration.exported_at, exported_at)
        await self.save("新备注\n内部修改")
        self.session.refresh(registration)
        self.assertIsNone(registration.exported_at)
        state = (await self.service.get_ticket_status_map([self.order_id]))[self.order_id]
        self.assertEqual(state["ticket_collection_status"], "collected")
        self.assertEqual(state["ticket_export_status"], "unexported")
        self.assertEqual((registration.version, registration.submitted_at, registration.identity_ciphertext,
                         registration.phone_ciphertext, registration.token_hash, registration.visitors), original)
        registration.exported_at = exported_at
        self.session.commit()
        await self.save("")
        self.session.refresh(registration)
        self.assertIsNone(registration.exported_at)

    async def test_empty_unchanged_and_pending_remark_save_do_not_change_registration(self):
        registration = self.registration(submitted_at=None)
        exported_at = registration.exported_at
        await self.save("")
        await self.save("待收集备注")
        self.session.refresh(registration)
        self.assertEqual(registration.exported_at, exported_at)
        self.assertEqual(registration.version, 7)
        self.assertIsNone(registration.submitted_at)

    async def test_denied_remark_edit_does_not_reset_other_orders_export(self):
        registration = self.registration(order_id=2)
        exported_at = registration.exported_at
        self.assertEqual((await self.save("越权备注", 2)).status_code, 404)
        self.session.refresh(registration)
        self.assertEqual(registration.exported_at, exported_at)

    async def test_export_repeats_order_remark_for_each_visitor_as_safe_text(self):
        remark = '=HYPERLINK("https://example.test","备注")\n同行取票 😀'
        await self.save(remark)
        registration = TicketRegistration(id=1, order_id=self.order_id, token_hash="synthetic-hash", token_ciphertext="synthetic-token",
            version=1, visit_date=date(2026, 11, 10), visitors=[
                {"id": "a", "ticket_type_name": "1日成人票"}, {"id": "b", "ticket_type_name": "1日儿童票"}],
            submitted_at=datetime(2026, 11, 7), identity_ciphertext="synthetic-identity", phone_ciphertext="synthetic-phone")
        self.session.add(registration)
        self.session.commit()
        identities = {"a": {"name": "合成成人", "id_number": "synthetic-adult-id"},
                      "b": {"name": "合成儿童", "id_number": "synthetic-child-id"}}
        with patch.object(exports.TicketRegistrationService, "_get_cipher", new=AsyncMock()), \
                patch.object(exports.TicketRegistrationService, "_identities", new=AsyncMock(return_value=identities)), \
                patch.object(exports.TicketRegistrationService, "contact_phone", new=AsyncMock(return_value="13800138000")):
            for owner_id in (None, 1):
                self.adapter.statements.clear()
                content = await exports.export_ticket_registrations(self.adapter, order_ids=[self.order_id], owner_id=owner_id)
                # SQLite 不执行行锁，但此断言确保管理员路径也发出相同的 MySQL FOR UPDATE。
                first = self.adapter.statements[0]
                self.assertIsNotNone(first._for_update_arg)
                self.assertIn("xy_orders.remark", str(first))
                self.assertIn("ORDER BY xy_orders.id", str(first))
        workbook = load_workbook(BytesIO(content), data_only=False)
        try:
            sheet = workbook.active
            self.assertEqual(tuple(cell.value for cell in sheet[1]), ("订单号", "日期", "姓名", "身份证", "票种", "用户名", "手机号", "备注"))
            self.assertEqual(sheet.max_row, 3)
            for row in (2, 3):
                self.assertEqual(sheet.cell(row, 8).value, remark)
                self.assertEqual(sheet.cell(row, 8).data_type, "s")
                self.assertTrue(sheet.cell(row, 8).alignment.wrap_text)
                self.assertEqual(sheet.cell(row, 7).value, "13800138000")
        finally:
            workbook.close()
        self.session.refresh(registration)
        self.assertIsNotNone(registration.exported_at)


class OrderRemarkSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_table_only_adds_missing_column_once(self):
        has_remark = False
        statements = []

        async def execute(statement):
            nonlocal has_remark
            sql = str(statement)
            statements.append(sql)
            if "information_schema.TABLES" in sql:
                return SimpleNamespace(scalar=lambda: 1)
            if "information_schema.COLUMNS" in sql:
                return SimpleNamespace(scalar=lambda: int(has_remark))
            self.assertEqual(sql, "ALTER TABLE xy_orders ADD COLUMN remark TEXT NULL COMMENT '订单内部备注'")
            has_remark = True

        connection = SimpleNamespace(execute=execute)
        await ensure_order_remark_schema(connection)
        await ensure_order_remark_schema(connection)
        self.assertEqual(sum(sql.startswith("ALTER TABLE") for sql in statements), 1)
        self.assertFalse(any(sql.startswith(("DELETE", "UPDATE", "DROP")) for sql in statements))

    async def test_absent_table_or_failed_migration_does_not_silently_start(self):
        absent = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar=lambda: 0)))
        with self.assertRaises(RuntimeError):
            await ensure_order_remark_schema(absent)
        failed = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(scalar=lambda: 1),
            SimpleNamespace(scalar=lambda: 0), RuntimeError("synthetic denied DDL")]))
        with self.assertRaisesRegex(RuntimeError, "synthetic denied DDL"):
            await ensure_order_remark_schema(failed)


@unittest.skipUnless(os.getenv("TICKET_TEST_MYSQL_URL"), "需要显式设置隔离测试用 MySQL URL")
class OrderRemarkMySQLMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_table_migrates_without_losing_rows_or_existing_remarks(self):
        # 忽略 URL 中指定的库名，只创建和清理本测试生成的随机隔离库。
        database = "xianyu_ticket_test_" + uuid.uuid4().hex[:16]
        admin_url = make_url(os.environ["TICKET_TEST_MYSQL_URL"]).set(database=None)
        admin_engine = create_async_engine(admin_url, echo=False)
        engine = None
        created = False
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(
                    f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                ))
                created = True
            engine = create_async_engine(admin_url.set(database=database), echo=False)
            async with engine.begin() as connection:
                await connection.execute(text(
                    "CREATE TABLE xy_orders (id BIGINT PRIMARY KEY, synthetic_legacy VARCHAR(255) NOT NULL) "
                    "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                ))
                legacy = "合成历史订单数据 😀\n不可丢失"
                await connection.execute(text(
                    "INSERT INTO xy_orders (id, synthetic_legacy) VALUES (:id, :legacy)"
                ), {"id": 9123456789012345678, "legacy": legacy})
                await ensure_order_remark_schema(connection)
                column = (await connection.execute(text(
                    "SELECT DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'xy_orders' AND COLUMN_NAME = 'remark'"
                ))).one()
                self.assertEqual(tuple(column), ("text", "YES"))
                row = (await connection.execute(text("SELECT id, synthetic_legacy, remark FROM xy_orders"))).one()
                self.assertEqual(tuple(row), (9123456789012345678, legacy, None))
                remark = "已有人工备注\n二次迁移必须完整保留 😀"
                await connection.execute(text("UPDATE xy_orders SET remark = :remark WHERE id = :id"),
                                         {"remark": remark, "id": 9123456789012345678})
                await ensure_order_remark_schema(connection)
                rows = (await connection.execute(text("SELECT id, synthetic_legacy, remark FROM xy_orders"))).all()
                self.assertEqual([tuple(row) for row in rows], [(9123456789012345678, legacy, remark)])
                columns = (await connection.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'xy_orders' ORDER BY ORDINAL_POSITION"
                ))).scalars().all()
                self.assertEqual(columns, ["id", "synthetic_legacy", "remark"])
        finally:
            try:
                if engine is not None:
                    await engine.dispose()
                if created:
                    if not re.fullmatch(r"xianyu_ticket_test_[a-f0-9]{16}", database):
                        raise AssertionError("Unexpected test database name")
                    async with admin_engine.begin() as connection:
                        await connection.execute(text(f"DROP DATABASE `{database}`"))
            finally:
                await admin_engine.dispose()


if __name__ == "__main__":
    unittest.main()
