"""
IM会话管理器

功能：
1. 管理多账号的GoofishImClient实例（单例模式）
2. 根据account_id获取或创建IM客户端连接
3. 断开指定账号连接
4. 从数据库获取账号Cookie
5. 管理前端WebSocket客户端，将IM推送消息转发给前端
"""
import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Set

from loguru import logger
from sqlalchemy import select
from starlette.websockets import WebSocket

from common.db.session import async_session_maker
from common.models import XYAccount

from .im_client import GoofishImClient
from .push_bridge import ChatPushBridge

FRONTEND_SEND_TIMEOUT = 3.0
RECONNECT_RETRY_SECONDS = 5.0


class ImSessionManager:
    """IM会话管理器（单例），管理多账号的IM WebSocket客户端"""

    _instance: Optional["ImSessionManager"] = None

    def __init__(self):
        # account_id -> GoofishImClient
        self.clients: Dict[str, GoofishImClient] = {}
        self._lock = asyncio.Lock()
        # 前端 WebSocket 客户端: account_id -> Set[WebSocket]
        self._ws_clients: Dict[str, Set[WebSocket]] = {}
        self._reconnect_after: Dict[str, float] = {}
        self._push_bridge = ChatPushBridge(lambda aid: bool(self._ws_clients.get(aid)), self._broadcast)

    @classmethod
    def get_instance(cls) -> "ImSessionManager":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = ImSessionManager()
        return cls._instance

    async def get_or_connect(self, account_id: str) -> GoofishImClient:
        """
        获取已连接的客户端，或新建连接

        Args:
            account_id: 账号ID

        Returns:
            已连接的GoofishImClient

        Raises:
            ValueError: 账号不存在或Cookie为空
            Exception: 连接失败
        """
        async with self._lock:
            return await self._connect_locked(account_id)

    async def _connect_locked(self, account_id: str) -> GoofishImClient:
        previous = self.clients.get(account_id)
        if previous and previous.is_connected:
            return previous
        if previous:
            await previous.disconnect()
            # 失败时保留离线引用，表示用户仍希望连接；主动断开才删除它。
        account_context = await self._get_account_context(account_id)
        if not account_context:
            raise ValueError(f"账号Cookie为空: {account_id}")
        account_row_id, cookies_str = account_context
        client = GoofishImClient(account_id, cookies_str, account_row_id=account_row_id)
        if not await client.connect():
            raise Exception(f"IM连接失败: {account_id}")
        self.clients[account_id] = client
        self._reconnect_after.pop(account_id, None)
        await self._setup_push_callback_after_connect(account_id, client)
        return client

    async def reconnect_existing(self, account_id: str) -> GoofishImClient | None:
        """浏览器重新订阅时恢复意外断线；不复活用户主动移除的账号。"""
        async with self._lock:
            if account_id not in self.clients:
                return None
            if self.clients[account_id].is_connected:
                return self.clients[account_id]
            now = asyncio.get_running_loop().time()
            if now < self._reconnect_after.get(account_id, 0):
                return None
            try:
                return await self._connect_locked(account_id)
            except Exception:
                self._reconnect_after[account_id] = asyncio.get_running_loop().time() + RECONNECT_RETRY_SECONDS
                raise

    async def disconnect(self, account_id: str):
        """
        断开指定账号的IM连接

        Args:
            account_id: 账号ID
        """
        async with self._lock:
            client = self.clients.pop(account_id, None)
            self._reconnect_after.pop(account_id, None)
            if client:
                await client.disconnect()
                logger.info(f"【{account_id}】IM会话已断开并移除")

    async def disconnect_all(self):
        """断开所有连接"""
        await self._push_bridge.stop()
        async with self._lock:
            for account_id, client in list(self.clients.items()):
                try:
                    await client.disconnect()
                except Exception as e:
                    logger.warning(f"【{account_id}】断开连接异常: {e}")
            self.clients.clear()
            self._reconnect_after.clear()
            logger.info("所有IM会话已断开")

    def get_connected_account_ids(self) -> list:
        """获取所有已连接的账号ID列表"""
        return [
            aid for aid, client in self.clients.items()
            if client.is_connected
        ]

    # ==================== 前端 WebSocket 客户端管理 ====================

    async def register_ws_client(self, account_id: str, ws: WebSocket):
        """
        注册前端 WebSocket 客户端，同时在 IM 客户端上挂载推送回调

        Args:
            account_id: 账号ID
            ws: 前端 WebSocket 连接
        """
        if account_id not in self._ws_clients:
            self._ws_clients[account_id] = set()
        self._ws_clients[account_id].add(ws)
        self._push_bridge.start()
        logger.info(f"【{account_id}】前端WebSocket客户端已注册，当前连接数: {len(self._ws_clients[account_id])}")

        # 确保 IM 客户端上已挂载推送回调
        client = self.clients.get(account_id)
        if client and client.is_connected:
            self._ensure_push_callback(account_id, client)

    async def unregister_ws_client(self, account_id: str, ws: WebSocket):
        """
        注销前端 WebSocket 客户端

        Args:
            account_id: 账号ID
            ws: 前端 WebSocket 连接
        """
        clients = self._ws_clients.get(account_id)
        if clients:
            clients.discard(ws)
            if not clients:
                del self._ws_clients[account_id]
        logger.info(
            f"【{account_id}】前端WebSocket客户端已注销，"
            f"剩余连接数: {len(self._ws_clients.get(account_id, set()))}"
        )

    def _ensure_push_callback(self, account_id: str, client: GoofishImClient):
        """确保 IM 客户端上已挂载推送回调（避免重复注册）"""
        # 用属性标记是否已挂载
        if getattr(client, "_push_cb_registered", False):
            return

        async def _forward_to_frontend(parsed_msg: dict):
            """将解析后的推送消息转发给所有前端 WebSocket 客户端"""
            await self._broadcast(account_id, parsed_msg)

        client.add_push_callback(_forward_to_frontend)
        client._push_cb_registered = True
        logger.info(f"【{account_id}】IM推送回调已挂载")

    async def broadcast_message(self, account_id: str, cid: str, message: dict) -> None:
        """确认发送成功后同步所有已授权订阅，不依赖上游是否回显自己的消息。"""
        try:
            await self._broadcast(account_id, {"event": "new_message", "cid": cid.split("@")[0], "message": message})
        except Exception as exc:
            # IM 已确认发送，浏览器推送失败不能伪装成发送失败诱发用户重复发送。
            logger.warning("【{}】同步已发送消息失败: {}", account_id, type(exc).__name__)

    async def broadcast_conversation_read(self, account_id: str, data: dict) -> None:
        """上游确认后同步已读边界，浏览器保留边界之后新到达的未读。"""
        try:
            await self._broadcast(account_id, {**data, "event": "conversation_read"})
        except Exception as exc:
            logger.warning("【{}】同步会话已读失败: {}", account_id, type(exc).__name__)

    async def _broadcast(self, account_id: str, event: dict) -> None:
        ws_set = self._ws_clients.get(account_id)
        if not ws_set:
            return
        payload = json.dumps({**event, "account_id": account_id}, ensure_ascii=False)
        # await 期间页面可以增加/注销订阅，不能遍历正在修改的原集合。
        subscribers = tuple(ws_set)

        async def deliver(ws):
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=FRONTEND_SEND_TIMEOUT)
                return True
            except Exception:
                # 关闭慢/失效订阅，让浏览器重连补偿；不能继续保持假在线。
                try:
                    await asyncio.wait_for(ws.close(code=1013), timeout=1)
                except Exception:
                    pass
                return False

        delivered = await asyncio.gather(*(deliver(ws) for ws in subscribers))
        for ws, success in zip(subscribers, delivered):
            if not success:
                ws_set.discard(ws)
        # 旧订阅全断开后可能已经注册了新集合，不要把新订阅一起删除。
        if not ws_set and self._ws_clients.get(account_id) is ws_set:
            self._ws_clients.pop(account_id, None)

    async def _setup_push_callback_after_connect(
        self, account_id: str, client: GoofishImClient
    ):
        """连接成功后，如果已有前端客户端在等待，立即挂载推送回调"""
        if account_id in self._ws_clients and self._ws_clients[account_id]:
            self._ensure_push_callback(account_id, client)

    async def _get_account_context(self, account_id: str) -> tuple[int, str] | None:
        """
        从数据库获取账号行 ID 和 Cookie。

        Args:
            account_id: 账号ID

        Returns:
            ``(账号行ID, Cookie字符串)``，账号不存在或 Cookie 为空时返回 ``None``。
        """
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(XYAccount).where(
                        XYAccount.account_id == account_id
                    )
                )
                account = result.scalar_one_or_none()

                if not account:
                    logger.error(
                        f"【{account_id}】数据库中未找到账号"
                    )
                    return None

                if not account.cookie:
                    logger.error(
                        f"【{account_id}】账号Cookie为空"
                    )
                    return None

                return int(account.id), account.cookie

        except Exception as e:
            logger.error(f"【{account_id}】获取账号Cookie失败: {e}")
            return None


def get_im_session_manager() -> ImSessionManager:
    """获取IM会话管理器单例的快捷方法"""
    return ImSessionManager.get_instance()
