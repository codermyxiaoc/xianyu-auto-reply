"""门票登记关键边界测试，不访问闲鱼或发送真实消息。

运行：在backend-web目录执行 ../.venv/Scripts/python.exe -m unittest test_ticket_registration -v
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.fernet import Fernet
from pydantic import ValidationError
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES, TicketConfigRequest, TicketPhoneRequest, TicketSubmitRequest, TicketVisitorConfig
from common.schemas.ticket_message_template import DEFAULT_TICKET_MESSAGE_TEMPLATE
from common.utils.time_utils import get_beijing_now
from app.services.ticket_registration_service import (
    CANONICAL_TICKET_TYPE_NAMES, TicketRegistrationService, TicketRegistrationError, public_link_base,
    require_paid_order, validate_identity, validate_slot_ids,
)


def example_id(prefix="11010519900101001"):
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return prefix + "10X98765432"[sum(int(prefix[i]) * weights[i] for i in range(17)) % 11]


SLOT = "a" * 32
OTHER_SLOT = "b" * 32
PHONE = "13800138000"


class ValidationTests(unittest.TestCase):
    def test_payment_does_not_trust_processing_or_refund_states(self):
        for status in ("pending_ship", "shipped", "completed"):
            require_paid_order(SimpleNamespace(status=status))
        for status in ("processing", "processed", "pending_payment", "refunding", "refunded", "cancelled", "unknown", ""):
            with self.subTest(status=status), self.assertRaises(TicketRegistrationError):
                require_paid_order(SimpleNamespace(status=status))

    def test_separate_admin_and_buyer_payloads_forbid_tampering(self):
        buyer = {"version": 1, "phone": PHONE, "visitors": [{"id": SLOT, "name": "测试游客", "id_number": example_id()}]}
        for field, value in (("visit_date", "2030-01-01"), ("amount", "0"), ("order_id", "123")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                TicketSubmitRequest.model_validate({**buyer, field: value})
        with self.assertRaises(ValidationError):
            TicketSubmitRequest.model_validate({**buyer, "visitors": [{**buyer["visitors"][0], "ticket_type_id": "2"}]})
        with self.assertRaises(ValidationError):
            TicketVisitorConfig.model_validate({"category": "adult", "amount": "1", "ticket_type_id": "1", "name": "不能代填"})
        with self.assertRaises(ValidationError):
            TicketSubmitRequest.model_validate({**buyer, "version": True})

    def test_money_and_date_boundaries(self):
        self.assertEqual(TicketVisitorConfig(category="child", amount="0", ticket_type_id="1").amount, "0.00")
        self.assertEqual(TicketVisitorConfig(category="adult", amount="99999999.99", ticket_type_id="1").amount, "99999999.99")
        for value in ("-1", "1.001", "NaN", "1e3", "100000000", "inf", 100, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TicketVisitorConfig(category="adult", amount=value, ticket_type_id="1")
        with self.assertRaises(ValidationError):
            TicketConfigRequest(version=0, visit_date=get_beijing_now().date() - timedelta(days=1), visitors=[{"category":"adult","amount":"1","ticket_type_id":"1"}])

    def test_phone_is_required_and_accepts_only_one_mainland_mobile_number(self):
        payload = {"version": 1, "visitors": [{"id": SLOT, "name": "测试游客", "id_number": example_id()}]}
        with self.assertRaises(ValidationError):
            TicketSubmitRequest.model_validate(payload)
        for phone in (None, "", "12800138000", "1380013800", "138001380000", "+8613800138000", "1380013800A", "１３８００１３８０００", 13800138000, [PHONE]):
            with self.subTest(phone=phone), self.assertRaises(ValidationError):
                TicketSubmitRequest.model_validate({**payload, "phone": phone})
        self.assertEqual(TicketSubmitRequest.model_validate({**payload, "phone": PHONE}).phone, PHONE)
        with self.assertRaises(ValidationError):
            TicketPhoneRequest.model_validate({"version": 1, "phone": PHONE, "visitors": payload["visitors"]})

    def test_identity_checksum_birthday_and_name(self):
        identity = example_id()
        self.assertEqual(validate_identity("  测试游客  ", identity.lower(), 1), ("测试游客", identity))
        self.assertEqual(validate_identity("John Smith", identity, 1)[0], "John Smith")
        for name, number in (("张", identity), ("<script>", identity), ("测试游客", identity[:-1] + ("0" if identity[-1] != "0" else "1")), ("测试游客", example_id("11010519900230001")), ("测试游客", example_id("11010520900101001"))):
            with self.subTest(name=name), self.assertRaises(TicketRegistrationError) as error:
                validate_identity(name, number, 2)
            self.assertIn("第 2 位", str(error.exception))
            self.assertNotIn(number, str(error.exception))

    def test_exact_slots_only(self):
        expected = [{"id": SLOT}, {"id": OTHER_SLOT}]
        validate_slot_ids(expected, [SimpleNamespace(id=OTHER_SLOT), SimpleNamespace(id=SLOT)])
        for ids in ((SLOT,), (SLOT, SLOT), (SLOT, "c" * 32)):
            with self.assertRaises(TicketRegistrationError):
                validate_slot_ids(expected, [SimpleNamespace(id=value) for value in ids])

    def test_origin_allows_current_lan_but_rejects_url_injection(self):
        for value in ("http://127.0.0.1:9000", "http://192.168.1.20:9000", "http://localhost:9000", "https://tickets.example.com", "http://[::1]:9000"):
            self.assertTrue(public_link_base(value)[1])
        self.assertEqual(public_link_base("https://TICKETS.example.com:443")[0], "https://tickets.example.com")
        for value in ("", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com/path", "https://example.com?next=x", "https://example.com#token", "https://example.com\n", "https://example.com\\@evil.test", "https://%65xample.com", "https://-bad.example", "https://example.com:0"):
            with self.subTest(value=value), self.assertRaises(TicketRegistrationError):
                public_link_base(value)


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), execute=AsyncMock())
        service = TicketRegistrationService(session, "https://tickets.example.com")
        order = SimpleNamespace(id=42, owner_id=9, order_no="TEST-ONLY-ORDER", status="pending_ship")
        config = SimpleNamespace(id=1, version=3, submitted_at=None, identity_ciphertext=None, phone_ciphertext=None, visit_date=get_beijing_now().date(),
            visitors=[{"id": SLOT, "category":"adult", "amount":"12.50", "ticket_type_name":"1日成人票", "ticket_type_id":"1"}])
        service._from_token = AsyncMock(return_value=(order, config))
        service._order = AsyncMock(return_value=order)
        service._config = AsyncMock(return_value=config)
        service._get_cipher = AsyncMock(return_value=Fernet(Fernet.generate_key()))
        service.effective_age_policy = AsyncMock(return_value={"enabled": True, "rules": DEFAULT_TICKET_AGE_RULES})
        service.effective_message_template = AsyncMock(return_value=DEFAULT_TICKET_MESSAGE_TEMPLATE)
        return service, session, order, config

    async def test_stale_version_does_not_commit_admin_or_buyer(self):
        service, session, _, _ = self.make_service()
        with self.assertRaises(TicketRegistrationError):
            await service.save(42, TicketConfigRequest(version=2, visit_date=get_beijing_now().date(), visitors=[{"id":SLOT,"category":"adult","amount":"1","ticket_type_id":"1"}]))
        with self.assertRaises(TicketRegistrationError):
            await service.submit("token", TicketSubmitRequest(version=2, phone=PHONE, visitors=[{"id":SLOT,"name":"测试游客","id_number":example_id()}]))
        session.commit.assert_not_awaited()

    async def test_submit_encrypts_identity_and_public_response_is_masked(self):
        service, session, _, config = self.make_service()
        number = example_id()
        payload = TicketSubmitRequest(version=3, phone=PHONE, visitors=[{"id":SLOT,"name":"测试游客","id_number":number}])
        response = await service.submit("token", payload)
        self.assertEqual(config.version, 4)
        self.assertEqual(response["status"], "submitted")
        self.assertNotIn(number, json.dumps(response))
        self.assertNotIn(number, config.identity_ciphertext)
        self.assertNotIn("测试游客", config.identity_ciphertext)
        self.assertEqual(json.loads(await service._decrypt(config.identity_ciphertext))[SLOT]["id_number"], number)
        self.assertEqual(response["visitors"][0]["id_number_masked"], number[:3] + "***********" + number[-4:])
        self.assertEqual(response["phone_masked"], "138****8000")
        self.assertNotIn(PHONE, json.dumps(response))
        self.assertNotIn(PHONE, config.phone_ciphertext)
        self.assertEqual(await service.contact_phone(config), PHONE)
        self.assertEqual(config.visitors[0]["amount"], "12.50")
        with self.assertRaises(TicketRegistrationError):
            await service.submit("token", payload)
        session.commit.assert_awaited_once()

    async def test_duplicate_identity_never_commits(self):
        service, session, _, config = self.make_service()
        config.visitors.append({**config.visitors[0], "id": OTHER_SLOT})
        with self.assertRaises(TicketRegistrationError):
            await service.submit("token", TicketSubmitRequest(version=3, phone=PHONE, visitors=[{"id":identifier,"name":"测试游客","id_number":example_id()} for identifier in (SLOT, OTHER_SLOT)]))
        self.assertIsNone(config.identity_ciphertext)
        session.commit.assert_not_awaited()

    async def test_legacy_phone_supplement_is_once_only_and_preserves_identity(self):
        service, session, order, config = self.make_service()
        with self.assertRaises(TicketRegistrationError):
            await service.supplement_phone("token", TicketPhoneRequest(version=3, phone=PHONE))
        identity = {SLOT: {"name": "测试游客", "id_number": example_id()}}
        config.identity_ciphertext = (await service._get_cipher()).encrypt(json.dumps(identity).encode()).decode()
        config.submitted_at = get_beijing_now().replace(tzinfo=None)
        original_ciphertext, original_submitted_at = config.identity_ciphertext, config.submitted_at
        with self.assertRaises(TicketRegistrationError):
            await service.supplement_phone("token", TicketPhoneRequest(version=2, phone=PHONE))
        self.assertIsNone(config.phone_ciphertext)
        response = await service.supplement_phone("token", TicketPhoneRequest(version=3, phone=PHONE))
        self.assertEqual(response["status"], "submitted")
        self.assertEqual(response["version"], 4)
        self.assertEqual(response["phone_masked"], "138****8000")
        self.assertEqual(config.identity_ciphertext, original_ciphertext)
        self.assertEqual(config.submitted_at, original_submitted_at)
        self.assertEqual(await service.contact_phone(config), PHONE)
        with self.assertRaises(TicketRegistrationError):
            await service.supplement_phone("token", TicketPhoneRequest(version=4, phone="13900139000"))
        session.commit.assert_awaited_once()

    async def test_sent_link_idempotency_includes_frontend_host(self):
        service, session, order, config = self.make_service()
        old_link = "https://old.example.com/ticket-registration#token"
        new_link = "https://tickets.example.com/ticket-registration#token"
        config.sent_version, config.sent_link_hash = 3, hashlib.sha256(old_link.encode()).hexdigest()
        config.send_attempt_version, config.send_attempt_status = None, None
        order.account_id, order.owner_id, order.buyer_id, order.item_id = "acct", 9, "buyer", "item"
        service._admin_view = AsyncMock(return_value={"configuration":{"link":new_link,"public_url_ready":True}})
        # 若错误地仅按version去重，此调用会返回成功；正确实现继续校验订单账号。
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
        with self.assertRaises(TicketRegistrationError) as error:
            await service.send(42, 3)
        self.assertIn("订单账号", str(error.exception))
        config.sent_link_hash = hashlib.sha256(new_link.encode()).hexdigest()
        message, _ = await service.send(42, 3)
        self.assertIn("无需重复发送", message)

    async def test_definite_presend_rejection_can_retry_without_duplicate_success(self):
        service, session, order, config = self.make_service()
        order.account_id, order.owner_id, order.buyer_id, order.item_id, order.chat_id = "acct", 9, "buyer", "item", "chat"
        config.sent_version = config.sent_link_hash = config.send_attempt_version = config.send_attempt_status = None
        view = {"configuration":{"link":"https://tickets.example.com/ticket-registration#token","public_url_ready":True}}
        service._admin_view = AsyncMock(return_value=view)
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(status="active"))
        with patch("app.services.ticket_registration_service.websocket_client.get_account_status", AsyncMock(return_value={"success":True,"data":{"is_connected":True}})), patch("app.services.ticket_registration_service.websocket_client.http_client.post", AsyncMock(side_effect=[{"success":False,"code":404,"data":None},{"success":True,"data":{"send_status":"success"}}])) as post:
            with self.assertRaises(TicketRegistrationError):
                await service.send(42, 3)
            self.assertEqual(config.send_attempt_status, "failed")
            message, _ = await service.send(42, 3)
            self.assertIn("已发送", message)
            self.assertEqual(post.await_count, 2)
            await service.send(42, 3)
            self.assertEqual(post.await_count, 2)
            self.assertEqual(post.await_args.kwargs["max_retries"], 1)

    async def test_unknown_send_blocks_retries_even_from_different_host(self):
        service, session, order, config = self.make_service()
        order.account_id, order.owner_id, order.buyer_id, order.item_id, order.chat_id = "acct", 9, "buyer", "item", "chat"
        config.sent_version = config.sent_link_hash = config.send_attempt_version = config.send_attempt_status = None
        service._admin_view = AsyncMock(return_value={"configuration":{"link":"https://tickets.example.com/ticket-registration#token","public_url_ready":True}})
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(status="active"))
        with patch("app.services.ticket_registration_service.websocket_client.get_account_status", AsyncMock(return_value={"success":True,"data":{"is_connected":True}})), patch("app.services.ticket_registration_service.websocket_client.http_client.post", AsyncMock(side_effect=asyncio.TimeoutError)) as post:
            with self.assertRaises(TicketRegistrationError):
                await service.send(42, 3)
            self.assertEqual(config.send_attempt_status, "pending")
            service._admin_view.return_value = {"configuration":{"link":"https://other.example.com/ticket-registration#token","public_url_ready":True}}
            with self.assertRaises(TicketRegistrationError):
                await service.send(42, 3)
            self.assertEqual(post.await_count, 1)

    async def test_admin_edit_before_dispatch_cancels_without_poisoning_retries(self):
        service, session, order, config = self.make_service()
        order.account_id, order.owner_id, order.buyer_id, order.item_id, order.chat_id = "acct", 9, "buyer", "item", "chat"
        config.sent_version = config.sent_link_hash = config.send_attempt_version = config.send_attempt_status = None
        service._admin_view = AsyncMock(return_value={"configuration":{"link":"https://tickets.example.com/ticket-registration#token","public_url_ready":True}})
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(status="active"))
        async def concurrent_edit():
            config.version = 4
        session.commit.side_effect = concurrent_edit
        with patch("app.services.ticket_registration_service.websocket_client.get_account_status", AsyncMock(return_value={"success":True,"data":{"is_connected":True}})), patch("app.services.ticket_registration_service.websocket_client.http_client.post", AsyncMock()) as post:
            with self.assertRaises(TicketRegistrationError):
                await service.send(42, 3)
            self.assertEqual(config.send_attempt_status, "failed")
            post.assert_not_awaited()

    async def test_recollect_clears_only_collected_data_and_tracking(self):
        service, session, order, config = self.make_service()
        config.submitted_at = config.exported_at = config.sent_at = get_beijing_now().replace(tzinfo=None)
        config.identity_ciphertext, config.phone_ciphertext = "old-encrypted-identities", "old-encrypted-phone"
        config.token_hash, config.token_ciphertext = "unchanged-hash", "unchanged-encrypted-token"
        config.sent_version = config.send_attempt_version = 3
        config.sent_link_hash = config.send_attempt_link_hash = "old-link-hash"
        config.send_attempt_status = "unknown"
        original = (config.visit_date, list(config.visitors), config.token_hash, config.token_ciphertext)
        service._admin_view = AsyncMock(return_value={"configuration": {"status": "draft"}})
        result = await service.recollect(42, 3)
        self.assertEqual(result["configuration"]["status"], "draft")
        self.assertEqual(config.version, 4)
        for field in ("identity_ciphertext", "phone_ciphertext", "submitted_at", "exported_at", "sent_at", "sent_version", "sent_link_hash", "send_attempt_version", "send_attempt_link_hash", "send_attempt_status"):
            self.assertIsNone(getattr(config, field), field)
        self.assertEqual((config.visit_date, config.visitors, config.token_hash, config.token_ciphertext), original)
        service._order.assert_awaited_once_with(42, lock=True)
        service._config.assert_awaited_once_with(42, lock=True)
        session.commit.assert_awaited_once()

    async def test_recollect_rejects_stale_missing_and_unsubmitted_configuration(self):
        for state in ("stale", "missing", "draft"):
            with self.subTest(state=state):
                service, session, _, config = self.make_service()
                if state == "stale":
                    config.submitted_at = get_beijing_now().replace(tzinfo=None)
                elif state == "missing":
                    service._config.return_value = None
                with self.assertRaises(TicketRegistrationError) as error:
                    await service.recollect(42, 2 if state == "stale" else 3)
                self.assertEqual(error.exception.code, 409)
                session.commit.assert_not_awaited()

    async def test_recollect_between_send_intent_and_dispatch_cancels_old_sender(self):
        service, session, order, config = self.make_service()
        order.account_id, order.owner_id, order.buyer_id, order.item_id, order.chat_id = "acct", 9, "buyer", "item", "chat"
        config.submitted_at = get_beijing_now().replace(tzinfo=None)
        config.sent_version = config.sent_link_hash = config.send_attempt_version = config.send_attempt_status = None
        service._admin_view = AsyncMock(return_value={"configuration": {"link": "https://tickets.example.com/ticket-registration#token"}})
        session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(status="active"))
        invoked = False

        async def recollect_after_intent_commit():
            nonlocal invoked
            if not invoked:
                invoked = True
                self.assertEqual(config.send_attempt_status, "pending")
                await service.recollect(42, 3)

        session.commit.side_effect = recollect_after_intent_commit
        with patch("app.services.ticket_registration_service.websocket_client.get_account_status", AsyncMock(return_value={"success": True, "data": {"is_connected": True}})), patch("app.services.ticket_registration_service.websocket_client.http_client.post", AsyncMock()) as post:
            with self.assertRaises(TicketRegistrationError) as error:
                await service.send(42, 3)
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(config.version, 4)
            self.assertIsNone(config.send_attempt_status)
            self.assertIsNone(config.submitted_at)
            post.assert_not_awaited()

    async def test_types_have_canonical_display_order_and_keep_legacy_ids(self):
        service, session, _, _ = self.make_service()
        names = list(CANONICAL_TICKET_TYPE_NAMES)
        rows = [SimpleNamespace(id=index + 20, name=name, category=None) for index, name in enumerate(reversed(names))]
        rows.append(SimpleNamespace(id=99, name="未知自定义票", category=None))
        session.execute.return_value = SimpleNamespace(scalars=lambda: rows)
        options = await service.types()
        self.assertEqual([option["name"] for option in options], names)
        self.assertEqual({option["name"]: option["id"] for option in options}, {row.name: str(row.id) for row in rows if row.id != 99})
        for category, person in (("adult", "成人"), ("senior", "老人"), ("child", "儿童")):
            self.assertEqual([option["name"] for option in options if option["category"] == category], [f"{duration}日{person}票" for duration in ("1", "1.5", "2")])


if __name__ == "__main__":
    unittest.main()
