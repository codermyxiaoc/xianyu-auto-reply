"""门票分类和按游玩日期计算周岁，不访问数据库或保留身份证信息。"""
from __future__ import annotations

from datetime import date

TICKET_CATEGORY_LABELS = {"child": "儿童", "adult": "成人", "senior": "老人"}


def infer_ticket_category(name: str) -> str | None:
    matches = [category for category, label in TICKET_CATEGORY_LABELS.items() if label in name]
    return matches[0] if len(matches) == 1 else None


def age_on_date(birthday: date, visit_date: date) -> int:
    """周岁以当年的生日是否已到为准，2 月 29 日出生在平年 3 月 1 日增长一岁。"""
    return visit_date.year - birthday.year - ((visit_date.month, visit_date.day) < (birthday.month, birthday.day))
