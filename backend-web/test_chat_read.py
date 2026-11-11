"""已读协议和权限回归，所有 IM I/O 均 mock，无真实消息或已读操作。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user, get_db_session
from app.api.routes import chat_new_read
from app.services.chat_new.im_client import GoofishImClient
from app.services.chat_new.im_session_manager import ImSessionManager
from app.services.chat_new.im_transport import ImRequestRejected
from common.models import User, XYAccount
from common.models.user import UserRole, UserStatus


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session
    async def execute(self, statement):
        return self.session.execute(statement)


class Browser:
    def __init__(self):
        self.events = []
    async def send_text(self, data):
        self.events.append(json.loads(data))


class ReadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        XYAccount.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.session.add_all([
            XYAccount(id=1, owner_id=1, account_id="mine", cookie="synthetic", login_method="cookie"),
            XYAccount(id=2, owner_id=2, account_id="other", cookie="synthetic", login_method="cookie"),
        ])
        self.session.commit()
        self.user = User(id=1, role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.client = GoofishImClient("synthetic", "unb=123456789;")
        self.client._connected = self.client._registered = True
        self.client._ws = SimpleNamespace(closed=False)
        self.client._send_and_wait = AsyncMock(return_value={"code": 200})
        self.manager = ImSessionManager()
        self.manager.clients = {"mine": self.client, "other": self.client}
        self.first, self.second, self.other = Browser(), Browser(), Browser()
        self.manager._ws_clients = {"mine": {self.first, self.second}, "other": {self.other}}
        self.manager_patch = patch.object(chat_new_read, "get_im_session_manager", return_value=self.manager)
        self.manager_getter = self.manager_patch.start()
        self.app = FastAPI()
        self.app.include_router(chat_new_read.router, prefix="/api/v1")
        self.app.dependency_overrides[get_current_active_user] = lambda: self.user
        self.app.dependency_overrides[get_db_session] = lambda: AsyncSessionAdapter(self.session)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.payload = {"cid": "synthetic-chat@goofish", "messageIds": ["older.PNM", "newer.PNM"],
                        "lastMessageId": "newer.PNM", "lastMessageTime": 1794100000000}

    async def asyncTearDown(self):
        self.manager_patch.stop()
        await self.http.aclose()
        self.session.close()
        self.engine.dispose()

    async def post(self, account="mine", **changes):
        return await self.http.post(f"/api/v1/chat-new/mark-read/{account}", json={**self.payload, **changes})

    async def test_two_confirmed_upstream_operations_then_sync_all_own_browsers(self):
        result = (await self.post()).json()
        self.assertTrue(result["success"])
        self.assertEqual(self.client._send_and_wait.await_count, 2)
        frames = [call.args[1] for call in self.client._send_and_wait.await_args_list]
        # Wire bodies match the official xy-site MessageStatus/Conversation service wrappers.
        self.assertEqual(frames[0]["lwp"], "/r/MessageStatus/read")
        self.assertEqual(frames[0]["body"], [["older.PNM", "newer.PNM"]])
        self.assertEqual(frames[1]["lwp"], "/r/Conversation/clearRedPoint")
        self.assertEqual(frames[1]["body"], [[{"messageId": "newer.PNM", "cid": "synthetic-chat@goofish"}]])
        self.assertNotIn("lastMessageTime", frames[1])
        self.assertEqual(self.first.events, self.second.events)
        self.assertEqual(self.first.events, [{**result["data"], "event": "conversation_read"}])
        self.assertEqual(result["data"]["cid"], "synthetic-chat")
        self.assertEqual(result["data"]["lastMessageTime"], self.payload["lastMessageTime"])
        self.assertFalse(self.other.events)
        self.assertNotIn("unreadCount", result["data"])  # 通知是边界，不将后到消息永远归零。

    async def test_foreign_and_missing_accounts_never_access_manager(self):
        for account in ("other", "missing"):
            self.assertFalse((await self.post(account)).json()["success"])
        self.manager_getter.assert_not_called()
        self.client._send_and_wait.assert_not_awaited()

    async def test_admin_can_mark_owned_other_account_but_only_that_accounts_browsers(self):
        self.user.role = UserRole.ADMIN
        self.assertTrue((await self.post("other")).json()["success"])
        self.assertEqual(len(self.other.events), 1)
        self.assertFalse(self.first.events)
        self.assertFalse(self.second.events)

    async def test_offline_account_does_not_attempt_protocol(self):
        self.client._connected = False
        self.assertFalse((await self.post()).json()["success"])
        self.client._send_and_wait.assert_not_awaited()

    async def test_read_failure_does_not_clear_platform_redpoint_or_broadcast(self):
        self.client._send_and_wait.side_effect = TimeoutError("synthetic timeout")
        self.assertFalse((await self.post()).json()["success"])
        self.client._send_and_wait.assert_awaited_once()
        self.assertFalse(self.first.events)

    async def test_partial_failure_never_reports_success_and_retry_is_safe(self):
        self.client._send_and_wait.side_effect = [{"code": 200}, {"code": 403}]
        self.assertFalse((await self.post()).json()["success"])
        self.assertEqual(self.client._send_and_wait.await_count, 2)
        self.assertFalse(self.first.events)
        self.client._send_and_wait.side_effect = None
        self.assertTrue((await self.post()).json()["success"])
        self.assertEqual(self.client._send_and_wait.await_count, 4)
        self.assertEqual(len(self.first.events), 1)

    async def test_invalid_request_or_extra_account_fields_never_reach_im(self):
        invalid = [
            {"messageIds": []}, {"messageIds": ["same"] * 101}, {"messageIds": [123]},
            {"messageIds": [" "]}, {"lastMessageId": "not-in-list"}, {"lastMessageId": 123},
            {"cid": ""}, {"cid": "@goofish"}, {"cid": "chat@evil"}, {"cid": "chat\n"},
            {"lastMessageTime": 0}, {"lastMessageTime": True}, {"lastMessageTime": "1794100000000"},
            {"lastMessageTime": 9007199254740992}, {"account_id": "other"}, {"owner_id": 2},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertEqual((await self.post(**changes)).status_code, 422)
        self.client._send_and_wait.assert_not_awaited()

    async def test_duplicate_message_ids_only_report_once(self):
        result = (await self.post(messageIds=["older.PNM", "newer.PNM", "newer.PNM"])).json()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["messageIds"], ["older.PNM", "newer.PNM"])
        self.assertEqual(self.client._send_and_wait.await_args_list[0].args[1]["body"], [["older.PNM", "newer.PNM"]])

    async def test_browser_failure_does_not_turn_confirmed_read_into_failure(self):
        self.manager._broadcast = AsyncMock(side_effect=RuntimeError("synthetic browser failure"))
        self.assertTrue((await self.post()).json()["success"])
        self.assertEqual(self.client._send_and_wait.await_count, 2)


class ReadProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_ack_and_business_rejection_never_clear_readpoint(self):
        for response in ({}, {"body": {}}, {"code": 403}, {"code": 200, "body": {"reason": "synthetic denied"}}):
            client = GoofishImClient("synthetic", "unb=123;")
            client._send_and_wait = AsyncMock(return_value=response)
            with self.subTest(response=response), self.assertRaises(Exception):
                await client.mark_read("chat", ["id.PNM"], "id.PNM")
            client._send_and_wait.assert_awaited_once()

    async def test_token_refresh_retries_only_same_idempotent_read_boundary(self):
        client = GoofishImClient("synthetic", "unb=123;")
        client._send_and_wait = AsyncMock(side_effect=[ImRequestRejected("synthetic expired", 400),
                                                     {"code": 200}, {"code": 200}])
        client._reconnect_with_new_token = AsyncMock(return_value=True)
        await client.mark_read("chat", ["id.PNM"], "id.PNM")
        self.assertEqual(client._send_and_wait.await_count, 3)
        frames = [call.args[1] for call in client._send_and_wait.await_args_list]
        self.assertEqual(frames[0]["body"], frames[1]["body"])
        self.assertEqual(frames[2]["lwp"], "/r/Conversation/clearRedPoint")
        self.assertFalse(any(frame["lwp"].startswith("/r/MessageSend") for frame in frames))
        client._reconnect_with_new_token.assert_awaited_once()

    async def test_client_rejects_empty_or_unrelated_boundary_without_transport(self):
        client = GoofishImClient("synthetic", "unb=123;")
        client._send_and_wait = AsyncMock()
        for ids, boundary in (([], ""), (["one"], "two")):
            with self.assertRaises(ValueError):
                await client.mark_read("chat", ids, boundary)
        client._send_and_wait.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
