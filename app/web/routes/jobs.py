"""Jobs, line items, notes, and collecting payment."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.constants import CATEGORIES, MANUAL_METHODS, STATUSES
from app.formatting import datetime_local_value, format_money, format_percent, like_pattern, utcnow
from app.forms import clean_multiline, clean_text, job_form_defaults, parse_cents, parse_job_form, parse_line_form
from app.models import Customer, Job, JobLine, JobNote, Shop, User
from app.services.jobs import (
    add_event,
    allocate_number,
    apply_status,
    balance_cents,
    ensure_invoice_number,
    paid_cents,
    recalc_job,
    record_manual_payment,
    set_status,
)
from app.services.stripe_billing import StripeNotReady, refund_payment, start_checkout, stripe_state
from app.web.deps import assignee_choices, flash, get_db, go, load_job, posted, require_admin, require_user
from app.web.rendering import render

router = APIRouter()


def _page(raw: str | None) -> int:
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1


def _shop(db: Session) -> Shop:
    shop = db.get(Shop, 1)
    if shop is None:
        raise HTTPException(status_code=500)
    return shop


def _ids(rows) -> set[str]:
    return {row.id for row in rows}


@router.get("/jobs")
def job_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    category: str = "",
    assignee: str = "",
    page: str = "1",
):
    user = require_user(request, db)
    term = q.strip()[:100]
    if status not in STATUSES and status != "all":
        status = ""
    if category not in CATEGORIES:
        category = ""
    current = _page(page)
    stmt = select(Job).join(Job.customer)
    count_stmt = select(func.count()).select_from(Job).join(Job.customer)
    if status == "":
        clause = Job.status.notin_(("cancelled", "paid"))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    elif status != "all":
        stmt = stmt.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)
    if assignee == "me":
        stmt = stmt.where(Job.assigned_user_id == user.id)
        count_stmt = count_stmt.where(Job.assigned_user_id == user.id)
    elif assignee:
        stmt = stmt.where(Job.assigned_user_id == assignee)
        count_stmt = count_stmt.where(Job.assigned_user_id == assignee)
    if category:
        stmt = stmt.where(Job.category == category)
        count_stmt = count_stmt.where(Job.category == category)
    if term:
        pattern = like_pattern(term)
        text = or_(
            Job.title.ilike(pattern, escape="\\"),
            Job.number.ilike(pattern, escape="\\"),
            Job.service_address.ilike(pattern, escape="\\"),
            Customer.name.ilike(pattern, escape="\\"),
        )
        stmt = stmt.where(text)
        count_stmt = count_stmt.where(text)
    total = db.scalar(count_stmt) or 0
    per_page = 20
    pages = max(1, (total + per_page - 1) // per_page)
    current = min(current, pages)
    jobs = db.scalars(
        stmt.options(selectinload(Job.customer), selectinload(Job.assignee), selectinload(Job.payments))
        .order_by((Job.priority != "urgent"), Job.scheduled_start.is_(None), Job.scheduled_start.asc(), Job.created_at.desc())
        .offset((current - 1) * per_page)
        .limit(per_page)
    ).all()
    filters = {"q": term, "status": status, "category": category, "assignee": assignee}

    def link(number: int) -> str:
        payload = {key: value for key, value in filters.items() if value}
        payload["page"] = str(number)
        return "/jobs?" + urlencode(payload)

    return render(
        request,
        db,
        "jobs/list.html",
        jobs=jobs,
        q=term,
        status=status,
        category=category,
        assignee=assignee,
        assignees=assignee_choices(db),
        page=current,
        pages=pages,
        total=total,
        prev_url=link(current - 1) if current > 1 else "",
        next_url=link(current + 1) if current < pages else "",
    )


@router.get("/jobs/new")
def job_new(request: Request, db: Session = Depends(get_db), customer: str = ""):
    require_user(request, db)
    shop = _shop(db)
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    values = job_form_defaults(shop, customer if any(row.id == customer for row in customers) else "")
    return render(
        request,
        db,
        "jobs/form.html",
        mode="new",
        customers=customers,
        assignees=assignee_choices(db),
        values=values,
        errors={},
        job=None,
    )


@router.post("/jobs")
async def job_create(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    shop = _shop(db)
    form = await posted(request)
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    users = assignee_choices(db)
    parsed, errors, raw = parse_job_form(form, shop.zoneinfo(), _ids(customers), _ids(users), with_line=True)
    if parsed is None:
        return render(
            request,
            db,
            "jobs/form.html",
            status_code=400,
            mode="new",
            customers=customers,
            assignees=users,
            values=raw,
            errors=errors,
            job=None,
        )
    job = Job(
        number=allocate_number(db, shop, "job"),
        customer_id=parsed.customer_id,
        title=parsed.title,
        category=parsed.category,
        description=parsed.description,
        service_address=parsed.service_address,
        priority=parsed.priority,
        status="inquiry",
        scheduled_start=parsed.scheduled_start,
        scheduled_end=parsed.scheduled_end,
        assigned_user_id=parsed.assigned_user_id,
        tax_rate_bps=parsed.tax_rate_bps,
        tax_labor=parsed.tax_labor,
        discount_cents=parsed.discount_cents,
        customer_note=parsed.customer_note,
        created_by_id=user.id,
    )
    db.add(job)
    db.flush()
    if parsed.line_description and parsed.line_quantity is not None and parsed.line_unit_price_cents is not None:
        job.lines.append(
            JobLine(
                kind=parsed.line_kind,
                description=parsed.line_description,
                quantity=parsed.line_quantity,
                unit_price_cents=parsed.line_unit_price_cents,
                sort_order=1,
            )
        )
    apply_status(job, parsed.status)
    recalc_job(db, job)
    if job.status in {"invoiced", "paid"}:
        ensure_invoice_number(db, shop, job)
    add_event(db, job, user, "Opened this job.")
    flash(request, f"Opened {job.number}.")
    return go(f"/jobs/{job.id}")


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    require_user(request, db)
    if request.query_params.get("payment") == "cancelled":
        flash(request, "The card payment was cancelled. Nothing was charged.", "warn")
        return go(f"/jobs/{job_id}")
    job = load_job(db, job_id)
    return render(
        request,
        db,
        "jobs/detail.html",
        job=job,
        paid=paid_cents(job),
        balance=balance_cents(job),
        stripe=stripe_state(request.app.state.settings),
    )


@router.get("/jobs/{job_id}/edit")
def job_edit(request: Request, job_id: str, db: Session = Depends(get_db)):
    require_user(request, db)
    job = load_job(db, job_id)
    shop = _shop(db)
    values = job_form_defaults(shop, job.customer_id)
    values.update(
        {
            "customer_id": job.customer_id,
            "title": job.title,
            "category": job.category,
            "description": job.description,
            "service_address": job.service_address,
            "priority": job.priority,
            "status": job.status,
            "scheduled_start": datetime_local_value(job.scheduled_start, shop.timezone),
            "scheduled_end": datetime_local_value(job.scheduled_end, shop.timezone),
            "assigned_user_id": job.assigned_user_id or "",
            "tax_rate": format_percent(job.tax_rate_bps),
            "tax_labor": "1" if job.tax_labor else "",
            "discount": "" if job.discount_cents == 0 else f"{job.discount_cents / 100:.2f}",
            "customer_note": job.customer_note,
        }
    )
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    return render(
        request,
        db,
        "jobs/form.html",
        mode="edit",
        customers=customers,
        assignees=assignee_choices(db, job.assigned_user_id),
        values=values,
        errors={},
        job=job,
    )


@router.post("/jobs/{job_id}")
async def job_update(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    shop = _shop(db)
    job = load_job(db, job_id)
    form = await posted(request)
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    users = assignee_choices(db, job.assigned_user_id)
    parsed, errors, raw = parse_job_form(form, shop.zoneinfo(), _ids(customers), _ids(users), with_line=False)
    if parsed is None:
        return render(
            request,
            db,
            "jobs/form.html",
            status_code=400,
            mode="edit",
            customers=customers,
            assignees=users,
            values=raw,
            errors=errors,
            job=job,
        )
    before = (
        job.customer_id,
        job.title,
        job.category,
        job.description,
        job.service_address,
        job.priority,
        job.scheduled_start,
        job.scheduled_end,
        job.assigned_user_id,
        job.tax_rate_bps,
        job.tax_labor,
        job.discount_cents,
        job.customer_note,
    )
    old_status = job.status
    job.customer_id = parsed.customer_id
    job.title = parsed.title
    job.category = parsed.category
    job.description = parsed.description
    job.service_address = parsed.service_address
    job.priority = parsed.priority
    job.scheduled_start = parsed.scheduled_start
    job.scheduled_end = parsed.scheduled_end
    job.assigned_user_id = parsed.assigned_user_id
    job.tax_rate_bps = parsed.tax_rate_bps
    job.tax_labor = parsed.tax_labor
    job.discount_cents = parsed.discount_cents
    job.customer_note = parsed.customer_note
    after = (
        job.customer_id,
        job.title,
        job.category,
        job.description,
        job.service_address,
        job.priority,
        job.scheduled_start,
        job.scheduled_end,
        job.assigned_user_id,
        job.tax_rate_bps,
        job.tax_labor,
        job.discount_cents,
        job.customer_note,
    )
    if old_status != parsed.status:
        set_status(db, shop, job, user, parsed.status)
    recalc_job(db, job)
    if before != after:
        add_event(db, job, user, "Updated the job.")
    flash(request, "Job updated.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/status")
async def job_status(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    shop = _shop(db)
    job = load_job(db, job_id)
    form = await posted(request)
    status = str(form.get("status") or "")
    if status not in STATUSES:
        flash(request, "Choose a valid status.", "error")
        return go(f"/jobs/{job.id}")
    if not set_status(db, shop, job, user, status):
        flash(request, "Status is already set.", "warn")
    else:
        flash(request, "Status updated.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/lines")
async def line_add(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    job = load_job(db, job_id)
    form = await posted(request)
    parsed, errors = parse_line_form(form)
    if parsed is None:
        flash(request, next(iter(errors.values())), "error")
        return go(f"/jobs/{job.id}")
    sort = max((line.sort_order for line in job.lines), default=0) + 1
    job.lines.append(
        JobLine(
            kind=parsed.kind,
            description=parsed.description,
            quantity=parsed.quantity,
            unit_price_cents=parsed.unit_price_cents,
            sort_order=sort,
        )
    )
    recalc_job(db, job)
    add_event(db, job, user, f"Added a line: {parsed.description}.")
    flash(request, "Line added.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/lines/{line_id}")
async def line_edit(request: Request, job_id: str, line_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    job = load_job(db, job_id)
    line = next((item for item in job.lines if item.id == line_id), None)
    if line is None:
        raise HTTPException(status_code=404)
    form = await posted(request)
    parsed, errors = parse_line_form(form)
    if parsed is None:
        flash(request, next(iter(errors.values())), "error")
        return go(f"/jobs/{job.id}")
    line.kind = parsed.kind
    line.description = parsed.description
    line.quantity = parsed.quantity
    line.unit_price_cents = parsed.unit_price_cents
    recalc_job(db, job)
    add_event(db, job, user, f"Updated a line: {parsed.description}.")
    flash(request, "Line updated.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/lines/{line_id}/delete")
async def line_delete(request: Request, job_id: str, line_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    job = load_job(db, job_id)
    await posted(request)
    line = next((item for item in job.lines if item.id == line_id), None)
    if line is None:
        raise HTTPException(status_code=404)
    description = line.description
    db.delete(line)
    recalc_job(db, job)
    add_event(db, job, user, f"Removed a line: {description}.")
    flash(request, "Line removed.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/notes")
async def note_add(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    job = load_job(db, job_id)
    form = await posted(request)
    body = clean_multiline(str(form.get("body") or ""), 2000)
    if not body:
        flash(request, "Write a note before saving it.", "error")
        return go(f"/jobs/{job.id}")
    if len(str(form.get("body") or "").strip()) > 2000:
        flash(request, "That note is too long.", "error")
        return go(f"/jobs/{job.id}")
    db.add(JobNote(job_id=job.id, author_id=user.id, body=body))
    job.updated_at = utcnow()
    flash(request, "Note added.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/payments")
async def payment_add(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    shop = _shop(db)
    job = load_job(db, job_id)
    form = await posted(request)
    if job.status == "cancelled":
        flash(request, "Cancelled jobs do not take payments.", "error")
        return go(f"/jobs/{job.id}")
    due = balance_cents(job)
    if due <= 0:
        flash(request, "Nothing is due on this job.", "warn")
        return go(f"/jobs/{job.id}")
    method = str(form.get("method") or "")
    if method not in MANUAL_METHODS:
        flash(request, "Choose cash, check, or other.", "error")
        return go(f"/jobs/{job.id}")
    try:
        amount = parse_cents(str(form.get("amount") or ""))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return go(f"/jobs/{job.id}")
    if amount <= 0:
        flash(request, "Enter an amount greater than zero.", "error")
        return go(f"/jobs/{job.id}")
    if amount > due:
        flash(request, f"That is more than the balance of {format_money(due)}.", "error")
        return go(f"/jobs/{job.id}")
    note = clean_text(str(form.get("note") or ""), 500)
    record_manual_payment(db, shop, job, user, amount, method, note)
    flash(request, "Payment recorded.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/checkout")
async def job_checkout(request: Request, job_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    shop = _shop(db)
    job = load_job(db, job_id)
    await posted(request)
    try:
        url = start_checkout(db, request.app.state.settings, shop, job, user)
        return go_external(url)
    except StripeNotReady as exc:
        flash(request, exc.message, "warn")
        return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/payments/{payment_id}/refund")
async def payment_refund(request: Request, job_id: str, payment_id: str, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    job = load_job(db, job_id)
    await posted(request)
    payment = next((item for item in job.payments if item.id == payment_id), None)
    if payment is None:
        raise HTTPException(status_code=404)
    try:
        refund_payment(db, request.app.state.settings, job, payment, user)
    except StripeNotReady as exc:
        flash(request, exc.message, "error")
        return go(f"/jobs/{job.id}")
    flash(request, "Payment refunded in the ledger.")
    return go(f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/delete")
async def job_delete(request: Request, job_id: str, db: Session = Depends(get_db)):
    require_admin(request, db)
    job = load_job(db, job_id)
    await posted(request)
    number = job.number
    db.delete(job)
    flash(request, f"Deleted {number}.")
    return go("/jobs")


@router.get("/jobs/{job_id}/invoice")
def job_invoice(request: Request, job_id: str, db: Session = Depends(get_db)):
    require_user(request, db)
    job = load_job(db, job_id)
    return render(
        request,
        db,
        "jobs/invoice.html",
        job=job,
        paid=paid_cents(job),
        balance=balance_cents(job),
    )


def go_external(url: str):
    from starlette.responses import RedirectResponse

    from app.services.stripe_billing import safe_checkout_url

    return RedirectResponse(safe_checkout_url(url), status_code=303)
