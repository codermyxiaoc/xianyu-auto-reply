"""在线聊天我的宝贝列表及原生商品卡片发送。"""
from __future__ import annotations

import re
import time
import uuid

from fastapi import APIRouter, Depends, Query, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db_session
from app.services.chat_new import get_im_session_manager
from app.services.chat_new.item_service import ChatItemService
from common.models import User, XYAccount
from common.schemas.common import ApiResponse
from common.utils.auth_scope import is_admin_user
from common.utils.xianyu_official_accounts import ensure_xianyu_message_recipient

router = APIRouter(prefix="/chat-new")


class SendItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(strict=True, min_length=1, max_length=128)
    toUserId: str = Field(strict=True, min_length=1, max_length=64)
    itemId: str = Field(strict=True, min_length=1, max_length=19)

    @field_validator("cid", "toUserId")
    @classmethod
    def validate_chat_identifier(cls, value: str) -> str:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("会话及接收方编号不能包含空白或控制字符")
        return value

    @field_validator("itemId")
    @classmethod
    def validate_item_id(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 9223372036854775807:
            raise ValueError("商品编号无效")
        return value


async def _owned_account(account_id: str, current_user: User, db: AsyncSession) -> XYAccount | None:
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    return (await db.execute(query)).scalar_one_or_none()


@router.get("/items/{account_id}")
async def list_chat_items(
    account_id: str, response: Response,
    page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    keyword: str = Query(default="", max_length=100),
    current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "no-store, private"
    account = await _owned_account(account_id, current_user, db)
    if account is None:
        return ApiResponse(success=False, message="账号不存在或无权查看")
    return ApiResponse(success=True, data=await ChatItemService(db).list_items(account, page, page_size, keyword))


@router.post("/send-item/{account_id}")
async def send_item(
    account_id: str, req: SendItemRequest,
    current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db_session),
):
    account = await _owned_account(account_id, current_user, db)
    if account is None:
        return ApiResponse(success=False, message="账号不存在或无权操作")
    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接，请先连接")
    item = await ChatItemService(db).get_item(account, req.itemId)
    if item is None:
        return ApiResponse(success=False, message="商品不存在或不属于当前账号")
    if not item["canSend"]:
        return ApiResponse(success=False, message=item["unavailableReason"])
    card = {key: item[key] for key in ("itemId", "title", "price", "mainPic", "url")}
    try:
        ensure_xianyu_message_recipient(req.toUserId)
        sent_at = int(time.time() * 1000)
        result = await client.send_item_message(cid=req.cid, to_user_id=req.toUserId, item_card=card)
        message_id = str(result.get("messageId", "") or "")
        message = {
            "messageId": message_id, "clientMessageId": str(result.get("uuid") or uuid.uuid4().hex),
            "senderId": client.myid, "senderName": "", "isSelf": True, "type": "item",
            "text": "[宝贝] " + card["title"], "images": [], "itemCard": card, "time": sent_at,
            **({"failed": False} if not message_id else {}),
        }
        await manager.broadcast_message(account_id, req.cid, message)
        return ApiResponse(success=True, message="发送成功", data={"message": message})
    except Exception as exc:
        logger.warning("【{}】发送商品卡片失败: {}", account_id, type(exc).__name__)
        return ApiResponse(success=False, message=f"发送失败：{str(exc)}")
