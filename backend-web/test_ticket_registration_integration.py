"""门票功能的隔离 MySQL 集成测试，不向真实买家发送消息。

设置 TICKET_TEST_MYSQL_URL 为有建库权限的 mysql+asyncmy URL 后运行：
    python -m unittest test_ticket_registration_integration -v
每个测试只创建、删除随机命名的 xianyu_ticket_test_* 数据库。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import deps
from app.api.routes import ticket_registration as routes
from app.services.ticket_registration_service import websocket_client
from common.db.base_class import Base
from common.db.ticket_registration_schema import DEFAULT_TICKET_TYPES, TICKET_KEY_SETTING, ensure_ticket_registration_schema
from common.models.system_setting import SystemSetting
from common.models.ticket_registration import TicketRegistration, TicketType
from common.models.user import User, UserRole, UserStatus
from common.models.user_setting import UserSetting
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES, TICKET_AGE_RULES_SETTING
from common.schemas.ticket_age_policy import PERSONAL_TICKET_AGE_POLICY_SETTING, TICKET_AGE_ENABLED_SETTING
from common.schemas.ticket_message_template import PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING, TICKET_MESSAGE_TEMPLATE_SETTING
from common.services.redis_rate_limit_service import RateLimitResult
from common.utils.time_utils import get_beijing_now


def fictional_identity(birth: str, sequence: str) -> str:
    prefix = '990000' + birth + sequence
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return prefix + '10X98765432'[sum(int(n) * w for n, w in zip(prefix, weights)) % 11]


@unittest.skipUnless(os.getenv('TICKET_TEST_MYSQL_URL'), '需要显式设置隔离测试用 MySQL URL')
class TicketRegistrationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = 'xianyu_ticket_test_' + uuid.uuid4().hex[:16]
        admin_url = make_url(os.environ['TICKET_TEST_MYSQL_URL']).set(database=None)
        self.admin_engine = create_async_engine(admin_url, echo=False)
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE DATABASE `{self.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'))
        self.engine = create_async_engine(admin_url.set(database=self.database), echo=False)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[SystemSetting.__table__, User.__table__, UserSetting.__table__, XYOrder.__table__, XYAccount.__table__]))
            await ensure_ticket_registration_schema(connection)
        async with self.sessions() as session:
            session.add_all([
                User(id=1, username='ticket-test-admin', email='admin@example.test', password_hash='synthetic-hash', role=UserRole.ADMIN, status=UserStatus.ACTIVE),
                User(id=2, username='ticket-test-member', email='member@example.test', password_hash='synthetic-hash', role=UserRole.MEMBER, status=UserStatus.ACTIVE),
            ])
            await session.commit()
        self.app = FastAPI()
        self.app.include_router(routes.router, prefix='/api/v1')

        async def session_dependency():
            async with self.sessions() as session:
                yield session

        self.app.dependency_overrides[deps.get_db_session] = session_dependency
        self.admin = User(id=1, username='ticket-test-admin', role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        self.limit = patch.object(routes, 'check_sliding_window_limit', new=AsyncMock(return_value=RateLimitResult(True, 100, 0)))
        self.limit_mock = self.limit.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://backend.test')
        self.origin = 'https://tickets.example.test:9443'
        self.headers = {'X-Frontend-Origin': self.origin}
        self.prefix = '/api/v1/ticket-registration'
        self.visit_date = (get_beijing_now().date() + timedelta(days=7)).isoformat()
        self.identities = [fictional_identity('19900101', '001'), fictional_identity('20160101', '002')]
        self.phone = '13800138000'

    async def asyncTearDown(self):
        self.limit.stop()
        await self.client.aclose()
        await self.engine.dispose()
        # 名称由本测试生成；拒绝对指定业务库或任何其他名称执行清理。
        if not re.fullmatch(r'xianyu_ticket_test_[a-f0-9]{16}', self.database):
            raise AssertionError('Unexpected test database name')
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP DATABASE `{self.database}`'))
        await self.admin_engine.dispose()

    async def order(self, status='pending_ship', *, account_id=None, order_no=None, owner_id=1):
        account_id = account_id or 'test-' + uuid.uuid4().hex[:8]
        async with self.sessions() as session:
            session.add(XYAccount(owner_id=owner_id, account_id=account_id, cookie='', login_method='manual', status='active'))
            order = XYOrder(owner_id=owner_id, account_id=account_id, order_no=order_no or 'TEST-' + uuid.uuid4().hex,
                            status=status, buyer_id='test-buyer', item_id='test-item', amount='120.00', quantity=2)
            session.add(order)
            await session.commit()
            return str(order.id)

    def config_payload(self, version=0):
        return {'version': version, 'visit_date': self.visit_date, 'visitors': [
            {'category': 'adult', 'amount': '80.00', 'ticket_type_id': '4'},
            {'category': 'child', 'amount': '40.00', 'ticket_type_id': '5'},
        ]}

    def submission(self, configuration, *, version=None):
        return {'version': version if version is not None else configuration['version'], 'phone': self.phone, 'visitors': [
            {'id': visitor['id'], 'name': '测试成人' if index == 0 else '测试儿童', 'id_number': self.identities[index]}
            for index, visitor in enumerate(configuration['visitors'])
        ]}

    async def create_config(self, order_id):
        response = await self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=self.config_payload())
        result = response.json()
        self.assertTrue(result['success'], result.get('message'))
        return result['data']['configuration']

    async def test_schema_initialization_is_idempotent(self):
        async with self.sessions() as session:
            key_before = (await session.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_KEY_SETTING))).scalar_one()
        async with self.engine.begin() as connection:
            await ensure_ticket_registration_schema(connection)
        async with self.sessions() as session:
            names = (await session.execute(select(TicketType.name).order_by(TicketType.sort_order))).scalars().all()
            key_after = (await session.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_KEY_SETTING))).scalar_one()
        self.assertEqual(names, list(DEFAULT_TICKET_TYPES))
        self.assertEqual(key_before, key_after)

    async def test_old_schema_adds_categories_and_export_status_without_rewriting_registration(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        async with self.sessions() as session:
            before = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            old_visitors, old_token, old_version = before.visitors, before.token_ciphertext, before.version
        async with self.engine.begin() as connection:
            await connection.execute(text('ALTER TABLE xy_ticket_types DROP COLUMN category'))
            await connection.execute(text('ALTER TABLE xy_ticket_registrations DROP COLUMN exported_at'))
            await ensure_ticket_registration_schema(connection)
            await ensure_ticket_registration_schema(connection)
        async with self.sessions() as session:
            categories = (await session.execute(select(TicketType.category).order_by(TicketType.sort_order))).scalars().all()
            repaired = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertEqual(categories, ['adult', 'child', 'senior'] * 3)
            self.assertEqual(repaired.visitors, old_visitors)
            self.assertEqual(repaired.token_ciphertext, old_token)
            self.assertEqual(repaired.version, old_version)
            self.assertIsNone(repaired.exported_at)

    async def test_age_rules_admin_permissions_validation_and_persistence(self):
        path = self.prefix + '/age-rules'
        defaults = (await self.client.get(path)).json()
        self.assertEqual(defaults['data'], DEFAULT_TICKET_AGE_RULES)
        custom = {'child': {'min_age': 0, 'max_age': 11}, 'adult': {'min_age': 12, 'max_age': 64}, 'senior': {'min_age': 65, 'max_age': None}}
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(id=2, username='member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.assertEqual((await self.client.put(path, json=custom)).json()['code'], 403)
        del self.app.dependency_overrides[deps.get_current_active_user]
        self.assertEqual((await self.client.put(path, json=custom)).json()['code'], 401)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        response = (await self.client.put(path, json=custom)).json()
        self.assertTrue(response['success'])
        self.assertEqual((await self.client.get(path)).json()['data'], custom)
        overlap = {**custom, 'child': {'min_age': 0, 'max_age': 12}}
        invalid = (await self.client.put(path, json=overlap)).json()
        self.assertEqual(invalid['code'], 400)
        self.assertIn('不能重叠', invalid['message'])
        self.assertEqual((await self.client.get(path)).json()['data'], custom)
        async with self.engine.begin() as connection:
            await ensure_ticket_registration_schema(connection)
        self.assertEqual((await self.client.get(path)).json()['data'], custom)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(SystemSetting, TICKET_AGE_RULES_SETTING))
        order_id = await self.order()
        config = await self.create_config(order_id)
        admin_view = (await self.client.get(f'{self.prefix}/orders/{order_id}', headers=self.headers)).json()['data']
        public_view = (await self.client.get(f'{self.prefix}/public/form', headers={'X-Ticket-Token': urlsplit(config['link']).fragment})).json()['data']
        self.assertEqual(admin_view['age_rules'], custom)
        self.assertEqual(public_view['age_rules'], custom)

    async def test_senior_ticket_category_and_public_age_check(self):
        order_id = await self.order()
        types = (await self.client.get(f'{self.prefix}/types')).json()['data']
        self.assertEqual({row['category'] for row in types}, {'adult', 'child', 'senior'})
        mismatch = {'version': 0, 'visit_date': self.visit_date, 'visitors': [{'category': 'adult', 'amount': '30', 'ticket_type_id': '6'}]}
        rejected = (await self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=mismatch)).json()
        self.assertFalse(rejected['success'])
        mismatch['visitors'][0]['category'] = 'senior'
        response = (await self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=mismatch)).json()
        self.assertTrue(response['success'], response.get('message'))
        config = response['data']['configuration']
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        payload = {'version': config['version'], 'phone': self.phone, 'visitors': [{'id': config['visitors'][0]['id'], 'name': '测试老人', 'id_number': self.identities[0]}]}
        underage = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=payload)
        self.assertFalse(underage.json()['success'])
        self.assertIn('老人票年龄要求', underage.json()['message'])
        self.assertNotIn(self.identities[0], underage.text)
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertIsNone(stored.identity_ciphertext)
            self.assertIsNone(stored.submitted_at)
        payload['visitors'][0]['id_number'] = fictional_identity('19500101', '001')
        success = (await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=payload)).json()
        self.assertTrue(success['success'], success.get('message'))
        self.assertEqual(success['data']['visitors'][0]['category'], 'senior')

    async def test_age_policy_uses_order_owner_and_disabled_check_keeps_identity_validation(self):
        personal_path = self.prefix + '/personal-age-policy'
        custom = {'child': {'min_age': 0, 'max_age': 11}, 'adult': {'min_age': 12, 'max_age': 64}, 'senior': {'min_age': 65, 'max_age': None}}
        member = User(id=2, username='ticket-test-member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: member
        saved = (await self.client.put(personal_path, json={'use_personal': True, 'enabled': False, 'rules': custom})).json()
        self.assertTrue(saved['success'], saved.get('message'))
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        admin_order, member_order = await self.order(), await self.order(owner_id=2)
        admin_config, member_config = await self.create_config(admin_order), await self.create_config(member_order)
        admin_view = (await self.client.get(f'{self.prefix}/orders/{member_order}', headers=self.headers)).json()['data']
        self.assertFalse(admin_view['age_validation_enabled'])
        self.assertEqual(admin_view['age_rules'], custom)
        token_headers = {'X-Ticket-Token': urlsplit(member_config['link']).fragment}
        public = (await self.client.get(f'{self.prefix}/public/form', headers=token_headers)).json()['data']
        self.assertFalse(public['age_validation_enabled'])
        self.assertEqual(public['age_rules'], custom)
        # 成人槽填写 10 周岁身份证，关闭校验时允许年龄不符；身份证校验码仍必须正确。
        payload = self.submission(member_config)
        number = fictional_identity('20160101', '003')
        payload['visitors'][0]['id_number'] = number[:-1] + ('0' if number[-1] != '0' else '1')
        invalid = (await self.client.post(f'{self.prefix}/public/submit', headers=token_headers, json=payload)).json()
        self.assertFalse(invalid['success'])
        self.assertIn('校验码', invalid['message'])
        payload['visitors'][0]['id_number'] = number
        accepted = (await self.client.post(f'{self.prefix}/public/submit', headers=token_headers, json=payload)).json()
        self.assertTrue(accepted['success'], accepted.get('message'))
        self.assertFalse(accepted['data']['age_validation_enabled'])
        admin_payload = self.submission(admin_config)
        admin_payload['visitors'][0]['id_number'] = number
        admin_headers = {'X-Ticket-Token': urlsplit(admin_config['link']).fragment}
        rejected = (await self.client.post(f'{self.prefix}/public/submit', headers=admin_headers, json=admin_payload)).json()
        self.assertFalse(rejected['success'])
        self.assertIn('成人票年龄要求', rejected['message'])
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(admin_order)))).scalar_one()
            self.assertIsNone(stored.identity_ciphertext)

    async def test_personal_enabled_overrides_system_disabled_and_can_restore_inheritance(self):
        system_path, personal_path = self.prefix + '/age-policy', self.prefix + '/personal-age-policy'
        disabled = {'enabled': False, 'rules': DEFAULT_TICKET_AGE_RULES}
        result = (await self.client.put(system_path, json=disabled)).json()
        self.assertTrue(result['success'], result.get('message'))
        # 旧接口只调整范围，schema 初始化不会把开关改回默认值。
        custom = {'child': {'min_age': 0, 'max_age': 11}, 'adult': {'min_age': 12, 'max_age': 64}, 'senior': {'min_age': 65, 'max_age': None}}
        self.assertTrue((await self.client.put(self.prefix + '/age-rules', json=custom)).json()['success'])
        async with self.engine.begin() as connection:
            await ensure_ticket_registration_schema(connection)
        self.assertEqual((await self.client.get(system_path)).json()['data'], {'enabled': False, 'rules': custom})
        member = User(id=2, username='ticket-test-member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: member
        self.assertEqual((await self.client.get(personal_path)).json()['data'], {'use_personal': False, 'enabled': False, 'rules': custom})
        override = {'use_personal': True, 'enabled': True, 'rules': DEFAULT_TICKET_AGE_RULES}
        self.assertTrue((await self.client.put(personal_path, json=override)).json()['success'])
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        order_id = await self.order(owner_id=2)
        config = await self.create_config(order_id)
        token_headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        view = (await self.client.get(f'{self.prefix}/public/form', headers=token_headers)).json()['data']
        self.assertTrue(view['age_validation_enabled'])
        self.assertEqual(view['age_rules'], DEFAULT_TICKET_AGE_RULES)
        payload = self.submission(config)
        payload['visitors'][0]['id_number'] = fictional_identity('20100101', '003')
        rejected = (await self.client.post(f'{self.prefix}/public/submit', headers=token_headers, json=payload)).json()
        self.assertFalse(rejected['success'])
        self.assertIn('成人票年龄要求', rejected['message'])
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: member
        inherited = (await self.client.put(personal_path, json={'use_personal': False})).json()
        self.assertEqual(inherited['data'], {'use_personal': False, 'enabled': False, 'rules': custom})
        async with self.sessions() as session:
            rows = (await session.execute(select(UserSetting).where(UserSetting.user_id == 2, UserSetting.key == PERSONAL_TICKET_AGE_POLICY_SETTING))).scalars().all()
            self.assertEqual(rows, [])
            self.assertEqual((await session.get(SystemSetting, TICKET_AGE_ENABLED_SETTING)).value, 'false')
        accepted = (await self.client.post(f'{self.prefix}/public/submit', headers=token_headers, json=payload)).json()
        self.assertTrue(accepted['success'], accepted.get('message'))
        self.assertFalse(accepted['data']['age_validation_enabled'])
        self.assertEqual(accepted['data']['age_rules'], custom)

    async def test_concurrent_personal_age_policy_saves_keep_one_setting(self):
        path = self.prefix + '/personal-age-policy'
        first = {'use_personal': True, 'enabled': False, 'rules': DEFAULT_TICKET_AGE_RULES}
        second = {'use_personal': True, 'enabled': True, 'rules': DEFAULT_TICKET_AGE_RULES}
        responses = await asyncio.gather(self.client.put(path, json=first), self.client.put(path, json=second))
        self.assertTrue(all(response.json()['success'] for response in responses))
        async with self.sessions() as session:
            settings = (await session.execute(select(UserSetting).where(UserSetting.user_id == 1, UserSetting.key == PERSONAL_TICKET_AGE_POLICY_SETTING))).scalars().all()
            self.assertEqual(len(settings), 1)
        effective = (await self.client.get(path)).json()['data']
        self.assertIn(effective, (first, second))

    async def test_old_schema_missing_phone_column_is_repaired_without_changing_registration(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        submitted = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=self.submission(config))
        self.assertTrue(submitted.json()['success'])
        async with self.sessions() as session:
            before = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            original_identity, original_token, original_version = before.identity_ciphertext, before.token_ciphertext, before.version
        # 仅操作本测试随机隔离库，模拟旧版本根本没有联系电话字段。
        async with self.engine.begin() as connection:
            await connection.execute(text('ALTER TABLE xy_ticket_registrations DROP COLUMN phone_ciphertext'))
            await ensure_ticket_registration_schema(connection)
            await ensure_ticket_registration_schema(connection)
            columns = set((await connection.execute(text('SHOW COLUMNS FROM xy_ticket_registrations'))).scalars())
        self.assertIn('phone_ciphertext', columns)
        async with self.sessions() as session:
            repaired = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertIsNone(repaired.phone_ciphertext)
            self.assertEqual(repaired.identity_ciphertext, original_identity)
            self.assertEqual(repaired.token_ciphertext, original_token)
            self.assertEqual(repaired.version, original_version)
        public = (await self.client.get(f'{self.prefix}/public/form', headers=headers)).json()
        self.assertEqual(public['data']['status'], 'submitted')
        self.assertIsNone(public['data']['phone_masked'])

    async def test_phone_required_validated_encrypted_and_not_overwritable(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        self.assertIsNone(config['phone'])
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        missing = self.submission(config)
        del missing['phone']
        response = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=missing)
        self.assertEqual(response.json()['code'], 400)
        self.assertIn('手机号', response.json()['message'])
        self.assertIn('no-store', response.headers['cache-control'])
        for invalid in ['', '12800138000', '1380013800', '+8613800138000', 13800138000]:
            payload = self.submission(config)
            payload['phone'] = invalid
            response = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=payload)
            self.assertFalse(response.json()['success'])
            self.assertNotIn(self.identities[0], response.text)
            self.assertNotIn('测试成人', response.text)
        draft_phone = await self.client.post(f'{self.prefix}/public/phone', headers=headers, json={'version': config['version'], 'phone': self.phone})
        self.assertEqual(draft_phone.json()['code'], 409)
        async with self.sessions() as session:
            draft = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertIsNone(draft.identity_ciphertext)
            self.assertIsNone(draft.phone_ciphertext)
        response = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=self.submission(config))
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['data']['phone_masked'], '138****8000')
        self.assertNotIn(self.phone, response.text)
        overwrite = await self.client.post(f'{self.prefix}/public/phone', headers=headers, json={'version': response.json()['data']['version'], 'phone': '13900139000'})
        self.assertEqual(overwrite.json()['code'], 409)
        admin = (await self.client.get(f'{self.prefix}/orders/{order_id}', headers=self.headers)).json()
        self.assertEqual(admin['data']['configuration']['phone'], self.phone)
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertNotIn(self.phone, stored.phone_ciphertext)

    async def test_legacy_phone_supplement_rejects_tampering_and_allows_only_one_writer(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        response = await self.client.post(f'{self.prefix}/public/submit', headers=headers, json=self.submission(config))
        self.assertTrue(response.json()['success'])
        version = response.json()['data']['version']
        async with self.sessions() as session:
            legacy = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            legacy.phone_ciphertext = None
            original_identity, original_visitors, original_date, original_submission = legacy.identity_ciphertext, legacy.visitors, legacy.visit_date, legacy.submitted_at
            await session.commit()
        payload = {'version': version, 'phone': self.phone}
        for field, value in [('visitors', []), ('visit_date', self.visit_date), ('amount', '0'), ('name', '不能改名')]:
            rejected = await self.client.post(f'{self.prefix}/public/phone', headers=headers, json={**payload, field: value})
            self.assertEqual(rejected.json()['code'], 400)
            self.assertNotIn(self.phone, rejected.text)
        stale = await self.client.post(f'{self.prefix}/public/phone', headers=headers, json={**payload, 'version': version - 1})
        self.assertEqual(stale.json()['code'], 409)
        responses = await asyncio.gather(*[
            self.client.post(f'{self.prefix}/public/phone', headers=headers, json={**payload, 'phone': phone})
            for phone in (self.phone, '13900139000')
        ])
        self.assertEqual(sum(result.json()['success'] for result in responses), 1)
        self.assertEqual(next(result.json()['code'] for result in responses if not result.json()['success']), 409)
        winner = next(index for index, result in enumerate(responses) if result.json()['success'])
        expected_phone = (self.phone, '13900139000')[winner]
        public = (await self.client.get(f'{self.prefix}/public/form', headers=headers)).json()['data']
        self.assertEqual(public['phone_masked'], expected_phone[:3] + '****' + expected_phone[-4:])
        self.assertEqual(public['version'], version + 1)
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(order_id)))).scalar_one()
            self.assertEqual(stored.identity_ciphertext, original_identity)
            self.assertEqual(stored.visitors, original_visitors)
            self.assertEqual(stored.visit_date, original_date)
            self.assertEqual(stored.submitted_at, original_submission)

    async def test_order_binding_versions_immutable_submission_and_encryption(self):
        first = await self.order(order_no='SAME-ORDER-NUMBER')
        second = await self.order(order_no='SAME-ORDER-NUMBER')
        config = await self.create_config(first)
        self.assertEqual(urlsplit(config['link']).netloc, 'tickets.example.test:9443')
        other = (await self.client.get(f'{self.prefix}/orders/{second}', headers=self.headers)).json()
        self.assertIsNone(other['data']['configuration'])
        token = urlsplit(config['link']).fragment
        public_headers = {'X-Ticket-Token': token}
        public = await self.client.get(f'{self.prefix}/public/form', headers=public_headers)
        self.assertIn('no-store', public.headers['cache-control'])
        self.assertTrue(public.json()['success'])
        self.assertNotIn('id_number', public.json()['data']['visitors'][0])
        changed = self.config_payload(version=1)
        for entry, old in zip(changed['visitors'], config['visitors']):
            entry['id'] = old['id']
        changed['visitors'][0]['amount'] = '81.50'
        updated = (await self.client.put(f'{self.prefix}/orders/{first}', headers=self.headers, json=changed)).json()
        self.assertTrue(updated['success'])
        config = updated['data']['configuration']
        self.assertEqual(config['version'], 2)
        stale = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(config, version=1))
        self.assertEqual(stale.json()['code'], 409)
        missing = self.submission(config)
        missing['visitors'].pop()
        self.assertFalse((await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=missing)).json()['success'])
        tampered = self.submission(config)
        tampered['visitors'][0]['amount'] = '0.01'
        forbidden = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=tampered)
        self.assertFalse(forbidden.json()['success'])
        self.assertNotIn(self.identities[0], forbidden.text)
        submitted = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(config))
        self.assertTrue(submitted.json()['success'], submitted.json().get('message'))
        self.assertEqual(submitted.json()['data']['status'], 'submitted')
        self.assertEqual(submitted.json()['data']['phone_masked'], '138****8000')
        self.assertNotIn(self.phone, submitted.text)
        self.assertNotIn(self.identities[0], submitted.text)
        self.assertNotIn(self.identities[1], submitted.text)
        repeat = (await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(config))).json()
        self.assertFalse(repeat['success'])
        admin = (await self.client.get(f'{self.prefix}/orders/{first}', headers=self.headers)).json()
        self.assertEqual(admin['data']['configuration']['visitors'][0]['id_number'], self.identities[0])
        self.assertEqual(admin['data']['configuration']['status'], 'submitted')
        self.assertEqual(admin['data']['configuration']['phone'], self.phone)
        changed['version'] = 3
        self.assertFalse((await self.client.put(f'{self.prefix}/orders/{first}', headers=self.headers, json=changed)).json()['success'])
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(first)))).scalar_one()
            self.assertNotIn(self.identities[0], stored.identity_ciphertext)
            self.assertNotIn('测试成人', stored.identity_ciphertext)
            self.assertNotIn(token, stored.token_ciphertext)
            self.assertNotIn(self.phone, stored.phone_ciphertext)
            order = await session.get(XYOrder, int(first))
            order.status = 'refunded'
            await session.commit()
        self.assertFalse((await self.client.get(f'{self.prefix}/public/form', headers=public_headers)).json()['success'])

    async def test_concurrent_creation_and_submission_accept_only_one_writer(self):
        order_id = await self.order()
        responses = await asyncio.gather(*[self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=self.config_payload()) for _ in range(2)])
        self.assertEqual(sum(response.json()['success'] for response in responses), 1)
        config = next(response.json()['data']['configuration'] for response in responses if response.json()['success'])
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        submissions = await asyncio.gather(*[self.client.post(f'{self.prefix}/public/submit', headers=headers, json=self.submission(config)) for _ in range(2)])
        self.assertEqual(sum(response.json()['success'] for response in submissions), 1)
        self.assertEqual(next(response.json()['code'] for response in submissions if not response.json()['success']), 409)

    async def test_admin_permissions_paid_statuses_and_send_transport(self):
        for state in ['pending_payment', 'processing', 'processed', 'refunding', 'refunded', 'cancelled', 'unknown']:
            order_id = await self.order(status=state)
            self.assertFalse((await self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=self.config_payload())).json()['success'])
        order_id = await self.order()
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(id=2, username='member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.assertEqual((await self.client.get(f'{self.prefix}/orders/{order_id}', headers=self.headers)).json()['code'], 403)
        del self.app.dependency_overrides[deps.get_current_active_user]
        self.assertEqual((await self.client.get(f'{self.prefix}/orders/{order_id}', headers=self.headers)).json()['code'], 401)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        config = await self.create_config(order_id)
        with patch.object(websocket_client, 'get_account_status', AsyncMock(return_value={'success': True, 'data': {'is_connected': True}})), \
             patch.object(websocket_client, 'create_chat', AsyncMock(return_value={'success': True, 'data': {'chat_id': 'test-chat'}})), \
             patch.object(websocket_client.http_client, 'post', AsyncMock(return_value={'success': True, 'data': {'send_status': 'success'}})) as send:
            result = (await self.client.post(f'{self.prefix}/orders/{order_id}/send', headers=self.headers, json={'version': config['version']})).json()
            self.assertTrue(result['success'], result.get('message'))
            send.assert_awaited_once()
            kwargs = send.call_args.kwargs
            self.assertEqual(kwargs['max_retries'], 1)
            self.assertEqual(kwargs['json']['to_user_id'], 'test-buyer')
            self.assertIn(self.origin + '/ticket-registration#', kwargs['json']['message'])
            self.assertEqual(kwargs['json']['message'], f"请填写订单 {result['data']['order_no']} 的门票实名信息：每位游客的姓名、身份证，以及一个整单共用的联系手机号。\n{config['link']}")
            repeated = (await self.client.post(f'{self.prefix}/orders/{order_id}/send', headers=self.headers, json={'version': config['version']})).json()
            self.assertTrue(repeated['success'])
            self.assertEqual(send.await_count, 1)
            new_origin = {'X-Frontend-Origin': 'https://new-tickets.example.test'}
            self.assertTrue((await self.client.post(f'{self.prefix}/orders/{order_id}/send', headers=new_origin, json={'version': config['version']})).json()['success'])
            self.assertEqual(send.await_count, 2)
        async with self.sessions() as session:
            self.assertEqual((await session.get(XYOrder, int(order_id))).status, 'pending_ship')

    async def test_send_uses_order_owners_message_template_and_restores_system_inheritance(self):
        system_path, personal_path = self.prefix + '/message-template', self.prefix + '/personal-message-template'
        system_template = '系统票务：订单 {orderId}\n登记链接 {infourl}\n请仔细核对。'
        admin_template = '管理员自己的订单 {orderId}：{infourl}'
        member_template = '  商家二的门票订单 {orderId}\n{infourl}\n请填写出行人。  '
        self.assertTrue((await self.client.put(system_path, json={'message_template': system_template})).json()['success'])
        self.assertTrue((await self.client.put(personal_path, json={'use_personal': True, 'message_template': admin_template})).json()['success'])
        member = User(id=2, username='ticket-test-member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: member
        self.assertTrue((await self.client.put(personal_path, json={'use_personal': True, 'message_template': member_template})).json()['success'])
        disabled_age = {'use_personal': True, 'enabled': False, 'rules': DEFAULT_TICKET_AGE_RULES}
        self.assertTrue((await self.client.put(self.prefix + '/personal-age-policy', json=disabled_age)).json()['success'])
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        member_no, admin_no = 'TICKET-MEMBER-ORDER-5127851592171166512', 'TICKET-ADMIN-ORDER'
        member_order = await self.order(owner_id=2, order_no=member_no)
        admin_order = await self.order(order_no=admin_no)
        member_config, admin_config = await self.create_config(member_order), await self.create_config(admin_order)
        with patch.object(websocket_client, 'get_account_status', AsyncMock(return_value={'success': True, 'data': {'is_connected': True}})), \
             patch.object(websocket_client, 'create_chat', AsyncMock(return_value={'success': True, 'data': {'chat_id': 'test-template-chat'}})), \
             patch.object(websocket_client.http_client, 'post', AsyncMock(return_value={'success': True, 'data': {'send_status': 'success'}})) as send:
            result = (await self.client.post(f'{self.prefix}/orders/{member_order}/send', headers=self.headers, json={'version': member_config['version']})).json()
            self.assertTrue(result['success'], result.get('message'))
            self.assertEqual(send.await_args.kwargs['json']['message'], f'  商家二的门票订单 {member_no}\n{member_config["link"]}\n请填写出行人。  ')
            self.assertFalse(result['data']['age_validation_enabled'])
            self.assertTrue((await self.client.post(f'{self.prefix}/orders/{admin_order}/send', headers=self.headers, json={'version': admin_config['version']})).json()['success'])
            self.assertEqual(send.await_args.kwargs['json']['message'], f'管理员自己的订单 {admin_no}：{admin_config["link"]}')
            self.app.dependency_overrides[deps.get_current_active_user] = lambda: member
            restored = (await self.client.put(personal_path, json={'use_personal': False})).json()
            self.assertEqual(restored['data'], {'use_personal': False, 'message_template': system_template})
            self.assertEqual((await self.client.get(self.prefix + '/personal-age-policy')).json()['data'], disabled_age)
            self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
            inherited_no = 'TICKET-MEMBER-INHERITED'
            inherited_order = await self.order(owner_id=2, order_no=inherited_no)
            inherited_config = await self.create_config(inherited_order)
            self.assertTrue((await self.client.post(f'{self.prefix}/orders/{inherited_order}/send', headers=self.headers, json={'version': inherited_config['version']})).json()['success'])
            self.assertEqual(send.await_args.kwargs['json']['message'], f'系统票务：订单 {inherited_no}\n登记链接 {inherited_config["link"]}\n请仔细核对。')
            self.assertEqual(send.await_count, 3)

    async def test_message_template_changes_do_not_resend_sent_or_unknown_versions(self):
        path = self.prefix + '/message-template'
        self.assertTrue((await self.client.put(path, json={'message_template': '旧模板 {orderId} {infourl}'})).json()['success'])
        sent_order, unknown_order = await self.order(), await self.order()
        sent_config, unknown_config = await self.create_config(sent_order), await self.create_config(unknown_order)
        with patch.object(websocket_client, 'get_account_status', AsyncMock(return_value={'success': True, 'data': {'is_connected': True}})), \
             patch.object(websocket_client, 'create_chat', AsyncMock(return_value={'success': True, 'data': {'chat_id': 'test-template-chat'}})), \
             patch.object(websocket_client.http_client, 'post', AsyncMock(side_effect=[{'success': True, 'data': {'send_status': 'success'}}, asyncio.TimeoutError()])) as send:
            self.assertTrue((await self.client.post(f'{self.prefix}/orders/{sent_order}/send', headers=self.headers, json={'version': sent_config['version']})).json()['success'])
            self.assertTrue((await self.client.put(path, json={'message_template': '新模板只带登记地址\n{infourl}'})).json()['success'])
            repeated = (await self.client.post(f'{self.prefix}/orders/{sent_order}/send', headers=self.headers, json={'version': sent_config['version']})).json()
            self.assertTrue(repeated['success'])
            self.assertIn('无需重复发送', repeated['message'])
            self.assertEqual(send.await_count, 1)
            failed = (await self.client.post(f'{self.prefix}/orders/{unknown_order}/send', headers=self.headers, json={'version': unknown_config['version']})).json()
            self.assertEqual(failed['code'], 502)
            self.assertEqual(send.await_args.kwargs['json']['message'], '新模板只带登记地址\n' + unknown_config['link'])
            self.assertTrue((await self.client.put(path, json={'message_template': '再次修改模板 {infourl}'})).json()['success'])
            retry = (await self.client.post(f'{self.prefix}/orders/{unknown_order}/send', headers=self.headers, json={'version': unknown_config['version']})).json()
            self.assertEqual(retry['code'], 409)
            self.assertEqual(send.await_count, 2)
        async with self.sessions() as session:
            unknown = (await session.execute(select(TicketRegistration).where(TicketRegistration.order_id == int(unknown_order)))).scalar_one()
            self.assertEqual(unknown.send_attempt_status, 'pending')
            self.assertIsNone(unknown.sent_at)

    async def test_invalid_stored_message_template_cannot_create_send_intent(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        async with self.sessions() as session:
            session.add(SystemSetting(key=TICKET_MESSAGE_TEMPLATE_SETTING, value='缺失链接的损坏模板'))
            await session.commit()
        with patch.object(websocket_client, 'get_account_status', AsyncMock()) as status, \
             patch.object(websocket_client.http_client, 'post', AsyncMock()) as send:
            rejected = (await self.client.post(f'{self.prefix}/orders/{order_id}/send', headers=self.headers, json={'version': config['version']})).json()
            self.assertEqual(rejected['code'], 503)
            status.assert_not_awaited()
            send.assert_not_awaited()
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertIsNone(stored.send_attempt_status)
            self.assertIsNone(stored.sent_at)
            self.assertIsNone((await session.get(XYOrder, int(order_id))).chat_id)

    async def test_concurrent_message_template_saves_keep_one_system_and_personal_value(self):
        system_path, personal_path = self.prefix + '/message-template', self.prefix + '/personal-message-template'
        first, second = {'message_template': '第一份 {infourl}'}, {'message_template': '第二份 {orderId} {infourl}'}
        system = await asyncio.gather(self.client.put(system_path, json=first), self.client.put(system_path, json=second))
        self.assertTrue(all(response.json()['success'] for response in system))
        personal = await asyncio.gather(self.client.put(personal_path, json={'use_personal': True, **first}), self.client.put(personal_path, json={'use_personal': True, **second}))
        self.assertTrue(all(response.json()['success'] for response in personal))
        async with self.sessions() as session:
            rows = (await session.execute(select(UserSetting).where(UserSetting.user_id == 1, UserSetting.key == PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING))).scalars().all()
            self.assertEqual(len(rows), 1)
            system_value = (await session.get(SystemSetting, TICKET_MESSAGE_TEMPLATE_SETTING)).value
            self.assertIn(system_value, (first['message_template'], second['message_template']))

    async def test_public_limits_and_invalid_origin_fail_safely(self):
        order_id = await self.order()
        config = await self.create_config(order_id)
        headers = {'X-Ticket-Token': urlsplit(config['link']).fragment}
        for origin in ['javascript:alert(1)', 'https://user:password@example.test', 'https://example.test/path', 'https://example.test?bad=1']:
            result = (await self.client.get(f'{self.prefix}/orders/{order_id}', headers={'X-Frontend-Origin': origin})).json()
            self.assertFalse(result['success'], origin)
        self.limit_mock.return_value = RateLimitResult(False, 0, 60)
        self.assertEqual((await self.client.get(f'{self.prefix}/public/form', headers=headers)).json()['code'], 429)
        self.limit_mock.side_effect = RuntimeError('redis unavailable')
        self.assertEqual((await self.client.get(f'{self.prefix}/public/form', headers=headers)).json()['code'], 503)

    async def test_recollect_clears_previous_identity_export_and_send_then_reuses_link(self):
        from app.api.routes import ticket_registration_export as export_routes
        from common.services.order_service import OrderService
        self.app.include_router(export_routes.router, prefix='/api/v1')
        order_id = await self.order()
        configured = await self.create_config(order_id)
        public_headers = {'X-Ticket-Token': urlsplit(configured['link']).fragment}
        submitted = (await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(configured))).json()
        self.assertTrue(submitted['success'])
        version = submitted['data']['version']
        exported = await self.client.post(f'{self.prefix}/export.xlsx', headers=self.headers, json={'order_ids': [order_id]})
        self.assertTrue(exported.content.startswith(b'PK'))
        async with self.sessions() as session:
            original = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertIsNotNone(original.exported_at)
            old_token = (original.token_hash, original.token_ciphertext)
            original.sent_at = original.submitted_at
            original.sent_version = original.send_attempt_version = version
            original.sent_link_hash = original.send_attempt_link_hash = 'a' * 64
            original.send_attempt_status = 'unknown'
            await session.commit()
        response = await self.client.post(f'{self.prefix}/orders/{order_id}/recollect', headers=self.headers, json={'version': version})
        result = response.json()
        self.assertTrue(result['success'], result.get('message'))
        self.assertIn('no-store', response.headers.get('cache-control', ''))
        current = result['data']['configuration']
        self.assertEqual(current['link'], configured['link'])
        self.assertEqual(current['version'], version + 1)
        self.assertEqual(current['status'], 'draft')
        self.assertIsNone(current['phone'])
        self.assertIsNone(current['exported_at'])
        self.assertIsNone(current['submitted_at'])
        self.assertIsNone(current['sent_at'])
        for old, new in zip(configured['visitors'], current['visitors']):
            for key in ('id', 'category', 'amount', 'ticket_type_id', 'ticket_type_name'):
                self.assertEqual(old[key], new[key])
            self.assertIsNone(new['name'])
            self.assertIsNone(new['id_number'])
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertEqual((stored.token_hash, stored.token_ciphertext), old_token)
            for field in ('identity_ciphertext', 'phone_ciphertext', 'submitted_at', 'exported_at', 'sent_at', 'sent_version', 'sent_link_hash', 'send_attempt_version', 'send_attempt_link_hash', 'send_attempt_status'):
                self.assertIsNone(getattr(stored, field), field)
            states = await OrderService(session).get_ticket_status_map([int(order_id)])
            self.assertEqual(states[int(order_id)]['ticket_collection_status'], 'pending')
            self.assertEqual(states[int(order_id)]['ticket_export_status'], 'unexported')
        blank = (await self.client.get(f'{self.prefix}/public/form', headers=public_headers)).json()['data']
        self.assertEqual(blank['status'], 'draft')
        self.assertIsNone(blank['phone_masked'])
        self.assertTrue(all(visitor['name'] is None and visitor['id_number_masked'] is None for visitor in blank['visitors']))
        no_export = await self.client.post(f'{self.prefix}/export.xlsx', headers=self.headers, json={'order_ids': [order_id]})
        self.assertFalse(no_export.json()['success'])
        self.assertNotIn(self.identities[0], no_export.text)
        for stale_version in (configured['version'], version):
            stale = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(current, version=stale_version))
            self.assertEqual(stale.json()['code'], 409)
        duplicate = await self.client.post(f'{self.prefix}/orders/{order_id}/recollect', headers=self.headers, json={'version': version})
        self.assertEqual(duplicate.json()['code'], 409)
        fresh = self.submission(current)
        fresh['phone'] = '13900139000'
        fresh['visitors'][0]['name'] = '重新填写成人'
        again = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=fresh)
        self.assertTrue(again.json()['success'], again.text)
        exported_again = await self.client.post(f'{self.prefix}/export.xlsx', headers=self.headers, json={'order_ids': [order_id]})
        self.assertTrue(exported_again.content.startswith(b'PK'))

    async def test_recollect_is_admin_only_and_rejects_missing_draft_and_unpaid(self):
        order_id = await self.order()
        url = f'{self.prefix}/orders/{order_id}/recollect'
        self.assertEqual((await self.client.post(url, headers=self.headers, json={'version': 1})).json()['code'], 409)
        configured = await self.create_config(order_id)
        self.assertEqual((await self.client.post(url, headers=self.headers, json={'version': 1})).json()['code'], 409)
        public_headers = {'X-Ticket-Token': urlsplit(configured['link']).fragment}
        submitted = (await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(configured))).json()['data']
        payload = {'version': submitted['version']}
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: User(id=2, username='member', role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.assertEqual((await self.client.post(url, headers=self.headers, json=payload)).json()['code'], 403)
        del self.app.dependency_overrides[deps.get_current_active_user]
        self.assertEqual((await self.client.post(url, headers=self.headers, json=payload)).json()['code'], 401)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.admin
        tampered = await self.client.post(url, headers=self.headers, json={**payload, 'phone': '13900139000'})
        self.assertFalse(tampered.json()['success'])
        async with self.sessions() as session:
            order = await session.get(XYOrder, int(order_id))
            order.status = 'refunded'
            await session.commit()
        unpaid = await self.client.post(url, headers=self.headers, json=payload)
        self.assertFalse(unpaid.json()['success'])
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration))).scalar_one()
            self.assertIsNotNone(stored.identity_ciphertext)
            self.assertEqual(stored.version, submitted['version'])

    async def test_expired_submitted_order_can_recollect_and_update_visit_date(self):
        order_id = await self.order()
        configured = await self.create_config(order_id)
        public_headers = {'X-Ticket-Token': urlsplit(configured['link']).fragment}
        submitted = (await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(configured))).json()['data']
        yesterday = get_beijing_now().date() - timedelta(days=1)
        async with self.sessions() as session:
            stored = (await session.execute(select(TicketRegistration))).scalar_one()
            stored.visit_date = yesterday
            await session.commit()
        responses = await asyncio.gather(*[
            self.client.post(f'{self.prefix}/orders/{order_id}/recollect', headers=self.headers, json={'version': submitted['version']})
            for _ in range(2)
        ])
        self.assertEqual(sum(response.json()['success'] for response in responses), 1)
        reset = next(response.json()['data']['configuration'] for response in responses if response.json()['success'])
        self.assertEqual(reset['visit_date'], yesterday.isoformat())
        self.assertEqual(reset['status'], 'draft')
        payload = self.config_payload(version=reset['version'])
        for visitor, previous in zip(payload['visitors'], reset['visitors']):
            visitor['id'] = previous['id']
        updated = (await self.client.put(f'{self.prefix}/orders/{order_id}', headers=self.headers, json=payload)).json()
        self.assertTrue(updated['success'], updated.get('message'))
        self.assertEqual(updated['data']['configuration']['visit_date'], self.visit_date)
        fresh = await self.client.post(f'{self.prefix}/public/submit', headers=public_headers, json=self.submission(updated['data']['configuration']))
        self.assertTrue(fresh.json()['success'], fresh.text)

    async def test_ticket_type_api_uses_canonical_order_without_changing_existing_ids(self):
        async with self.sessions() as session:
            types = (await session.execute(select(TicketType))).scalars().all()
            ids_before = {ticket.name: ticket.id for ticket in types}
            # 模拟可识别名称存在、分类尚未回填的旧票种。
            types[0].category = None
            await session.commit()
        result = (await self.client.get(f'{self.prefix}/types', headers=self.headers)).json()
        self.assertTrue(result['success'])
        for category, person in (('adult', '成人'), ('senior', '老人'), ('child', '儿童')):
            self.assertEqual([ticket['name'] for ticket in result['data'] if ticket['category'] == category], [f'{days}日{person}票' for days in ('1', '1.5', '2')])
        self.assertEqual({ticket['name']: int(ticket['id']) for ticket in result['data']}, ids_before)


if __name__ == '__main__':
    unittest.main()
