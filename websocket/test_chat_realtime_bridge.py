"""只使用合成消息和模拟 Redis；不连接闲鱼、数据库或发送买家消息。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.services import chat_realtime_bridge as bridge


# 隔离加载该处理器，避免与 backend-web 的同名 app 包发生冲突。
PACKAGE_NAME = "_chat_realtime_bridge_test_xianyu"
PACKAGE_PATH = ROOT / "websocket" / "app" / "services" / "xianyu"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.message_handler", PACKAGE_PATH / "message_handler.py"
)
handler_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler_module)


class ChatEventPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Mock(spec=["publish"])
        self.redis.publish = AsyncMock(return_value=1)
        self.get_client = AsyncMock(return_value=self.redis)
        self.patch_client = patch.object(bridge, "get_redis_client", self.get_client)
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)

    async def test_publishes_exact_envelope_without_mutation_or_storage(self):
        message = {"1": {"10": {"reminderContent": "合成测试消息"}}, "id": "9007199254740993999"}
        before = json.loads(json.dumps(message))
        self.assertTrue(await bridge.publish_chat_event("9007199254740993999", "buyer-self", message))
        channel, payload = self.redis.publish.await_args.args
        self.assertEqual(channel, "chat_new:im_events:v1")
        self.assertEqual(json.loads(payload), {
            "version": 1, "account_id": "9007199254740993999",
            "self_user_id": "buyer-self", "message": before,
        })
        self.assertIn("合成测试消息", payload)
        self.assertEqual(message, before)
        self.assertEqual(len(self.redis.mock_calls), 1)

    async def test_no_subscribers_is_success(self):
        self.redis.publish.return_value = 0
        self.assertTrue(await bridge.publish_chat_event("account", "self", {}))

    async def test_redis_acquisition_failure_returns_false(self):
        self.get_client.side_effect = ConnectionError("synthetic connection failure")
        self.assertFalse(await bridge.publish_chat_event("account", "self", {}))
        self.redis.publish.assert_not_awaited()

    async def test_publish_failure_returns_false(self):
        self.redis.publish.side_effect = RuntimeError("synthetic publish failure")
        self.assertFalse(await bridge.publish_chat_event("account", "self", {}))

    async def test_timeout_covers_client_acquisition(self):
        cancelled = asyncio.Event()

        async def slow_client():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.get_client.side_effect = slow_client
        with patch.object(bridge, "CHAT_EVENT_PUBLISH_TIMEOUT", 0.01):
            self.assertFalse(await bridge.publish_chat_event("account", "self", {}))
        self.assertTrue(cancelled.is_set())
        self.redis.publish.assert_not_awaited()

    async def test_timeout_covers_publish(self):
        cancelled = asyncio.Event()

        async def slow_publish(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.redis.publish.side_effect = slow_publish
        with patch.object(bridge, "CHAT_EVENT_PUBLISH_TIMEOUT", 0.01):
            self.assertFalse(await bridge.publish_chat_event("account", "self", {}))
        self.assertTrue(cancelled.is_set())

    async def test_shutdown_cancellation_is_not_swallowed(self):
        self.get_client.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await bridge.publish_chat_event("account", "self", {})

    async def test_unserializable_payload_returns_false_before_redis(self):
        for message in ({"bad": object()}, {"bad": float("nan")}):
            with self.subTest(message=type(message["bad"]).__name__):
                self.assertFalse(await bridge.publish_chat_event("account", "self", message))
        self.get_client.assert_not_awaited()

    async def test_invalid_envelopes_are_ignored(self):
        for account, self_id, message in (("", "self", {}), (123, "self", {}),
                                           ("account", "", {}), ("account", "self", []),
                                           ("a" * 129, "self", {}), ("account", "s" * 129, {})):
            with self.subTest(account=account, self_id=self_id):
                self.assertFalse(await bridge.publish_chat_event(account, self_id, message))
        self.get_client.assert_not_awaited()

    async def test_maximum_identifier_lengths_are_accepted(self):
        self.assertTrue(await bridge.publish_chat_event("a" * 128, "s" * 128, {}))
        self.redis.publish.assert_awaited_once()

    async def test_payload_limit_counts_utf8_bytes_including_envelope(self):
        self.assertEqual(bridge.MAX_EVENT_BYTES, 1024 * 1024)
        message = {"text": "合成消息"}
        payload = json.dumps({
            "version": 1, "account_id": "account", "self_user_id": "self", "message": message,
        }, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        encoded_length = len(payload.encode("utf-8"))
        self.assertGreater(encoded_length, len(payload))
        with patch.object(bridge, "MAX_EVENT_BYTES", encoded_length - 1):
            self.assertFalse(await bridge.publish_chat_event("account", "self", message))
        self.get_client.assert_not_awaited()
        with patch.object(bridge, "MAX_EVENT_BYTES", encoded_length):
            self.assertTrue(await bridge.publish_chat_event("account", "self", message))
        self.redis.publish.assert_awaited_once()

    async def test_oversized_message_does_not_access_redis(self):
        self.assertFalse(await bridge.publish_chat_event(
            "account", "self", {"text": "x" * bridge.MAX_EVENT_BYTES},
        ))
        self.get_client.assert_not_awaited()


class MessageHandlerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(handler_module.MessageHandler, "_load_message_expire_time", return_value=3600):
            self.handler = handler_module.MessageHandler("account", "self")
        self.events = []
        self.handler._send_ack = AsyncMock(side_effect=lambda *args: self.events.append("ack"))
        self.handler._process_single_message = AsyncMock(side_effect=lambda message, ws: self.events.append(("process", message)))
        self.socket = object()

    def sync_package(self, count=1):
        return {"body": {"syncPushPackage": {"data": [{"data": "synthetic"}] * count}}}

    async def test_bridge_receives_each_decrypted_dict_before_original_processing(self):
        messages = [{"id": "one"}, {"id": "two"}]
        self.handler._decrypt_message = Mock(side_effect=messages)

        async def published(account, self_id, message):
            self.assertEqual((account, self_id), ("account", "self"))
            self.events.append(("publish", message))
            return True

        with patch.object(handler_module, "publish_chat_event", side_effect=published):
            self.assertTrue(await self.handler.handle_message(self.sync_package(2), self.socket))
        self.assertEqual(self.events, ["ack", ("publish", messages[0]), ("process", messages[0]),
                                      ("publish", messages[1]), ("process", messages[1])])

    async def test_redis_failure_does_not_interrupt_original_processing(self):
        messages = [{"id": "one"}, {"id": "two"}]
        self.handler._decrypt_message = Mock(side_effect=messages)
        with patch.object(bridge, "get_redis_client", AsyncMock(side_effect=ConnectionError("offline"))):
            self.assertTrue(await self.handler.handle_message(self.sync_package(2), self.socket))
        self.assertEqual(self.events, ["ack", ("process", messages[0]), ("process", messages[1])])

    async def test_timeout_does_not_interrupt_original_processing(self):
        message = {"id": "one"}
        self.handler._decrypt_message = Mock(return_value=message)
        slow_client = AsyncMock(side_effect=lambda: None)

        async def unavailable():
            await asyncio.Event().wait()

        slow_client.side_effect = unavailable
        with patch.object(bridge, "get_redis_client", slow_client), patch.object(bridge, "CHAT_EVENT_PUBLISH_TIMEOUT", 0.01):
            self.assertTrue(await self.handler.handle_message(self.sync_package(), self.socket))
        self.handler._process_single_message.assert_awaited_once_with(message, self.socket)

    async def test_invalid_decryption_is_not_published_or_processed(self):
        self.handler._decrypt_message = Mock(side_effect=[None, ["invalid"]])
        with patch.object(handler_module, "publish_chat_event", AsyncMock()) as publish:
            self.assertTrue(await self.handler.handle_message(self.sync_package(2), self.socket))
            publish.assert_not_awaited()
        self.handler._process_single_message.assert_not_awaited()

    async def test_non_sync_control_message_keeps_original_path(self):
        message = {"code": 200}
        self.handler._process_single_message.return_value = True
        self.handler._process_single_message.side_effect = None
        with patch.object(handler_module, "publish_chat_event", AsyncMock()) as publish:
            self.assertTrue(await self.handler.handle_message(message, self.socket))
            publish.assert_not_awaited()
        self.handler._process_single_message.assert_awaited_once_with(message, self.socket)


if __name__ == "__main__":
    unittest.main()
