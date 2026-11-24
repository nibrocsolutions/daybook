"""HTML rendering and chrome."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.constants import (
    KIND_LABELS,
    LINE_KINDS,
    METHOD_LABELS,
    PRIORITIES,
    PRIORITY_LABELS,
    ROLE_LABELS,
    ROLES,
    STATUS_LABELS,
    STATUSES,
)
from app.formatting import (
    format_clock,
    format_day,
    format_money,
    format_quantity,
    format_when,
    mailto_href,
    maps_url,
    tax_label,
    tel_href,
)
from app.models import Shop
from app.security import ensure_csrf
from app.services.jobs import balance_cents, paid_cents
from app.services.totals import line_amount_cents
from app.web.deps import current_user

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _when(value, tz_name: str = "America/New_York") -> str:
    return format_when(value, tz_name or "America/New_York")


def _day(value, tz_name: str = "America/New_York") -> str:
    return format_day(value, tz_name or "America/New_York")


def _clock(value, tz_name: str = "America/New_York") -> str:
    return format_clock(value, tz_name or "America/New_York")


def _line_amount(line) -> int:
    return line_amount_cents(line.quantity, line.unit_price_cents)


@pass_context
def _csrf_field(context) -> Markup:
    token = context.get("csrf_token") or ""
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(token)}">')


templates.env.filters["money"] = lambda value: format_money(0 if value is None else int(value))
templates.env.filters["when"] = _when
templates.env.filters["day"] = _day
templates.env.filters["clock"] = _clock
templates.env.filters["qty"] = format_quantity
templates.env.globals.update(
    {
        "STATUS_LABELS": STATUS_LABELS,
        "STATUSES": STATUSES,
        "PRIORITIES": PRIORITIES,
        "PRIORITY_LABELS": PRIORITY_LABELS,
        "LINE_KINDS": LINE_KINDS,
        "KIND_LABELS": KIND_LABELS,
        "METHOD_LABELS": METHOD_LABELS,
        "ROLES": ROLES,
        "ROLE_LABELS": ROLE_LABELS,
        "MANUAL_METHODS": ("cash", "check", "other"),
        "paid_cents": paid_cents,
        "balance_cents": balance_cents,
        "line_amount": _line_amount,
        "tel_href": tel_href,
        "mailto_href": mailto_href,
        "maps_url": maps_url,
        "tax_caption": tax_label,
        "csrf": _csrf_field,
    }
)


def pop_flash(request: Request) -> list[dict]:
    items = list(request.session.get("flash") or [])
    if "flash" in request.session:
        del request.session["flash"]
    return items


def navigation(request: Request, user) -> tuple[list, list]:
    path = request.url.path

    def on(href: str, exact: bool = False) -> bool:
        if exact:
            return path == href
        return path == href or path.startswith(href + "/")

    main = [
        ("/", "Dashboard", on("/", exact=True)),
        ("/schedule", "Schedule", on("/schedule")),
        ("/jobs", "Jobs", on("/jobs")),
        ("/customers", "Customers", on("/customers")),
        ("/payments", "Payments", on("/payments")),
    ]
    admin = []
    if user is not None and user.role == "admin":
        admin = [
            ("/admin/users", "Users", on("/admin/users")),
            ("/admin/shop", "Shop", on("/admin/shop")),
            ("/admin/backup", "Backup", on("/admin/backup")),
        ]
    return main, admin


def render(request: Request, db: Session, name: str, status_code: int = 200, **context):
    user = current_user(request, db)
    shop = db.get(Shop, 1)
    nav, admin_nav = navigation(request, user)
    payload = {
        "request": request,
        "csrf_token": ensure_csrf(request),
        "user": user,
        "shop": shop,
        "business_name": shop.business_name if shop else "Fieldbook",
        "nav": nav,
        "admin_nav": admin_nav,
        "account_active": request.url.path.startswith("/account"),
        "flash": pop_flash(request),
    }
    payload.update(context)
    return templates.TemplateResponse(request, name, payload, status_code=status_code)
