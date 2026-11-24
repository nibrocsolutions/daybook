"""Shop, people, jobs, and payments."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator
from sqlalchemy.types import DateTime as SADateTime

from app.db import Base
from app.formatting import postal_address, utcnow, zone


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    impl = SADateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=utcnow().tzinfo)
        return value.astimezone(utcnow().tzinfo)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=utcnow().tzinfo)
        return value.astimezone(utcnow().tzinfo)


class Shop(Base):
    __tablename__ = "shop"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    business_name: Mapped[str] = mapped_column(String(120), default="Fieldbook")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    tax_labor: Mapped[bool] = mapped_column(Boolean, default=True)
    job_prefix: Mapped[str] = mapped_column(String(12), default="JOB-")
    invoice_prefix: Mapped[str] = mapped_column(String(12), default="INV-")
    next_job_number: Mapped[int] = mapped_column(Integer, default=1001)
    next_invoice_number: Mapped[int] = mapped_column(Integer, default=1001)
    demo_seeded: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    def zoneinfo(self):
        return zone(self.timezone)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role in ('admin', 'technician')", name="ck_users_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="technician")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    assigned_jobs: Mapped[list[Job]] = relationship(
        back_populates="assignee", foreign_keys="Job.assigned_user_id"
    )


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), index=True)
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    address_line: Mapped[str] = mapped_column(String(160), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    region: Mapped[str] = mapped_column(String(40), default="")
    postal_code: Mapped[str] = mapped_column(String(20), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    jobs: Mapped[list[Job]] = relationship(back_populates="customer", cascade="all, delete-orphan")

    @property
    def postal_address(self) -> str:
        return postal_address(self.address_line, self.city, self.region, self.postal_code)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("number", name="uq_jobs_number"),
        CheckConstraint(
            "status in ('inquiry', 'scheduled', 'in_progress', 'completed', 'invoiced', 'paid', 'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("priority in ('low', 'normal', 'urgent')", name="ck_jobs_priority"),
        Index("ix_jobs_status_schedule", "status", "scheduled_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(24))
    invoice_number: Mapped[str | None] = mapped_column(String(24), nullable=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(140))
    description: Mapped[str] = mapped_column(Text, default="")
    service_address: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="inquiry", index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    scheduled_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    assigned_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tax_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    tax_labor: Mapped[bool] = mapped_column(Boolean, default=True)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    customer_note: Mapped[str] = mapped_column(Text, default="")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    customer: Mapped[Customer] = relationship(back_populates="jobs")
    assignee: Mapped[User | None] = relationship(
        back_populates="assigned_jobs", foreign_keys=[assigned_user_id]
    )
    lines: Mapped[list[JobLine]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobLine.sort_order",
    )
    notes: Mapped[list[JobNote]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobNote.created_at",
    )
    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobEvent.created_at",
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Payment.created_at",
    )

    @property
    def display_address(self) -> str:
        if self.service_address and self.service_address.strip():
            return self.service_address.strip()
        if self.customer is not None:
            return self.customer.postal_address
        return ""


class JobLine(Base):
    __tablename__ = "job_lines"
    __table_args__ = (
        CheckConstraint("kind in ('labor', 'material', 'other')", name="ck_lines_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="labor")
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="lines")


class JobNote(Base):
    __tablename__ = "job_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="notes")
    author: Mapped[User | None] = relationship()


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("stripe_checkout_session_id", name="uq_payments_checkout_session"),
        CheckConstraint(
            "method in ('stripe', 'cash', 'check', 'other')",
            name="ck_payments_method",
        ),
        CheckConstraint(
            "status in ('pending', 'succeeded', 'failed', 'refunded')",
            name="ck_payments_status",
        ),
        Index("ix_payments_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str] = mapped_column(String(500), default="")
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="payments")
    recorder: Mapped[User | None] = relationship()
