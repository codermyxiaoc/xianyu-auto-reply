"""门票Excel导出的隔离MySQL集成测试；样本全部为合成数据。

设置TICKET_TEST_MYSQL_URL后在backend-web执行：
    python -m unittest test_ticket_registration_export -v
仅创建/删除fixture生成的随机xianyu_ticket_test_*数据库。
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select, update

import test_ticket_registration_integration as fixtures
from app.api import deps
from app.api.routes import ticket_registration_export as export_routes
from app.services.ticket_registration_service import TicketRegistrationService
from app.services.ticket_registration_export_service import build_ticket_workbook, export_ticket_registrations
from common.models.ticket_registration import TicketRegistration
from common.models.user import User, UserRole, UserStatus
from common.models.xy_order import XYOrder
from common.schemas.ticket_registration import TicketExportRequest
from common.services.order_service import OrderService


HEADERS = ("订单号", "日期", "姓名", "身份证", "票种", "用户名", "手机号", "备注")
PHONE = "13800138000"


class TicketExportRequestTests(unittest.TestCase):
    def test_selection_is_required_strict_and_bounded(self):
        invalid = [{}, {"order_ids": []}, {"order_ids": None}, {"order_ids": "1"},
                   {"order_ids": ["1"], "search": "all"}, {"order_ids": ["1"] * 1001}]
        invalid.extend({"order_ids": [value]} for value in (1, True, None, "", "0", "01", "-1", "+1", "1.0", " 1", "1 ", "9223372036854775808", "18446744073709551615"))
        for payload in invalid:
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(ValidationError):
                TicketExportRequest.model_validate(payload)

    def test_valid_ids_are_deduplicated_without_losing_precision(self):
        payload = TicketExportRequest.model_validate({"order_ids": ["9223372036854775807", "1", "1"]})
        self.assertEqual(payload.order_ids, ["9223372036854775807", "1"])
        self.assertEqual(len(TicketExportRequest(order_ids=[str(value) for value in range(1, 1001)]).order_ids), 1000)


@unittest.skipUnless(os.getenv("TICKET_TEST_MYSQL_URL"), "需要显式设置隔离测试用MySQL URL")
class TicketRegistrationExportTests(unittest.IsolatedAsyncioTestCase):
    # 仅复用生命周期，不继承原有测试方法，避免重复执行整套登记测试。
    asyncTearDown = fixtures.TicketRegistrationIntegrationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.TicketRegistrationIntegrationTests.asyncSetUp(self)
        self.app.include_router(export_routes.router, prefix="/api/v1")
        self.url = self.prefix + "/export.xlsx"

    async def seed(self, records: list[dict]) -> dict[str, int]:
        """直接构造合成业务记录，避免每条测试订单都走前端配置请求。"""
        identifiers = {}
        async with self.sessions() as session:
            cipher = await TicketRegistrationService(session)._get_cipher()
            for spec in records:
                names = spec.get("names", ["合成游客"])
                values = dict(owner_id=1, account_id="synthetic-account", order_no="SYNTH-" + uuid.uuid4().hex,
                    status="pending_ship", buyer_id="synthetic-buyer", buyer_fish_nick="合成买家", buyer_nick="绝不能使用的历史昵称",
                    item_id="synthetic-item", quantity=len(names), amount="100.00", currency="CNY", is_bargain=False,
                    is_rated=False, remark=None, placed_at=datetime(2026, 10, 15, 12, 0, 0))
                values.update({key: value for key, value in spec.items() if key in values or key == "delivery_method"})
                order = XYOrder(**values)
                session.add(order)
                await session.flush()
                identifiers[order.order_no] = order.id
                if not spec.get("configured", True):
                    continue
                visitors, identities = [], {}
                for index, name in enumerate(names):
                    identifier = uuid.uuid4().hex
                    identity_number = fixtures.fictional_identity("19900101" if index == 0 else "20160101", f"{index + 1:03d}")
                    visitors.append({"id": identifier, "category": "adult" if index == 0 else "child", "amount": "100.00",
                        "ticket_type_id": "4" if index == 0 else "5", "ticket_type_name": spec.get("ticket_type_name", "1日成人票" if index == 0 else "1日儿童票")})
                    identities[identifier] = {"name": name, "id_number": identity_number}
                token = secrets.token_urlsafe(32)
                submitted = spec.get("submitted", True)
                phone = spec.get("phone", PHONE) if submitted else None
                session.add(TicketRegistration(order_id=order.id,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(), token_ciphertext=cipher.encrypt(token.encode()).decode(),
                    visit_date=date.fromisoformat(self.visit_date), visitors=visitors, version=2 if submitted else 1,
                    identity_ciphertext=cipher.encrypt(json.dumps(identities, ensure_ascii=False).encode()).decode() if submitted else None,
                    phone_ciphertext=cipher.encrypt(phone.encode()).decode() if phone else None,
                    submitted_at=datetime(2026, 10, 15, 13, 0, 0) if submitted else None))
            await session.commit()
        return identifiers

    async def request_export(self, order_ids):
        return await self.client.post(self.url, json={"order_ids": [str(value) for value in order_ids]}, headers=self.headers)

    async def workbook(self, order_ids):
        response = await self.request_export(order_ids)
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response.headers.get("content-type", ""), response.text[:250] if not response.content.startswith(b"PK") else "unexpected MIME")
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        return response, load_workbook(BytesIO(response.content), data_only=False)

    async def test_permissions_and_empty_export_do_not_leak_workbooks(self):
        empty = await self.request_export([])
        self.assertEqual(empty.headers.get("content-type"), "application/json")
        self.assertFalse(empty.json()["success"])
        self.assertIn("no-store", empty.headers.get("cache-control", ""))
        identifiers = await self.seed([{"order_no": "SYNTH-PROTECTED"}])
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(id=2, username="synthetic-member", role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        member = await self.request_export(identifiers.values())
        self.assertEqual(member.json()["code"], 403)
        self.assertNotIn("SYNTH-PROTECTED", member.text)
        del self.app.dependency_overrides[deps.get_current_active_user]
        anonymous = await self.request_export(identifiers.values())
        self.assertEqual(anonymous.json()["code"], 401)
        self.assertNotIn("SYNTH-PROTECTED", anonymous.text)

    async def test_multiple_visitors_phone_blanks_native_dates_and_text_safety(self):
        long_order = "5127851592171166512"
        identifiers = await self.seed([
            {"order_no": long_order, "names": ["合成成人", "合成儿童"], "buyer_fish_nick": "  合成昵称  ", "remark": "内部备注\n两位游客一起取票"},
            {"order_no": "SYNTH-DRAFT", "submitted": False, "buyer_fish_nick": "  ", "buyer_id": "0000123456789012345"},
            {"order_no": "SYNTH-LEGACY", "phone": None, "buyer_fish_nick": None, "buyer_id": "legacy-buyer"},
            {"order_no": "SYNTH-FORMULA", "buyer_fish_nick": "=1+1", "ticket_type_name": "=HYPERLINK(\"https://example.test\",\"合成票种\")", "remark": "=1+1\n合成内部备注"},
            {"order_no": "SYNTH-NO-CONFIG", "configured": False},
        ])
        response, workbook = await self.workbook(identifiers.values())
        try:
            sheet = workbook.active
            self.assertEqual(tuple(cell.value for cell in sheet[1]), HEADERS)
            self.assertEqual(sheet.max_column, 8)
            self.assertEqual(sheet.max_row, 5)
            rows = list(sheet.iter_rows(min_row=2))
            by_order = {}
            for row in rows:
                by_order.setdefault(row[0].value, []).append(row)
                self.assertIsInstance(row[1].value, datetime)
                self.assertEqual(row[1].value.date(), date.fromisoformat(self.visit_date))
                self.assertEqual(row[1].data_type, "d")
                for column in (0, 2, 3, 4, 5, 6, 7):
                    self.assertEqual(row[column].number_format, "@")
                    self.assertNotEqual(row[column].data_type, "f")
                self.assertNotIn("绝不能使用的历史昵称", [cell.value for cell in row])
            shared = by_order[long_order]
            self.assertEqual(len(shared), 2)
            self.assertEqual({row[6].value for row in shared}, {PHONE})
            self.assertEqual({row[5].value for row in shared}, {"合成昵称"})
            self.assertEqual({row[7].value for row in shared}, {"内部备注\n两位游客一起取票"})
            self.assertTrue(all(row[7].alignment.wrap_text for row in shared))
            self.assertEqual(shared[0][0].data_type, "s")
            self.assertEqual(shared[0][0].value, long_order)
            for row in shared:
                self.assertEqual(row[3].data_type, "s")
                self.assertEqual(len(row[3].value), 18)
                self.assertEqual(row[6].data_type, "s")
            self.assertNotIn("SYNTH-DRAFT", by_order)
            legacy = by_order["SYNTH-LEGACY"][0]
            self.assertIn(legacy[6].value, (None, ""))
            self.assertEqual(legacy[5].value, "legacy-buyer")
            self.assertIn(legacy[7].value, (None, ""))
            formula = by_order["SYNTH-FORMULA"][0]
            self.assertEqual(formula[5].value, "=1+1")
            self.assertEqual(formula[5].data_type, "s")
            self.assertTrue(formula[4].value.startswith("=HYPERLINK("))
            self.assertEqual(formula[4].data_type, "s")
            self.assertEqual(formula[7].value, "=1+1\n合成内部备注")
            self.assertEqual(formula[7].data_type, "s")
        finally:
            workbook.close()

    async def test_export_includes_all_rows_beyond_list_and_stream_page_boundaries(self):
        count = 263
        identifiers = await self.seed([{"order_no": f"SYNTH-PAGE-{index:04d}"} for index in range(count)] + [{"order_no": "SYNTH-PAGE-NOFORM", "configured": False}])
        await self.seed([{"order_no": "UNSELECTED-OTHER-PAGE"}])
        _, workbook = await self.workbook(identifiers.values())
        try:
            rows = list(workbook.active.iter_rows(min_row=2, values_only=True))
            self.assertEqual(len(rows), count)
            self.assertEqual({row[0] for row in rows}, {f"SYNTH-PAGE-{index:04d}" for index in range(count)})
        finally:
            workbook.close()

    async def test_only_selected_orders_are_exported_even_when_other_orders_match(self):
        identifiers = await self.seed([
            {"order_no": "SELECTED-A", "account_id": "same-account", "item_id": "same-item"},
            {"order_no": "UNSELECTED-B", "account_id": "same-account", "item_id": "same-item"},
            {"order_no": "SELECTED-C", "account_id": "another-account"},
        ])
        selected = [identifiers["SELECTED-A"], identifiers["SELECTED-C"]]
        # 重复 ID 和不存在的 ID 不能扩大导出范围，也不能产生重复游客行。
        _, workbook = await self.workbook([*selected, selected[0], 9223372036854775807])
        try:
            actual = [row[0] for row in workbook.active.iter_rows(min_row=2, values_only=True)]
            self.assertEqual(set(actual), {"SELECTED-A", "SELECTED-C"})
            self.assertEqual(len(actual), 2)
        finally:
            workbook.close()
        async with self.sessions() as session:
            states = await OrderService(session).get_ticket_status_map(list(identifiers.values()))
            self.assertEqual(states[identifiers["UNSELECTED-B"]]["ticket_export_status"], "unexported")
            for identifier in selected:
                self.assertEqual(states[identifier]["ticket_export_status"], "exported")
        missing = await self.request_export([9223372036854775807])
        self.assertFalse(missing.json()["success"])

    async def test_missing_empty_invalid_and_filter_based_exports_are_rejected(self):
        identifiers = await self.seed([{"order_no": "MUST-NOT-BE-EXPORTED"}])
        selected_id = str(identifiers["MUST-NOT-BE-EXPORTED"])
        invalid = [{}, {"order_ids": []}, {"order_ids": None},
                   {"order_ids": [selected_id], "search": "MUST"},
                   {"order_ids": [selected_id], "ticket_export_status": "unexported"},
                   {"search": "MUST"}, {"order_ids": [selected_id] * 1001}]
        invalid.extend({"order_ids": [value]} for value in (1, True, "0", "-1", "1.5", "01", " 1", "9223372036854775808"))
        for payload in invalid:
            with self.subTest(payload=str(payload)[:100]):
                response = await self.client.post(self.url, headers=self.headers, json=payload)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["code"], 400)
                self.assertIn("no-store", response.headers.get("cache-control", ""))
                self.assertNotIn("MUST-NOT-BE-EXPORTED", response.text)
        legacy = await self.client.get(self.url, headers=self.headers, params={"search": "MUST"})
        self.assertEqual(legacy.status_code, 405)
        query = await self.client.post(self.url, headers=self.headers, params={"search": "MUST"}, json={"order_ids": [selected_id]})
        self.assertFalse(query.json()["success"])
        async with self.sessions() as session:
            registration = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertIsNone(registration.exported_at)

    async def test_export_marks_only_completed_matching_registrations(self):
        identifiers = await self.seed([
            {"order_no": "STATE-COLLECTED"},
            {"order_no": "STATE-PENDING", "submitted": False},
            {"order_no": "STATE-NOFORM", "configured": False},
            {"order_no": "OUTSIDE-FILTER"},
        ])
        selected = [identifiers[name] for name in ("STATE-COLLECTED", "STATE-PENDING", "STATE-NOFORM")]
        _, workbook = await self.workbook(selected)
        self.assertEqual([row[0] for row in workbook.active.iter_rows(min_row=2, values_only=True)], ["STATE-COLLECTED"])
        workbook.close()
        async with self.sessions() as session:
            states = await OrderService(session).get_ticket_status_map(list(identifiers.values()))
            self.assertEqual(states[identifiers["STATE-COLLECTED"]]["ticket_export_status"], "exported")
            self.assertEqual(states[identifiers["STATE-PENDING"]]["ticket_export_status"], "unexported")
            self.assertEqual(states[identifiers["OUTSIDE-FILTER"]]["ticket_export_status"], "unexported")
        not_collected = await self.request_export([identifiers["STATE-PENDING"], identifiers["STATE-NOFORM"]])
        self.assertFalse(not_collected.json()["success"])
        _, workbook = await self.workbook([identifiers["STATE-COLLECTED"]])
        self.assertEqual([row[0] for row in workbook.active.iter_rows(min_row=2, values_only=True)], ["STATE-COLLECTED"])
        workbook.close()

    async def test_workbook_failure_does_not_mark_exported(self):
        identifiers = await self.seed([{"order_no": "SYNTH-GENERATION-FAIL"}])
        with patch('app.services.ticket_registration_export_service.build_ticket_workbook', side_effect=RuntimeError('synthetic workbook failure')):
            response = await self.request_export(identifiers.values())
        self.assertFalse(response.json()["success"])
        async with self.sessions() as session:
            registration = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertIsNone(registration.exported_at)

    async def test_admin_export_serializes_concurrent_remark_save_then_remains_unexported(self):
        identifiers = await self.seed([{"order_no": "SYNTH-REMARK-RACE", "remark": "导出时原备注"}])
        order_id = identifiers["SYNTH-REMARK-RACE"]
        loop = asyncio.get_running_loop()
        pending = []

        async def edit_remark():
            async with self.sessions() as session:
                self.assertTrue(await OrderService(session).save_remark(order_id, "保存后的新备注", owner_id=None))

        async def start_waiting_edit():
            task = asyncio.create_task(edit_remark())
            pending.append(task)
            await asyncio.sleep(0.1)
            self.assertFalse(task.done(), "导出生成中必须持有订单锁，备注保存应等待导出事务完成")

        def build_while_editing(rows):
            asyncio.run_coroutine_threadsafe(start_waiting_edit(), loop).result(timeout=10)
            return build_ticket_workbook(rows)

        try:
            with patch('app.services.ticket_registration_export_service.build_ticket_workbook', side_effect=build_while_editing):
                response = await self.request_export([order_id])
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b"PK"), response.text[:300])
        finally:
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=15)
        workbook = load_workbook(BytesIO(response.content))
        try:
            self.assertEqual(workbook.active.cell(2, 8).value, "导出时原备注")
        finally:
            workbook.close()
        async with self.sessions() as session:
            order = (await session.execute(select(XYOrder).where(XYOrder.id == order_id))).scalar_one()
            registration = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == order_id))).scalar_one()
            self.assertEqual(order.remark, "保存后的新备注")
            self.assertIsNone(registration.exported_at)
            self.assertEqual(registration.version, 2)
            self.assertIsNotNone(registration.submitted_at)

    async def test_export_uses_current_locked_remark_not_older_repeatable_read_snapshot(self):
        identifiers = await self.seed([{"order_no": "SYNTH-REMARK-SNAPSHOT", "remark": "旧备注"}])
        order_id = identifiers["SYNTH-REMARK-SNAPSHOT"]
        async with self.sessions() as exporter:
            # 模拟认证或其他先前查询已经建立 MySQL REPEATABLE READ 快照。
            old = (await exporter.execute(select(XYOrder).where(XYOrder.id == order_id))).scalar_one()
            self.assertEqual(old.remark, "旧备注")
            async with self.sessions() as writer:
                self.assertTrue(await OrderService(writer).save_remark(order_id, "当前新备注", owner_id=None))
            content = await export_ticket_registrations(exporter, order_ids=[order_id])
        workbook = load_workbook(BytesIO(content))
        try:
            self.assertEqual(workbook.active.cell(2, 8).value, "当前新备注")
        finally:
            workbook.close()
        async with self.sessions() as session:
            registration = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == order_id))).scalar_one()
            self.assertIsNotNone(registration.exported_at)
            self.assertEqual(registration.version, 2)

    async def test_concurrent_configuration_change_cancels_stale_export(self):
        identifiers = await self.seed([{"order_no": "SYNTH-CONCURRENT-EXPORT"}, {"order_no": "SYNTH-UNMODIFIED"}])
        loop = asyncio.get_running_loop()

        async def edit_configuration():
            async with self.sessions() as session:
                await session.execute(update(TicketRegistration).where(TicketRegistration.order_id == identifiers["SYNTH-CONCURRENT-EXPORT"]).values(
                    version=TicketRegistration.version + 1, submitted_at=None,
                    identity_ciphertext=None, phone_ciphertext=None, exported_at=None,
                ))
                await session.commit()

        def build_while_edited(rows):
            asyncio.run_coroutine_threadsafe(edit_configuration(), loop).result(timeout=15)
            return build_ticket_workbook(rows)

        with patch('app.services.ticket_registration_export_service.build_ticket_workbook', side_effect=build_while_edited):
            response = await self.request_export(identifiers.values())
            self.assertFalse(response.json()["success"])
            self.assertEqual(response.json()["code"], 409)
            self.assertNotIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response.headers.get("content-type", ""))
        async with self.sessions() as session:
            registration = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == identifiers["SYNTH-CONCURRENT-EXPORT"]))).scalar_one()
            self.assertEqual(registration.version, 3)
            self.assertIsNone(registration.exported_at)
            self.assertIsNone(registration.submitted_at)
            untouched = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == identifiers["SYNTH-UNMODIFIED"]))).scalar_one()
            self.assertEqual(untouched.version, 2)
            self.assertIsNone(untouched.exported_at)
            self.assertIsNotNone(untouched.submitted_at)


if __name__ == "__main__":
    unittest.main()
