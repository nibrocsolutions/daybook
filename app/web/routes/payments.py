"""Money waiting to be collected, and the return from Stripe Checkout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.jobs import balance_cents, collectable
from app.services.stripe_billing import StripeNotReady, retrieve_and_apply
from app.web.deps import flash, get_db, go, require_user
from app.web.rendering import render

router = APIRouter()


@router.get("/payments")
def payment_list(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    from app.models import Job, Payment

    jobs = db.scalars(
        select(Job)
        .where(Job.status.in_(("completed", "invoiced", "paid", "in_progress", "scheduled")))
        .options(selectinload(Job.customer), selectinload(Job.payments))
        .order_by(Job.updated_at.desc())
    ).all()
    due = [job for job in jobs if job.status in {"completed", "invoiced"} and collectable(job)]
    due.sort(key=lambda job: job.completed_at or job.created_at)
    recent = db.scalars(
        select(Payment)
        .options(selectinload(Payment.job), selectinload(Payment.recorder))
        .order_by(Payment.created_at.desc())
        .limit(30)
    ).all()
    return render(
        request,
        db,
        "payments/list.html",
        due_jobs=due,
        due_cents=sum(balance_cents(job) for job in due),
        recent=recent,
    )


@router.get("/payments/return")
def payment_return(request: Request, db: Session = Depends(get_db), session_id: str = ""):
    require_user(request, db)
    settings = request.app.state.settings
    try:
        result = retrieve_and_apply(db, settings, session_id.strip())
    except StripeNotReady as exc:
        flash(request, exc.message, "warn")
        return go("/payments")
    if result.status in {"paid", "exists"} and result.job_id:
        flash(request, "Card payment recorded.")
        return go(f"/jobs/{result.job_id}")
    if result.status == "unpaid":
        flash(request, "The card payment is not complete yet.", "warn")
    elif result.status == "mismatch":
        flash(request, "Stripe reported an amount that does not match this job. Review it before trusting the balance.", "error")
    else:
        flash(request, "Fieldbook could not match that checkout to a job.", "error")
    if result.job_id:
        return go(f"/jobs/{result.job_id}")
    return go("/payments")
