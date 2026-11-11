"""按订单归属授权的门票管理API和免登录实名登记API；所有响应禁止缓存。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.services.ticket_age_policy_service import TicketAgePolicyService
from app.services.ticket_message_template_service import TicketMessageTemplateService
from app.services.ticket_registration_service import TicketRegistrationError, TicketRegistrationService, public_link_base
from common.models.user import User
from common.schemas.common import ApiResponse
from common.schemas.ticket_age_policy import PersonalTicketAgePolicyRequest, TicketAgePolicy
from common.schemas.ticket_message_template import PersonalTicketMessageTemplateRequest, TicketMessageTemplate
from common.schemas.ticket_registration import TicketAgeRules, TicketConfigRequest, TicketPhoneRequest, TicketSendRequest, TicketSubmitRequest
from common.services.redis_rate_limit_service import check_sliding_window_limit
from common.utils.http_utils import get_client_ip
from common.utils.auth_scope import resolve_owner_scope


class PrivateTicketRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except RequestValidationError as exc:
                # 不输出Pydantic错误input/body；其中可能含完整身份证和姓名。
                invalid_phone = any(error.get("loc", ())[-1:] == ("phone",) for error in exc.errors())
                message = "请输入一个有效的 11 位大陆联系手机号" if invalid_phone else "请求参数不正确，请检查填写内容；不允许修改未开放的字段"
                if request.url.path.endswith(("/age-rules", "/age-policy", "/personal-age-policy")):
                    message = "年龄规则不正确：请填写 0 至 150 的整数，最低年龄不能大于最高年龄，三类年龄范围不能重叠；最高年龄留空表示不限"
                if request.url.path.endswith(("/message-template", "/personal-message-template")):
                    message = "消息模板须为 1 至 2000 字，必须包含 {infourl}；只支持 {orderId} 和 {infourl} 占位符，不允许其他或不完整的花括号"
                response = JSONResponse({"success": False, "code": 400, "message": message, "data": None})
            except TicketRegistrationError as exc:
                response = JSONResponse({"success": False, "code": exc.code, "message": exc.message, "data": None})
            except HTTPException as exc:
                response = JSONResponse({"success": False, "code": exc.status_code,
                    "message": "请先登录" if exc.status_code == 401 else "无权执行此操作" if exc.status_code == 403 else "请求不可用", "data": None})
            except Exception as exc:
                # 不用logger.exception，避免诊断局部变量中的实名信息或完整请求内容。
                logger.error("门票登记请求失败: error_type={}", type(exc).__name__)
                response = JSONResponse({"success": False, "code": 503, "message": "登记服务暂不可用，请稍后重试", "data": None})
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        return handler


router = APIRouter(prefix="/ticket-registration", tags=["门票实名登记"], route_class=PrivateTicketRoute)


def frontend_origin(request: Request) -> str:
    """仅已认证的订单管理路由使用此header；不信任买家提交地址，不写入全局配置。"""
    candidate = request.headers.get("X-Frontend-Origin")
    if candidate is None:
        candidate = request.headers.get("Origin") or str(request.base_url).rstrip("/")
    return public_link_base(candidate)[0]


async def limit_public(request: Request, token: str) -> None:
    try:
        ip = get_client_ip(request)
        ip_limit = await check_sliding_window_limit("ticket-ip", ip, window_seconds=60, max_requests=120)
        token_limit = await check_sliding_window_limit("ticket-submit" if request.method == "POST" else "ticket-read",
            token, window_seconds=60, max_requests=10 if request.method == "POST" else 40)
    except Exception:
        raise TicketRegistrationError("登记服务暂不可用，请稍后重试", 503) from None
    if not ip_limit.allowed or not token_limit.allowed:
        raise TicketRegistrationError("操作过于频繁，请一分钟后重试", 429)


@router.get("/types", response_model=ApiResponse)
async def get_types(current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketRegistrationService(session).types())


@router.get("/age-rules", response_model=ApiResponse)
async def get_age_rules(current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketRegistrationService(session).age_rules())


@router.put("/age-rules", response_model=ApiResponse)
async def save_age_rules(payload: TicketAgeRules, current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, message="身份信息年龄规则已保存", data=await TicketRegistrationService(session).save_age_rules(payload))


@router.get("/age-policy", response_model=ApiResponse)
async def get_age_policy(current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketAgePolicyService(session).system())


@router.put("/age-policy", response_model=ApiResponse)
async def save_age_policy(payload: TicketAgePolicy, current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, message="身份信息年龄校验设置已保存", data=await TicketAgePolicyService(session).save_system(payload))


@router.get("/personal-age-policy", response_model=ApiResponse)
async def get_personal_age_policy(current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketAgePolicyService(session).personal(current_user.id))


@router.put("/personal-age-policy", response_model=ApiResponse)
async def save_personal_age_policy(payload: PersonalTicketAgePolicyRequest, current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, message="个人身份信息年龄校验设置已保存", data=await TicketAgePolicyService(session).save_personal(current_user.id, payload))


@router.get("/message-template", response_model=ApiResponse)
async def get_message_template(current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketMessageTemplateService(session).system())


@router.put("/message-template", response_model=ApiResponse)
async def save_message_template(payload: TicketMessageTemplate, current_user: User = Depends(deps.get_current_admin_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, message="买家消息模板已保存", data=await TicketMessageTemplateService(session).save_system(payload))


@router.get("/personal-message-template", response_model=ApiResponse)
async def get_personal_message_template(current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, data=await TicketMessageTemplateService(session).personal(current_user.id))


@router.put("/personal-message-template", response_model=ApiResponse)
async def save_personal_message_template(payload: PersonalTicketMessageTemplateRequest, current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    return ApiResponse(success=True, message="个人买家消息模板已保存", data=await TicketMessageTemplateService(session).save_personal(current_user.id, payload))


@router.get("/orders/{order_id}", response_model=ApiResponse)
async def get_order(request: Request, order_id: int = Path(gt=0, le=9223372036854775807), current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    owner_id, _ = resolve_owner_scope(current_user)
    return ApiResponse(success=True, data=await TicketRegistrationService(session, frontend_origin(request), owner_id=owner_id).get_admin(order_id))


@router.put("/orders/{order_id}", response_model=ApiResponse)
async def save_order(request: Request, payload: TicketConfigRequest, order_id: int = Path(gt=0, le=9223372036854775807), current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    owner_id, _ = resolve_owner_scope(current_user)
    return ApiResponse(success=True, message="门票配置已保存，登记链接已生成", data=await TicketRegistrationService(session, frontend_origin(request), owner_id=owner_id).save(order_id, payload))


@router.post("/orders/{order_id}/send", response_model=ApiResponse)
async def send_order(request: Request, payload: TicketSendRequest, order_id: int = Path(gt=0, le=9223372036854775807), current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    owner_id, _ = resolve_owner_scope(current_user)
    message, data = await TicketRegistrationService(session, frontend_origin(request), owner_id=owner_id).send(order_id, payload.version)
    return ApiResponse(success=True, message=message, data=data)


@router.post("/orders/{order_id}/recollect", response_model=ApiResponse)
async def recollect_order(request: Request, payload: TicketSendRequest, order_id: int = Path(gt=0, le=9223372036854775807), current_user: User = Depends(deps.get_current_active_user), session: AsyncSession = Depends(deps.get_db_session)):
    owner_id, _ = resolve_owner_scope(current_user)
    data = await TicketRegistrationService(session, frontend_origin(request), owner_id=owner_id).recollect(order_id, payload.version)
    return ApiResponse(success=True, message="原身份信息和导出记录已清除，订单已恢复待收集", data=data)


@router.get("/public/form", response_model=ApiResponse)
async def public_form(request: Request, token: str = Header(default="", alias="X-Ticket-Token", max_length=128), session: AsyncSession = Depends(deps.get_db_session)):
    await limit_public(request, token)
    return ApiResponse(success=True, data=await TicketRegistrationService(session).get_public(token))


@router.post("/public/submit", response_model=ApiResponse)
async def public_submit(request: Request, payload: TicketSubmitRequest, token: str = Header(default="", alias="X-Ticket-Token", max_length=128), session: AsyncSession = Depends(deps.get_db_session)):
    await limit_public(request, token)
    return ApiResponse(success=True, message="实名信息提交成功", data=await TicketRegistrationService(session).submit(token, payload))


@router.post("/public/phone", response_model=ApiResponse)
async def public_phone(request: Request, payload: TicketPhoneRequest, token: str = Header(default="", alias="X-Ticket-Token", max_length=128), session: AsyncSession = Depends(deps.get_db_session)):
    await limit_public(request, token)
    return ApiResponse(success=True, message="联系电话补填成功", data=await TicketRegistrationService(session).supplement_phone(token, payload))
