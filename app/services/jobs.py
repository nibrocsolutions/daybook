"""Job ledger operations shared by routes, seed data, and Stripe."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import COLLECT_STATUSES, STATUS_LABELS
from app.formatting import format_money, utcnow
from app.models import Job, JobEvent, Payment, Shop
from app.services.totals import compute_totals


def touch(job: Job) -> None:
    job.updated_at = utcnow()


def add_event(db: Session, job: Job, actor, message: str) -> None:
    db.add(
        JobEvent(
            job_id=job.id,
            actor_id=getattr(actor, "id", None),
            message=message[:500],
        )
    )


def paid_cents(job: Job) -> int:
    return sum(payment.amount_cents for payment in job.payments if payment.status == "succeeded")


def balance_cents(job: Job) -> int:
    return int(job.total_cents) - paid_cents(job)


def allocate_number(db: Session, shop: Shop, kind: str) -> str:
    query = select(Shop).where(Shop.id == shop.id)
    bind = db.get_bind()
    if bind is not None and bind.dialect.name != "sqlite":
        query = query.with_for_update()
    locked = db.scalars(query).one()
    if kind == "invoice":
        number = locked.next_invoice_number
        locked.next_invoice_number = number + 1
        prefix = locked.invoice_prefix
    else:
        number = locked.next_job_number
        locked.next_job_number = number + 1
        prefix = locked.job_prefix
    db.flush()
    return f"{prefix}{number}"


def ensure_invoice_number(db: Session, shop: Shop, job: Job) -> None:
    if job.invoice_number:
        return
    job.invoice_number = allocate_number(db, shop, "invoice")


def apply_status(job: Job, status: str) -> None:
    job.status = status
    if status in {"completed", "invoiced", "paid"}:
        if job.completed_at is None:
            job.completed_at = utcnow()
    elif status in {"inquiry", "scheduled", "in_progress", "cancelled"}:
        job.completed_at = None
    touch(job)


def sync_payment_status(job: Job) -> None:
    if job.status == "cancelled":
        return
    collected = paid_cents(job)
    if (
        job.total_cents > 0
        and collected >= job.total_cents
        and job.status in {"completed", "invoiced", "paid"}
    ):
        job.status = "paid"
        if job.completed_at is None:
            job.completed_at = utcnow()
    elif job.status == "paid" and collected < job.total_cents:
        job.status = "invoiced"
    touch(job)


def recalc_job(db: Session, job: Job) -> None:
    db.flush()
    db.expire(job, ["lines", "payments"])
    totals = compute_totals(list(job.lines), job.tax_rate_bps, job.tax_labor, job.discount_cents)
    job.subtotal_cents = totals.subtotal_cents
    job.tax_cents = totals.tax_cents
    job.discount_cents = totals.discount_cents
    job.total_cents = totals.total_cents
    sync_payment_status(job)


def set_status(db: Session, shop: Shop, job: Job, actor, status: str) -> bool:
    if status not in STATUS_LABELS or status == job.status:
        return False
    previous = job.status
    apply_status(job, status)
    if status in {"invoiced", "paid"}:
        ensure_invoice_number(db, shop, job)
    add_event(
        db,
        job,
        actor,
        f"Status changed from {STATUS_LABELS[previous]} to {STATUS_LABELS[status]}.",
    )
    return True


def record_manual_payment(db: Session, shop: Shop, job: Job, actor, amount_cents: int, method: str, note: str) -> Payment:
    ensure_invoice_number(db, shop, job)
    payment = Payment(
        job_id=job.id,
        amount_cents=amount_cents,
        method=method,
        status="succeeded",
        note=note[:500],
        recorded_by_id=actor.id,
        received_at=utcnow(),
    )
    db.add(payment)
    job.payments.append(payment)
    sync_payment_status(job)
    add_event(
        db,
        job,
        actor,
        f"Recorded a {method} payment of {format_money(amount_cents)}.",
    )
    touch(job)
    return payment


def refund_local(db: Session, job: Job, payment: Payment, actor) -> None:
    payment.status = "refunded"
    payment.refunded_at = utcnow()
    sync_payment_status(job)
    add_event(
        db,
        job,
        actor,
        f"Refunded a {payment.method} payment of {format_money(payment.amount_cents)}.",
    )
    touch(job)


def month_bounds(tz_name: str):
    from datetime import datetime

    from app.formatting import local_day_bounds, zone

    local = datetime.now(zone(tz_name))
    start_day = local.date().replace(day=1)
    start, _ = local_day_bounds(start_day, tz_name)
    if start_day.month == 12:
        next_month = start_day.replace(year=start_day.year + 1, month=1)
    else:
        next_month = start_day.replace(month=start_day.month + 1)
    end, _ = local_day_bounds(next_month, tz_name)
    return start, end


def net_collected(payments, start, end) -> int:
    incoming = 0
    outgoing = 0
    for payment in payments:
        received = payment.received_at
        refunded = payment.refunded_at
        if (
            payment.status in {"succeeded", "refunded"}
            and received is not None
            and start <= received < end
        ):
            incoming += payment.amount_cents
        if payment.status == "refunded" and refunded is not None and start <= refunded < end:
            outgoing += payment.amount_cents
    return incoming - outgoing


def collectable(job: Job) -> bool:
    return job.status in COLLECT_STATUSES and balance_cents(job) > 0


def recent_pending_checkout(job: Job, amount: int):
    cutoff = utcnow() - timedelta(hours=23)
    candidates = [
        payment
        for payment in job.payments
        if payment.method == "stripe"
        and payment.status == "pending"
        and payment.amount_cents == amount
        and payment.checkout_url
        and payment.created_at >= cutoff
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda payment: payment.created_at)
