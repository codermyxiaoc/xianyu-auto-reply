"""门票年龄、分类和越权边界；不访问业务库或发送消息。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
from pydantic import ValidationError
from app.services.ticket_registration_service import TicketRegistrationError, TicketRegistrationService, validate_visitor_age
from common.schemas.ticket_registration import (
    DEFAULT_TICKET_AGE_RULES, TicketAgeRules, TicketConfigRequest, TicketSubmitRequest,
)
from common.utils.ticket_age import age_on_date, infer_ticket_category
from common.utils.time_utils import get_beijing_now


def test_identity(birthday: str) -> str:
    # 990000 是虚构地区码，不对应真实居民身份。
    prefix = "990000" + birthday + "001"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return prefix + "10X98765432"[sum(int(n) * w for n, w in zip(prefix, weights)) % 11]


class AgeRuleValidationTests(unittest.TestCase):
    def test_exact_birthday_boundaries_and_visit_date(self):
        rules = deepcopy(DEFAULT_TICKET_AGE_RULES)
        birthday = test_identity("20081107")
        self.assertEqual(validate_visitor_age(birthday, date(2026, 11, 6), "child", rules, 1), 17)
        self.assertEqual(validate_visitor_age(birthday, date(2026, 11, 7), "adult", rules, 1), 18)
        with self.assertRaises(TicketRegistrationError) as error:
            validate_visitor_age(birthday, date(2026, 11, 7), "child", rules, 2)
        self.assertIn("第 2 位", str(error.exception))
        self.assertNotIn(birthday, str(error.exception))
        senior = test_identity("19661107")
        self.assertEqual(validate_visitor_age(senior, date(2026, 11, 6), "adult", rules, 1), 59)
        self.assertEqual(validate_visitor_age(senior, date(2026, 11, 7), "senior", rules, 1), 60)
        with self.assertRaises(TicketRegistrationError):
            validate_visitor_age(senior, date(2026, 11, 7), "adult", rules, 1)

    def test_leap_day_age_and_unlimited_upper_age(self):
        self.assertEqual(age_on_date(date(2008, 2, 29), date(2026, 2, 28)), 17)
        self.assertEqual(age_on_date(date(2008, 2, 29), date(2026, 3, 1)), 18)
        self.assertEqual(validate_visitor_age(test_identity("19000101"), date(2026, 1, 1), "senior", DEFAULT_TICKET_AGE_RULES, 1), 126)

    def test_custom_rules_and_invalid_ranges(self):
        custom = {"child": {"min_age": 3, "max_age": 11}, "adult": {"min_age": 12, "max_age": 64}, "senior": {"min_age": 65, "max_age": None}}
        self.assertEqual(TicketAgeRules.model_validate(custom).model_dump(), custom)
        self.assertEqual(validate_visitor_age(test_identity("20140101"), date(2026, 1, 1), "adult", custom, 1), 12)
        invalid = [
            ("child", "max_age", 18), ("adult", "min_age", 60),
            ("senior", "min_age", -1), ("child", "min_age", True),
            ("child", "max_age", "17"), ("senior", "max_age", 151),
            ("child", "max_age", None),
        ]
        for category, key, value in invalid:
            rules = deepcopy(DEFAULT_TICKET_AGE_RULES)
            rules[category][key] = value
            with self.subTest(category=category, key=key, value=value), self.assertRaises(ValidationError):
                TicketAgeRules.model_validate(rules)
        with self.assertRaises(ValidationError):
            TicketAgeRules.model_validate({**custom, "disable_check": True})

    def test_ticket_category_inference_does_not_guess_unknown_names(self):
        for label, category in [("成人", "adult"), ("儿童", "child"), ("老人", "senior")]:
            self.assertEqual(infer_ticket_category("1.5日" + label + "票"), category)
        self.assertIsNone(infer_ticket_category("通用票"))
        self.assertIsNone(infer_ticket_category("成人儿童家庭票"))


class AgeRuleServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, category="senior", ticket_name="1日老人票"):
        session = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
        service = TicketRegistrationService(session)
        order = SimpleNamespace(id=1, owner_id=42, order_no="TEST-AGE", status="pending_ship")
        slot = "a" * 32
        config = SimpleNamespace(version=1, visit_date=get_beijing_now().date() + timedelta(days=7),
            submitted_at=None, identity_ciphertext=None, phone_ciphertext=None,
            exported_at=get_beijing_now().replace(tzinfo=None),
            visitors=[{"id": slot, "category": category, "amount": "10", "ticket_type_id": "3", "ticket_type_name": ticket_name}])
        service._from_token = AsyncMock(return_value=(order, config))
        service._order = AsyncMock(return_value=order)
        service._config = AsyncMock(return_value=config)
        service._get_cipher = AsyncMock(return_value=Fernet(Fernet.generate_key()))
        service.effective_age_policy = AsyncMock(return_value={"enabled": True, "rules": deepcopy(DEFAULT_TICKET_AGE_RULES)})
        service._public_view = AsyncMock(return_value={"status": "submitted"})
        return service, session, config

    async def test_backend_rejects_underage_senior_even_if_client_bypassed(self):
        service, session, config = self.make_service()
        payload = TicketSubmitRequest(version=1, phone="13800138000", visitors=[
            {"id": config.visitors[0]["id"], "name": "测试游客", "id_number": test_identity("19900101")},
        ])
        with self.assertRaises(TicketRegistrationError) as error:
            await service.submit("token", payload)
        self.assertIn("老人票年龄要求", str(error.exception))
        self.assertIsNone(config.identity_ciphertext)
        session.commit.assert_not_awaited()

    async def test_valid_senior_submission_invalidates_old_export_mark(self):
        service, session, config = self.make_service()
        payload = TicketSubmitRequest(version=1, phone="13800138000", visitors=[
            {"id": config.visitors[0]["id"], "name": "测试游客", "id_number": test_identity("19500101")},
        ])
        await service.submit("token", payload)
        self.assertIsNone(config.exported_at)
        session.commit.assert_awaited_once()

    async def test_disabled_age_check_accepts_age_mismatch_for_order_owner(self):
        service, session, config = self.make_service()
        service.effective_age_policy.return_value["enabled"] = False
        payload = TicketSubmitRequest(version=1, phone="13800138000", visitors=[
            {"id": config.visitors[0]["id"], "name": "测试游客", "id_number": test_identity("19900101")},
        ])
        await service.submit("token", payload)
        self.assertIsNotNone(config.identity_ciphertext)
        service.effective_age_policy.assert_awaited_once_with(42)
        session.commit.assert_awaited_once()

    async def test_disabled_age_check_still_rejects_bad_identity_and_wrong_ticket(self):
        for failure in ("checksum", "birthday", "category", "slot"):
            with self.subTest(failure=failure):
                service, session, config = self.make_service()
                service.effective_age_policy.return_value["enabled"] = False
                number = test_identity("19900101")
                if failure == "checksum":
                    number = number[:-1] + ("0" if number[-1] != "0" else "1")
                if failure == "birthday":
                    number = test_identity("19900231")
                if failure == "category":
                    config.visitors[0]["category"] = "adult"
                slot = "b" * 32 if failure == "slot" else config.visitors[0]["id"]
                payload = TicketSubmitRequest(version=1, phone="13800138000", visitors=[
                    {"id": slot, "name": "测试游客", "id_number": number},
                ])
                with self.assertRaises(TicketRegistrationError):
                    await service.submit("token", payload)
                self.assertIsNone(config.identity_ciphertext)
                session.commit.assert_not_awaited()

    async def test_legacy_wrong_ticket_category_cannot_bypass_age_rules(self):
        service, session, config = self.make_service(category="adult")
        payload = TicketSubmitRequest(version=1, phone="13800138000", visitors=[
            {"id": config.visitors[0]["id"], "name": "测试游客", "id_number": test_identity("19900101")},
        ])
        with self.assertRaises(TicketRegistrationError) as error:
            await service.submit("token", payload)
        self.assertIn("票种与游客分类不一致", str(error.exception))
        session.commit.assert_not_awaited()

    async def test_admin_cannot_assign_adult_ticket_to_senior_slot(self):
        service, session, config = self.make_service()
        session.execute.return_value = SimpleNamespace(scalars=lambda: [SimpleNamespace(id=3, name="1日成人票", category="adult")])
        payload = TicketConfigRequest(version=1, visit_date=config.visit_date, visitors=[
            {"id": config.visitors[0]["id"], "category": "senior", "amount": "10", "ticket_type_id": "3"},
        ])
        with self.assertRaises(TicketRegistrationError) as error:
            await service.save(1, payload)
        self.assertIn("老人只能选择对应的老人票种", str(error.exception))
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
