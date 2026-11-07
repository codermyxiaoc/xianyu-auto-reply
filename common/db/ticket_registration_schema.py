"""统一初始化锁内的门票表、索引、默认票种与专用密钥自检。"""
from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text, update
from sqlalchemy.schema import CreateColumn, CreateIndex
from common.models.ticket_registration import TicketRegistration, TicketType
from common.models.system_setting import SystemSetting
from common.schemas.ticket_registration import DEFAULT_TICKET_AGE_RULES, TICKET_AGE_RULES_SETTING, TicketAgeRules
from common.schemas.ticket_age_policy import TICKET_AGE_ENABLED_SETTING
from common.utils.ticket_age import infer_ticket_category
from common.utils.time_utils import get_beijing_now_naive

TICKET_KEY_SETTING = "security.ticket_registration_key"
DEFAULT_TICKET_TYPES = tuple(f"{days}日{person}票" for days in ("1.5", "1", "2") for person in ("成人", "儿童", "老人"))


async def ensure_ticket_registration_schema(conn) -> None:
    for model in (TicketType, TicketRegistration):
        table = model.__table__
        await conn.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
        columns = set((await conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name"
        ), {"name": table.name})).scalars())
        for column in table.columns:
            if column.name not in columns:
                definition = str(CreateColumn(column).compile(dialect=conn.dialect))
                if column.primary_key:
                    definition += " PRIMARY KEY"
                await conn.execute(text(f"ALTER TABLE `{table.name}` ADD COLUMN {definition}"))
        indexes = set((await conn.execute(text(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:name"
        ), {"name": table.name})).scalars())
        for index in table.indexes:
            if index.name not in indexes:
                await conn.execute(CreateIndex(index))
    names = set((await conn.execute(select(TicketType.name))).scalars())
    now = get_beijing_now_naive()
    for position, name in enumerate(DEFAULT_TICKET_TYPES):
        if name not in names:
            await conn.execute(insert(TicketType).values(
                name=name, category=infer_ticket_category(name), sort_order=position, enabled=True, created_at=now, updated_at=now,
            ))
    # 只补缺失分类，保留已有分类和所有历史登记，不按新规则改写买家资料。
    missing_categories = (await conn.execute(select(TicketType.id, TicketType.name).where(TicketType.category.is_(None)))).all()
    for type_id, name in missing_categories:
        category = infer_ticket_category(name)
        if category:
            await conn.execute(update(TicketType).where(TicketType.id == type_id).values(category=category))
    age_rules = (await conn.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_AGE_RULES_SETTING))).scalar_one_or_none()
    if age_rules is None:
        await conn.execute(insert(SystemSetting).values(
            key=TICKET_AGE_RULES_SETTING,
            value=TicketAgeRules.model_validate(DEFAULT_TICKET_AGE_RULES).model_dump_json(),
            description="门票身份信息年龄规则（以游玩日期计算周岁）",
        ))
    age_enabled = (await conn.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_AGE_ENABLED_SETTING))).scalar_one_or_none()
    if age_enabled is None:
        await conn.execute(insert(SystemSetting).values(
            key=TICKET_AGE_ENABLED_SETTING, value="true", description="门票身份信息年龄校验开关",
        ))
    key = (await conn.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_KEY_SETTING))).scalar_one_or_none()
    if key is None:
        # 不在业务请求中生成，避免读请求提交用户事务；初始化已持有数据库命名锁。
        await conn.execute(insert(SystemSetting).values(
            key=TICKET_KEY_SETTING, value=Fernet.generate_key().decode("ascii"),
            description="门票实名信息专用加密密钥（自动维护，请勿修改或删除）",
        ))
    else:
        # 已存在但损坏时拒绝启动，绝不换密钥导致历史身份信息不可解密。
        try:
            Fernet(key.encode("ascii"))
        except Exception:
            raise RuntimeError("门票实名信息加密密钥无效，请恢复原密钥") from None
