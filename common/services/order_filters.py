"""订单列表的筛选条件，保持列表与分页统计的查询口径一致。"""
from __future__ import annotations

from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import func, or_, select
from common.models.xy_order import XYOrder
from common.models.auto_reply_message_log import XYAutoReplyMessageLog
from common.models.ticket_registration import TicketRegistration


def build_order_filter_conditions(
    owner_id: int | None,
    *,
    account_id: str | None = None,
    item_ids: list[str] | None = None,
    order_no: str | None = None,
    status: str | None = None,
    search: str | None = None,
    delivery_method: str | None = None,
    is_bargain: bool | None = None,
    is_rated: bool | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    delivery_send_status: str | None = None,
    ticket_collection_status: str | None = None,
    ticket_export_status: str | None = None,
) -> list:
    conditions = []
    if owner_id is not None:
        conditions.append(XYOrder.owner_id == owner_id)
    if account_id:
        conditions.append(XYOrder.account_id == account_id)
    if item_ids:
        conditions.append(XYOrder.item_id.in_(item_ids))
    if order_no:
        conditions.append(XYOrder.order_no == order_no)
    if status:
        conditions.append(XYOrder.status == status)
    if search:
        conditions.append(or_(
            XYOrder.order_no.ilike(f"%{search}%"),
            XYOrder.item_id.ilike(f"%{search}%"),
            XYOrder.buyer_id.ilike(f"%{search}%"),
        ))
    if delivery_method is not None:
        if delivery_method == "none":
            conditions.append(or_(XYOrder.delivery_method.is_(None), XYOrder.delivery_method == ""))
        else:
            conditions.append(XYOrder.delivery_method == delivery_method)
    if is_bargain is not None:
        conditions.append(XYOrder.is_bargain == is_bargain)
    if is_rated is not None:
        conditions.append(XYOrder.is_rated == is_rated)
    # 显式关联订单，保证列表、计数以及已 JOIN 登记表的导出查询使用相同口径。
    registration = (
        select(TicketRegistration.id)
        .where(TicketRegistration.order_id == XYOrder.id)
        .correlate(XYOrder)
    )
    if ticket_collection_status:
        if ticket_collection_status == "uncreated":
            conditions.append(~registration.exists())
        elif ticket_collection_status == "pending":
            conditions.append(registration.where(TicketRegistration.submitted_at.is_(None)).exists())
        elif ticket_collection_status == "collected":
            conditions.append(registration.where(TicketRegistration.submitted_at.isnot(None)).exists())
        else:
            raise ValueError("无效的信息收集状态")
    if ticket_export_status:
        if ticket_export_status == "exported":
            conditions.append(registration.where(TicketRegistration.exported_at.isnot(None)).exists())
        elif ticket_export_status == "unexported":
            # 未创建登记的普通商品订单没有导出状态，不能混入“未导出”。
            conditions.append(registration.where(TicketRegistration.exported_at.is_(None)).exists())
        else:
            raise ValueError("无效的身份信息导出状态")
    # 此处沿用订单列表的下单时间筛选，不是门票的游玩日期。
    if start_date:
        try:
            conditions.append(XYOrder.placed_at >= datetime.strptime(start_date, "%Y-%m-%d"))
        except ValueError:
            logger.warning(f"无效的开始日期格式: {start_date}")
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            conditions.append(XYOrder.placed_at < end)
        except ValueError:
            logger.warning(f"无效的结束日期格式: {end_date}")
    # 与列表展示一致：按订单号最后一条auto_delivery日志的状态筛选。
    if delivery_send_status and delivery_send_status.strip():
        latest = (
            select(XYAutoReplyMessageLog.order_no.label("order_no"), func.max(XYAutoReplyMessageLog.id).label("max_id"))
            .where(XYAutoReplyMessageLog.reply_strategy == "auto_delivery", XYAutoReplyMessageLog.order_no.isnot(None))
            .group_by(XYAutoReplyMessageLog.order_no).subquery()
        )
        matched = (
            select(XYAutoReplyMessageLog.order_no)
            .join(latest, XYAutoReplyMessageLog.id == latest.c.max_id)
            .where(XYAutoReplyMessageLog.send_status == delivery_send_status.strip())
        )
        conditions.append(XYOrder.order_no.in_(matched))
    return conditions
