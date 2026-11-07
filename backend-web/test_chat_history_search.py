"""只使用合成 IM 响应验证跨页历史搜索；不连接闲鱼或发送消息。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.chat_new.history_search import ChatHistorySearch, HistorySearchError, next_cursor


def conversation(cid, name="普通买家"):
    return {"cid": cid, "otherUserId": "buyer-" + cid, "otherUserName": name, "itemTitle": "测试商品"}


def message(identifier, text="门票关键词"):
    return {"messageId": identifier, "senderId": "buyer", "text": text, "time": 1794000000000}


def page(key, rows, cursor=None):
    return {key: rows, "hasMore": cursor is not None, "nextCursor": cursor}


class HistorySearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ChatHistorySearch()
        self.sleep = patch("app.services.chat_new.history_search.asyncio.sleep", new=AsyncMock())
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def job(self, accounts=("a",), query="关键词"):
        created = self.manager.create(1, query, [{"account_id": key} for key in accounts], 1)
        return self.manager.get(created["search_id"], 1)

    async def test_all_account_conversation_and_message_pages_are_searched(self):
        job = self.job(("a", "b"))
        conversations = AsyncMock(side_effect=lambda account, cursor: {
            ("a", None): page("conversations", [conversation("a1")], 100),
            ("a", 100): page("conversations", [conversation("a1"), conversation("a2", "关键词昵称")]),
            ("b", None): page("conversations", [conversation("b1")]),
        }[(account, cursor)])
        messages = AsyncMock(side_effect=lambda account, cid, cursor: {
            ("a", "a1", None): page("messages", [message("new", "不匹配")], 90),
            ("a", "a1", 90): page("messages", [message("old")], 80),
            ("a", "a1", 80): page("messages", [message("old"), message("oldest")]),
            ("a", "a2", None): page("messages", [message("second")]),
            ("b", "b1", None): page("messages", [message("other-account")]),
        }[(account, cid, cursor)])
        hits = []
        while not job.complete:
            result = await self.manager.advance(job, job.revision, {"a", "b"}, conversations, messages)
            self.assertFalse(result["error"])
            hits.extend(result["results"])
        self.assertEqual({hit["message"]["messageId"] for hit in hits if hit["message"]}, {"old", "oldest", "second", "other-account"})
        self.assertEqual(len([hit for hit in hits if hit["message"] is None]), 1)
        self.assertEqual(job.scanned_conversations, 3)
        self.assertEqual(job.scanned_messages, 5)
        self.assertEqual(conversations.await_count, 3)
        self.assertEqual(messages.await_count, 5)

    async def test_cancelled_batch_replays_hits_and_resumes_without_skipping(self):
        job = self.job()
        conversations = AsyncMock(return_value=page("conversations", [conversation("a1")]))
        second_page_started = asyncio.Event()

        async def fetch_messages(account, cid, cursor):
            if cursor is None:
                return page("messages", [message("first")], 100)
            second_page_started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.manager.advance(job, 0, {"a"}, conversations, fetch_messages))
        await second_page_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(job.revision, 1)
        replay = await self.manager.advance(job, 0, {"a"}, conversations, fetch_messages)
        self.assertEqual([hit["message"]["messageId"] for hit in replay["results"]], ["first"])
        self.assertEqual(conversations.await_count, 1)
        remaining = AsyncMock(return_value=page("messages", [message("second")]))
        result = await self.manager.advance(job, 1, {"a"}, conversations, remaining)
        self.assertEqual([hit["message"]["messageId"] for hit in result["results"]], ["second"])
        self.assertTrue(result["complete"])

    async def test_same_revision_is_idempotent_even_with_concurrent_retry(self):
        job = self.job()
        conversations = AsyncMock(return_value=page("conversations", [conversation("a1")]))
        messages = AsyncMock(return_value=page("messages", [message("match")]))
        results = await asyncio.gather(*[self.manager.advance(job, 0, {"a"}, conversations, messages) for _ in range(2)])
        self.assertEqual(results[0], results[1])
        self.assertEqual(messages.await_count, 1)

    async def test_failed_page_is_not_treated_as_complete_and_retry_preserves_cursor(self):
        job = self.job()
        conversations = AsyncMock(return_value=page("conversations", [conversation("a1")]))
        messages = AsyncMock(side_effect=[page("messages", [message("first")], 100), RuntimeError("temporary")])
        result = await self.manager.advance(job, 0, {"a"}, conversations, messages)
        self.assertFalse(result["complete"])
        self.assertTrue(result["error"])
        self.assertEqual(job.message_cursor, 100)
        self.assertEqual(len(result["results"]), 1)
        retry = AsyncMock(return_value=page("messages", [message("second")]))
        result = await self.manager.advance(job, 1, {"a"}, conversations, retry)
        retry.assert_awaited_once_with("a", "a1", 100)
        self.assertTrue(result["complete"])

    async def test_owner_revocation_and_expiry_precede_all_history_reads(self):
        job = self.job()
        with self.assertRaises(HistorySearchError):
            self.manager.get(job.id, 2)
        fetch = AsyncMock()
        with self.assertRaises(HistorySearchError):
            await self.manager.advance(job, 0, set(), fetch, fetch)
        fetch.assert_not_awaited()
        job.touched_at -= 601
        with self.assertRaises(HistorySearchError):
            self.manager.get(job.id, 1)

    async def test_bad_cursors_pause_instead_of_looping(self):
        for value in (None, 100, 101, True, "invalid", 99.5, -1):
            with self.subTest(cursor=value), self.assertRaises(HistorySearchError):
                next_cursor({"hasMore": True, "nextCursor": value}, 100, set())
        job = self.job()
        conversations = AsyncMock(return_value=page("conversations", [conversation("a1")]))
        messages = AsyncMock(side_effect=[page("messages", [message("first")], 100), page("messages", [message("invalid")], 101)])
        result = await self.manager.advance(job, 0, {"a"}, conversations, messages)
        self.assertFalse(result["complete"])
        self.assertTrue(result["error"])
        self.assertEqual(job.message_cursor, 100)


@unittest.skipUnless(os.getenv("TICKET_TEST_MYSQL_URL"), "需要显式设置隔离测试MySQL URL")
class HistorySearchAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_ticket_registration_integration as fixtures
        from app.api import deps
        from app.api.routes import chat_new
        from common.models.user import User, UserRole, UserStatus
        from common.models.xy_account import XYAccount
        self.fixtures = fixtures
        await fixtures.TicketRegistrationIntegrationTests.asyncSetUp(self)
        self.app.include_router(chat_new.router, prefix="/api/v1")
        self.deps, self.routes = deps, chat_new
        self.member = User(id=1, username="synthetic-member", role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        self.app.dependency_overrides[deps.get_current_active_user] = lambda: self.member
        async with self.sessions() as session:
            session.add_all([XYAccount(owner_id=owner, account_id=account, cookie="synthetic-cookie", login_method="manual", status="active")
                             for owner, account in [(1, "mine"), (2, "other"), (1, "offline")]])
            await session.commit()
        self.im = patch.object(chat_new, "get_im_session_manager")
        self.im_mock = self.im.start()
        self.im_mock.return_value.get_connected_account_ids.return_value = ["mine", "other"]
        self.search = patch.object(chat_new, "_history_search", ChatHistorySearch())
        self.search.start()

    async def asyncTearDown(self):
        self.im.stop()
        self.search.stop()
        await self.fixtures.TicketRegistrationIntegrationTests.asyncTearDown(self)

    async def test_account_scope_job_ownership_and_context_authorization(self):
        prefix = "/api/v1/chat-new"
        created = await self.client.post(prefix + "/history-search", json={"query": "合成关键词"})
        self.assertTrue(created.json()["success"])
        self.assertIn("no-store", created.headers["cache-control"])
        data = created.json()["data"]
        self.assertEqual(data["total_accounts"], 1)
        self.assertEqual(data["skipped_accounts"], 1)
        job = self.routes._history_search.get(data["search_id"], 1)
        self.assertEqual([account["account_id"] for account in job.accounts], ["mine"])
        other = await self.client.post(prefix + "/history-search", json={"query": "合成关键词", "account_id": "other"})
        self.assertFalse(other.json()["success"])
        self.im_mock.reset_mock()
        for path in ("/conversations/other", "/messages/other/cid"):
            response = await self.client.get(prefix + path)
            self.assertFalse(response.json()["success"])
            self.assertIn("no-store", response.headers["cache-control"])
        self.im_mock.assert_not_called()
        self.app.dependency_overrides[self.deps.get_current_active_user] = lambda: self.admin
        admin = await self.client.post(prefix + "/history-search", json={"query": "合成关键词"})
        self.assertEqual(admin.json()["data"]["total_accounts"], 2)
        self.member.id = 3
        self.app.dependency_overrides[self.deps.get_current_active_user] = lambda: self.member
        denied = await self.client.post(prefix + f"/history-search/{data['search_id']}/next", json={"revision": 0})
        self.assertFalse(denied.json()["success"])
        self.assertNotIn("合成关键词", denied.text)


if __name__ == "__main__":
    unittest.main()
