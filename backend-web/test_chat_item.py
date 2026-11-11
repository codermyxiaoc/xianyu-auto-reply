"""本账号商品卡片：真实 SQLite 查询与模拟 IM 出站帧，绝不连接闲鱼。"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
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
from app.api.routes import chat_new_item
from app.services.chat_new.im_client import GoofishImClient
from app.services.chat_new.im_session_manager import ImSessionManager
from app.services.chat_new.im_transport import ImRequestRejected
from app.services.chat_new.item_service import serialize_chat_item
from common.models import User, XYAccount, XYCatalogItem
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


class ChatItemApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (XYAccount, XYCatalogItem):
            model.__table__.create(self.engine)
        self.session = Session(self.engine)
        self.db = AsyncSessionAdapter(self.session)
        self.user = User(id=1, role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.session.add_all([
            XYAccount(id=1, owner_id=1, account_id="mine", cookie="synthetic", login_method="cookie"),
            XYAccount(id=2, owner_id=1, account_id="second", cookie="synthetic", login_method="cookie"),
            XYAccount(id=3, owner_id=2, account_id="other", cookie="synthetic", login_method="cookie"),
        ])
        for index in range(1, 24):
            self.session.add(self.row(index, 1, 1, str(10000 + index), f"合成商品 {index}"))
        # 相同商品ID存在另一账号时，也不能跨账号取标题图片价格。
        self.session.add_all([
            self.row(101, 1, 2, "10001", "第二账号同ID商品"),
            self.row(102, 1, 2, "20001", "第二账号独有商品"),
            self.row(103, 2, 3, "30001", "其他用户商品"),
            self.row(104, 2, 1, "40001", "损坏owner映射商品"),
            self.row(105, 1, 1, "50001", "缺图商品", detail={}),
        ])
        self.session.commit()
        self.manager = ImSessionManager()
        self.im = SimpleNamespace(is_connected=True, myid="synthetic-seller",
            send_item_message=AsyncMock(return_value={"messageId": "server-item-id", "uuid": "send-uuid"}))
        self.manager.clients = {"mine": self.im, "second": self.im, "other": self.im}
        self.tabs = [Browser(), Browser()]
        self.other_tab = Browser()
        self.manager._ws_clients = {"mine": set(self.tabs), "other": {self.other_tab}}
        self.manager_patch = patch.object(chat_new_item, "get_im_session_manager", return_value=self.manager)
        self.manager_getter = self.manager_patch.start()
        self.app = FastAPI()
        self.app.include_router(chat_new_item.router, prefix="/api/v1")
        self.app.dependency_overrides[get_current_active_user] = lambda: self.user
        self.app.dependency_overrides[get_db_session] = lambda: self.db
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    @staticmethod
    def row(identifier, owner, account, item_id, title, detail=None):
        return XYCatalogItem(id=identifier, owner_id=owner, account_pk=account, item_id=item_id,
            title=title, price="8.88", created_at=datetime(2026, 11, 1) + timedelta(seconds=identifier),
            metadata_json={"detail": json.dumps(detail if detail is not None else {
                "pic_info": {"url": "https://img.alicdn.com/synthetic-product.jpg"},
                "title": "过期标题", "price_text": "888.00", "cookie": "must-not-return",
            })})

    async def asyncTearDown(self):
        self.manager_patch.stop()
        await self.http.aclose()
        self.session.close()
        self.engine.dispose()

    async def list(self, account="mine", **params):
        return await self.http.get(f"/api/v1/chat-new/items/{account}", params=params)

    async def send(self, account="mine", **changes):
        return await self.http.post(f"/api/v1/chat-new/send-item/{account}", json={
            "cid": "synthetic-chat", "toUserId": "999999999", "itemId": "10001", **changes})

    async def test_pagination_is_exact_account_and_owner_with_safe_fields(self):
        first = await self.list(page=1, page_size=20)
        self.assertIn("no-store", first.headers["cache-control"])
        data = first.json()["data"]
        self.assertEqual((data["total"], data["total_pages"], len(data["items"])), (24, 2, 20))
        second = (await self.list(page=2, page_size=20)).json()["data"]
        self.assertEqual(len(second["items"]), 4)
        items = data["items"] + second["items"]
        self.assertEqual(len({item["itemId"] for item in items}), 24)
        self.assertFalse({"20001", "30001", "40001"} & {item["itemId"] for item in items})
        self.assertNotIn("must-not-return", first.text)
        selected = next(item for item in items if item["itemId"] == "10001")
        self.assertEqual(selected["title"], "合成商品 1")
        self.assertEqual(selected["price"], "¥8.88")
        self.assertTrue(selected["canSend"])
        self.assertEqual(selected["url"], "https://www.goofish.com/item?id=10001")

    async def test_search_uses_title_or_id_and_literal_wildcards(self):
        data = (await self.list(keyword="10001")).json()["data"]
        self.assertEqual([item["itemId"] for item in data["items"]], ["10001"])
        data = (await self.list(keyword="缺图")).json()["data"]
        self.assertEqual(data["items"][0]["itemId"], "50001")
        self.assertFalse(data["items"][0]["canSend"])
        self.assertIn("图片", data["items"][0]["unavailableReason"])
        for keyword in ("%", "_", "must-not-return"):
            self.assertEqual((await self.list(keyword=keyword)).json()["data"]["total"], 0)

    async def test_forbidden_accounts_do_not_access_global_manager(self):
        for account in ("other", "missing"):
            self.assertFalse((await self.list(account)).json()["success"])
            self.assertFalse((await self.send(account)).json()["success"])
        self.manager_getter.assert_not_called()
        self.im.send_item_message.assert_not_awaited()

    async def test_admin_still_cannot_take_goods_from_different_account_or_wrong_owner(self):
        self.user.role = UserRole.ADMIN
        self.assertEqual((await self.list("other")).json()["data"]["total"], 1)
        for item_id in ("20001", "30001", "40001", "99999"):
            self.assertFalse((await self.send(itemId=item_id)).json()["success"])
        self.im.send_item_message.assert_not_awaited()
        self.assertTrue((await self.send("other", itemId="30001")).json()["success"])

    async def test_send_uses_server_catalog_and_identical_http_broadcast(self):
        response = (await self.send()).json()
        self.assertTrue(response["success"])
        message = response["data"]["message"]
        self.assertEqual(message["itemCard"]["title"], "合成商品 1")
        self.assertEqual(message["itemCard"]["price"], "¥8.88")
        self.assertEqual(message["text"], "[宝贝] 合成商品 1")
        self.assertEqual(message["type"], "item")
        self.assertTrue(message["isSelf"])
        self.assertEqual(message["messageId"], "server-item-id")
        self.assertEqual(message["clientMessageId"], "send-uuid")
        for tab in self.tabs:
            self.assertEqual(tab.events[0]["message"], message)
        self.assertFalse(self.other_tab.events)
        self.assertEqual(self.im.send_item_message.await_args.kwargs["item_card"], message["itemCard"])

    async def test_missing_id_uses_stable_pending_confirmation(self):
        self.im.send_item_message.return_value = {"messageId": "", "uuid": "send-uuid"}
        message = (await self.send()).json()["data"]["message"]
        self.assertFalse(message["failed"])
        self.assertEqual(message["clientMessageId"], "send-uuid")
        self.assertEqual(self.tabs[0].events[0]["message"], message)

    async def test_no_send_when_offline_missing_image_or_official_recipient(self):
        self.im.is_connected = False
        self.assertFalse((await self.send()).json()["success"])
        self.im.is_connected = True
        self.assertFalse((await self.send(itemId="50001")).json()["success"])
        self.assertFalse((await self.send(toUserId="2223046249624")).json()["success"])
        self.im.send_item_message.assert_not_awaited()
        self.assertTrue(all(not tab.events for tab in self.tabs))

    async def test_strict_request_does_not_accept_client_supplied_product_contents(self):
        for change in ({"price": "0.01"}, {"mainPic": "https://example.invalid/a.jpg"}, {"title": "fake"},
                       {"itemId": 10001}, {"itemId": True}, {"itemId": "0"}, {"itemId": "01"},
                       {"itemId": "9223372036854775808"}, {"cid": " "}, {"toUserId": "\n"}):
            with self.subTest(change=change):
                self.assertEqual((await self.send(**change)).status_code, 422)
        self.im.send_item_message.assert_not_awaited()

    async def test_pagination_validation(self):
        for params in ({"page": 0}, {"page_size": 101}, {"page_size": 0}, {"keyword": "x" * 101}):
            self.assertEqual((await self.list(**params)).status_code, 422)

    async def test_timeout_or_rejection_does_not_broadcast_or_retry(self):
        for error in (TimeoutError("synthetic timeout"), RuntimeError("synthetic rejection")):
            self.im.send_item_message.reset_mock()
            self.im.send_item_message.side_effect = error
            self.assertFalse((await self.send()).json()["success"])
            self.im.send_item_message.assert_awaited_once()
        self.assertTrue(all(not tab.events for tab in self.tabs))


class ItemNormalizationTests(unittest.TestCase):
    def test_dict_detail_and_seller_image_url_are_supported(self):
        item = ChatItemApiTests.row(1, 1, 1, "10001", "合成商品")
        item.metadata_json = {"detail": {"image_url": "//img.alicdn.com/synthetic.jpg"}}
        self.assertEqual(serialize_chat_item(item)["mainPic"], "https://img.alicdn.com/synthetic.jpg")
        self.assertTrue(serialize_chat_item(item)["canSend"])

    def test_invalid_cached_metadata_must_not_create_sendable_card(self):
        item = ChatItemApiTests.row(1, 1, 1, "10001", "合成商品")
        for metadata in (None, [], {"detail": "invalid"}, {"detail": "[]"},
                         {"detail": {"pic_info": {"url": "javascript:alert(1)"}}}):
            item.metadata_json = metadata
            self.assertFalse(serialize_chat_item(item)["canSend"])
        item.metadata_json = {"detail": {"pic_info": {"url": "https://img.alicdn.com/synthetic.jpg"}}}
        item.price = "0.00"
        self.assertTrue(serialize_chat_item(item)["canSend"])
        item.price = "NaN"
        self.assertFalse(serialize_chat_item(item)["canSend"])


class ItemProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = GoofishImClient("synthetic", "unb=123456789;")
        self.card = {"itemId": "10001", "title": "合成手机支架", "price": "¥8.88",
                     "mainPic": "https://img.alicdn.com/synthetic.jpg", "url": "https://evil.invalid/ignored"}

    async def test_captured_frame_is_native_item_payload_and_known_outer_envelope(self):
        self.client._send_and_wait = AsyncMock(return_value={"code": 200, "body": {"messageId": "native-id"}})
        result = await self.client.send_item_message("synthetic-chat", "999999999", self.card)
        self.client._send_and_wait.assert_awaited_once()
        mid, frame = self.client._send_and_wait.await_args.args
        self.assertEqual(frame["headers"]["mid"], mid)
        self.assertEqual(frame["lwp"], "/r/MessageSend/sendByReceiverScope")
        first = frame["body"][0]
        self.assertEqual(first["cid"], "synthetic-chat@goofish")
        self.assertEqual(first["content"]["contentType"], 101)
        self.assertEqual(first["content"]["custom"]["type"], 1)
        payload = json.loads(base64.b64decode(first["content"]["custom"]["data"]))
        self.assertEqual(payload["contentType"], 7)
        self.assertEqual(payload["atUsers"], [])
        self.assertEqual(payload["itemCard"]["item"], {
            "itemId": 10001, "title": self.card["title"], "price": "¥8.88", "mainPic": self.card["mainPic"]})
        self.assertEqual(payload["itemCard"]["itemTip"], "我想要")
        self.assertEqual(frame["body"][1]["actualReceivers"], ["999999999@goofish", "123456789@goofish"])
        self.assertEqual(result["messageId"], "native-id")
        self.assertEqual(result["uuid"], first["uuid"])

    async def test_rejection_and_timeout_do_not_repeat_native_send(self):
        self.client._send_and_wait = AsyncMock(return_value={"code": 200, "body": {"reason": "synthetic refused"}})
        with self.assertRaisesRegex(Exception, "synthetic refused"):
            await self.client.send_item_message("chat", "999999999", self.card)
        self.client._send_and_wait.assert_awaited_once()
        self.client._send_and_wait = AsyncMock(side_effect=TimeoutError("synthetic timeout"))
        with self.assertRaises(TimeoutError):
            await self.client.send_item_message("chat", "999999999", self.card)
        self.client._send_and_wait.assert_awaited_once()

    async def test_token_reconnect_never_resends_non_idempotent_item(self):
        self.client._send_and_wait = AsyncMock(side_effect=ImRequestRejected("synthetic invalid token", 400))
        self.client._reconnect_with_new_token = AsyncMock(return_value=True)
        with self.assertRaises(ImRequestRejected):
            await self.client.send_item_message("chat", "999999999", self.card)
        self.client._send_and_wait.assert_awaited_once()
        self.client._reconnect_with_new_token.assert_awaited_once()

    async def test_invalid_card_and_robot_never_write_transport(self):
        self.client._send_and_wait = AsyncMock()
        for change in ({"itemId": "bad"}, {"price": "bad"}, {"mainPic": "javascript:x"}, {"title": ""}):
            with self.assertRaises(ValueError):
                await self.client.send_item_message("chat", "999999999", {**self.card, **change})
        with self.assertRaises(ValueError):
            await self.client.send_item_message("chat", "2223046249624", self.card)
        self.client._send_and_wait.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
