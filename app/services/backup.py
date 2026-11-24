"""JSON backup and spreadsheet export. Passwords are never included."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.constants import (
    BACKUP_VERSION,
    CATEGORIES,
    LINE_KINDS,
    MAX_MONEY_CENTS,
    METHOD_LABELS,
    PAYMENT_STATUSES,
    PRIORITIES,
    ROLES,
    STATUSES,
)
from app.errors import BackupError
from app.formatting import clean_timezone, csv_safe, format_money, utcnow
from app.models import Customer, Job, JobLine, Payment, Shop, User, new_id
from app.services.jobs import recalc_job
from app.services.totals import line_amount_cents

_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}\Z")
_PREFIX = re.compile(r"^[A-Za-z0-9-]{1,12}\Z")
_NUMBER_TAIL = re.compile(r"(\d+)\Z")


def export_payload(db: Session) -> dict:
    shop = db.get(Shop, 1)
    if shop is None:
        raise BackupError("Shop settings are missing.")
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    jobs = db.scalars(
        select(Job).options(selectinload(Job.lines), selectinload(Job.payments)).order_by(Job.number)
    ).all()
    users = db.scalars(select(User).order_by(User.name)).all()
    lines = []
    payments = []
    for job in jobs:
        for line in job.lines:
            lines.append(_line_out(line))
        for payment in job.payments:
            payments.append(_payment_out(payment))
    return {
        "version": BACKUP_VERSION,
        "exported_at": utcnow().isoformat(),
        "shop": _shop_out(shop),
        "customers": [_customer_out(customer) for customer in customers],
        "jobs": [_job_out(job) for job in jobs],
        "line_items": lines,
        "payments": payments,
        "users": [_user_out(user) for user in users],
    }


def jobs_csv(db: Session) -> str:
    from app.services.jobs import balance_cents, paid_cents

    jobs = db.scalars(
        select(Job)
        .options(selectinload(Job.customer), selectinload(Job.assignee), selectinload(Job.payments))
        .order_by(Job.number)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "number",
            "invoice_number",
            "title",
            "category",
            "status",
            "priority",
            "customer",
            "phone",
            "email",
            "service_address",
            "scheduled_start",
            "assignee",
            "subtotal",
            "tax",
            "total",
            "paid",
            "balance",
            "created_at",
        ]
    )
    for job in jobs:
        customer = job.customer
        writer.writerow(
            [
                csv_safe(job.number),
                csv_safe(job.invoice_number or ""),
                csv_safe(job.title),
                csv_safe(job.category),
                csv_safe(job.status),
                csv_safe(job.priority),
                csv_safe(customer.name if customer else ""),
                csv_safe(customer.phone if customer else ""),
                csv_safe(customer.email if customer else ""),
                csv_safe(job.display_address),
                csv_safe(_iso(job.scheduled_start)),
                csv_safe(job.assignee.name if job.assignee else ""),
                format_money(job.subtotal_cents),
                format_money(job.tax_cents),
                format_money(job.total_cents),
                format_money(paid_cents(job)),
                format_money(balance_cents(job)),
                csv_safe(_iso(job.created_at)),
            ]
        )
    return buffer.getvalue()


def customers_csv(db: Session) -> str:
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "phone", "email", "address", "city", "region", "postal_code", "notes"])
    for customer in customers:
        writer.writerow(
            [
                csv_safe(customer.name),
                csv_safe(customer.phone),
                csv_safe(customer.email),
                csv_safe(customer.address_line),
                csv_safe(customer.city),
                csv_safe(customer.region),
                csv_safe(customer.postal_code),
                csv_safe(customer.notes),
            ]
        )
    return buffer.getvalue()


def payments_csv(db: Session) -> str:
    payments = db.scalars(
        select(Payment).options(selectinload(Payment.job)).order_by(Payment.created_at)
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["job_number", "amount", "method", "status", "note", "received_at", "refunded_at", "stripe_payment_intent"]
    )
    for payment in payments:
        writer.writerow(
            [
                csv_safe(payment.job.number if payment.job else ""),
                format_money(payment.amount_cents),
                csv_safe(payment.method),
                csv_safe(payment.status),
                csv_safe(payment.note),
                csv_safe(_iso(payment.received_at)),
                csv_safe(_iso(payment.refunded_at)),
                csv_safe(payment.stripe_payment_intent_id or ""),
            ]
        )
    return buffer.getvalue()


def import_payload(db: Session, payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise BackupError("That file is not a Daybook backup.")
    if payload.get("version") != BACKUP_VERSION:
        raise BackupError("This backup version is not supported.")
    try:
        shop = db.get(Shop, 1)
        if shop is None:
            raise BackupError("Shop settings are missing.")
        _import_shop(shop, payload.get("shop") or {})
        customers = _import_customers(db, _as_list(payload.get("customers"), "customers", 5000))
        jobs = _import_jobs(db, customers, _as_list(payload.get("jobs"), "jobs", 20000))
        _import_lines(db, jobs, _as_list(payload.get("line_items"), "line_items", 50000))
        _import_payments(db, jobs, _as_list(payload.get("payments"), "payments", 50000))
        for job in jobs.values():
            recalc_job(db, job)
        _bump_sequences(shop, list(jobs.values()))
        db.flush()
    except BackupError:
        raise
    except IntegrityError as exc:
        raise BackupError("That file conflicts with records already in the shop. Nothing was changed.") from exc
    except (TypeError, ValueError) as exc:
        raise BackupError("That file has a field Daybook could not read. Nothing was changed.") from exc
    user_rows = payload.get("users")
    return {
        "customers": len(customers),
        "jobs": len(jobs),
        "users_skipped": len(user_rows) if isinstance(user_rows, list) else 0,
    }


def _as_list(value, label: str, limit: int) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BackupError(f"{label} must be a list.")
    if len(value) > limit:
        raise BackupError(f"{label} has more rows than Daybook will import at once.")
    return value


def _import_shop(shop: Shop, raw: object) -> None:
    if not isinstance(raw, dict):
        raise BackupError("Shop settings in the backup are unreadable.")
    if "business_name" in raw:
        shop.business_name = _text(raw.get("business_name"), "Business name", 120, required=True)
    if "phone" in raw:
        shop.phone = _text(raw.get("phone"), "Phone", 40)
    if "email" in raw:
        shop.email = _email(raw.get("email"), required=False) or ""
    if "address" in raw:
        shop.address = _multiline(raw.get("address"), "Address", 500)
    if "timezone" in raw:
        shop.timezone = clean_timezone(str(raw.get("timezone") or ""))
    if "tax_rate_bps" in raw:
        shop.tax_rate_bps = _cents_like(raw.get("tax_rate_bps"), "Tax rate", maximum=2500)
    if "tax_labor" in raw:
        shop.tax_labor = bool(raw.get("tax_labor"))
    if "job_prefix" in raw:
        shop.job_prefix = _prefix(raw.get("job_prefix"), "JOB-")
    if "invoice_prefix" in raw:
        shop.invoice_prefix = _prefix(raw.get("invoice_prefix"), "INV-")
    shop.updated_at = utcnow()


def _import_customers(db: Session, rows: list) -> dict[str, Customer]:
    found: dict[str, Customer] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("A customer row is unreadable.")
        customer_id = _optional_id(row.get("id"))
        customer = db.get(Customer, customer_id) if customer_id else None
        if customer is None:
            customer = Customer(id=customer_id or new_id())
            db.add(customer)
        customer.name = _text(row.get("name"), "Customer name", 120, required=True)
        customer.phone = _text(row.get("phone"), "Phone", 40)
        customer.email = _email(row.get("email"), required=False) or ""
        customer.address_line = _text(row.get("address_line"), "Address", 160)
        customer.city = _text(row.get("city"), "City", 80)
        customer.region = _text(row.get("region"), "Region", 40)
        customer.postal_code = _text(row.get("postal_code"), "Postal code", 20)
        customer.notes = _multiline(row.get("notes"), "Notes", 4000)
        customer.is_sample = bool(row.get("is_sample"))
        found[customer.id] = customer
    db.flush()
    return found


def _import_jobs(db: Session, customers: dict[str, Customer], rows: list) -> dict[str, Job]:
    known_customers = {customer.id: customer for customer in db.scalars(select(Customer)).all()}
    known_customers.update(customers)
    user_ids = set(db.scalars(select(User.id)).all())
    found: dict[str, Job] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("A job row is unreadable.")
        job_id = _optional_id(row.get("id"))
        number = _text(row.get("number"), "Job number", 24, required=True)
        customer_id = _optional_id(row.get("customer_id"))
        if not customer_id or customer_id not in known_customers:
            raise BackupError(f"{number} points at a customer that is not in the shop.")
        job = db.get(Job, job_id) if job_id else None
        if job is None:
            job = db.scalars(select(Job).where(Job.number == number)).first()
        if job is None:
            job = Job(id=job_id or new_id(), number=number, customer_id=customer_id, title="Import")
            db.add(job)
        status = str(row.get("status") or "inquiry")
        priority = str(row.get("priority") or "normal")
        if status not in STATUSES:
            raise BackupError(f"{number} has a status Daybook does not use.")
        if priority not in PRIORITIES:
            raise BackupError(f"{number} has a priority Daybook does not use.")
        category = str(row.get("category") or "other")
        if category not in CATEGORIES:
            raise BackupError(f"{number} has a kind of work Daybook does not use.")
        assignee = _optional_id(row.get("assigned_user_id"))
        if assignee and assignee not in user_ids:
            assignee = None
        job.number = number
        job.invoice_number = _optional_text(row.get("invoice_number"), 24)
        job.customer_id = customer_id
        job.title = _text(row.get("title"), "Job title", 140, required=True)
        job.category = category
        job.description = _multiline(row.get("description"), "Description", 8000)
        job.service_address = _multiline(row.get("service_address"), "Service address", 500)
        job.status = status
        job.priority = priority
        job.scheduled_start = _dt(row.get("scheduled_start"))
        job.scheduled_end = _dt(row.get("scheduled_end"))
        job.assigned_user_id = assignee
        job.tax_rate_bps = _cents_like(row.get("tax_rate_bps") or 0, "Tax rate", maximum=2500)
        job.tax_labor = bool(row.get("tax_labor", True))
        job.discount_cents = _cents_like(row.get("discount_cents") or 0, "Discount")
        job.customer_note = _multiline(row.get("customer_note"), "Customer note", 2000)
        job.is_sample = bool(row.get("is_sample"))
        job.completed_at = _dt(row.get("completed_at"))
        found[job.id] = job
    db.flush()
    return found


def _import_lines(db: Session, jobs: dict[str, Job], rows: list) -> None:
    known = {job.id for job in db.scalars(select(Job)).all()}
    known.update(jobs)
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("A line item is unreadable.")
        job_id = _optional_id(row.get("job_id"))
        if not job_id or job_id not in known:
            raise BackupError("A line item points at a job that is not in the shop.")
        kind = str(row.get("kind") or "labor")
        if kind not in LINE_KINDS:
            raise BackupError("A line item has a kind Daybook does not use.")
        line_id = _optional_id(row.get("id"))
        line = db.get(JobLine, line_id) if line_id else None
        if line is None:
            line = JobLine(
                id=line_id or new_id(),
                job_id=job_id,
                description="Import",
                quantity=Decimal("1"),
                unit_price_cents=0,
            )
            db.add(line)
        quantity = _quantity(row.get("quantity"))
        unit = _cents_like(row.get("unit_price_cents"), "Unit price")
        line_amount_cents(quantity, unit)
        line.job_id = job_id
        line.kind = kind
        line.description = _text(row.get("description"), "Line description", 200, required=True)
        line.quantity = quantity
        line.unit_price_cents = unit
        line.sort_order = _cents_like(row.get("sort_order") or 0, "Sort", maximum=100000)
    db.flush()


def _import_payments(db: Session, jobs: dict[str, Job], rows: list) -> None:
    known = {job.id for job in db.scalars(select(Job)).all()}
    known.update(jobs)
    user_ids = set(db.scalars(select(User.id)).all())
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("A payment row is unreadable.")
        job_id = _optional_id(row.get("job_id"))
        if not job_id or job_id not in known:
            raise BackupError("A payment points at a job that is not in the shop.")
        method = str(row.get("method") or "")
        status = str(row.get("status") or "")
        if method not in METHOD_LABELS:
            raise BackupError("A payment has a method Daybook does not use.")
        if status not in PAYMENT_STATUSES:
            raise BackupError("A payment has a status Daybook does not use.")
        payment_id = _optional_id(row.get("id"))
        payment = db.get(Payment, payment_id) if payment_id else None
        if payment is None:
            payment = Payment(
                id=payment_id or new_id(),
                job_id=job_id,
                amount_cents=0,
                method=method,
                status=status,
            )
            db.add(payment)
        recorder = _optional_id(row.get("recorded_by_id"))
        if recorder and recorder not in user_ids:
            recorder = None
        session_id = _optional_text(row.get("stripe_checkout_session_id"), 255)
        payment.job_id = job_id
        payment.amount_cents = _cents_like(row.get("amount_cents"), "Payment amount")
        payment.method = method
        payment.status = status
        payment.note = _text(row.get("note"), "Payment note", 500)
        payment.stripe_checkout_session_id = session_id
        payment.stripe_payment_intent_id = _optional_text(row.get("stripe_payment_intent_id"), 255)
        payment.recorded_by_id = recorder
        payment.received_at = _dt(row.get("received_at"))
        payment.refunded_at = _dt(row.get("refunded_at"))
    db.flush()


def _bump_sequences(shop: Shop, jobs: list[Job]) -> None:
    job_numbers = [_tail(job.number) for job in jobs]
    invoice_numbers = [_tail(job.invoice_number or "") for job in jobs]
    job_numbers = [number for number in job_numbers if number]
    invoice_numbers = [number for number in invoice_numbers if number]
    if job_numbers:
        shop.next_job_number = max(shop.next_job_number, max(job_numbers) + 1)
    if invoice_numbers:
        shop.next_invoice_number = max(shop.next_invoice_number, max(invoice_numbers) + 1)


def _tail(value: str) -> int | None:
    match = _NUMBER_TAIL.search(value or "")
    if not match:
        return None
    return int(match.group(1))


def _shop_out(shop: Shop) -> dict:
    return {
        "business_name": shop.business_name,
        "phone": shop.phone,
        "email": shop.email,
        "address": shop.address,
        "timezone": shop.timezone,
        "tax_rate_bps": shop.tax_rate_bps,
        "tax_labor": shop.tax_labor,
        "job_prefix": shop.job_prefix,
        "invoice_prefix": shop.invoice_prefix,
    }


def _customer_out(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "address_line": customer.address_line,
        "city": customer.city,
        "region": customer.region,
        "postal_code": customer.postal_code,
        "notes": customer.notes,
        "is_sample": customer.is_sample,
        "created_at": _iso(customer.created_at),
    }


def _job_out(job: Job) -> dict:
    return {
        "id": job.id,
        "number": job.number,
        "invoice_number": job.invoice_number,
        "customer_id": job.customer_id,
        "title": job.title,
        "category": job.category,
        "description": job.description,
        "service_address": job.service_address,
        "status": job.status,
        "priority": job.priority,
        "scheduled_start": _iso(job.scheduled_start),
        "scheduled_end": _iso(job.scheduled_end),
        "assigned_user_id": job.assigned_user_id,
        "tax_rate_bps": job.tax_rate_bps,
        "tax_labor": job.tax_labor,
        "discount_cents": job.discount_cents,
        "customer_note": job.customer_note,
        "is_sample": job.is_sample,
        "completed_at": _iso(job.completed_at),
        "created_at": _iso(job.created_at),
    }


def _line_out(line: JobLine) -> dict:
    return {
        "id": line.id,
        "job_id": line.job_id,
        "kind": line.kind,
        "description": line.description,
        "quantity": format(Decimal(line.quantity), "f"),
        "unit_price_cents": line.unit_price_cents,
        "sort_order": line.sort_order,
    }


def _payment_out(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "job_id": payment.job_id,
        "amount_cents": payment.amount_cents,
        "method": payment.method,
        "status": payment.status,
        "note": payment.note,
        "stripe_checkout_session_id": payment.stripe_checkout_session_id,
        "stripe_payment_intent_id": payment.stripe_payment_intent_id,
        "recorded_by_id": payment.recorded_by_id,
        "received_at": _iso(payment.received_at),
        "refunded_at": _iso(payment.refunded_at),
        "created_at": _iso(payment.created_at),
    }


def _user_out(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role if user.role in ROLES else "technician",
        "active": user.active,
        "is_sample": user.is_sample,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _text(value, label: str, limit: int, required: bool = False) -> str:
    if value is None:
        text = ""
    else:
        text = " ".join(str(value).replace("\x00", "").split())
    if required and not text:
        raise BackupError(f"{label} is required.")
    if len(text) > limit:
        raise BackupError(f"{label} is longer than {limit} characters.")
    return text


def _optional_text(value, limit: int) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = _text(value, "Field", limit)
    return text or None


def _multiline(value, label: str, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        raise BackupError(f"{label} is longer than {limit} characters.")
    return text


def _email(value, required: bool) -> str | None:
    text = "" if value is None else str(value).strip().lower()
    if not text:
        if required:
            raise BackupError("Email is required.")
        return ""
    if len(text) > 254 or "@" not in text or text.startswith("@") or text.endswith("@"):
        raise BackupError("An email address in the backup is not valid.")
    return text


def _optional_id(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not _ID.match(text):
        raise BackupError("A record id in the backup is not valid.")
    return text


def _cents_like(value, label: str, maximum: int = MAX_MONEY_CENTS) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(f"{label} must be a whole number.")
    if value < 0 or value > maximum:
        raise BackupError(f"{label} is out of range.")
    return value


def _quantity(value) -> Decimal:
    from app.forms import parse_quantity

    if isinstance(value, bool) or value is None:
        raise BackupError("A line quantity is missing.")
    if isinstance(value, float):
        text = f"{value:.2f}"
    else:
        text = str(value).strip()
    try:
        return parse_quantity(text)
    except ValueError as exc:
        raise BackupError("A line quantity is not valid.") from exc


def _dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BackupError("A date in the backup is not valid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("A date in the backup is not valid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prefix(value, fallback: str) -> str:
    text = "" if value is None else str(value).strip()
    if not _PREFIX.match(text):
        return fallback
    return text
