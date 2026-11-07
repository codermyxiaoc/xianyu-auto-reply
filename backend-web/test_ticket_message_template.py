"""门票消息模板的校验、继承和权限测试；仅使用合成数据和内存 SQLite。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

import common.models  # 加载 User 的关联模型，不连接业务数据库。
from app.api import deps
from app.api.routes.ticket_registration import router
from app.api.routes.user_settings import UserSettingUpdate, delete_user_setting, update_user_setting
from app.services.ticket_age_policy_service import TicketAgePolicyService
from app.services.ticket_message_template_service import TicketMessageTemplateService, render_ticket_message
from app.services.ticket_registration_errors import TicketRegistrationError
from common.models.system_setting import SystemSetting
from common.models.user import User, UserRole, UserStatus
from common.models.user_setting import UserSetting
from common.schemas.ticket_age_policy import PersonalTicketAgePolicy, TicketAgePolicy
from common.schemas.ticket_message_template import (
    DEFAULT_TICKET_MESSAGE_TEMPLATE, PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
    TICKET_MESSAGE_TEMPLATE_SETTING, InheritTicketMessageTemplate,
    PersonalTicketMessageTemplate, PersonalTicketMessageTemplateRequest, TicketMessageTemplate,
)
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES


SYSTEM_MESSAGE = "亲爱的买家，订单 {orderId}\n请填写门票信息：{infourl}\n感谢配合。"
PERSONAL_MESSAGE = "我的门票登记地址：{infourl}\n订单编号 {orderId}"


def personal_payload(message=PERSONAL_MESSAGE, *, use_personal=True):
    return PersonalTicketMessageTemplate(use_personal=use_personal, message_template=message)


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, instance):
        self.session.add(instance)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


class TicketMessageTemplateSchemaTests(unittest.TestCase):
    def test_template_requires_exact_link_placeholder_and_rejects_invalid_braces(self):
        for message in (
            "", "  \n\t", "只有订单 {orderId}", "{infoUrl}",
            "{infourl} {unknown}", "{infourl} {order_id}",
            "{infourl} {orderId.__class__}", "{infourl} {orderId!r}",
            "{infourl} {orderId:>10}", "{{infourl}}", "{infourl} {{orderId}}",
            "{infourl} {", "{infourl} }", "{infourl} {}", "{infourl} {orderId",
        ):
            with self.subTest(message=message):
                for model, extra in ((TicketMessageTemplate, {}), (PersonalTicketMessageTemplate, {"use_personal": True})):
                    with self.assertRaises(ValidationError):
                        model.model_validate({"message_template": message, **extra})
        self.assertEqual(TicketMessageTemplate(message_template="只需填写：{infourl}").message_template, "只需填写：{infourl}")

    def test_strict_string_length_boundary_and_original_chinese_whitespace(self):
        for invalid in (0, False, True, None, [], {}, b"{infourl}"):
            with self.subTest(value=invalid):
                with self.assertRaises(ValidationError):
                    TicketMessageTemplate(message_template=invalid)
        exactly_2000 = "中" * (2000 - len("{infourl}")) + "{infourl}"
        self.assertEqual(TicketMessageTemplate(message_template=exactly_2000).message_template, exactly_2000)
        with self.assertRaises(ValidationError):
            TicketMessageTemplate(message_template=exactly_2000 + "中")
        message = "  尊敬的用户\r\n订单：{orderId}\n\n请填写：{infourl}\n【温馨提示】｛中文全角括号｝  "
        self.assertEqual(TicketMessageTemplate(message_template=message).message_template, message)
        self.assertEqual(personal_payload(message).message_template, message)

    def test_personal_switch_and_minimal_inheritance_are_strict(self):
        request = TypeAdapter(PersonalTicketMessageTemplateRequest)
        self.assertIs(request.validate_python({"use_personal": False}).use_personal, False)
        for value in (0, 1, "false", "true", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    request.validate_python({"use_personal": value, "message_template": PERSONAL_MESSAGE})
                with self.assertRaises(ValidationError):
                    request.validate_python({"use_personal": value})
        for payload in (
            {"use_personal": True}, {"use_personal": False, "user_id": 3},
            {"use_personal": True, "message_template": PERSONAL_MESSAGE, "user_id": 3},
            {"message_template": SYSTEM_MESSAGE, "enabled": False},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    request.validate_python(payload)
        with self.assertRaises(ValidationError):
            TicketMessageTemplate.model_validate({"message_template": SYSTEM_MESSAGE, "user_id": 3})

    def test_literal_render_preserves_newlines_all_placeholders_and_order_number(self):
        template = "订单 {orderId}\n链接 {infourl}\n重复 {orderId} / {infourl}"
        order_no = "9223372036854775806"
        link = "https://tickets.example.test/ticket-registration#synthetic-token"
        self.assertEqual(render_ticket_message(template, order_no, link),
                         f"订单 {order_no}\n链接 {link}\n重复 {order_no} / {link}")
        # 替换值中的占位符不是表达式，不再次求值，也不格式化其中的大括号。
        literal_order = "SYNTH-{infourl}-{orderId.__class__}"
        self.assertEqual(render_ticket_message("{orderId}\n{infourl}", literal_order, link), literal_order + "\n" + link)
        literal_link = "https://tickets.example.test/{orderId}#synthetic-{infourl}"
        self.assertEqual(render_ticket_message("{orderId}\n{infourl}", "{infourl}", literal_link), "{infourl}\n" + literal_link)
        self.assertEqual(render_ticket_message("只有链接 {infourl}", order_no, link), "只有链接 " + link)

    def test_default_template_preserves_previous_fixed_message(self):
        order_no, link = "SYNTH-001", "https://tickets.example.test/ticket-registration#synthetic"
        self.assertEqual(render_ticket_message(DEFAULT_TICKET_MESSAGE_TEMPLATE, order_no, link),
                         f"请填写订单 {order_no} 的门票实名信息：每位游客的姓名、身份证，以及一个整单共用的联系手机号。\n{link}")


class TicketMessageTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (SystemSetting, UserSetting, User):
            model.__table__.create(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.users = {}
        for identifier in (1, 2, 3):
            user = User(id=identifier, username=f"synthetic-user-{identifier}",
                        email=f"synthetic-{identifier}@example.test", password_hash="synthetic-hash",
                        role=UserRole.ADMIN if identifier == 1 else UserRole.MEMBER,
                        status=UserStatus.ACTIVE)
            self.users[identifier] = user
            self.session.add(user)
        self.session.commit()
        self.adapter = AsyncSessionAdapter(self.session)
        self.service = TicketMessageTemplateService(self.adapter)
        self.current_user = self.users[2]
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[deps.get_current_user] = lambda: self.current_user
        self.app.dependency_overrides[deps.get_db_session] = lambda: self.adapter
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session.close()
        self.engine.dispose()

    def personal_rows(self, user_id):
        return self.session.execute(select(UserSetting).where(
            UserSetting.user_id == user_id, UserSetting.key == PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
        ).order_by(UserSetting.id)).scalars().all()

    def insert_personal(self, user_id, value):
        self.session.add(UserSetting(user_id=user_id, key=PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING, value=value))
        self.session.commit()

    async def test_absent_settings_use_previous_default_without_writing_rows(self):
        self.assertEqual(await self.service.system(), {"message_template": DEFAULT_TICKET_MESSAGE_TEMPLATE})
        for owner_id in (1, 2, 3):
            self.assertEqual(await self.service.personal(owner_id), {
                "use_personal": False, "message_template": DEFAULT_TICKET_MESSAGE_TEMPLATE,
            })
            self.assertEqual(await self.service.effective(owner_id), DEFAULT_TICKET_MESSAGE_TEMPLATE)
        self.assertEqual(self.session.execute(select(SystemSetting)).scalars().all(), [])
        self.assertEqual(self.personal_rows(2), [])

    async def test_personal_template_overrides_only_its_order_owner_and_preserves_newlines(self):
        await self.service.save_system(TicketMessageTemplate(message_template=SYSTEM_MESSAGE))
        message = "  买家您好\n\n" + PERSONAL_MESSAGE + "\n  "
        expected = personal_payload(message)
        self.assertEqual(await self.service.save_personal(2, expected), expected.model_dump())
        self.assertEqual(await self.service.personal(2), expected.model_dump())
        self.assertEqual(await self.service.effective(2), message)
        for other_owner in (1, 3):
            self.assertEqual(await self.service.effective(other_owner), SYSTEM_MESSAGE)
        updated_system = "系统新文案\n{infourl}"
        await self.service.save_system(TicketMessageTemplate(message_template=updated_system))
        self.assertEqual(await self.service.effective(2), message)
        self.assertEqual(await self.service.effective(3), updated_system)

    async def test_template_and_age_overrides_are_independent_in_both_directions(self):
        age_service = TicketAgePolicyService(self.adapter)
        await age_service.save_system(TicketAgePolicy(enabled=True, rules=DEFAULT_TICKET_AGE_RULES))
        await age_service.save_personal(2, PersonalTicketAgePolicy(
            use_personal=True, enabled=False, rules=DEFAULT_TICKET_AGE_RULES,
        ))
        await self.service.save_personal(2, personal_payload())
        await self.service.save_personal(2, InheritTicketMessageTemplate(use_personal=False))
        self.assertFalse((await age_service.effective(2))["enabled"])
        await self.service.save_personal(2, personal_payload())
        await age_service.save_personal(2, PersonalTicketAgePolicy(
            use_personal=False, enabled=True, rules=DEFAULT_TICKET_AGE_RULES,
        ))
        self.assertEqual(await self.service.effective(2), PERSONAL_MESSAGE)
        self.assertTrue((await age_service.effective(2))["enabled"])

    async def test_latest_duplicate_wins_and_save_collapses_only_matching_owner_key(self):
        self.insert_personal(2, personal_payload("旧模板 {infourl}").model_dump_json())
        self.insert_personal(2, personal_payload().model_dump_json())
        await self.service.save_personal(3, personal_payload("用户三 {infourl}"))
        self.session.add(UserSetting(user_id=2, key="unrelated.setting", value="keep"))
        self.session.commit()
        self.assertEqual(await self.service.effective(2), PERSONAL_MESSAGE)
        replacement = personal_payload("替换 {orderId}\n{infourl}")
        await self.service.save_personal(2, replacement)
        self.assertEqual(len(self.personal_rows(2)), 1)
        self.assertEqual(json.loads(self.personal_rows(2)[0].value), replacement.model_dump())
        self.assertEqual(await self.service.effective(3), "用户三 {infourl}")
        self.assertEqual(self.session.execute(select(UserSetting.value).where(
            UserSetting.user_id == 2, UserSetting.key == "unrelated.setting",
        )).scalar_one(), "keep")

    async def test_inherit_removes_all_override_rows_and_tracks_future_system_changes(self):
        self.insert_personal(2, personal_payload("旧文案 {infourl}").model_dump_json())
        self.insert_personal(2, personal_payload().model_dump_json())
        await self.service.save_personal(3, personal_payload())
        await self.service.save_system(TicketMessageTemplate(message_template=SYSTEM_MESSAGE))
        result = await self.service.save_personal(2, InheritTicketMessageTemplate(use_personal=False))
        self.assertEqual(result, {"use_personal": False, "message_template": SYSTEM_MESSAGE})
        self.assertEqual(self.personal_rows(2), [])
        self.assertEqual(len(self.personal_rows(3)), 1)
        updated = "新的系统设置 {infourl}"
        await self.service.save_system(TicketMessageTemplate(message_template=updated))
        self.assertEqual(await self.service.effective(2), updated)
        self.assertEqual(await self.service.effective(3), PERSONAL_MESSAGE)
        self.insert_personal(2, personal_payload("旧快照 {infourl}", use_personal=False).model_dump_json())
        self.assertEqual(await self.service.effective(2), updated)

    async def test_invalid_system_storage_fails_closed_and_save_repairs_it(self):
        for value in ("", "缺少链接", "{infourl} {unsupported}", "{infourl}" + "字" * 2000):
            with self.subTest(value=value[:30]):
                self.session.execute(delete(SystemSetting).where(SystemSetting.key == TICKET_MESSAGE_TEMPLATE_SETTING))
                self.session.add(SystemSetting(key=TICKET_MESSAGE_TEMPLATE_SETTING, value=value))
                self.session.commit()
                with self.assertRaises(TicketRegistrationError) as caught:
                    await self.service.effective(2)
                self.assertEqual(caught.exception.code, 503)
                await self.service.save_system(TicketMessageTemplate(message_template=SYSTEM_MESSAGE))
                self.assertEqual(await self.service.effective(2), SYSTEM_MESSAGE)

    async def test_invalid_latest_personal_storage_never_uses_older_or_system_value(self):
        for value in ("invalid-json", "{}", "null", json.dumps({"use_personal": True, "message_template": "缺少链接"})):
            with self.subTest(value=value):
                await self.service.save_personal(2, personal_payload())
                self.insert_personal(2, value)
                with self.assertRaises(TicketRegistrationError) as caught:
                    await self.service.effective(2)
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(await self.service.effective(3), DEFAULT_TICKET_MESSAGE_TEMPLATE)
                await self.service.save_personal(2, InheritTicketMessageTemplate(use_personal=False))
                self.assertEqual(await self.service.effective(2), DEFAULT_TICKET_MESSAGE_TEMPLATE)

    async def test_missing_owner_save_does_not_touch_existing_user_templates(self):
        await self.service.save_personal(2, personal_payload())
        with self.assertRaises(TicketRegistrationError) as caught:
            await self.service.save_personal(999, personal_payload())
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.personal_rows(999), [])
        self.assertEqual(await self.service.effective(2), PERSONAL_MESSAGE)

    async def test_http_member_can_edit_only_own_template_and_restore_corrupt_override(self):
        url = "/api/v1/ticket-registration/personal-message-template"
        response = await self.client.put(url, json=personal_payload().model_dump())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["data"], personal_payload().model_dump())
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual((await self.client.get(url)).json()["data"], personal_payload().model_dump())
        self.current_user = self.users[3]
        self.assertFalse((await self.client.get(url)).json()["data"]["use_personal"])
        self.current_user = self.users[2]
        response = await self.client.put(url + "?user_id=3", json=personal_payload("我的新文案 {infourl}").model_dump())
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.personal_rows(3), [])
        response = await self.client.put(url, json={**personal_payload().model_dump(), "user_id": 3})
        self.assertEqual(response.json()["code"], 400)
        self.assertEqual(await self.service.effective(2), "我的新文案 {infourl}")
        self.insert_personal(2, "invalid-json")
        self.assertEqual((await self.client.get(url)).json()["code"], 503)
        await self.service.save_system(TicketMessageTemplate(message_template=SYSTEM_MESSAGE))
        response = await self.client.put(url, json={"use_personal": False})
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["data"], {"use_personal": False, "message_template": SYSTEM_MESSAGE})
        self.assertEqual(self.personal_rows(2), [])

    async def test_http_system_template_requires_admin_and_rejects_invalid_payloads_without_mutation(self):
        url = "/api/v1/ticket-registration/message-template"
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": {"message_template": SYSTEM_MESSAGE}} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 403)
        self.assertEqual(await self.service.effective(2), DEFAULT_TICKET_MESSAGE_TEMPLATE)
        self.current_user = self.users[1]
        response = await self.client.put(url, json={"message_template": SYSTEM_MESSAGE})
        self.assertTrue(response.json()["success"])
        self.assertEqual((await self.client.get(url)).json()["data"], {"message_template": SYSTEM_MESSAGE})
        for payload in (
            {"message_template": "缺少链接"}, {"message_template": "{infourl} {bad}"},
            {"message_template": False}, {"message_template": SYSTEM_MESSAGE, "user_id": 2},
        ):
            with self.subTest(payload=payload):
                response = await self.client.put(url, json=payload)
                self.assertEqual(response.json()["code"], 400)
                self.assertEqual(await self.service.effective(2), SYSTEM_MESSAGE)

    async def test_http_personal_validation_and_anonymous_inactive_permissions(self):
        url = "/api/v1/ticket-registration/personal-message-template"
        await self.service.save_personal(2, personal_payload())
        for payload in (
            {"use_personal": 0}, {"use_personal": "false"}, {"use_personal": True},
            {"use_personal": False, "user_id": 3},
            {"use_personal": True, "message_template": "缺少链接"},
        ):
            with self.subTest(payload=payload):
                response = await self.client.put(url, json=payload)
                self.assertEqual(response.json()["code"], 400)
                self.assertEqual(await self.service.effective(2), PERSONAL_MESSAGE)
        self.current_user.status = UserStatus.INACTIVE
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": {"use_personal": False}} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 403)
        self.app.dependency_overrides.pop(deps.get_current_user)
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": {"use_personal": False}} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 401)
        self.assertEqual(await self.service.effective(2), PERSONAL_MESSAGE)

    async def test_generic_settings_reject_equivalent_template_keys_before_sql(self):
        for key in (
            PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
            PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING.upper(),
            PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING + " ",
            "tícket_registration.message_template", "ti\u0301cket_registration.message_template",
        ):
            with self.subTest(key=key):
                session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("template bypass must not execute SQL")), commit=AsyncMock())
                updated = await update_user_setting(key, UserSettingUpdate(value="invalid"), self.users[2], session)
                deleted = await delete_user_setting(key, self.users[2], session)
                self.assertFalse(updated.success)
                self.assertEqual(updated.code, 40001)
                self.assertFalse(deleted.success)
                self.assertEqual(deleted.code, 40001)
                session.execute.assert_not_awaited()
                session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
