"""在线聊天已读上报：通过闲鱼原生已读与红点接口同步平台状态。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db_session
from app.services.chat_new import get_im_session_manager
from common.models import User, XYAccount
from common.schemas.common import ApiResponse
from common.utils.auth_scope import is_admin_user

router = APIRouter(prefix="/chat-new")
MessageId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


class MarkReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(strict=True, min_length=1, max_length=128)
    messageIds: list[MessageId] = Field(min_length=1, max_length=100)
    lastMessageId: MessageId
    lastMessageTime: int = Field(strict=True, ge=1, le=9007199254740991)

    @field_validator("cid", "lastMessageId")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("会话和消息编号不能包含空白或控制字符")
        return value

    @field_validator("messageIds")
    @classmethod
    def validate_message_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            cls.validate_identifier(value)
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_read_boundary(self):
        if self.lastMessageId not in self.messageIds:
            raise ValueError("已读边界必须是本次已读消息之一")
        if "@" in self.cid and (not self.cid.endswith("@goofish") or self.cid.count("@") != 1):
            raise ValueError("会话编号无效")
        if not self.cid.removesuffix("@goofish"):
            raise ValueError("会话编号无效")
        return self


@router.post("/mark-read/{account_id}")
async def mark_conversation_read(
    account_id: str, payload: MarkReadRequest,
    current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db_session),
):
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if account is None:
        return ApiResponse(success=False, message="账号不存在或无权操作")
    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接，请先连接")
    cid = payload.cid.removesuffix("@goofish")
    try:
        await client.mark_read(cid, payload.messageIds, payload.lastMessageId)
    except Exception as exc:
        logger.warning("【{}】上报会话已读失败: {}", account_id, type(exc).__name__)
        return ApiResponse(success=False, message="已读状态未同步成功，请稍后重试")
    data = {"account_id": account_id, "cid": cid, "messageIds": payload.messageIds,
            "lastMessageId": payload.lastMessageId, "lastMessageTime": payload.lastMessageTime}
    await manager.broadcast_conversation_read(account_id, data)
    return ApiResponse(success=True, message="已同步已读状态", data=data)
