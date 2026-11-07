"""系统及订单所属用户的买家消息模板，不跨用户缓存或解释模板代码。"""
from __future__ import annotations

import re

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ticket_registration_errors import TicketRegistrationError
from common.models.system_setting import SystemSetting
from common.models.user import User
from common.models.user_setting import UserSetting
from common.schemas.ticket_message_template import (
    DEFAULT_TICKET_MESSAGE_TEMPLATE, PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
    TICKET_MESSAGE_TEMPLATE_SETTING, PersonalTicketMessageTemplate,
    PersonalTicketMessageTemplateRequest, TicketMessageTemplate,
)
from common.schemas.ticket_registration import TICKET_AGE_RULES_SETTING


def render_ticket_message(template: str, order_no: str, link: str) -> str:
    """一次替换原模板中的占位符，插入的订单号和 URL 不进行二次解释。"""
    values = {"orderId": order_no, "infourl": link}
    return re.sub(r"\{(orderId|infourl)\}", lambda match: values[match.group(1)], template)


class TicketMessageTemplateService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def system(self) -> dict:
        value = (await self.session.execute(select(SystemSetting.value).where(
            SystemSetting.key == TICKET_MESSAGE_TEMPLATE_SETTING
        ))).scalar_one_or_none()
        try:
            template = TicketMessageTemplate(message_template=value if value is not None else DEFAULT_TICKET_MESSAGE_TEMPLATE)
        except (ValidationError, ValueError, TypeError):
            raise TicketRegistrationError("系统买家消息模板不可用，请管理员在身份信息设置中重新保存", 503) from None
        return template.model_dump()

    async def save_system(self, payload: TicketMessageTemplate) -> dict:
        # 门票初始化已创建规则行；作为固定锁，避免首次并发新增模板时唯一键冲突。
        # 只锁定此行，不读取或改写年龄设置；模板本身无需新增数据库字段或迁移。
        await self.session.execute(select(SystemSetting.key).where(
            SystemSetting.key == TICKET_AGE_RULES_SETTING
        ).with_for_update())
        row = (await self.session.execute(select(SystemSetting).where(
            SystemSetting.key == TICKET_MESSAGE_TEMPLATE_SETTING
        ).with_for_update())).scalar_one_or_none()
        if row is None:
            self.session.add(SystemSetting(key=TICKET_MESSAGE_TEMPLATE_SETTING,
                value=payload.message_template, description="门票登记链接发送给买家的消息模板"))
        else:
            row.value = payload.message_template
        await self.session.commit()
        return payload.model_dump()

    async def personal(self, user_id: int) -> dict:
        value = (await self.session.execute(select(UserSetting.value).where(
            UserSetting.user_id == user_id, UserSetting.key == PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
        ).order_by(UserSetting.id.desc()).limit(1))).scalar_one_or_none()
        if value is None:
            return {"use_personal": False, **(await self.system())}
        try:
            template = PersonalTicketMessageTemplate.model_validate_json(value)
        except (ValidationError, ValueError, TypeError):
            raise TicketRegistrationError("个人买家消息模板不可用，请订单所属用户在身份信息设置中重新保存", 503) from None
        if not template.use_personal:
            return {"use_personal": False, **(await self.system())}
        return template.model_dump()

    async def save_personal(self, user_id: int, payload: PersonalTicketMessageTemplateRequest) -> dict:
        owner = (await self.session.execute(select(User.id).where(User.id == user_id).with_for_update())).scalar_one_or_none()
        if owner is None:
            raise TicketRegistrationError("用户不存在，请重新登录", 404)
        await self.session.execute(delete(UserSetting).where(
            UserSetting.user_id == user_id, UserSetting.key == PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
        ))
        if payload.use_personal:
            self.session.add(UserSetting(user_id=user_id, key=PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING,
                value=payload.model_dump_json(), description="个人门票买家消息模板"))
        await self.session.commit()
        return payload.model_dump() if payload.use_personal else {"use_personal": False, **(await self.system())}

    async def effective(self, owner_id: int) -> str:
        return (await self.personal(owner_id))["message_template"]
