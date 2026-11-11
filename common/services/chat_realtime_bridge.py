"""将已解密的 IM 消息发布给在线聊天服务，不保存消息或触发业务发送。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from common.db.redis_client import get_redis_client


CHAT_EVENT_CHANNEL = "chat_new:im_events:v1"
CHAT_EVENT_PUBLISH_TIMEOUT = 0.5
MAX_EVENT_BYTES = 1024 * 1024
MAX_ACCOUNT_ID_LENGTH = 128


async def publish_chat_event(
    account_id: str, self_user_id: str, message: dict[str, Any]
) -> bool:
    """尽力发布实时事件；Redis 不可用时返回 False，让原消息处理继续。

    PUBLISH 没有订阅者时也算成功。这里只使用瞬时 Pub/Sub，不写入 Redis
    键或记录消息内容；正常的任务取消仍向上传播，以便服务及时退出。
    """
    if not isinstance(account_id, str) or not 0 < len(account_id) <= MAX_ACCOUNT_ID_LENGTH:
        return False
    if not isinstance(self_user_id, str) or not 0 < len(self_user_id) <= MAX_ACCOUNT_ID_LENGTH:
        return False
    if not isinstance(message, dict):
        return False

    try:
        payload = json.dumps(
            {
                "version": 1,
                "account_id": account_id,
                "self_user_id": self_user_id,
                "message": message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
            return False

        async def publish() -> None:
            client = await get_redis_client()
            await client.publish(CHAT_EVENT_CHANNEL, payload)

        await asyncio.wait_for(publish(), timeout=CHAT_EVENT_PUBLISH_TIMEOUT)
        return True
    except Exception:
        # 不记录异常详情：底层异常可能包含消息参数。该旁路不影响自动回复。
        return False
