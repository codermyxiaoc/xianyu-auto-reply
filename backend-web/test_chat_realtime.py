"""在线聊天推送/发送同步回归：全部使用合成数据、内存 SQLite 和假网络。"""
from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import UploadFile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from app.api.routes import chat_new, chat_new_image, chat_new_ws
from app.services.chat_new import im_session_manager as manager_module
from app.services.chat_new import push_bridge as bridge_module
from app.services.chat_new.im_client import GoofishImClient
from app.services.chat_new.im_session_manager import ImSessionManager
from app.services.chat_new.im_transport import ImRequestRejected
from app.services.chat_new.push_bridge import ChatPushBridge
from common.models import User, XYAccount
from common.models.user import UserRole, UserStatus
from common.services import chat_realtime_bridge as publisher_module


class Browser:
    def __init__(self, action=None):
        self.events = []
        self.closed = []
        self.action = action

    async def send_text(self, data):
        if self.action:
            await self.action()
        self.events.append(json.loads(data))

    async def close(self, code):
        self.closed.append(code)


def synthetic_event(account_id="owner-account", sender="buyer", message_id="100001"):
    return {"version": 1, "account_id": account_id, "self_user_id": "seller", "message": {
        "1": {"2": "chat@goofish", "3": message_id, "5": 1794100000000,
              "10": {"senderUserId": sender, "senderNick": "测试买家", "reminderContent": "合成消息"}}
    }}


class RealtimeManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ImSessionManager()
        self.manager._push_bridge.start = Mock()

    async def test_subscriptions_can_change_during_broadcast(self):
        async def unregister():
            await self.manager.unregister_ws_client("a", first)
            await self.manager.register_ws_client("a", added)
        first, second, added = Browser(unregister), Browser(), Browser()
        await self.manager.register_ws_client("a", first)
        await self.manager.register_ws_client("a", second)
        await self.manager._broadcast("a", {"event": "new_message", "cid": "c"})
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(second.events), 1)
        self.assertFalse(added.events)
        self.assertEqual(second.events[0]["account_id"], "a")
        self.assertEqual(self.manager._ws_clients["a"], {second, added})

    async def test_stale_failed_set_does_not_delete_new_subscription(self):
        new = Browser()
        async def replace():
            await self.manager.unregister_ws_client("a", old)
            await self.manager.register_ws_client("a", new)
            raise RuntimeError("closed socket")
        old = Browser(replace)
        await self.manager.register_ws_client("a", old)
        await self.manager._broadcast("a", {"event": "new_message"})
        self.assertEqual(self.manager._ws_clients["a"], {new})
        self.assertEqual(old.closed, [1013])

    async def test_slow_tab_cannot_block_other_tab_or_cross_account(self):
        async def blocked():
            await asyncio.Event().wait()
        slow, healthy, unrelated = Browser(blocked), Browser(), Browser()
        self.manager._ws_clients = {"a": {slow, healthy}, "b": {unrelated}}
        with patch.object(manager_module, "FRONTEND_SEND_TIMEOUT", 0.01):
            await self.manager._broadcast("a", {"event": "new_message"})
        self.assertEqual(len(healthy.events), 1)
        self.assertFalse(unrelated.events)
        self.assertEqual(slow.closed, [1013])
        self.assertEqual(self.manager._ws_clients["a"], {healthy})

    async def test_failed_browser_sync_cannot_change_acked_send_result(self):
        self.manager._broadcast = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        await self.manager.broadcast_message("a", "chat@goofish", {"messageId": "id"})
        self.assertEqual(self.manager._broadcast.await_args.args[1]["cid"], "chat")

    async def test_concurrent_reconnect_only_connects_once_and_restores_callback(self):
        old = SimpleNamespace(is_connected=False, disconnect=AsyncMock())
        replacement = SimpleNamespace(is_connected=False, add_push_callback=Mock())
        async def connect():
            await asyncio.sleep(0)
            replacement.is_connected = True
            return True
        replacement.connect = AsyncMock(side_effect=connect)
        self.manager.clients["a"] = old
        self.manager._ws_clients["a"] = {Browser()}
        self.manager._get_account_context = AsyncMock(return_value=(1, "synthetic-cookie"))
        with patch.object(manager_module, "GoofishImClient", return_value=replacement):
            results = await asyncio.gather(*(self.manager.reconnect_existing("a") for _ in range(3)))
        self.assertEqual(results, [replacement] * 3)
        replacement.connect.assert_awaited_once()
        replacement.add_push_callback.assert_called_once()
        old.disconnect.assert_awaited_once()

    async def test_reconnect_backoff_preserves_intent_but_manual_disconnect_does_not_revive(self):
        old = SimpleNamespace(is_connected=False, disconnect=AsyncMock())
        self.manager.clients["a"] = old
        self.manager._get_account_context = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            await self.manager.reconnect_existing("a")
        self.assertIs(self.manager.clients["a"], old)
        self.assertIsNone(await self.manager.reconnect_existing("a"))
        self.manager._get_account_context.assert_awaited_once()
        await self.manager.disconnect("a")
        self.assertIsNone(await self.manager.reconnect_existing("a"))

    async def test_shutdown_stops_bridge(self):
        self.manager._push_bridge.stop = AsyncMock()
        await self.manager.disconnect_all()
        self.manager._push_bridge.stop.assert_awaited_once()


class PushBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_bridge_reuses_parser_and_scopes_account(self):
        forward = AsyncMock()
        bridge = ChatPushBridge(lambda aid: aid == "owner-account", forward)
        await bridge._handle_data(json.dumps(synthetic_event()))
        account_id, event = forward.await_args.args
        self.assertEqual(account_id, "owner-account")
        self.assertEqual(event["cid"], "chat")
        self.assertEqual(event["message"]["messageId"], "100001")
        self.assertFalse(event["message"]["isSelf"])
        await bridge._handle_data(json.dumps(synthetic_event(sender="seller")))
        self.assertTrue(forward.await_args.args[1]["message"]["isSelf"])
        await bridge._handle_data(json.dumps(synthetic_event(account_id="other-account")))
        self.assertEqual(forward.await_count, 2)

    async def test_bad_events_are_ignored(self):
        forward = AsyncMock()
        bridge = ChatPushBridge(lambda aid: True, forward)
        for bad in (b"\xff", "{", "[]", "null", "x" * (bridge_module.MAX_EVENT_BYTES + 1),
                    "中" * (bridge_module.MAX_EVENT_BYTES // 2),
                    json.dumps({**synthetic_event(), "version": True}),
                    json.dumps({**synthetic_event(), "version": 2}),
                    json.dumps({**synthetic_event(), "account_id": "a" * 129}),
                    json.dumps({**synthetic_event(), "self_user_id": "a" * 129}),
                    json.dumps({**synthetic_event(), "self_user_id": None}),
                    json.dumps({**synthetic_event(), "message": []})):
            await bridge._handle_data(bad)
        forward.assert_not_awaited()

    async def test_subscription_reconnects_and_shutdown_closes_it(self):
        got_event = asyncio.Event()
        first, second = Mock(), Mock()
        first.subscribe = AsyncMock(side_effect=ConnectionError("synthetic offline"))
        first.aclose = AsyncMock()
        second.subscribe = AsyncMock()
        second.aclose = AsyncMock()
        polls = 0
        async def get_message(timeout):
            nonlocal polls
            self.assertEqual(timeout, 1.0)
            polls += 1
            if polls == 1:
                return None  # 正常空闲，不断开、不重新订阅。
            if polls == 2:
                return {"type": "message", "data": json.dumps(synthetic_event())}
            await asyncio.Event().wait()
        second.get_message = get_message
        redis = Mock()
        redis.pubsub.side_effect = [first, second]
        async def forward(*args):
            got_event.set()
        bridge = ChatPushBridge(lambda aid: True, forward)
        with patch.object(bridge_module, "get_redis_client", AsyncMock(return_value=redis)), \
             patch.object(bridge_module, "RETRY_DELAY", 0.001):
            bridge.start()
            task = bridge._task
            bridge.start()
            self.assertIs(bridge._task, task)
            await asyncio.wait_for(got_event.wait(), 1)
            await bridge.stop()
        first.aclose.assert_awaited_once()
        second.aclose.assert_awaited_once()
        second.subscribe.assert_awaited_once()
        self.assertEqual(redis.pubsub.call_count, 2)
        self.assertIsNone(bridge._task)
        self.assertTrue(task.done())

    async def test_publisher_to_pubsub_to_two_browsers_without_im_connection(self):
        queue, subscribed, delivered = asyncio.Queue(), asyncio.Event(), asyncio.Event()
        class PubSub:
            async def subscribe(self, channel):
                self.channel = channel
                subscribed.set()
            async def get_message(self, timeout):
                return {"type": "message", "data": await queue.get()}
            async def aclose(self):
                pass
        pubsub = PubSub()
        class Redis:
            def pubsub(self, **kwargs):
                return pubsub
            async def publish(self, channel, payload):
                self.channel = channel
                await queue.put(payload)
        redis = Redis()
        manager = ImSessionManager()
        first, second, other = Browser(), Browser(), Browser()
        async def forward(account_id, event):
            await manager._broadcast(account_id, event)
            delivered.set()
        manager._push_bridge._forward = forward
        with patch.object(bridge_module, "get_redis_client", AsyncMock(return_value=redis)), \
             patch.object(publisher_module, "get_redis_client", AsyncMock(return_value=redis)):
            try:
                await manager.register_ws_client("owner-account", first)
                await manager.register_ws_client("owner-account", second)
                await manager.register_ws_client("other-account", other)
                await asyncio.wait_for(subscribed.wait(), 1)
                raw = synthetic_event()
                self.assertTrue(await publisher_module.publish_chat_event(
                    raw["account_id"], raw["self_user_id"], raw["message"]))
                await asyncio.wait_for(delivered.wait(), 1)
            finally:
                await manager.disconnect_all()
        self.assertEqual(redis.channel, pubsub.channel)
        self.assertEqual(first.events, second.events)
        self.assertEqual(first.events[0]["message"]["messageId"], "100001")
        self.assertFalse(other.events)
        self.assertFalse(manager.clients)


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session
    async def execute(self, statement):
        return self.session.execute(statement)


class SendSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        XYAccount.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.session.add_all([
            XYAccount(id=1, owner_id=1, account_id="mine", cookie="synthetic", login_method="cookie"),
            XYAccount(id=2, owner_id=2, account_id="other", cookie="synthetic", login_method="cookie"),
        ])
        self.session.commit()
        self.db = AsyncSessionAdapter(self.session)
        self.user = User(id=1, role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.manager = ImSessionManager()
        self.manager._push_bridge.start = Mock()
        self.client = SimpleNamespace(is_connected=True, myid="seller", cookies_str="synthetic",
            send_text_message=AsyncMock(return_value={"messageId": "real-id", "uuid": "protocol-uuid"}),
            send_image_message=AsyncMock(return_value={"messageId": "image-id", "uuid": "image-uuid"}),
            disconnect=AsyncMock())
        self.manager.clients = {"mine": self.client, "other": self.client}
        self.tabs = [Browser(), Browser()]
        self.manager._ws_clients = {"mine": set(self.tabs), "other": {Browser()}}
        self.req = chat_new.SendMessageRequest(cid="chat@goofish", toUserId="999999999", text="合成回复")

    async def asyncTearDown(self):
        self.session.close()
        self.engine.dispose()

    async def test_text_ack_and_two_tabs_share_message_identity_and_time(self):
        with patch.object(chat_new, "get_im_session_manager", return_value=self.manager):
            response = await chat_new.send_message("mine", self.req, self.user, self.db)
        self.assertTrue(response.success)
        self.client.send_text_message.assert_awaited_once()
        for tab in self.tabs:
            self.assertEqual(len(tab.events), 1)
            self.assertEqual(tab.events[0]["cid"], "chat")
            message = tab.events[0]["message"]
            for key in ("messageId", "clientMessageId", "time"):
                self.assertEqual(message[key], response.data[key])
            self.assertEqual(message["clientMessageId"], "protocol-uuid")
        self.assertFalse(next(iter(self.manager._ws_clients["other"])).events)

    async def test_text_without_server_id_can_be_reconciled_by_client_id(self):
        self.client.send_text_message.return_value = {}
        with patch.object(chat_new, "get_im_session_manager", return_value=self.manager):
            response = await chat_new.send_message("mine", self.req, self.user, self.db)
        self.assertTrue(response.success)
        self.assertEqual(response.data["messageId"], "")
        self.assertTrue(response.data["clientMessageId"])
        self.assertFalse(self.tabs[0].events[0]["message"]["failed"])
        self.assertEqual(response.data["clientMessageId"], self.tabs[1].events[0]["message"]["clientMessageId"])

    async def test_failed_send_never_broadcasts_or_retries(self):
        self.client.send_text_message.side_effect = RuntimeError("synthetic rejected")
        with patch.object(chat_new, "get_im_session_manager", return_value=self.manager):
            response = await chat_new.send_message("mine", self.req, self.user, self.db)
        self.assertFalse(response.success)
        self.client.send_text_message.assert_awaited_once()
        self.assertTrue(all(not tab.events for tab in self.tabs))

    async def test_send_and_disconnect_require_owner_before_manager_access(self):
        with patch.object(chat_new, "get_im_session_manager") as manager_getter:
            for account_id in ("other", "missing"):
                self.assertFalse((await chat_new.send_message(account_id, self.req, self.user, self.db)).success)
                self.assertFalse((await chat_new.disconnect_account(account_id, self.user, self.db)).success)
        manager_getter.assert_not_called()
        self.client.send_text_message.assert_not_awaited()
        self.client.disconnect.assert_not_awaited()

    async def test_admin_can_send_and_disconnect_other_owned_account(self):
        self.user.role = UserRole.ADMIN
        with patch.object(chat_new, "get_im_session_manager", return_value=self.manager):
            self.assertTrue((await chat_new.send_message("other", self.req, self.user, self.db)).success)
            self.assertTrue((await chat_new.disconnect_account("other", self.user, self.db)).success)
        self.client.send_text_message.assert_awaited_once()
        self.client.disconnect.assert_awaited_once()

    async def test_image_ack_is_broadcast_to_two_tabs_after_mock_upload(self):
        data = BytesIO()
        Image.new("RGB", (2, 3)).save(data, format="PNG")
        data.seek(0)
        upload = UploadFile(file=data, filename="synthetic.png", headers=Headers({"content-type": "image/png"}))
        uploader = AsyncMock()
        uploader.__aenter__.return_value = uploader
        uploader.upload_image.return_value = "https://example.invalid/synthetic.png"
        with patch.object(chat_new_image, "ImageUploader", return_value=uploader), \
             patch.object(chat_new_image, "get_im_session_manager", return_value=self.manager):
            response = await chat_new_image.send_image("mine", "chat", "999999999", upload, self.user, self.db)
        self.assertTrue(response.success)
        self.client.send_image_message.assert_awaited_once()
        self.assertEqual(self.client.send_image_message.await_args.kwargs["width"], 2)
        for tab in self.tabs:
            message = tab.events[0]["message"]
            self.assertEqual(message["images"], [response.data["imageUrl"]])
            self.assertEqual(message["type"], "image")
            for key in ("messageId", "clientMessageId", "time"):
                self.assertEqual(message[key], response.data[key])
        # 上传临时文件也清理，不污染项目数据。
        self.assertFalse(Path(uploader.upload_image.await_args.args[0]).exists())


class RegistrationAndWsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_or_timed_out_registration_never_sends_sync_ack(self):
        for response in ({"code": 403}, {"body": {}}, TimeoutError("synthetic timeout")):
            with self.subTest(response=response):
                client = GoofishImClient("synthetic", "unb=synthetic;")
                client._send_and_wait = AsyncMock()
                if isinstance(response, Exception):
                    client._send_and_wait.side_effect = response
                else:
                    client._send_and_wait.return_value = response
                client._send_raw = AsyncMock()
                with self.assertRaises((ImRequestRejected, TimeoutError)):
                    await client._register()
                client._send_raw.assert_not_awaited()
                self.assertFalse(client.is_connected)

    async def test_heartbeat_failure_marks_client_offline(self):
        client = GoofishImClient("synthetic", "unb=synthetic;")
        client._connected = client._registered = True
        client._ws = SimpleNamespace(closed=False)
        client._send_raw = AsyncMock(side_effect=ConnectionError("offline"))
        with patch("app.services.chat_new.im_transport.asyncio.sleep", AsyncMock()):
            await client._heartbeat_loop()
        self.assertFalse(client.is_connected)

    async def test_ws_reconnects_existing_client_without_bypassing_auth(self):
        ws = SimpleNamespace(accept=AsyncMock(), close=AsyncMock(), send_text=AsyncMock())
        user = SimpleNamespace(id=1)
        manager = SimpleNamespace(clients={"a": SimpleNamespace(is_connected=False)},
            reconnect_existing=AsyncMock(return_value=SimpleNamespace(is_connected=True)),
            register_ws_client=AsyncMock(), unregister_ws_client=AsyncMock())
        with patch.object(chat_new_ws, "_authenticate_ws_user", AsyncMock(return_value=user)), \
             patch.object(chat_new_ws, "_user_can_access_account", AsyncMock(return_value=True)), \
             patch.object(chat_new_ws, "get_im_session_manager", return_value=manager), \
             patch.object(chat_new_ws, "_receive_loop", AsyncMock(side_effect=WebSocketDisconnect())):
            await chat_new_ws.chat_new_websocket(ws, "a", "synthetic-token")
        manager.reconnect_existing.assert_awaited_once_with("a")
        manager.register_ws_client.assert_awaited_once_with("a", ws)
        manager.unregister_ws_client.assert_awaited_once_with("a", ws)

    async def test_forbidden_ws_never_accesses_im_manager(self):
        ws = SimpleNamespace(accept=AsyncMock(), close=AsyncMock())
        with patch.object(chat_new_ws, "_authenticate_ws_user", AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch.object(chat_new_ws, "_user_can_access_account", AsyncMock(return_value=False)), \
             patch.object(chat_new_ws, "get_im_session_manager") as manager_getter:
            await chat_new_ws.chat_new_websocket(ws, "other", "synthetic-token")
        ws.close.assert_awaited_once_with(code=4403)
        manager_getter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
