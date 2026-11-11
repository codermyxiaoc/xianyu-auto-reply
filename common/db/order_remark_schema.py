"""订单内部备注的无损、幂等启动迁移。"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text


async def ensure_order_remark_schema(conn) -> None:
    """在启动命名锁内调用；仅补缺失列，失败时阻止新 ORM 带病启动。"""
    table_exists = (await conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'xy_orders'"
    ))).scalar()
    if not table_exists:
        raise RuntimeError("订单表不存在，无法初始化内部备注")
    exists = (await conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'xy_orders' AND COLUMN_NAME = 'remark'"
    ))).scalar()
    if not exists:
        await conn.execute(text(
            "ALTER TABLE xy_orders ADD COLUMN remark TEXT NULL COMMENT '订单内部备注'"
        ))
        logger.info("订单内部备注字段已补齐：xy_orders.remark")
