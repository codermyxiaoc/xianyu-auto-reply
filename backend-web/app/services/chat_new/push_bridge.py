"""订阅自动回复进程已经收到的 IM 消息，补足独立聊天连接的推送。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from typing import Awaitable, Callable

from loguru import logger

from common.db.redis_client import get_redis_client
from common.services.chat_realtime_bridge import CHAT_EVENT_CHANNEL, MAX_ACCOUNT_ID_LENGTH, MAX_EVENT_BYTES
from .push_message_parser import PushMessageParser

RETRY_DELAY = 2.0


class ChatPushBridge:
    """进程内只订阅一次；消息仅转给已完成账号授权的浏览器订阅。"""

    def __init__(self, has_subscribers: Callable[[str], bool], forward: Callable[[str, dict], Awaitable[None]]):
        self._has_subscribers = has_subscribers
        self._forward = forward
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="chat-new-push-bridge")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _handle_data(self, data) -> None:
        if not isinstance(data, (str, bytes)) or len(data) > MAX_EVENT_BYTES:
            return
        try:
            if isinstance(data, str) and len(data.encode("utf-8")) > MAX_EVENT_BYTES:
                return
            event = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict) or type(event.get("version")) is not int or event["version"] != 1:
            return
        account_id = event.get("account_id")
        myid = event.get("self_user_id")
        raw_message = event.get("message")
        if not isinstance(account_id, str) or not 0 < len(account_id) <= MAX_ACCOUNT_ID_LENGTH:
            return
        if not self._has_subscribers(account_id):
            return
        if not isinstance(myid, str) or not 0 < len(myid) <= MAX_ACCOUNT_ID_LENGTH or not isinstance(raw_message, dict):
            return
        parsed = PushMessageParser(account_id, myid).parse(raw_message)
        if parsed and parsed.get("cid"):
            await self._forward(account_id, parsed)

    async def _run(self) -> None:
        while True:
            pubsub = None
            try:
                redis = await get_redis_client()
                pubsub = redis.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(CHAT_EVENT_CHANNEL)
                logger.info("在线聊天跨进程消息订阅已连接")
                # listen() 的无限阻塞读取会回落连接池的 socket_timeout（默认5秒），
                # 空闲时触发重连并丢失期间的 Pub/Sub 消息。显式短轮询超时仅返回
                # None，保留当前订阅连接，也使关闭任务能及时取消。
                while True:
                    event = await pubsub.get_message(timeout=1.0)
                    if event and event.get("type") == "message":
                        await self._handle_data(event.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不记录原始消息、Redis URL 或凭据。
                logger.warning("在线聊天跨进程消息订阅中断，将重连: {}", type(exc).__name__)
            finally:
                if pubsub is not None:
                    try:
                        await asyncio.wait_for(pubsub.aclose(), timeout=2)
                    except Exception:
                        pass
            await asyncio.sleep(RETRY_DELAY)
