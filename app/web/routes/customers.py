"""Customer records."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.formatting import like_pattern, utcnow
from app.forms import parse_customer_form
from app.models import Customer, Job
from app.web.deps import flash, get_db, go, posted, require_admin, require_user
from app.web.rendering import render

router = APIRouter()


def _page(raw: str | None) -> int:
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1


def _customer(db: Session, customer_id: str) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404)
    return customer


@router.get("/customers")
def customer_list(request: Request, db: Session = Depends(get_db), q: str = "", page: str = "1"):
    require_user(request, db)
    term = q.strip()[:100]
    current = _page(page)
    stmt = select(Customer)
    count_stmt = select(func.count()).select_from(Customer)
    if term:
        pattern = like_pattern(term)
        clause = (
            Customer.name.ilike(pattern, escape="\\")
            | Customer.phone.ilike(pattern, escape="\\")
            | Customer.email.ilike(pattern, escape="\\")
            | Customer.city.ilike(pattern, escape="\\")
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    pages = max(1, (total + 39) // 40)
    current = min(current, pages)
    customers = db.scalars(stmt.order_by(Customer.name).offset((current - 1) * 40).limit(40)).all()
    counts = dict(
        db.execute(
            select(Job.customer_id, func.count(Job.id))
            .where(Job.customer_id.in_([customer.id for customer in customers] or [""]))
            .group_by(Job.customer_id)
        ).all()
    )
    query = urlencode({"q": term}) if term else ""
    return render(
        request,
        db,
        "customers/list.html",
        customers=customers,
        counts=counts,
        q=term,
        page=current,
        pages=pages,
        total=total,
        query=query,
    )


@router.get("/customers/new")
def customer_new(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    return render(
        request,
        db,
        "customers/form.html",
        mode="new",
        values={
            "name": "",
            "phone": "",
            "email": "",
            "address_line": "",
            "city": "",
            "region": "",
            "postal_code": "",
            "notes": "",
            "is_sample": "",
        },
        errors={},
        customer=None,
    )


@router.post("/customers")
async def customer_create(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    form = await posted(request)
    parsed, errors, raw = parse_customer_form(form, allow_sample=False)
    if parsed is None:
        return render(request, db, "customers/form.html", status_code=400, mode="new", values=raw, errors=errors, customer=None)
    customer = Customer(
        name=parsed.name,
        phone=parsed.phone,
        email=parsed.email,
        address_line=parsed.address_line,
        city=parsed.city,
        region=parsed.region,
        postal_code=parsed.postal_code,
        notes=parsed.notes,
        is_sample=False,
        created_by_id=user.id,
    )
    db.add(customer)
    db.flush()
    flash(request, f"Added {customer.name}.")
    return go(f"/customers/{customer.id}")


@router.get("/customers/{customer_id}")
def customer_detail(request: Request, customer_id: str, db: Session = Depends(get_db)):
    require_user(request, db)
    customer = db.scalars(
        select(Customer).where(Customer.id == customer_id).options(selectinload(Customer.jobs))
    ).first()
    if customer is None:
        raise HTTPException(status_code=404)
    jobs = sorted(customer.jobs, key=lambda job: job.created_at, reverse=True)
    return render(request, db, "customers/detail.html", customer=customer, jobs=jobs)


@router.get("/customers/{customer_id}/edit")
def customer_edit(request: Request, customer_id: str, db: Session = Depends(get_db)):
    require_user(request, db)
    customer = _customer(db, customer_id)
    values = {
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "address_line": customer.address_line,
        "city": customer.city,
        "region": customer.region,
        "postal_code": customer.postal_code,
        "notes": customer.notes,
        "is_sample": "1" if customer.is_sample else "",
    }
    return render(request, db, "customers/form.html", mode="edit", values=values, errors={}, customer=customer)


@router.post("/customers/{customer_id}")
async def customer_update(request: Request, customer_id: str, db: Session = Depends(get_db)):
    user = require_user(request, db)
    customer = _customer(db, customer_id)
    form = await posted(request)
    parsed, errors, raw = parse_customer_form(form, allow_sample=user.role == "admin")
    if parsed is None:
        return render(
            request,
            db,
            "customers/form.html",
            status_code=400,
            mode="edit",
            values=raw,
            errors=errors,
            customer=customer,
        )
    customer.name = parsed.name
    customer.phone = parsed.phone
    customer.email = parsed.email
    customer.address_line = parsed.address_line
    customer.city = parsed.city
    customer.region = parsed.region
    customer.postal_code = parsed.postal_code
    customer.notes = parsed.notes
    if user.role == "admin":
        customer.is_sample = parsed.is_sample
    customer.updated_at = utcnow()
    flash(request, "Customer updated.")
    return go(f"/customers/{customer.id}")


@router.post("/customers/{customer_id}/delete")
async def customer_delete(request: Request, customer_id: str, db: Session = Depends(get_db)):
    require_admin(request, db)
    customer = _customer(db, customer_id)
    await posted(request)
    job_count = db.scalar(select(func.count()).select_from(Job).where(Job.customer_id == customer.id)) or 0
    if job_count:
        flash(request, "Delete or move this customer's jobs before deleting the customer.", "error")
        return go(f"/customers/{customer.id}")
    name = customer.name
    db.delete(customer)
    flash(request, f"Deleted {name}.")
    return go("/customers")
