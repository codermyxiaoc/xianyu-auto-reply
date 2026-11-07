"""年龄策略继承、用户隔离和权限测试；只使用内存 SQLite 合成数据。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

import common.models  # 注册 User 关联模型，不访问业务数据库。
from app.api import deps
from app.api.routes.ticket_registration import router
from app.api.routes.user_settings import UserSettingUpdate, delete_user_setting, update_user_setting
from app.services.ticket_age_policy_service import TicketAgePolicyService
from app.services.ticket_registration_errors import TicketRegistrationError
from common.models.system_setting import SystemSetting
from common.models.user import User, UserRole, UserStatus
from common.models.user_setting import UserSetting
from common.schemas.ticket_age_policy import (
    PERSONAL_TICKET_AGE_POLICY_SETTING, TICKET_AGE_ENABLED_SETTING,
    PersonalTicketAgePolicy, TicketAgePolicy,
)
from common.schemas.ticket_registration import (
    DEFAULT_TICKET_AGE_RULES, TICKET_AGE_RULES_SETTING, TicketAgeRules,
)


ALTERNATIVE_RULES = {
    "child": {"min_age": 0, "max_age": 11},
    "adult": {"min_age": 12, "max_age": 64},
    "senior": {"min_age": 65, "max_age": None},
}


def system_payload(enabled=True, rules=None):
    return TicketAgePolicy(enabled=enabled, rules=deepcopy(rules or DEFAULT_TICKET_AGE_RULES))


def personal_payload(enabled=True, *, use_personal=True, rules=None):
    return PersonalTicketAgePolicy(
        use_personal=use_personal, enabled=enabled,
        rules=deepcopy(rules or DEFAULT_TICKET_AGE_RULES),
    )


class AsyncSessionAdapter:
    """执行真实 SQL，并复用项目现有同步 SQLite 测试方式。"""

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


class TicketAgePolicySchemaTests(unittest.TestCase):
    def test_boolean_switches_are_strict_and_unknown_fields_are_rejected(self):
        for model, payload, fields in (
            (TicketAgePolicy, system_payload().model_dump(), ("enabled",)),
            (PersonalTicketAgePolicy, personal_payload().model_dump(), ("enabled", "use_personal")),
        ):
            for field in fields:
                for value in (0, 1, "true", "false", "0", None, [], {}):
                    with self.subTest(model=model.__name__, field=field, value=value):
                        with self.assertRaises(ValidationError):
                            model.model_validate({**payload, field: value})
                for value in (True, False):
                    self.assertIs(getattr(model.model_validate({**payload, field: value}), field), value)
            with self.assertRaises(ValidationError):
                model.model_validate({**payload, "user_id": 3})

    def test_ranges_remain_strict_even_when_validation_is_disabled(self):
        invalid_rules = []
        for value in (-1, 151, 1.5, "1", True, None):
            rules = deepcopy(DEFAULT_TICKET_AGE_RULES)
            rules["child"]["min_age"] = value
            invalid_rules.append(rules)
        for change in (
            {"min_age": 18, "max_age": 17},
            {"min_age": 0, "max_age": 18},
            {"min_age": 0, "max_age": None},
            {"min_age": 0, "max_age": 17, "unexpected": 1},
        ):
            rules = deepcopy(DEFAULT_TICKET_AGE_RULES)
            rules["child"] = change
            invalid_rules.append(rules)
        invalid_rules.append({"child": DEFAULT_TICKET_AGE_RULES["child"]})
        for rules in invalid_rules:
            with self.subTest(rules=rules):
                with self.assertRaises(ValidationError):
                    system_payload(False, rules)
                with self.assertRaises(ValidationError):
                    personal_payload(False, use_personal=False, rules=rules)
        self.assertIsNone(system_payload().rules.senior.max_age)


class TicketAgePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (SystemSetting, UserSetting, User):
            model.__table__.create(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.users = {}
        for identifier in (1, 2, 3):
            user = User(
                id=identifier, username=f"synthetic-user-{identifier}",
                email=f"synthetic-{identifier}@example.test", password_hash="synthetic-password-hash",
                role=UserRole.ADMIN if identifier == 1 else UserRole.MEMBER,
                status=UserStatus.ACTIVE,
            )
            self.users[identifier] = user
            self.session.add(user)
        self.session.commit()
        self.adapter = AsyncSessionAdapter(self.session)
        self.service = TicketAgePolicyService(self.adapter)
        self.current_user = self.users[2]
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        # 保留真实 active/admin 检查，只替换认证结果，避免需要真实 JWT 或 Redis。
        self.app.dependency_overrides[deps.get_current_user] = lambda: self.current_user
        self.app.dependency_overrides[deps.get_db_session] = lambda: self.adapter
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.session.close()
        self.engine.dispose()

    def personal_rows(self, user_id):
        return self.session.execute(select(UserSetting).where(
            UserSetting.user_id == user_id,
            UserSetting.key == PERSONAL_TICKET_AGE_POLICY_SETTING,
        ).order_by(UserSetting.id)).scalars().all()

    def insert_personal(self, user_id, value):
        self.session.add(UserSetting(user_id=user_id, key=PERSONAL_TICKET_AGE_POLICY_SETTING, value=value))
        self.session.commit()

    async def test_absent_settings_default_to_enabled_and_system_ranges(self):
        expected = {"enabled": True, "rules": DEFAULT_TICKET_AGE_RULES}
        self.assertEqual(await self.service.system(), expected)
        for owner_id in (1, 2, 3):
            self.assertEqual(await self.service.personal(owner_id), {"use_personal": False, **expected})
            self.assertEqual(await self.service.effective(owner_id), expected)
        self.assertEqual(self.session.execute(select(SystemSetting)).scalars().all(), [])
        self.assertEqual(self.personal_rows(2), [])

    async def test_personal_false_overrides_system_true_and_isolates_order_owners(self):
        await self.service.save_system(system_payload(True))
        expected = personal_payload(False, rules=ALTERNATIVE_RULES)
        await self.service.save_personal(2, expected)
        # 管理员 id=1 以及另一个订单所属用户 id=3 仍然使用系统策略。
        self.assertEqual(await self.service.effective(2), {"enabled": False, "rules": ALTERNATIVE_RULES})
        for owner_id in (1, 3):
            self.assertEqual(await self.service.effective(owner_id), system_payload().model_dump())
        self.assertEqual(await self.service.personal(2), expected.model_dump())

    async def test_personal_true_overrides_system_false_with_entire_rule_set(self):
        await self.service.save_system(system_payload(False))
        await self.service.save_personal(3, personal_payload(True, rules=ALTERNATIVE_RULES))
        self.assertEqual(await self.service.effective(3), {"enabled": True, "rules": ALTERNATIVE_RULES})
        self.assertEqual(await self.service.effective(2), system_payload(False).model_dump())
        # 更新系统设置后，个人范围与开关仍保持完整覆盖。
        await self.service.save_system(system_payload(True))
        self.assertEqual(await self.service.effective(3), {"enabled": True, "rules": ALTERNATIVE_RULES})

    async def test_inherit_removes_duplicates_only_for_owner_and_follows_future_changes(self):
        self.insert_personal(2, personal_payload(False).model_dump_json())
        self.insert_personal(2, personal_payload(True, rules=ALTERNATIVE_RULES).model_dump_json())
        await self.service.save_personal(3, personal_payload(False, rules=ALTERNATIVE_RULES))
        self.session.add(UserSetting(user_id=2, key="unrelated.setting", value="keep"))
        self.session.commit()
        result = await self.service.save_personal(2, personal_payload(False, use_personal=False, rules=ALTERNATIVE_RULES))
        self.assertEqual(result, {"use_personal": False, **system_payload().model_dump()})
        self.assertEqual(self.personal_rows(2), [])
        self.assertEqual(len(self.personal_rows(3)), 1)
        self.assertEqual(self.session.execute(select(UserSetting.value).where(
            UserSetting.user_id == 2, UserSetting.key == "unrelated.setting",
        )).scalar_one(), "keep")
        await self.service.save_system(system_payload(False, ALTERNATIVE_RULES))
        self.assertEqual(await self.service.effective(2), system_payload(False, ALTERNATIVE_RULES).model_dump())
        await self.service.save_system(system_payload(True))
        self.assertEqual(await self.service.effective(2), system_payload(True).model_dump())
        self.assertEqual(await self.service.effective(3), system_payload(False, ALTERNATIVE_RULES).model_dump())

    async def test_latest_duplicate_wins_and_save_collapses_duplicates(self):
        self.insert_personal(2, personal_payload(True).model_dump_json())
        latest = personal_payload(False, rules=ALTERNATIVE_RULES)
        self.insert_personal(2, latest.model_dump_json())
        self.assertEqual(await self.service.personal(2), latest.model_dump())
        replacement = personal_payload(True, rules=ALTERNATIVE_RULES)
        await self.service.save_personal(2, replacement)
        rows = self.personal_rows(2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0].value), replacement.model_dump())

    async def test_legacy_inherit_record_uses_current_system_instead_of_stored_snapshot(self):
        self.insert_personal(2, personal_payload(False, use_personal=False, rules=ALTERNATIVE_RULES).model_dump_json())
        self.assertEqual(await self.service.effective(2), system_payload().model_dump())
        await self.service.save_system(system_payload(False, ALTERNATIVE_RULES))
        self.assertEqual(await self.service.effective(2), system_payload(False, ALTERNATIVE_RULES).model_dump())

    async def test_legacy_range_save_preserves_disabled_switch(self):
        await self.service.save_system(system_payload(False))
        await self.service.save_rules(TicketAgeRules.model_validate(ALTERNATIVE_RULES))
        self.assertEqual(await self.service.system(), system_payload(False, ALTERNATIVE_RULES).model_dump())

    async def test_invalid_system_storage_fails_closed_and_can_be_repaired(self):
        for key, value in (
            (TICKET_AGE_ENABLED_SETTING, '"false"'),
            (TICKET_AGE_ENABLED_SETTING, "0"),
            (TICKET_AGE_ENABLED_SETTING, "null"),
            (TICKET_AGE_ENABLED_SETTING, "invalid-json"),
            (TICKET_AGE_RULES_SETTING, "{}"),
            (TICKET_AGE_RULES_SETTING, "null"),
        ):
            with self.subTest(key=key, value=value):
                self.session.execute(delete(SystemSetting))
                self.session.add(SystemSetting(key=key, value=value))
                self.session.commit()
                with self.assertRaises(TicketRegistrationError) as caught:
                    await self.service.effective(2)
                self.assertEqual(caught.exception.code, 503)
                await self.service.save_system(system_payload())
                self.assertEqual(await self.service.system(), system_payload().model_dump())

    async def test_invalid_latest_personal_storage_never_falls_back_to_older_value(self):
        for value in ("invalid-json", "{}", "null", json.dumps({**personal_payload().model_dump(), "enabled": "false"})):
            with self.subTest(value=value):
                await self.service.save_personal(2, personal_payload(False))
                self.insert_personal(2, value)
                with self.assertRaises(TicketRegistrationError) as caught:
                    await self.service.effective(2)
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(await self.service.effective(3), system_payload().model_dump())
                await self.service.save_personal(2, personal_payload(use_personal=False))
                self.assertEqual(await self.service.effective(2), system_payload().model_dump())

    async def test_missing_owner_save_does_not_create_or_delete_other_user_settings(self):
        await self.service.save_personal(2, personal_payload(False))
        with self.assertRaises(TicketRegistrationError) as caught:
            await self.service.save_personal(999, personal_payload())
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.personal_rows(999), [])
        self.assertEqual(await self.service.effective(2), system_payload(False).model_dump())

    async def test_member_can_save_only_own_personal_policy_and_inherit_system(self):
        url = "/api/v1/ticket-registration/personal-age-policy"
        payload = personal_payload(False, rules=ALTERNATIVE_RULES).model_dump()
        response = await self.client.put(url, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["data"], payload)
        self.assertEqual((await self.client.get(url)).json()["data"], payload)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(await self.service.effective(3), system_payload().model_dump())
        self.current_user = self.users[3]
        self.assertFalse((await self.client.get(url)).json()["data"]["use_personal"])
        self.current_user = self.users[2]
        # URL 查询参数不参与所有者选择；请求体多余 user_id 必须拒绝。
        response = await self.client.put(url + "?user_id=3", json=personal_payload(True).model_dump())
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.personal_rows(3), [])
        response = await self.client.put(url, json={**payload, "user_id": 3})
        self.assertEqual(response.json()["code"], 400)
        self.assertEqual(self.personal_rows(3), [])
        response = await self.client.put(url, json=personal_payload(use_personal=False).model_dump())
        self.assertFalse(response.json()["data"]["use_personal"])
        self.assertEqual(self.personal_rows(2), [])

    async def test_system_policy_requires_admin_and_strict_http_payload(self):
        url = "/api/v1/ticket-registration/age-policy"
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": system_payload(False).model_dump()} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 403)
            self.assertFalse(response.json()["success"])
        self.assertEqual(await self.service.system(), system_payload().model_dump())
        self.current_user = self.users[1]
        response = await self.client.put(url, json=system_payload(False, ALTERNATIVE_RULES).model_dump())
        self.assertTrue(response.json()["success"])
        self.assertEqual((await self.client.get(url)).json()["data"], system_payload(False, ALTERNATIVE_RULES).model_dump())
        for extra in ({"enabled": "false"}, {"enabled": 0}, {"user_id": 2}):
            response = await self.client.put(url, json={**system_payload().model_dump(), **extra})
            self.assertEqual(response.json()["code"], 400)
        self.assertEqual(await self.service.system(), system_payload(False, ALTERNATIVE_RULES).model_dump())

    async def test_minimal_inherit_request_recovers_corrupt_personal_policy_and_requires_false_boolean(self):
        url = "/api/v1/ticket-registration/personal-age-policy"
        await self.service.save_system(system_payload(False, ALTERNATIVE_RULES))
        self.insert_personal(2, "invalid-personal-json")
        self.assertEqual((await self.client.get(url)).json()["code"], 503)
        for payload in (
            {"use_personal": 0}, {"use_personal": "false"}, {"use_personal": True},
            {"use_personal": None}, {"use_personal": False, "user_id": 3},
        ):
            with self.subTest(payload=payload):
                response = await self.client.put(url, json=payload)
                self.assertEqual(response.json()["code"], 400)
                self.assertEqual(len(self.personal_rows(2)), 1)
        response = await self.client.put(url, json={"use_personal": False})
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["data"], {
            "use_personal": False, **system_payload(False, ALTERNATIVE_RULES).model_dump(),
        })
        self.assertEqual(self.personal_rows(2), [])
        self.assertEqual((await self.client.get(url)).json()["data"], response.json()["data"])

    async def test_generic_settings_cannot_bypass_personal_policy_validation_using_equivalent_keys(self):
        # MySQL 不敏感排序规则可能把这些拼写视为同一个 key，必须在任何 SQL 前阻止。
        for key in (
            PERSONAL_TICKET_AGE_POLICY_SETTING,
            PERSONAL_TICKET_AGE_POLICY_SETTING.upper(),
            PERSONAL_TICKET_AGE_POLICY_SETTING + " ",
            "tícket_registration.age_policy",
            "ti\u0301cket_registration.age_policy",
        ):
            with self.subTest(key=key):
                session = SimpleNamespace(
                    execute=AsyncMock(side_effect=AssertionError("generic settings must not access the policy row")),
                    commit=AsyncMock(),
                )
                result = await update_user_setting(
                    key, UserSettingUpdate(value="invalid-json"), self.users[2], session,
                )
                self.assertFalse(result.success)
                self.assertEqual(result.code, 40001)
                result = await delete_user_setting(key, self.users[2], session)
                self.assertFalse(result.success)
                self.assertEqual(result.code, 40001)
                session.execute.assert_not_awaited()
                session.commit.assert_not_awaited()

    async def test_anonymous_and_inactive_users_cannot_read_or_write_personal_policy(self):
        url = "/api/v1/ticket-registration/personal-age-policy"
        self.current_user.status = UserStatus.INACTIVE
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": personal_payload().model_dump()} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 403)
        self.app.dependency_overrides.pop(deps.get_current_user)
        for method in ("GET", "PUT"):
            response = await self.client.request(method, url, **({"json": personal_payload().model_dump()} if method == "PUT" else {}))
            self.assertEqual(response.json()["code"], 401)
        self.assertEqual(self.personal_rows(2), [])


if __name__ == "__main__":
    unittest.main()
