"""门票普通用户权限隔离：内存 SQLite、合成实名信息和假消息接口。"""
from __future__ import annotations

from datetime import timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
import httpx
from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes import ticket_registration as routes, ticket_registration_export as export_routes
from app.services.ticket_registration_service import TicketRegistrationError, TicketRegistrationService, websocket_client
from common.db.ticket_registration_schema import TICKET_KEY_SETTING
from common.models.system_setting import SystemSetting
from common.models.ticket_registration import TicketRegistration, TicketType
from common.models.user import User, UserRole, UserStatus
from common.models.user_setting import UserSetting
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES
from common.utils.time_utils import get_beijing_now, get_beijing_now_naive


class Stream:
    def __init__(self, rows):
        self.rows = rows

    async def partitions(self, size):
        for start in range(0, len(self.rows), size):
            yield self.rows[start:start + size]

    async def close(self):
        pass


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session
        self.next_id = 1000

    async def execute(self, statement):
        return self.session.execute(statement)

    async def stream(self, statement):
        return Stream(self.session.execute(statement).all())

    def add(self, value):
        if isinstance(value, TicketRegistration) and value.id is None:
            value.id = self.next_id
            self.next_id += 1
        self.session.add(value)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


def synthetic_identity(sequence):
    prefix = "11010119900101" + f"{sequence:03d}"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return prefix + "10X98765432"[sum(int(value) * weight for value, weight in zip(prefix, weights)) % 11]


class TicketPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (XYAccount, XYOrder, TicketRegistration, TicketType, SystemSetting, UserSetting):
            model.__table__.create(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.adapter = AsyncSessionAdapter(self.session)
        key = Fernet.generate_key()
        self.cipher = Fernet(key)
        self.session.add(SystemSetting(key=TICKET_KEY_SETTING, value=key.decode()))
        self.session.add(TicketType(id=1, name="1日成人票", category="adult", enabled=True))
        for identifier, owner in ((1, 2), (2, 3)):
            self.session.add(XYAccount(id=identifier, owner_id=owner, account_id=f"account-{identifier}",
                                      cookie="synthetic-cookie", login_method="cookie", status="active"))
        self.visit_date = get_beijing_now().date() + timedelta(days=7)
        self.big_id = 9007199254740993
        specs = [(100, 2, "account-1"), (101, 3, "account-2"), (102, 2, "account-2"),
                 (103, 3, "account-1"), (104, 2, "account-1"), (105, 2, "account-1"),
                 (106, 2, "account-1"), (107, 2, "account-1"), (self.big_id, 2, "account-1")]
        for identifier, owner, account in specs:
            self.session.add(XYOrder(id=identifier, owner_id=owner, account_id=account, order_no=f"SYNTH-{identifier}",
                status="pending_payment" if identifier == 105 else "pending_ship", buyer_id=f"buyer-{identifier}",
                buyer_fish_nick="合成买家" if identifier == 106 else "", item_id="synthetic-item", chat_id="synthetic-chat",
                amount="100.00", quantity=1))
        self.session.commit()
        for identifier in (100, 101, 102, 103, 106, 107, self.big_id):
            self.seed_registration(identifier, submitted=identifier != 100, count=2 if identifier == 106 else 1)
        self.session.commit()
        self.actor = User(id=2, username="synthetic-member", role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.include_router(export_routes.router)

        async def authenticated():
            if self.actor is None:
                raise HTTPException(status_code=401)
            return self.actor

        self.app.dependency_overrides[deps.get_current_user] = authenticated
        self.app.dependency_overrides[deps.get_db_session] = lambda: self.adapter
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.headers = {"X-Frontend-Origin": "https://tickets.example.test"}
        self.prefix = "/ticket-registration"
        self.send = AsyncMock(return_value={"success": True, "data": {"send_status": "success"}})
        self.status = AsyncMock(return_value={"success": True, "data": {"is_connected": True}})
        for mocked in (patch.object(websocket_client.http_client, "post", self.send),
                       patch.object(websocket_client, "get_account_status", self.status),
                       patch.object(routes, "limit_public", AsyncMock())):
            mocked.start()
            self.addCleanup(mocked.stop)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session.close()
        self.engine.dispose()

    def seed_registration(self, order_id, *, submitted, count):
        token = secrets.token_urlsafe(32)
        visitors = [{"id": f"{index + 1:032x}", "category": "adult", "amount": "68.00", "ticket_type_id": "1",
                     "ticket_type_name": "1日成人票", "ticket_type_category": "adult"} for index in range(count)]
        identities = {visitor["id"]: {"name": f"合成游客{chr(65 + index)}", "id_number": synthetic_identity(index + 1)}
                      for index, visitor in enumerate(visitors)}
        self.session.add(TicketRegistration(id=order_id, order_id=order_id, visit_date=self.visit_date,
            token_hash=hashlib.sha256(token.encode()).hexdigest(), token_ciphertext=self.cipher.encrypt(token.encode()).decode(),
            visitors=visitors, version=1, submitted_at=get_beijing_now_naive() if submitted else None,
            identity_ciphertext=self.cipher.encrypt(json.dumps(identities).encode()).decode() if submitted else None,
            phone_ciphertext=self.cipher.encrypt(b"13800138000").decode() if submitted else None))

    def configuration(self, version=0):
        return {"version": version, "visit_date": self.visit_date.isoformat(), "visitors": [
            {"category": "adult", "amount": "68.00", "ticket_type_id": "1"},
        ]}

    def registration(self, order_id):
        self.session.expire_all()
        return self.session.execute(select(TicketRegistration).where(TicketRegistration.order_id == order_id)).scalar_one_or_none()

    async def request(self, method, suffix, body=None):
        kwargs = {"headers": self.headers}
        if body is not None:
            kwargs["json"] = body
        return await self.client.request(method, self.prefix + suffix, **kwargs)

    async def test_member_can_load_types_create_and_edit_own_configuration(self):
        self.assertTrue((await self.request("GET", "/types")).json()["success"])
        created = (await self.request("PUT", "/orders/104", self.configuration())).json()
        self.assertTrue(created["success"], created)
        self.assertTrue(created["data"]["configuration"]["link"].startswith("https://tickets.example.test/ticket-registration#"))
        updated = (await self.request("PUT", "/orders/104", self.configuration(1))).json()
        self.assertTrue(updated["success"], updated)
        self.assertEqual(updated["data"]["configuration"]["version"], 2)
        self.assertTrue((await self.request("GET", "/orders/104")).json()["success"])
        self.assertFalse((await self.request("PUT", "/orders/105", self.configuration())).json()["success"])

    async def test_foreign_orders_and_account_owner_mismatches_reject_every_operation(self):
        for order_id in (101, 102, 103, 999999):
            for method, suffix, body in (("GET", "", None), ("PUT", "", self.configuration(1)),
                                        ("POST", "/send", {"version": 1}), ("POST", "/recollect", {"version": 1})):
                with self.subTest(order=order_id, method=method, suffix=suffix):
                    response = await self.request(method, f"/orders/{order_id}{suffix}", body)
                    data = response.json()
                    self.assertEqual(data["code"], 403, data)
                    self.assertIsNone(data["data"])
                    self.assertIn("no-store", response.headers["Cache-Control"])
        for order_id in (101, 102, 103):
            registration = self.registration(order_id)
            self.assertEqual(registration.version, 1)
            self.assertIsNotNone(registration.identity_ciphertext)
            self.assertIsNone(registration.send_attempt_status)
        self.send.assert_not_awaited()
        self.status.assert_not_awaited()

    async def test_owned_send_is_bound_to_order_buyer_and_idempotent(self):
        for _ in range(2):
            result = (await self.request("POST", "/orders/100/send", {"version": 1})).json()
            self.assertTrue(result["success"], result)
        self.send.assert_awaited_once()
        self.assertIn("/accounts/account-1/send-message", self.send.await_args.args[0])
        self.assertEqual(self.send.await_args.kwargs["json"]["to_user_id"], "buyer-100")
        self.assertEqual(self.send.await_args.kwargs["max_retries"], 1)

    async def test_member_can_view_complete_identity_and_recollect_own_order(self):
        read = (await self.request("GET", "/orders/106")).json()["data"]["configuration"]
        self.assertEqual(read["phone"], "13800138000")
        self.assertEqual(read["visitors"][0]["id_number"], synthetic_identity(1))
        recollected = (await self.request("POST", "/orders/106/recollect", {"version": 1})).json()
        self.assertTrue(recollected["success"], recollected)
        config = recollected["data"]["configuration"]
        self.assertEqual(config["status"], "draft")
        self.assertEqual(config["version"], 2)
        self.assertEqual(config["link"], read["link"])
        self.assertEqual(config["visitors"][0]["amount"], "68.00")
        self.assertIsNone(config["phone"])
        self.assertTrue(all(visitor["id_number"] is None for visitor in config["visitors"]))
        stored = self.registration(106)
        self.assertIsNone(stored.identity_ciphertext)
        self.assertIsNone(stored.phone_ciphertext)
        self.assertIsNone(stored.exported_at)

    async def test_member_export_is_selected_only_per_visitor_and_preserves_phone(self):
        response = await self.request("POST", "/export.xlsx", {"order_ids": ["106", str(self.big_id), "100"]})
        self.assertIn("spreadsheetml", response.headers["Content-Type"])
        workbook = load_workbook(BytesIO(response.content))
        try:
            rows = list(workbook.active.iter_rows(min_row=2, values_only=True))
            self.assertEqual(len(rows), 3)
            self.assertEqual({row[0] for row in rows}, {"SYNTH-106", f"SYNTH-{self.big_id}"})
            shared = [row for row in rows if row[0] == "SYNTH-106"]
            self.assertEqual(len(shared), 2)
            self.assertEqual({row[6] for row in shared}, {"13800138000"})
            self.assertEqual({row[5] for row in shared}, {"合成买家"})
            self.assertEqual(next(row for row in rows if row[0] != "SYNTH-106")[5], f"buyer-{self.big_id}")
        finally:
            workbook.close()
        self.assertIsNotNone(self.registration(106).exported_at)
        self.assertIsNotNone(self.registration(self.big_id).exported_at)
        for identifier in (100, 101, 107):
            self.assertIsNone(self.registration(identifier).exported_at)

    async def test_mixed_foreign_missing_or_wrong_account_selection_fails_atomically(self):
        with patch.object(TicketRegistrationService, "_get_cipher", AsyncMock()) as cipher:
            for identifier in (101, 102, 103, 999999):
                response = await self.request("POST", "/export.xlsx", {"order_ids": ["106", str(identifier)]})
                self.assertEqual(response.json()["code"], 403)
            cipher.assert_not_awaited()
        self.assertIsNone(self.registration(106).exported_at)
        self.assertIsNone(self.registration(101).exported_at)

    async def test_admin_retains_cross_owner_read_write_send_recollect_and_export(self):
        self.actor = User(id=1, username="synthetic-admin", role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        self.assertTrue((await self.request("GET", "/orders/101")).json()["success"])
        exported = await self.request("POST", "/export.xlsx", {"order_ids": ["101", "106"]})
        self.assertIn("spreadsheetml", exported.headers["Content-Type"])
        self.assertTrue((await self.request("POST", "/orders/101/send", {"version": 1})).json()["success"])
        self.assertTrue((await self.request("POST", "/orders/101/recollect", {"version": 1})).json()["success"])
        self.assertTrue((await self.request("PUT", "/orders/101", self.configuration(2))).json()["success"])

    async def test_global_policy_stays_admin_only_and_personal_policy_remains_accessible(self):
        for suffix, payload in (("/age-rules", DEFAULT_TICKET_AGE_RULES),
                                ("/age-policy", {"enabled": True, "rules": DEFAULT_TICKET_AGE_RULES}),
                                ("/message-template", {"message_template": "{infourl}"})):
            for method in ("GET", "PUT"):
                result = (await self.request(method, suffix, payload if method == "PUT" else None)).json()
                self.assertEqual(result["code"], 403)
        for suffix in ("/personal-age-policy", "/personal-message-template"):
            self.assertTrue((await self.request("GET", suffix)).json()["success"])

    async def test_operator_is_owner_scoped_and_inactive_or_anonymous_cannot_manage(self):
        self.actor.role = UserRole.OPERATOR
        self.assertTrue((await self.request("GET", "/orders/100")).json()["success"])
        self.assertEqual((await self.request("GET", "/orders/101")).json()["code"], 403)
        for actor, code in ((User(id=2, role=UserRole.MEMBER, status=UserStatus.SUSPENDED), 403), (None, 401)):
            self.actor = actor
            for method, suffix, body in (("GET", "/types", None), ("GET", "/orders/100", None),
                ("PUT", "/orders/104", self.configuration()), ("POST", "/orders/100/send", {"version": 1}),
                ("POST", "/orders/106/recollect", {"version": 1}), ("POST", "/export.xlsx", {"order_ids": ["106"]})):
                with self.subTest(actor=actor is not None, suffix=suffix):
                    self.assertEqual((await self.request(method, suffix, body)).json()["code"], code)
        self.send.assert_not_awaited()

    async def test_scope_is_enforced_in_service_and_cannot_be_overridden_in_body(self):
        service = TicketRegistrationService(self.adapter, owner_id=2)
        with self.assertRaises(TicketRegistrationError):
            await service.get_admin(101)
        for suffix, payload in (("/orders/104", {**self.configuration(), "owner_id": 3}),
                                ("/export.xlsx", {"order_ids": ["101"], "owner_id": None})):
            method = "PUT" if suffix.startswith("/orders/") else "POST"
            self.assertEqual((await self.request(method, suffix, payload)).json()["code"], 400)

    async def test_public_token_flow_stays_separate_from_management_scope(self):
        created = (await self.request("PUT", "/orders/104", self.configuration())).json()["data"]["configuration"]
        self.actor = None
        token_headers = {"X-Ticket-Token": urlsplit(created["link"]).fragment}
        response = await self.client.post(self.prefix + "/public/submit", headers=token_headers, json={
            "version": created["version"], "phone": "13800138000", "visitors": [
                {"id": created["visitors"][0]["id"], "name": "合成游客", "id_number": synthetic_identity(1)},
            ],
        })
        self.assertTrue(response.json()["success"], response.json())
        self.assertNotIn("id_number", response.json()["data"]["visitors"][0])
        self.assertEqual((await self.request("GET", "/orders/104")).json()["code"], 401)


if __name__ == "__main__":
    unittest.main()
