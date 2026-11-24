"""Dashboard and the week ahead."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.formatting import greeting, local_day_bounds, today_label, zone
from app.models import Customer, Job, Payment
from app.services.jobs import balance_cents, collectable, month_bounds, net_collected
from app.services.sample import sample_customer_count
from app.web.deps import get_db, require_user
from app.web.rendering import render

router = APIRouter()


def _load_jobs(db: Session, stmt):
    return db.scalars(
        stmt.options(selectinload(Job.customer), selectinload(Job.assignee), selectinload(Job.payments))
    ).all()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    from app.models import Shop

    shop_row = db.get(Shop, 1)
    tz_name = shop_row.timezone if shop_row else "America/New_York"
    now_local = datetime.now(zone(tz_name))
    start, end = local_day_bounds(now_local.date(), tz_name)
    today_jobs = _load_jobs(
        db,
        select(Job)
        .where(Job.scheduled_start >= start, Job.scheduled_start < end, Job.status != "cancelled")
        .order_by(Job.scheduled_start),
    )
    behind = _load_jobs(
        db,
        select(Job)
        .where(
            Job.status.in_(("scheduled", "in_progress")),
            Job.scheduled_start.is_not(None),
            Job.scheduled_start < datetime.now(zone(tz_name)).astimezone(start.tzinfo),
        )
        .order_by(Job.scheduled_start)
        .limit(8),
    )
    money_jobs = _load_jobs(
        db,
        select(Job).where(Job.status.in_(("completed", "invoiced"))).order_by(Job.completed_at),
    )
    to_collect = [job for job in money_jobs if collectable(job)]
    to_collect_cents = sum(balance_cents(job) for job in to_collect)
    month_start, month_end = month_bounds(tz_name)
    month_payments = db.scalars(
        select(Payment).where(
            (
                (Payment.received_at >= month_start)
                & (Payment.received_at < month_end)
                & (Payment.status.in_(("succeeded", "refunded")))
            )
            | (
                (Payment.refunded_at >= month_start)
                & (Payment.refunded_at < month_end)
                & (Payment.status == "refunded")
            )
        )
    ).all()
    in_progress = db.scalar(
        select(func.count()).select_from(Job).where(Job.status == "in_progress")
    ) or 0
    open_count = db.scalar(
        select(func.count()).select_from(Job).where(Job.status.in_(("inquiry", "scheduled", "in_progress")))
    ) or 0
    return render(
        request,
        db,
        "dashboard.html",
        greeting=greeting(tz_name),
        today_label=today_label(tz_name),
        today_jobs=today_jobs,
        behind=behind,
        to_collect=to_collect[:6],
        to_collect_count=len(to_collect),
        to_collect_cents=to_collect_cents,
        collected_cents=net_collected(month_payments, month_start, month_end),
        in_progress_count=in_progress,
        open_count=open_count,
        sample_count=sample_customer_count(db),
    )


@router.get("/schedule")
def schedule(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    from app.models import Shop

    shop_row = db.get(Shop, 1)
    tz_name = shop_row.timezone if shop_row else "America/New_York"
    today = datetime.now(zone(tz_name)).date()
    week_start, _ = local_day_bounds(today, tz_name)
    week_end = week_start + timedelta(days=7)
    jobs = _load_jobs(
        db,
        select(Job)
        .where(
            Job.scheduled_start >= week_start,
            Job.scheduled_start < week_end,
            Job.status != "cancelled",
        )
        .order_by(Job.scheduled_start),
    )
    buckets = {today + timedelta(days=offset): [] for offset in range(7)}
    for job in jobs:
        if job.scheduled_start is None:
            continue
        local_day = job.scheduled_start.astimezone(zone(tz_name)).date()
        if local_day in buckets:
            buckets[local_day].append(job)
    days = []
    for offset, day in enumerate(buckets):
        if offset == 0:
            label = "Today"
        elif offset == 1:
            label = "Tomorrow"
        else:
            label = f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}"
        days.append({"label": label, "jobs": buckets[day]})
    unscheduled = _load_jobs(
        db,
        select(Job)
        .where(
            Job.scheduled_start.is_(None),
            Job.status.in_(("inquiry", "scheduled", "in_progress", "completed")),
        )
        .order_by(Job.created_at.desc())
        .limit(40),
    )
    return render(request, db, "schedule.html", days=days, unscheduled=unscheduled)
