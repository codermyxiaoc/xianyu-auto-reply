"""管理员仅导出明确勾选的订单中的已收集门票身份信息。"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.routes.ticket_registration import PrivateTicketRoute
from app.services.ticket_registration_export_service import export_ticket_registrations
from app.services.ticket_registration_service import TicketRegistrationError
from common.models.user import User
from common.schemas.ticket_registration import TicketExportRequest
from common.utils.time_utils import get_beijing_now

router = APIRouter(prefix="/ticket-registration", tags=["门票实名登记"], route_class=PrivateTicketRoute)


@router.post("/export.xlsx")
async def export_tickets(
    request: Request,
    payload: TicketExportRequest,
    current_user: User = Depends(deps.get_current_admin_user),
    session: AsyncSession = Depends(deps.get_db_session),
):
    if request.query_params:
        raise TicketRegistrationError("导出仅支持勾选订单，请清除旧导出筛选参数后重试")
    content = await export_ticket_registrations(session, order_ids=[int(value) for value in payload.order_ids])
    filename = f"门票身份信息_{get_beijing_now():%Y%m%d_%H%M%S}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=\"ticket-registrations.xlsx\"; filename*=UTF-8''{quote(filename)}"})
