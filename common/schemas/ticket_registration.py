"""门票配置与买家实名提交严格分离，禁止额外字段越权。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from common.utils.time_utils import get_beijing_now


class StrictTicketModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


TICKET_AGE_RULES_SETTING = "ticket_registration.age_rules"
DEFAULT_TICKET_AGE_RULES = {
    "child": {"min_age": 0, "max_age": 17},
    "adult": {"min_age": 18, "max_age": 59},
    "senior": {"min_age": 60, "max_age": None},
}


class TicketAgeRange(StrictTicketModel):
    min_age: int = Field(ge=0, le=150, strict=True)
    max_age: int | None = Field(ge=0, le=150, strict=True)

    @model_validator(mode="after")
    def validate_range(self) -> "TicketAgeRange":
        if self.max_age is not None and self.min_age > self.max_age:
            raise ValueError("最低年龄不能大于最高年龄")
        return self


class TicketAgeRules(StrictTicketModel):
    child: TicketAgeRange
    adult: TicketAgeRange
    senior: TicketAgeRange

    @model_validator(mode="after")
    def validate_no_overlap(self) -> "TicketAgeRules":
        ranges = sorted((getattr(self, key) for key in ("child", "adult", "senior")), key=lambda item: item.min_age)
        for lower, upper in zip(ranges, ranges[1:]):
            if lower.max_age is None or lower.max_age >= upper.min_age:
                raise ValueError("儿童、成人、老人的年龄范围不能重叠")
        return self


class TicketVisitorConfig(StrictTicketModel):
    id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    category: Literal["adult", "child", "senior"]
    amount: str = Field(max_length=16)
    ticket_type_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        if not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", value):
            raise ValueError("金额必须为不超过八位整数、两位小数的非负数")
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError("金额不正确") from None
        return format(amount.quantize(Decimal("0.01")), "f")


class TicketConfigRequest(StrictTicketModel):
    version: int = Field(ge=0, strict=True)
    visit_date: date
    visitors: list[TicketVisitorConfig] = Field(min_length=1, max_length=50)

    @field_validator("visit_date")
    @classmethod
    def validate_date(cls, value: date) -> date:
        if value < get_beijing_now().date():
            raise ValueError("游玩日期不能早于今天")
        return value


class TicketSendRequest(StrictTicketModel):
    version: int = Field(ge=1, strict=True)


class TicketExportRequest(StrictTicketModel):
    """导出范围必须由当前用户明确勾选，禁止空选择或筛选条件回退到全量。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    order_ids: list[Annotated[str, Field(strict=True, pattern=r"^[1-9][0-9]{0,18}$")]] = Field(min_length=1, max_length=1000)

    @field_validator("order_ids")
    @classmethod
    def validate_order_ids(cls, values: list[str]) -> list[str]:
        if any(int(value) > 9223372036854775807 for value in values):
            raise ValueError("订单 ID 超出允许范围")
        return list(dict.fromkeys(values))


class TicketVisitorIdentity(StrictTicketModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    # 业务校验在service内完成，能返回第几位游客的中文错误，且不回显用户输入。
    name: str = Field(min_length=1, max_length=100)
    id_number: str = Field(min_length=1, max_length=32)


TicketPhone = Annotated[str, Field(strict=True, min_length=11, max_length=11, pattern=r"^1[3-9][0-9]{9}$")]


class TicketSubmitRequest(StrictTicketModel):
    version: int = Field(ge=1, strict=True)
    phone: TicketPhone
    visitors: list[TicketVisitorIdentity] = Field(min_length=1, max_length=50)


class TicketPhoneRequest(StrictTicketModel):
    """仅供已提交但尚无手机号的历史登记补填整单联系电话。"""
    version: int = Field(ge=1, strict=True)
    phone: TicketPhone
