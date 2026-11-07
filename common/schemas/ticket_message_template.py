"""门票买家消息模板，只允许明确支持的订单号与登记链接占位符。"""
from pydantic import ConfigDict, Field, field_validator

from common.schemas.ticket_age_policy import InheritTicketAgePolicy
from common.schemas.ticket_registration import StrictTicketModel
from common.utils.setting_keys import normalize_setting_key


TICKET_MESSAGE_TEMPLATE_SETTING = "ticket_registration.message_template"
PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING = TICKET_MESSAGE_TEMPLATE_SETTING
DEFAULT_TICKET_MESSAGE_TEMPLATE = "请填写订单 {orderId} 的门票实名信息：每位游客的姓名、身份证，以及一个整单共用的联系手机号。\n{infourl}"


def is_personal_ticket_message_template_key(key: str) -> bool:
    return normalize_setting_key(key) == PERSONAL_TICKET_MESSAGE_TEMPLATE_SETTING


class TicketMessageTemplate(StrictTicketModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    message_template: str = Field(strict=True, min_length=1, max_length=2000)

    @field_validator("message_template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        if not value.strip() or "{infourl}" not in value:
            raise ValueError("消息模板不能为空，且必须包含 {infourl} 登记链接占位符")
        remaining = value.replace("{infourl}", "").replace("{orderId}", "")
        if "{" in remaining or "}" in remaining:
            raise ValueError("只支持 {orderId} 和 {infourl} 占位符，请删除其他或不完整的花括号")
        return value


class PersonalTicketMessageTemplate(TicketMessageTemplate):
    use_personal: bool = Field(strict=True)


class InheritTicketMessageTemplate(InheritTicketAgePolicy):
    pass


PersonalTicketMessageTemplateRequest = PersonalTicketMessageTemplate | InheritTicketMessageTemplate
