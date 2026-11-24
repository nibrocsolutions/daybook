"""Form parsers. Routes redisplay `raw` when `errors` is set."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.constants import LINE_KINDS, MAX_MONEY_CENTS, MANUAL_METHODS, PRIORITIES, ROLES, STATUSES
from app.formatting import clean_timezone, format_percent
from app.security import password_problem

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+\Z")
_MONEY = re.compile(r"\d+(\.\d{1,2})?\Z")
_QTY = re.compile(r"\d+(\.\d{1,2})?\Z")
_PERCENT = re.compile(r"\d+(\.\d{1,2})?\Z")
_PREFIX = re.compile(r"^[A-Za-z0-9-]{1,12}\Z")
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\Z")


@dataclass
class JobInput:
    customer_id: str
    title: str
    description: str
    service_address: str
    priority: str
    status: str
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    assigned_user_id: str | None
    tax_rate_bps: int
    tax_labor: bool
    discount_cents: int
    customer_note: str
    line_description: str
    line_kind: str
    line_quantity: Decimal | None
    line_unit_price_cents: int | None


@dataclass
class CustomerInput:
    name: str
    phone: str
    email: str
    address_line: str
    city: str
    region: str
    postal_code: str
    notes: str
    is_sample: bool


@dataclass
class LineInput:
    description: str
    kind: str
    quantity: Decimal
    unit_price_cents: int


def clean_text(value: str, limit: int) -> str:
    return " ".join((value or "").replace("\x00", "").split())[:limit]


def clean_multiline(value: str, limit: int) -> str:
    text = (value or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()[:limit]


def parse_email(value: str, required: bool) -> tuple[str, str | None]:
    email = (value or "").strip().lower()
    if not email:
        if required:
            return "", "Email is required."
        return "", None
    if len(email) > 254 or not _EMAIL.match(email):
        return "", "Enter a valid email address."
    return email, None


def parse_cents(value: str) -> int:
    text = (value or "").strip().replace("$", "").replace(",", "").strip()
    if not text or not _MONEY.match(text):
        raise ValueError("Enter a dollar amount like 95 or 95.50.")
    if "." in text:
        dollars, fraction = text.split(".")
        fraction = fraction.ljust(2, "0")
        cents = int(dollars) * 100 + int(fraction)
    else:
        cents = int(text) * 100
    if cents < 0 or cents > MAX_MONEY_CENTS:
        raise ValueError("That amount is out of range.")
    return cents


def parse_quantity(value: str) -> Decimal:
    text = (value or "").strip()
    if not text or not _QTY.match(text):
        raise ValueError("Enter a quantity like 1 or 1.5.")
    quantity = Decimal(text)
    if quantity <= 0 or quantity > Decimal("10000"):
        raise ValueError("Quantity must be greater than 0 and at most 10000.")
    return quantity


def parse_percent_to_bps(value: str) -> int:
    text = (value or "").strip().replace("%", "")
    if text == "":
        return 0
    if not _PERCENT.match(text):
        raise ValueError("Enter a tax rate like 8 or 8.25.")
    bps = int((Decimal(text) * Decimal(100)).to_integral_value())
    if bps < 0 or bps > 2500:
        raise ValueError("Tax rate must be between 0 and 25.")
    return bps


def _local_dt(value: str, tz) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if not _WHEN.match(text):
        raise ValueError("Enter a valid date and time.")
    parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M")
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc)


def raw_form(form, names: list[str]) -> dict[str, str]:
    return {name: str(form.get(name) or "") for name in names}


JOB_FIELDS = [
    "customer_id",
    "title",
    "description",
    "service_address",
    "priority",
    "status",
    "scheduled_start",
    "scheduled_end",
    "assigned_user_id",
    "tax_rate",
    "discount",
    "customer_note",
    "line_description",
    "line_kind",
    "line_quantity",
    "line_unit_price",
]


def parse_job_form(form, tz, customer_ids: set[str], user_ids: set[str], *, with_line: bool) -> tuple[JobInput | None, dict[str, str], dict[str, str]]:
    raw = raw_form(form, JOB_FIELDS)
    raw["tax_labor"] = "1" if str(form.get("tax_labor") or "") in {"1", "on", "true", "yes"} else ""
    errors: dict[str, str] = {}
    customer_id = raw["customer_id"].strip()
    if customer_id not in customer_ids:
        errors["customer_id"] = "Choose a customer."
    title = clean_text(raw["title"], 140)
    if not title:
        errors["title"] = "Give the job a short title."
    elif len(raw["title"].strip()) > 140:
        errors["title"] = "Keep the title under 140 characters."
    priority = raw["priority"] if raw["priority"] in PRIORITIES else ""
    if not priority:
        errors["priority"] = "Choose a priority."
    status = raw["status"] if raw["status"] in STATUSES else ""
    if not status:
        errors["status"] = "Choose a status."
    try:
        start = _local_dt(raw["scheduled_start"], tz)
    except ValueError as exc:
        start = None
        errors["scheduled_start"] = str(exc)
    try:
        end = _local_dt(raw["scheduled_end"], tz)
    except ValueError as exc:
        end = None
        errors["scheduled_end"] = str(exc)
    if start and end and end <= start and "scheduled_end" not in errors:
        errors["scheduled_end"] = "The end time has to be after the start."
    if end and not start and "scheduled_start" not in errors:
        errors["scheduled_start"] = "Add a start time, or clear the end time."
    assignee = raw["assigned_user_id"].strip()
    if assignee and assignee not in user_ids:
        errors["assigned_user_id"] = "Choose a valid person."
    try:
        tax_rate = parse_percent_to_bps(raw["tax_rate"])
    except ValueError as exc:
        tax_rate = 0
        errors["tax_rate"] = str(exc)
    discount = 0
    if raw["discount"].strip():
        try:
            discount = parse_cents(raw["discount"])
        except ValueError as exc:
            errors["discount"] = str(exc)
    description = clean_multiline(raw["description"], 8000)
    if len((raw["description"] or "").strip()) > 8000:
        errors["description"] = "Description is too long."
    service_address = clean_multiline(raw["service_address"], 500)
    if len((raw["service_address"] or "").strip()) > 500:
        errors["service_address"] = "Address is too long."
    customer_note = clean_multiline(raw["customer_note"], 2000)
    if len((raw["customer_note"] or "").strip()) > 2000:
        errors["customer_note"] = "Customer note is too long."
    line_description = clean_text(raw["line_description"], 200)
    line_kind = raw["line_kind"] if raw["line_kind"] in LINE_KINDS else "labor"
    line_quantity = None
    line_price = None
    if with_line and any(raw[name].strip() for name in ("line_description", "line_quantity", "line_unit_price")):
        if not line_description:
            errors["line_description"] = "Describe the labor or material."
        if raw["line_kind"] and raw["line_kind"] not in LINE_KINDS:
            errors["line_kind"] = "Choose labor, material, or other."
        try:
            line_quantity = parse_quantity(raw["line_quantity"])
        except ValueError as exc:
            errors["line_quantity"] = str(exc)
        try:
            line_price = parse_cents(raw["line_unit_price"])
        except ValueError as exc:
            errors["line_unit_price"] = str(exc)
    if errors:
        return None, errors, raw
    return (
        JobInput(
            customer_id=customer_id,
            title=title,
            description=description,
            service_address=service_address,
            priority=priority,
            status=status,
            scheduled_start=start,
            scheduled_end=end,
            assigned_user_id=assignee or None,
            tax_rate_bps=tax_rate,
            tax_labor=raw["tax_labor"] == "1",
            discount_cents=discount,
            customer_note=customer_note,
            line_description=line_description,
            line_kind=line_kind,
            line_quantity=line_quantity,
            line_unit_price_cents=line_price,
        ),
        {},
        raw,
    )


def parse_line_form(form) -> tuple[LineInput | None, dict[str, str]]:
    errors: dict[str, str] = {}
    description = clean_text(str(form.get("description") or ""), 200)
    kind = str(form.get("kind") or "labor")
    if not description:
        errors["description"] = "Describe the labor or material."
    if kind not in LINE_KINDS:
        errors["kind"] = "Choose labor, material, or other."
        kind = "labor"
    try:
        quantity = parse_quantity(str(form.get("quantity") or ""))
    except ValueError as exc:
        quantity = Decimal("1")
        errors["quantity"] = str(exc)
    try:
        price = parse_cents(str(form.get("unit_price") or ""))
    except ValueError as exc:
        price = 0
        errors["unit_price"] = str(exc)
    if errors:
        return None, errors
    return LineInput(description, kind, quantity, price), {}


CUSTOMER_FIELDS = ["name", "phone", "email", "address_line", "city", "region", "postal_code", "notes"]


def parse_customer_form(form, *, allow_sample: bool) -> tuple[CustomerInput | None, dict[str, str], dict[str, str]]:
    raw = raw_form(form, CUSTOMER_FIELDS)
    raw["is_sample"] = "1" if allow_sample and str(form.get("is_sample") or "") in {"1", "on", "true"} else ""
    errors: dict[str, str] = {}
    name = clean_text(raw["name"], 120)
    if not name:
        errors["name"] = "Customer name is required."
    elif len(raw["name"].strip()) > 120:
        errors["name"] = "Keep the name under 120 characters."
    phone = clean_text(raw["phone"], 40)
    if len(raw["phone"].strip()) > 40:
        errors["phone"] = "Phone is too long."
    email, email_error = parse_email(raw["email"], required=False)
    if email_error:
        errors["email"] = email_error
    address_line = clean_text(raw["address_line"], 160)
    city = clean_text(raw["city"], 80)
    region = clean_text(raw["region"], 40)
    postal = clean_text(raw["postal_code"], 20)
    for field, limit, label in (
        ("address_line", 160, "Street address"),
        ("city", 80, "City"),
        ("region", 40, "State / region"),
        ("postal_code", 20, "Postal code"),
    ):
        if len(raw[field].strip()) > limit:
            errors[field] = f"{label} is too long."
    notes = clean_multiline(raw["notes"], 4000)
    if len((raw["notes"] or "").strip()) > 4000:
        errors["notes"] = "Notes are too long."
    if errors:
        return None, errors, raw
    return (
        CustomerInput(
            name=name,
            phone=phone,
            email=email,
            address_line=address_line,
            city=city,
            region=region,
            postal_code=postal,
            notes=notes,
            is_sample=raw["is_sample"] == "1",
        ),
        {},
        raw,
    )


def parse_user_form(form, *, existing: bool) -> tuple[dict | None, dict[str, str], dict[str, str]]:
    fields = ["name", "email", "role", "password", "confirm"]
    raw = raw_form(form, fields)
    raw["active"] = "1" if str(form.get("active") or "") in {"1", "on", "true"} else ""
    errors: dict[str, str] = {}
    name = clean_text(raw["name"], 120)
    if not name:
        errors["name"] = "Name is required."
    email, email_error = parse_email(raw["email"], required=True)
    if email_error:
        errors["email"] = email_error
    role = raw["role"] if raw["role"] in ROLES else ""
    if not role:
        errors["role"] = "Choose a role."
    password = raw["password"]
    confirm = raw["confirm"]
    if not existing or password or confirm:
        problem = password_problem(password)
        if problem:
            errors["password"] = problem
        elif password != confirm:
            errors["confirm"] = "Those passwords do not match."
    if errors:
        return None, errors, raw
    return (
        {
            "name": name,
            "email": email,
            "role": role,
            "password": password,
            "active": raw["active"] == "1",
        },
        {},
        raw,
    )


def parse_account_password(form) -> tuple[str | None, str | None]:
    current = str(form.get("current_password") or "")
    new = str(form.get("new_password") or "")
    confirm = str(form.get("confirm") or "")
    problem = password_problem(new)
    if problem:
        return None, problem
    if new != confirm:
        return None, "Those passwords do not match."
    if not current:
        return None, "Enter the current password."
    return new, None


def parse_shop_form(form) -> tuple[dict | None, dict[str, str], dict[str, str]]:
    fields = ["business_name", "phone", "email", "address", "timezone", "tax_rate", "job_prefix", "invoice_prefix"]
    raw = raw_form(form, fields)
    raw["tax_labor"] = "1" if str(form.get("tax_labor") or "") in {"1", "on", "true"} else ""
    errors: dict[str, str] = {}
    name = clean_text(raw["business_name"], 120)
    if not name:
        errors["business_name"] = "The shop needs a name."
    phone = clean_text(raw["phone"], 40)
    email, email_error = parse_email(raw["email"], required=False)
    if email_error:
        errors["email"] = email_error
    address = clean_multiline(raw["address"], 500)
    if not raw["timezone"].strip():
        timezone_name = "America/New_York"
    else:
        timezone_name = clean_timezone(raw["timezone"], fallback="")
        if timezone_name != raw["timezone"].strip():
            errors["timezone"] = "Choose a timezone from the list."
            timezone_name = "America/New_York"
    try:
        tax_rate = parse_percent_to_bps(raw["tax_rate"])
    except ValueError as exc:
        tax_rate = 0
        errors["tax_rate"] = str(exc)
    job_prefix = raw["job_prefix"].strip() or "JOB-"
    invoice_prefix = raw["invoice_prefix"].strip() or "INV-"
    if not _PREFIX.match(job_prefix):
        errors["job_prefix"] = "Use letters, numbers, or hyphens, up to 12 characters."
    if not _PREFIX.match(invoice_prefix):
        errors["invoice_prefix"] = "Use letters, numbers, or hyphens, up to 12 characters."
    if errors:
        return None, errors, raw
    return (
        {
            "business_name": name,
            "phone": phone,
            "email": email,
            "address": address,
            "timezone": timezone_name,
            "tax_rate_bps": tax_rate,
            "tax_labor": raw["tax_labor"] == "1",
            "job_prefix": job_prefix,
            "invoice_prefix": invoice_prefix,
        },
        {},
        raw,
    )


def job_form_defaults(shop, customer_id: str = "") -> dict[str, str]:
    return {
        "customer_id": customer_id,
        "title": "",
        "description": "",
        "service_address": "",
        "priority": "normal",
        "status": "inquiry",
        "scheduled_start": "",
        "scheduled_end": "",
        "assigned_user_id": "",
        "tax_rate": format_percent(shop.tax_rate_bps),
        "tax_labor": "1" if shop.tax_labor else "",
        "discount": "",
        "customer_note": "",
        "line_description": "",
        "line_kind": "labor",
        "line_quantity": "",
        "line_unit_price": "",
    }
