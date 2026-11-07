"""门票登记：一单一份配置，身份信息独立加密，不写入原订单内容。"""
from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin


class TicketType(TimestampMixin, Base):
    __tablename__ = "xy_ticket_types"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # 旧版自定义票种无法按名称识别时保留为空，不能默认当成成人票。
    category: Mapped[str | None] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (Index("uk_ticket_type_name", "name", unique=True),)


class TicketRegistration(TimestampMixin, Base):
    __tablename__ = "xy_ticket_registrations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    visitors: Mapped[list] = mapped_column(JSON, nullable=False)
    identity_ciphertext: Mapped[str | None] = mapped_column(Text)
    phone_ciphertext: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_version: Mapped[int | None] = mapped_column(Integer)
    sent_link_hash: Mapped[str | None] = mapped_column(String(64))
    send_attempt_version: Mapped[int | None] = mapped_column(Integer)
    send_attempt_link_hash: Mapped[str | None] = mapped_column(String(64))
    send_attempt_status: Mapped[str | None] = mapped_column(String(20))
    __table_args__ = (
        Index("uk_ticket_registration_order", "order_id", unique=True),
        Index("uk_ticket_registration_token", "token_hash", unique=True),
    )
