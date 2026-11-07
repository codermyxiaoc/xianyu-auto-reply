"""实时解析系统/订单所属用户的门票年龄策略，不跨用户缓存。"""
from __future__ import annotations

import json

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ticket_registration_errors import TicketRegistrationError
from common.models.system_setting import SystemSetting
from common.models.user import User
from common.models.user_setting import UserSetting
from common.schemas.ticket_age_policy import (
    PERSONAL_TICKET_AGE_POLICY_SETTING, TICKET_AGE_ENABLED_SETTING,
    PersonalTicketAgePolicy, PersonalTicketAgePolicyRequest, TicketAgePolicy,
)
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES, TICKET_AGE_RULES_SETTING, TicketAgeRules


class TicketAgePolicyService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def system(self) -> dict:
        rows = (await self.session.execute(select(SystemSetting.key, SystemSetting.value).where(
            SystemSetting.key.in_((TICKET_AGE_ENABLED_SETTING, TICKET_AGE_RULES_SETTING))
        ))).all()
        values = dict(rows)
        try:
            policy = TicketAgePolicy.model_validate({
                "enabled": json.loads(values[TICKET_AGE_ENABLED_SETTING]) if TICKET_AGE_ENABLED_SETTING in values else True,
                "rules": json.loads(values[TICKET_AGE_RULES_SETTING]) if TICKET_AGE_RULES_SETTING in values else DEFAULT_TICKET_AGE_RULES,
            })
        except (ValidationError, ValueError, TypeError):
            raise TicketRegistrationError("门票年龄策略不可用，请联系管理员在系统设置中重新保存", 503) from None
        return policy.model_dump()

    async def save_system(self, payload: TicketAgePolicy) -> dict:
        # 初始化阶段已创建规则和开关键；固定顺序锁定，两个值在同一事务保存。
        rows = (await self.session.execute(select(SystemSetting).where(
            SystemSetting.key.in_((TICKET_AGE_ENABLED_SETTING, TICKET_AGE_RULES_SETTING))
        ).order_by(SystemSetting.key).with_for_update())).scalars().all()
        settings = {row.key: row for row in rows}
        updates = {
            TICKET_AGE_ENABLED_SETTING: (json.dumps(payload.enabled), "门票身份信息年龄校验开关"),
            TICKET_AGE_RULES_SETTING: (payload.rules.model_dump_json(), "门票身份信息年龄规则（以游玩日期计算周岁）"),
        }
        for key, (value, description) in updates.items():
            if key in settings:
                settings[key].value = value
            else:
                self.session.add(SystemSetting(key=key, value=value, description=description))
        await self.session.commit()
        return payload.model_dump()

    async def save_rules(self, rules: TicketAgeRules) -> dict:
        """兼容旧规则接口：仅更新范围，不能改变校验开关。"""
        row = (await self.session.execute(select(SystemSetting).where(
            SystemSetting.key == TICKET_AGE_RULES_SETTING
        ).with_for_update())).scalar_one_or_none()
        if row is None:
            self.session.add(SystemSetting(key=TICKET_AGE_RULES_SETTING, value=rules.model_dump_json(),
                description="门票身份信息年龄规则（以游玩日期计算周岁）"))
        else:
            row.value = rules.model_dump_json()
        await self.session.commit()
        return rules.model_dump()

    async def personal(self, user_id: int) -> dict:
        # 历史表没有 user_id/key 唯一约束；读取最新值，保存时在用户行锁内清除重复。
        value = (await self.session.execute(select(UserSetting.value).where(
            UserSetting.user_id == user_id, UserSetting.key == PERSONAL_TICKET_AGE_POLICY_SETTING,
        ).order_by(UserSetting.id.desc()).limit(1))).scalar_one_or_none()
        if value is None:
            return {"use_personal": False, **(await self.system())}
        try:
            policy = PersonalTicketAgePolicy.model_validate_json(value)
        except (ValidationError, ValueError, TypeError):
            raise TicketRegistrationError("个人门票年龄策略不可用，请订单所属用户在个人设置中重新保存", 503) from None
        if not policy.use_personal:
            return {"use_personal": False, **(await self.system())}
        return policy.model_dump()

    async def save_personal(self, user_id: int, payload: PersonalTicketAgePolicyRequest) -> dict:
        # 锁父级用户行，覆盖首次保存的空键间隙，使并发保存不会产生重复键。
        owner = (await self.session.execute(select(User.id).where(User.id == user_id).with_for_update())).scalar_one_or_none()
        if owner is None:
            raise TicketRegistrationError("用户不存在，请重新登录", 404)
        await self.session.execute(delete(UserSetting).where(
            UserSetting.user_id == user_id, UserSetting.key == PERSONAL_TICKET_AGE_POLICY_SETTING,
        ))
        if payload.use_personal:
            self.session.add(UserSetting(user_id=user_id, key=PERSONAL_TICKET_AGE_POLICY_SETTING,
                value=payload.model_dump_json(), description="个人门票身份信息年龄校验策略"))
        await self.session.commit()
        return payload.model_dump() if payload.use_personal else {"use_personal": False, **(await self.system())}

    async def effective(self, owner_id: int) -> dict:
        policy = await self.personal(owner_id)
        return {"enabled": policy["enabled"], "rules": policy["rules"]}
