"""分批遍历 IM 历史，搜索范围不受前端已加载页数限制。

任务只保留遍历游标及上一批响应，十分钟未继续即过期。每批可重试，
使用 revision 避免网络重试跳过命中记录；不调用发消息、已读等写接口。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import secrets
import time
from typing import Awaitable, Callable


class HistorySearchError(ValueError):
    pass


@dataclass
class SearchJob:
    id: str
    owner_id: int
    query: str
    accounts: list[dict]
    skipped_accounts: int
    touched_at: float = field(default_factory=time.monotonic)
    revision: int = 0
    account_index: int = 0
    conversation_cursor: int | None = None
    conversation_cursors: set[int] = field(default_factory=set)
    conversations_seen: set[str] = field(default_factory=set)
    conversations: list[dict] = field(default_factory=list)
    conversations_finished: bool = False
    message_cursor: int | None = None
    message_cursors: set[int] = field(default_factory=set)
    messages_seen: set[str] = field(default_factory=set)
    scanned_conversations: int = 0
    scanned_messages: int = 0
    last_response: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def complete(self) -> bool:
        return self.account_index >= len(self.accounts)

    def response(self, hits: list[dict], error: str = "") -> dict:
        return {"search_id": self.id, "revision": self.revision, "query": self.query,
                "results": hits, "complete": self.complete,
                "scanned_conversations": self.scanned_conversations, "scanned_messages": self.scanned_messages,
                "total_accounts": len(self.accounts), "finished_accounts": self.account_index,
                "skipped_accounts": self.skipped_accounts, "error": error}


def next_cursor(page: dict, current: int | None, seen: set[int]) -> int | None:
    if not page.get("hasMore"):
        return None
    candidate = page.get("nextCursor")
    try:
        if isinstance(candidate, bool) or not isinstance(candidate, (str, int)):
            raise ValueError
        candidate = int(candidate)
        if candidate < 0 or (current is not None and candidate >= current) or candidate in seen:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise HistorySearchError("闲鱼返回的历史翻页位置异常，搜索尚未完成，请稍后继续或重新搜索") from None
    return candidate


class ChatHistorySearch:
    TTL = 600

    def __init__(self):
        self.jobs: dict[str, SearchJob] = {}
        self.batch_slots = asyncio.Semaphore(2)
        self.account_locks: dict[str, asyncio.Lock] = {}

    def create(self, owner_id: int, query: str, accounts: list[dict], skipped_accounts: int) -> dict:
        now = time.monotonic()
        self.jobs = {key: job for key, job in self.jobs.items() if job.lock.locked() or now - job.touched_at < self.TTL}
        owned = sorted((job for job in self.jobs.values() if job.owner_id == owner_id), key=lambda job: job.touched_at)
        if len(owned) >= 3:
            oldest = next((job for job in owned if not job.lock.locked()), None)
            if oldest is None:
                raise HistorySearchError("已有搜索正在处理，请稍后重试")
            self.jobs.pop(oldest.id, None)
        if len(self.jobs) >= 100:
            raise HistorySearchError("搜索任务较多，请稍后重试")
        job = SearchJob(secrets.token_urlsafe(24), owner_id, query.strip(), accounts, skipped_accounts)
        self.jobs[job.id] = job
        return job.response([])

    def get(self, identifier: str, owner_id: int) -> SearchJob:
        job = self.jobs.get(identifier)
        if job is None or job.owner_id != owner_id or time.monotonic() - job.touched_at >= self.TTL:
            raise HistorySearchError("搜索已过期或不可访问，请重新搜索")
        job.touched_at = time.monotonic()
        return job

    async def advance(
        self, job: SearchJob, revision: int, allowed_accounts: set[str],
        fetch_conversations: Callable[[str, int | None], Awaitable[dict]],
        fetch_messages: Callable[[str, str, int | None], Awaitable[dict]],
    ) -> dict:
        async with job.lock:
            if any(account["account_id"] not in allowed_accounts for account in job.accounts):
                raise HistorySearchError("账号访问权限已变化，请重新搜索")
            if revision == job.revision - 1 and job.last_response is not None:
                return job.last_response
            if revision != job.revision:
                raise HistorySearchError("搜索进度已变化，请重新搜索")
            if job.complete:
                return job.response([])
            hits, error = [], ""
            keyword = job.query.casefold()
            async with self.batch_slots:
                started = time.monotonic()
                for _ in range(4):
                    if job.complete or time.monotonic() - started > 6:
                        break
                    account = job.accounts[job.account_index]
                    account_id = account["account_id"]
                    try:
                        async with self.account_locks.setdefault(account_id, asyncio.Lock()):
                            if job.conversations:
                                conversation = job.conversations[0]
                                page = await asyncio.wait_for(fetch_messages(account_id, conversation["cid"], job.message_cursor), 15)
                                cursor = next_cursor(page, job.message_cursor, job.message_cursors)
                                for message in page["messages"]:
                                    identifier = str(message.get("messageId") or (message.get("senderId"), message.get("time"), message.get("text")))
                                    if identifier in job.messages_seen:
                                        continue
                                    job.messages_seen.add(identifier)
                                    job.scanned_messages += 1
                                    if keyword in str(message.get("text", "")).casefold():
                                        hits.append({"key": f"{account_id}:{conversation['cid']}:m:{identifier}",
                                                     "account": account, "conversation": conversation, "message": message})
                                if cursor is None:
                                    job.conversations.pop(0)
                                    job.scanned_conversations += 1
                                    job.message_cursor = None
                                    job.message_cursors.clear()
                                    job.messages_seen.clear()
                                else:
                                    job.message_cursor = cursor
                                    job.message_cursors.add(cursor)
                            elif job.conversations_finished:
                                job.account_index += 1
                                job.conversation_cursor = None
                                job.conversation_cursors.clear()
                                job.conversations_seen.clear()
                                job.conversations_finished = False
                                continue
                            else:
                                page = await asyncio.wait_for(fetch_conversations(account_id, job.conversation_cursor), 15)
                                cursor = next_cursor(page, job.conversation_cursor, job.conversation_cursors)
                                for conversation in page["conversations"]:
                                    if not conversation.get("cid") or conversation["cid"] in job.conversations_seen:
                                        continue
                                    job.conversations_seen.add(conversation["cid"])
                                    job.conversations.append(conversation)
                                    fields = ("otherUserId", "otherUserName", "itemTitle")
                                    if any(keyword in str(conversation.get(field, "")).casefold() for field in fields):
                                        hits.append({"key": f"{account_id}:{conversation['cid']}:c",
                                                     "account": account, "conversation": conversation, "message": None})
                                job.conversation_cursor = cursor
                                job.conversations_finished = cursor is None
                                if cursor is not None:
                                    job.conversation_cursors.add(cursor)
                            await asyncio.sleep(0.2)
                    except asyncio.CancelledError:
                        # 请求中断时，之前已推进的页面仍须可按旧 revision 重取，
                        # 否则断网/取消会跳过已命中但尚未送达浏览器的消息。
                        job.revision += 1
                        job.touched_at = time.monotonic()
                        job.last_response = job.response(hits)
                        raise
                    except HistorySearchError as exc:
                        error = str(exc)
                        break
                    except Exception:
                        # 不记录关键词、聊天正文或完整上游异常，保留游标供重试。
                        error = "读取闲鱼历史暂时失败，搜索尚未完成；请检查账号连接后继续"
                        break
            job.revision += 1
            job.touched_at = time.monotonic()
            job.last_response = job.response(hits, error)
            return job.last_response
