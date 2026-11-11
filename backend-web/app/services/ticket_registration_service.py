"""按订单归属管理门票配置、买家实名登记与绑定订单的链接发送。"""
from __future__ import annotations

from datetime import date
import hashlib
import ipaddress
import json
import re
import secrets
from urllib.parse import urlsplit, urlunsplit
import uuid

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.ticket_age_policy_service import TicketAgePolicyService
from app.services.ticket_message_template_service import TicketMessageTemplateService, render_ticket_message
from app.services.ticket_registration_errors import TicketRegistrationError
from app.services.websocket_client import websocket_client
from common.db.ticket_registration_schema import TICKET_KEY_SETTING
from common.models.system_setting import SystemSetting
from common.models.ticket_registration import TicketRegistration, TicketType
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.schemas.ticket_registration import (
    TicketAgeRules,
    TicketConfigRequest, TicketPhoneRequest, TicketSubmitRequest,
)
from common.utils.ticket_age import TICKET_CATEGORY_LABELS, age_on_date, infer_ticket_category
from common.utils.time_utils import get_beijing_now, get_beijing_now_naive, safe_isoformat

PAID_ORDER_STATUSES = frozenset({"pending_ship", "shipped", "completed"})
CANONICAL_TICKET_TYPE_NAMES = tuple(
    f"{days}日{person}票"
    for person in ("成人", "老人", "儿童")
    for days in ("1", "1.5", "2")
)


def ticket_order_scope(owner_id: int | None) -> list:
    """普通用户须同时拥有订单及对应闲鱼账号；管理员使用不受限范围。"""
    if owner_id is None:
        return []
    return [
        XYOrder.owner_id == owner_id,
        select(XYAccount.id).where(
            XYAccount.account_id == XYOrder.account_id,
            XYAccount.owner_id == owner_id,
        ).exists(),
    ]


def require_paid_order(order: XYOrder | None, *, public: bool = False) -> XYOrder:
    if order is None or (order.status or "").lower() not in PAID_ORDER_STATUSES:
        raise TicketRegistrationError("登记链接无效或订单已不可登记" if public else "仅已付款的待发货、已发货或已完成订单可配置门票", 400)
    return order


def validate_identity(name: str, id_number: str, index: int) -> tuple[str, str]:
    name, number = name.strip(), id_number.strip().upper()
    prefix = f"第 {index} 位游客："
    if not 2 <= len(name) <= 50 or not re.fullmatch(r"[\u3400-\u9fffA-Za-zÀ-ÿ·•．.'’\- ]+", name):
        raise TicketRegistrationError(prefix + "姓名需为 2 至 50 个汉字或字母，可含间隔符")
    if not re.fullmatch(r"[1-9]\d{16}[\dX]", number):
        raise TicketRegistrationError(prefix + "请输入有效的 18 位身份证号码")
    try:
        birthday = date(int(number[6:10]), int(number[10:12]), int(number[12:14]))
        if birthday > get_beijing_now().date() or birthday.year < 1900:
            raise ValueError
    except ValueError:
        raise TicketRegistrationError(prefix + "身份证出生日期不正确") from None
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    expected = "10X98765432"[sum(int(number[i]) * weights[i] for i in range(17)) % 11]
    if number[-1] != expected:
        raise TicketRegistrationError(prefix + "身份证校验码不正确")
    return name, number


def validate_slot_ids(expected: list[dict], supplied: list) -> None:
    identifiers = [item.id for item in supplied]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != {item["id"] for item in expected}:
        raise TicketRegistrationError("游客名单已变化，请刷新页面后按当前名单填写", 409)


def validate_visitor_age(number: str, visit_date: date, category: str, rules: dict, index: int) -> int:
    """身份证已通过格式和校验码检查后，按当前规则验证周岁。"""
    if category not in TICKET_CATEGORY_LABELS or category not in rules:
        raise TicketRegistrationError(f"第 {index} 位游客：游客分类不可用，请联系卖家重新配置", 409)
    birthday = date(int(number[6:10]), int(number[10:12]), int(number[12:14]))
    age = age_on_date(birthday, visit_date)
    rule = rules[category]
    minimum, maximum = rule["min_age"], rule["max_age"]
    if age < minimum or (maximum is not None and age > maximum):
        allowed = f"{minimum} 岁及以上" if maximum is None else f"{minimum}–{maximum} 岁"
        raise TicketRegistrationError(
            f"第 {index} 位游客：游玩当日为 {age} 周岁，不符合{TICKET_CATEGORY_LABELS[category]}票年龄要求（{allowed}），请核对身份证或联系卖家调整票种"
        )
    return age


def public_link_base(value: str) -> tuple[str, bool]:
    """管理员当前前端origin；不接收路径、凭据或买家传入的链接。"""
    try:
        if not value or value != value.strip() or re.search(r"[\x00-\x20\x7f\\]", value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.query or parsed.fragment or parsed.path:
            raise ValueError
        _ = parsed.port  # 拒绝不合法端口。
        if "%" in parsed.netloc or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc):
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            ipaddress.IPv6Address(host)
            host = f"[{host}]"
        elif len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.rstrip(".").split(".")):
            raise ValueError
        port = parsed.port
        if port is not None and port < 1:
            raise ValueError
        if port is not None and not (parsed.scheme == "http" and port == 80 or parsed.scheme == "https" and port == 443):
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, "", "", "")), True
    except ValueError:
        raise TicketRegistrationError("前端访问地址不正确，请从正常的管理页面打开订单") from None


class TicketRegistrationService:
    def __init__(self, session: AsyncSession, frontend_origin: str = "http://127.0.0.1:9000", *, owner_id: int | None = None):
        self.session = session
        self.owner_id = owner_id
        self.settings = get_settings()
        self.frontend_origin = frontend_origin
        self._cipher: Fernet | None = None

    async def _get_cipher(self) -> Fernet:
        if self._cipher is None:
            key = (await self.session.execute(select(SystemSetting.value).where(SystemSetting.key == TICKET_KEY_SETTING))).scalar_one_or_none()
            try:
                self._cipher = Fernet((key or "").encode("ascii"))
            except Exception:
                raise TicketRegistrationError("实名信息加密配置不可用，请联系管理员", 503) from None
        return self._cipher

    async def _decrypt(self, value: str) -> str:
        try:
            return (await self._get_cipher()).decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            raise TicketRegistrationError("实名登记数据无法读取，请联系管理员", 503) from None

    async def _order(self, order_id: int, *, lock: bool = False, public: bool = False) -> XYOrder:
        stmt = select(XYOrder).where(XYOrder.id == order_id, *ticket_order_scope(self.owner_id)).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if order is None and self.owner_id is not None:
            raise TicketRegistrationError("订单不存在或无权操作", 403)
        return require_paid_order(order, public=public)

    async def _config(self, order_id: int, *, lock: bool = False) -> TicketRegistration | None:
        stmt = select(TicketRegistration).where(TicketRegistration.order_id == order_id).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def types(self) -> list[dict]:
        rows = (await self.session.execute(select(TicketType).where(
            TicketType.enabled.is_(True)
        ).order_by(TicketType.sort_order, TicketType.id))).scalars()
        options = []
        for row in rows:
            # 可识别的旧票种即使尚未回填分类，也保留其 ID 和选择能力。
            category = row.category or infer_ticket_category(row.name)
            if category in TICKET_CATEGORY_LABELS:
                options.append({"id": str(row.id), "name": row.name, "category": category})
        positions = {name: index for index, name in enumerate(CANONICAL_TICKET_TYPE_NAMES)}
        # 仅调整展示顺序，不重建默认票种，避免历史登记中的 ID 对应错误。
        return sorted(options, key=lambda option: positions.get(option["name"], len(positions)))

    async def age_rules(self) -> dict:
        return (await TicketAgePolicyService(self.session).system())["rules"]

    async def save_age_rules(self, rules: TicketAgeRules) -> dict:
        return await TicketAgePolicyService(self.session).save_rules(rules)

    async def effective_age_policy(self, owner_id: int) -> dict:
        return await TicketAgePolicyService(self.session).effective(owner_id)

    async def effective_message_template(self, owner_id: int) -> str:
        return await TicketMessageTemplateService(self.session).effective(owner_id)

    async def _identities(self, config: TicketRegistration) -> dict:
        if not config.identity_ciphertext:
            return {}
        try:
            data = json.loads(await self._decrypt(config.identity_ciphertext))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError):
            raise TicketRegistrationError("实名登记数据无法读取，请联系管理员", 503) from None

    async def contact_phone(self, config: TicketRegistration) -> str | None:
        """读取整单联系电话，仅供已授权订单视图、导出及公开掩码使用。"""
        if not config.phone_ciphertext:
            return None
        return await self._decrypt(config.phone_ciphertext)

    async def _admin_view(self, order: XYOrder, config: TicketRegistration | None) -> dict:
        value = None
        if config:
            identities = await self._identities(config)
            base, ready = public_link_base(self.frontend_origin)
            token = await self._decrypt(config.token_ciphertext)
            value = {
                "id": str(config.id), "version": config.version, "visit_date": config.visit_date.isoformat(),
                "status": "submitted" if config.submitted_at else "draft",
                "visitors": [{**entry, "name": identities.get(entry["id"], {}).get("name"),
                              "id_number": identities.get(entry["id"], {}).get("id_number")} for entry in config.visitors],
                "link": f"{base}/ticket-registration#{token}", "public_url_ready": ready,
                "submitted_at": safe_isoformat(config.submitted_at), "sent_at": safe_isoformat(config.sent_at),
                "exported_at": safe_isoformat(config.exported_at),
                "phone": await self.contact_phone(config),
            }
        policy = await self.effective_age_policy(order.owner_id)
        return {"order_id": str(order.id), "order_no": order.order_no, "order_status": order.status,
                "configurable": True, "configuration": value, "age_rules": policy["rules"],
                "age_validation_enabled": policy["enabled"]}

    async def get_admin(self, order_id: int) -> dict:
        return await self._admin_view(await self._order(order_id), await self._config(order_id))

    async def save(self, order_id: int, payload: TicketConfigRequest) -> dict:
        # 先锁原订单，解决首次并发创建的空行间隙；所有写入遵循order -> config锁顺序。
        order = await self._order(order_id, lock=True)
        config = await self._config(order_id, lock=True)
        if (config.version if config else 0) != payload.version:
            raise TicketRegistrationError("配置已被修改，请重新打开后再保存", 409)
        if config and config.submitted_at:
            raise TicketRegistrationError("买家已提交实名信息，该登记配置不可再修改", 409)
        if payload.visit_date < get_beijing_now().date():
            raise TicketRegistrationError("游玩日期不能早于今天")
        type_ids = {int(item.ticket_type_id) for item in payload.visitors}
        types = {str(t.id): t for t in (await self.session.execute(select(TicketType).where(TicketType.id.in_(type_ids), TicketType.enabled.is_(True)))).scalars()}
        if len(types) != len(type_ids):
            raise TicketRegistrationError("选择的票种不存在或已停用")
        old_ids = {item["id"] for item in config.visitors} if config else set()
        seen = set()
        visitors = []
        for index, item in enumerate(payload.visitors, 1):
            ticket_type = types[item.ticket_type_id]
            ticket_category = ticket_type.category or infer_ticket_category(ticket_type.name)
            if ticket_category != item.category:
                raise TicketRegistrationError(f"第 {index} 位游客：{TICKET_CATEGORY_LABELS[item.category]}只能选择对应的{TICKET_CATEGORY_LABELS[item.category]}票种")
            if item.id and (item.id not in old_ids or item.id in seen):
                raise TicketRegistrationError("游客配置不匹配，请重新打开订单", 409)
            identifier = item.id or uuid.uuid4().hex
            seen.add(identifier)
            visitors.append({"id": identifier, "category": item.category, "amount": item.amount,
                             "ticket_type_id": item.ticket_type_id, "ticket_type_name": ticket_type.name,
                             "ticket_type_category": ticket_category})
        if config is None:
            token = secrets.token_urlsafe(32)
            config = TicketRegistration(order_id=order.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                token_ciphertext=(await self._get_cipher()).encrypt(token.encode()).decode("ascii"),
                visit_date=payload.visit_date, visitors=visitors, version=1)
            self.session.add(config)
        else:
            config.visit_date, config.visitors = payload.visit_date, visitors
            config.version += 1
        config.exported_at = None
        await self.session.commit()
        return await self._admin_view(order, config)

    async def _from_token(self, token: str, *, lock: bool = False) -> tuple[XYOrder, TicketRegistration]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token or ""):
            raise TicketRegistrationError("登记链接无效或订单已不可登记", 404)
        order_id = (await self.session.execute(select(TicketRegistration.order_id).where(
            TicketRegistration.token_hash == hashlib.sha256(token.encode()).hexdigest()
        ))).scalar_one_or_none()
        if order_id is None:
            raise TicketRegistrationError("登记链接无效或订单已不可登记", 404)
        order = await self._order(order_id, lock=lock, public=True)
        config = await self._config(order_id, lock=lock)
        if config is None:
            raise TicketRegistrationError("登记链接无效或订单已不可登记", 404)
        return order, config

    async def _public_view(self, order: XYOrder, config: TicketRegistration) -> dict:
        identities = await self._identities(config)
        phone = await self.contact_phone(config)
        visitors = []
        for item in config.visitors:
            identity = identities.get(item["id"], {})
            number = identity.get("id_number")
            visitors.append({"id": item["id"], "category": item["category"], "amount": item["amount"],
                "ticket_type_name": item["ticket_type_name"], "name": identity.get("name"),
                "id_number_masked": number[:3] + "***********" + number[-4:] if number else None})
        policy = await self.effective_age_policy(order.owner_id)
        return {"order_no": order.order_no, "version": config.version, "visit_date": config.visit_date.isoformat(),
                "status": "submitted" if config.submitted_at else "draft", "submitted_at": safe_isoformat(config.submitted_at),
                "visitors": visitors, "phone_masked": phone[:3] + "****" + phone[-4:] if phone else None,
                "age_rules": policy["rules"], "age_validation_enabled": policy["enabled"]}

    async def get_public(self, token: str) -> dict:
        return await self._public_view(*(await self._from_token(token)))

    async def submit(self, token: str, payload: TicketSubmitRequest) -> dict:
        order, config = await self._from_token(token, lock=True)
        if config.submitted_at:
            raise TicketRegistrationError("实名信息已提交，请勿重复提交；如需更正请联系卖家", 409)
        if config.version != payload.version:
            raise TicketRegistrationError("管理员已更新门票配置，请刷新后重新填写", 409)
        if config.visit_date < get_beijing_now().date():
            raise TicketRegistrationError("游玩日期已过，请联系卖家")
        validate_slot_ids(config.visitors, payload.visitors)
        policy = await self.effective_age_policy(order.owner_id)
        slots = {visitor["id"]: visitor for visitor in config.visitors}
        identities, numbers = {}, set()
        for index, item in enumerate(payload.visitors, 1):
            name, number = validate_identity(item.name, item.id_number, index)
            slot = slots[item.id]
            # 旧版允许混选票种；展示保留原样，但不能用不相符的游客分类绕过年龄校验。
            ticket_category = slot.get("ticket_type_category") or infer_ticket_category(slot.get("ticket_type_name", ""))
            if ticket_category != slot["category"]:
                raise TicketRegistrationError(f"第 {index} 位游客：票种与游客分类不一致，请联系卖家重新配置", 409)
            if policy["enabled"]:
                validate_visitor_age(number, config.visit_date, slot["category"], policy["rules"], index)
            if number in numbers:
                raise TicketRegistrationError(f"第 {index} 位游客：身份证与其他游客重复")
            numbers.add(number)
            identities[item.id] = {"name": name, "id_number": number}
        config.identity_ciphertext = (await self._get_cipher()).encrypt(json.dumps(identities, ensure_ascii=False).encode()).decode("ascii")
        config.phone_ciphertext = (await self._get_cipher()).encrypt(payload.phone.encode("ascii")).decode("ascii")
        config.submitted_at = get_beijing_now_naive()
        config.exported_at = None
        config.version += 1
        await self.session.commit()
        return await self._public_view(order, config)

    async def supplement_phone(self, token: str, payload: TicketPhoneRequest) -> dict:
        """历史登记只补手机号，不重写姓名身份证、配置或首次提交时间。"""
        order, config = await self._from_token(token, lock=True)
        if not config.submitted_at:
            raise TicketRegistrationError("请先完整提交游客实名信息和联系电话", 409)
        if config.phone_ciphertext:
            raise TicketRegistrationError("联系电话已填写，不能重复修改；如需更正请联系卖家", 409)
        if config.version != payload.version:
            raise TicketRegistrationError("登记信息已更新，请刷新后补填联系电话", 409)
        config.phone_ciphertext = (await self._get_cipher()).encrypt(payload.phone.encode("ascii")).decode("ascii")
        config.exported_at = None
        config.version += 1
        await self.session.commit()
        return await self._public_view(order, config)

    async def recollect(self, order_id: int, version: int) -> dict:
        """开启新一轮登记，保留票务配置及原链接，彻底移除上一轮身份数据。"""
        order = await self._order(order_id, lock=True)
        config = await self._config(order_id, lock=True)
        if config is None:
            raise TicketRegistrationError("该订单尚未创建门票信息，不能重新收集", 409)
        if config.version != version:
            raise TicketRegistrationError("登记信息已更新，请重新读取后再操作", 409)
        if config.submitted_at is None:
            raise TicketRegistrationError("该订单正在等待买家填写，无需重新收集", 409)

        config.identity_ciphertext = None
        config.phone_ciphertext = None
        config.submitted_at = None
        config.exported_at = None
        config.sent_at = None
        config.sent_version = None
        config.sent_link_hash = None
        config.send_attempt_version = None
        config.send_attempt_link_hash = None
        config.send_attempt_status = None
        config.version += 1
        # 发送过程在真正调用消息接口时也持有 order -> config 锁。
        # 本操作或等待该次发送完成，或先增加版本让旧发送在派发前取消；
        # 因此可清理异常遗留的 pending/unknown，而不会造成旧版本重发。
        # 允许过去日期解锁，随后管理员可修正游玩日期再让买家填写。
        await self.session.commit()
        return await self._admin_view(order, config)

    async def send(self, order_id: int, version: int) -> tuple[str, dict]:
        order = await self._order(order_id, lock=True)
        config = await self._config(order_id, lock=True)
        if config is None:
            raise TicketRegistrationError("请先保存门票配置")
        if config.version != version:
            raise TicketRegistrationError("配置已更新，请重新打开订单后发送", 409)
        view = await self._admin_view(order, config)
        link_hash = hashlib.sha256(view["configuration"]["link"].encode()).hexdigest()
        if config.sent_version == version and config.sent_link_hash == link_hash:
            return "该版本链接已发送，无需重复发送", await self._admin_view(order, config)
        if config.send_attempt_status in {"pending", "unknown"}:
            raise TicketRegistrationError("该链接正在发送或上次发送结果未知，请先到聊天中核实，避免重复发送", 409)
        if config.visit_date < get_beijing_now().date() and config.submitted_at is None:
            raise TicketRegistrationError("游玩日期已过，请更新配置后发送")
        account = (await self.session.execute(select(XYAccount).where(
            XYAccount.account_id == order.account_id, XYAccount.owner_id == order.owner_id
        ))).scalar_one_or_none()
        if account is None or account.status != "active" or not order.buyer_id or not order.item_id:
            raise TicketRegistrationError("订单账号归属或买家、商品信息不完整，无法发送")
        # 模板仅按订单所属用户解析；在记录发送意图前校验，错误模板不能留下 pending。
        template = await self.effective_message_template(order.owner_id)
        message = render_ticket_message(template, order.order_no, view["configuration"]["link"])
        state = await websocket_client.get_account_status(order.account_id)
        if not state.get("success") or not (state.get("data") or {}).get("is_connected"):
            raise TicketRegistrationError("闲鱼账号未连接，请先启动账号")
        if not order.chat_id:
            chat = await websocket_client.create_chat(order.account_id, order.buyer_id, order.item_id)
            chat_id = (chat.get("data") or {}).get("chat_id") if chat.get("success") else None
            if not chat_id:
                raise TicketRegistrationError("无法建立买家会话，请稍后重试")
            order.chat_id = str(chat_id)
        # 先持久化发送意图；若进程崩溃/断网，不将未知结果当作失败自动重发。
        config.send_attempt_version, config.send_attempt_status = version, "pending"
        config.send_attempt_link_hash = link_hash
        await self.session.commit()
        try:
            order = await self._order(order_id, lock=True)
            config = await self._config(order_id, lock=True)
            if config is None or config.version != version or config.send_attempt_link_hash != link_hash:
                # 此分支尚未调用消息接口，是明确未发送；只清理属于本次的意图。
                if (config is not None and config.send_attempt_version == version
                        and config.send_attempt_link_hash == link_hash and config.send_attempt_status == "pending"):
                    config.send_attempt_status = "failed"
                    await self.session.commit()
                raise TicketRegistrationError("配置已变化，发送已停止", 409)
            result = await websocket_client.http_client.post(
                f"{websocket_client.base_url}/internal/accounts/{order.account_id}/send-message",
                json={"chat_id": order.chat_id, "to_user_id": order.buyer_id, "wait_result": True,
                      "message": message},
                timeout=45, max_retries=1,
            )
            send_status = (result.get("data") or {}).get("send_status")
            if result.get("success") and send_status == "success":
                config.sent_at, config.sent_version = get_beijing_now_naive(), version
                config.sent_link_hash = link_hash
                config.send_attempt_status = "success"
                await self.session.commit()
                return "登记链接已发送给买家", await self._admin_view(order, config)
            # 内部接口在调用send_msg之前明确返回的账号/连接/收件人拒绝，可以修复后重试。
            # HTTP超时、5xx或无发送状态的其他结果不能证明未发送，仍保留未知。
            rejected_before_send = (result.get("success") is False and result.get("code") in {400, 404}
                                    and result.get("data") is None)
            definite_failure = send_status == "failed" or rejected_before_send
            config.send_attempt_status = "failed" if definite_failure else "unknown"
            await self.session.commit()
            raise TicketRegistrationError("链接发送失败，请检查账号和买家会话后重试" if definite_failure
                                          else "发送结果暂不明确，请先在聊天中核实，系统不会自动重发", 502)
        except TicketRegistrationError:
            # pending意图已持久化；保留未知结果，避免因订单变化/失败回滚而重复发送。
            raise
        except Exception:
            await self.session.rollback()
            # pending在独立提交中已写入，无论此处故障是否可恢复都禁止无确认重发。
            raise TicketRegistrationError("发送结果暂不明确，请先在聊天中核实，系统不会自动重发", 502) from None
