"""仅按明确勾选的订单导出已收集门票登记，每位游客一行。"""
from __future__ import annotations

import asyncio
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ticket_registration_service import TicketRegistrationError, TicketRegistrationService, ticket_order_scope
from common.models.ticket_registration import TicketRegistration
from common.models.xy_order import XYOrder
from common.utils.time_utils import get_beijing_now_naive

EXPORT_HEADERS = ("订单号", "日期", "姓名", "身份证", "票种", "用户名", "手机号", "备注")
XLSX_MAX_ROWS = 1_048_576


def build_ticket_workbook(rows: list[tuple]) -> bytes:
    """全部生成成功后才返回文件；文本类型同时避免长号码丢精度和公式执行。"""
    workbook = Workbook()
    workbook.remove(workbook.active)
    header_fill = PatternFill("solid", fgColor="2454A6")
    stripe_fill = PatternFill("solid", fgColor="F2F6FC")
    header_font = Font(name="Microsoft YaHei", size=11, color="FFFFFF", bold=True)
    body_font = Font(name="Microsoft YaHei", size=11, color="243247")
    widths = (29, 15, 19, 27, 23, 25, 19, 60)

    def new_sheet():
        number = len(workbook.worksheets) + 1
        sheet = workbook.create_sheet("门票登记" if number == 1 else f"门票登记{number}")
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        sheet.append(EXPORT_HEADERS)
        sheet.row_dimensions[1].height = 30
        for cell, width in zip(sheet[1], widths):
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", horizontal="left", indent=1)
            sheet.column_dimensions[cell.column_letter].width = width
        return sheet

    try:
        sheet = new_sheet()
        row_number = 1
        for values in rows:
            if row_number >= XLSX_MAX_ROWS:
                sheet.auto_filter.ref = sheet.dimensions
                sheet = new_sheet()
                row_number = 1
            row_number += 1
            sheet.row_dimensions[row_number].height = 26
            for column, value in enumerate(values, 1):
                cell = sheet.cell(row_number, column)
                if column == 2:
                    cell.value = value
                    cell.number_format = "yyyy-mm-dd"
                else:
                    cell.value = ILLEGAL_CHARACTERS_RE.sub("", str(value if value is not None else ""))
                    # number_format alone does not stop '=...' from becoming a formula.
                    cell.data_type = "s"
                    cell.number_format = "@"
                cell.font = body_font
                cell.alignment = Alignment(vertical="center", horizontal="left", indent=1, wrap_text=column == 8)
                if column == 8 and value:
                    sheet.row_dimensions[row_number].height = min(120, max(26, 18 * (str(value).count("\n") + 1)))
                if row_number % 2 == 0:
                    cell.fill = stripe_fill
        sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()


async def export_ticket_registrations(
    session: AsyncSession,
    *,
    order_ids: list[int],
    owner_id: int | None = None,
) -> bytes:
    if not order_ids or len(order_ids) > 1000:
        raise TicketRegistrationError("请勾选 1 至 1000 个要导出的订单")
    if any(type(value) is not int or value < 1 or value > 9223372036854775807 for value in order_ids):
        raise TicketRegistrationError("勾选的订单 ID 不正确")
    selected_ids = set(order_ids)
    scope = ticket_order_scope(owner_id)
    # 管理员与普通用户均先锁订单再标记登记，与保存备注的锁顺序一致。
    # 备注使用此锁定读取的当前值，不能使用 MySQL 旧事务快照中的普通查询值。
    locked_remarks = dict((await session.execute(select(XYOrder.id, XYOrder.remark).where(
        XYOrder.id.in_(selected_ids), *scope,
    ).order_by(XYOrder.id).with_for_update())).all())
    if owner_id is not None and set(locked_remarks) != selected_ids:
        raise TicketRegistrationError("勾选的订单不存在或无权导出，请刷新订单列表后重试", 403)
    statement = (
        select(XYOrder, TicketRegistration)
        .join(TicketRegistration, TicketRegistration.order_id == XYOrder.id)
        .where(XYOrder.id.in_(selected_ids), *scope, TicketRegistration.submitted_at.is_not(None))
        .order_by(XYOrder.placed_at.is_(None), XYOrder.placed_at.desc(), XYOrder.id.desc())
        .execution_options(yield_per=250)
    )
    service = TicketRegistrationService(session, owner_id=owner_id)
    # 先加载密钥，避免 MySQL 流式游标未关闭时在同一连接执行另一条查询。
    await service._get_cipher()
    result = await session.stream(statement)
    rows = []
    snapshots = []
    try:
        async for batch in result.partitions(250):
            for order, registration in batch:
                identities = await service._identities(registration)
                phone = await service.contact_phone(registration)
                username = (order.buyer_fish_nick or "").strip() or order.buyer_id or ""
                for visitor in registration.visitors:
                    identity = identities.get(visitor["id"], {})
                    if not identity.get("name") or not identity.get("id_number"):
                        raise TicketRegistrationError("已收集的登记中有身份信息缺失，请检查后重新导出")
                    rows.append((order.order_no, registration.visit_date, identity.get("name", ""),
                                 identity.get("id_number", ""), visitor["ticket_type_name"], username, phone, locked_remarks[order.id] or ""))
                snapshots.append((registration.id, registration.version,
                                  registration.identity_ciphertext, registration.phone_ciphertext))
    finally:
        await result.close()
    if not rows:
        raise TicketRegistrationError("勾选的订单中没有已收集、可导出的门票身份信息")
    try:
        # 文件生成失败不改变状态；只标记实际写入文件、且内容没有并发修改的登记。
        content = await asyncio.to_thread(build_ticket_workbook, rows)
        exported_at = get_beijing_now_naive()
        for registration_id, version, identity_ciphertext, phone_ciphertext in snapshots:
            marked = await session.execute(
                update(TicketRegistration).where(
                    TicketRegistration.id == registration_id,
                    TicketRegistration.version == version,
                    TicketRegistration.submitted_at.is_not(None),
                    TicketRegistration.identity_ciphertext == identity_ciphertext,
                    TicketRegistration.phone_ciphertext == phone_ciphertext,
                    TicketRegistration.order_id.in_(select(XYOrder.id).where(
                        XYOrder.id.in_(selected_ids), *scope,
                    )),
                ).values(exported_at=exported_at).execution_options(synchronize_session=False)
            )
            if marked.rowcount != 1:
                raise TicketRegistrationError("登记信息在导出期间已被修改或重新收集，请刷新订单列表后重新导出", 409)
        await session.commit()
        return content
    except BaseException:
        await session.rollback()
        raise
