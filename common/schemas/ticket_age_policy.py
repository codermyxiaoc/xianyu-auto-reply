"""门票年龄校验开关和完整年龄范围；个人策略整套覆盖系统策略。"""
from typing import Literal

from pydantic import Field, field_validator

from common.schemas.ticket_registration import StrictTicketModel, TicketAgeRules
from common.utils.setting_keys import normalize_setting_key


TICKET_AGE_ENABLED_SETTING = "ticket_registration.age_validation_enabled"
PERSONAL_TICKET_AGE_POLICY_SETTING = "ticket_registration.age_policy"


def is_personal_ticket_age_policy_key(key: str) -> bool:
    """泛用设置入口也拦截 MySQL 不区分大小写、重音及尾空格的键名变体。"""
    return normalize_setting_key(key) == PERSONAL_TICKET_AGE_POLICY_SETTING


class TicketAgePolicy(StrictTicketModel):
    enabled: bool = Field(strict=True)
    rules: TicketAgeRules


class PersonalTicketAgePolicy(TicketAgePolicy):
    use_personal: bool = Field(strict=True)


class InheritTicketAgePolicy(StrictTicketModel):
    use_personal: Literal[False]

    @field_validator("use_personal", mode="before")
    @classmethod
    def require_false_boolean(cls, value):
        if value is not False:
            raise ValueError("恢复跟随系统必须传入 false")
        return value


PersonalTicketAgePolicyRequest = PersonalTicketAgePolicy | InheritTicketAgePolicy
