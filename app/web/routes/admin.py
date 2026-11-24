"""Users, shop profile, and backup."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.constants import BACKUP_VERSION, COMMON_TIMEZONES, MAX_BACKUP_BYTES
from app.errors import BackupError
from app.formatting import format_percent, utcnow
from app.forms import parse_shop_form, parse_user_form
from app.models import Shop, User
from app.security import hash_password, weak_password
from app.services.backup import customers_csv, export_payload, import_payload, jobs_csv, payments_csv
from app.services.sample import remove_sample_data, sample_customer_count
from app.services.stripe_billing import stripe_state
from app.web.deps import flash, get_db, go, posted, require_admin
from app.web.rendering import render

router = APIRouter(prefix="/admin")


def _admins_besides(db: Session, user_id: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.active.is_(True), User.id != user_id)
        )
        or 0
    )


def _user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return user


@router.get("/users")
def user_list(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    users = db.scalars(select(User).order_by(User.name)).all()
    return render(request, db, "admin/users.html", users=users)


@router.get("/users/new")
def user_new(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return render(
        request,
        db,
        "admin/user_form.html",
        mode="new",
        subject=None,
        values={"name": "", "email": "", "role": "technician", "active": "1", "password": "", "confirm": ""},
        errors={},
    )


@router.post("/users")
async def user_create(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    form = await posted(request)
    parsed, errors, raw = parse_user_form(form, existing=False)
    raw["password"] = ""
    raw["confirm"] = ""
    if parsed is None:
        return render(request, db, "admin/user_form.html", status_code=400, mode="new", subject=None, values=raw, errors=errors)
    if db.scalars(select(User).where(User.email == parsed["email"])).first():
        errors = {"email": "Someone already uses that email."}
        return render(request, db, "admin/user_form.html", status_code=400, mode="new", subject=None, values=raw, errors=errors)
    if weak_password(parsed["password"]):
        errors = {"password": "Choose a password that is not a common default."}
        return render(request, db, "admin/user_form.html", status_code=400, mode="new", subject=None, values=raw, errors=errors)
    user = User(
        name=parsed["name"],
        email=parsed["email"],
        role=parsed["role"],
        active=parsed["active"],
        password_hash=hash_password(parsed["password"]),
    )
    db.add(user)
    flash(request, f"Added {user.name}. Share the password with them directly.")
    return go("/admin/users")


@router.get("/users/{user_id}")
def user_edit(request: Request, user_id: str, db: Session = Depends(get_db)):
    require_admin(request, db)
    subject = _user(db, user_id)
    values = {
        "name": subject.name,
        "email": subject.email,
        "role": subject.role,
        "active": "1" if subject.active else "",
        "password": "",
        "confirm": "",
    }
    return render(request, db, "admin/user_form.html", mode="edit", subject=subject, values=values, errors={})


@router.post("/users/{user_id}")
async def user_update(request: Request, user_id: str, db: Session = Depends(get_db)):
    actor = require_admin(request, db)
    subject = _user(db, user_id)
    form = await posted(request)
    parsed, errors, raw = parse_user_form(form, existing=True)
    raw["password"] = ""
    raw["confirm"] = ""
    if parsed is None:
        return render(
            request, db, "admin/user_form.html", status_code=400, mode="edit", subject=subject, values=raw, errors=errors
        )
    taken = db.scalars(select(User).where(User.email == parsed["email"], User.id != subject.id)).first()
    if taken:
        errors = {"email": "Someone already uses that email."}
        return render(
            request, db, "admin/user_form.html", status_code=400, mode="edit", subject=subject, values=raw, errors=errors
        )
    if actor.id == subject.id and not parsed["active"]:
        errors = {"active": "You cannot deactivate your own account."}
        return render(
            request, db, "admin/user_form.html", status_code=400, mode="edit", subject=subject, values=raw, errors=errors
        )
    if subject.role == "admin" and (parsed["role"] != "admin" or not parsed["active"]):
        if _admins_besides(db, subject.id) == 0:
            errors = {"role": "The shop needs at least one active admin."}
            return render(
                request,
                db,
                "admin/user_form.html",
                status_code=400,
                mode="edit",
                subject=subject,
                values=raw,
                errors=errors,
            )
    if parsed["password"] and weak_password(parsed["password"]):
        errors = {"password": "Choose a password that is not a common default."}
        return render(
            request, db, "admin/user_form.html", status_code=400, mode="edit", subject=subject, values=raw, errors=errors
        )
    subject.name = parsed["name"]
    subject.email = parsed["email"]
    subject.role = parsed["role"]
    subject.active = parsed["active"]
    subject.updated_at = utcnow()
    if parsed["password"]:
        subject.password_hash = hash_password(parsed["password"])
    if actor.id == subject.id and not subject.active:
        request.session.pop("uid", None)
    flash(request, "User updated.")
    return go("/admin/users")


@router.get("/shop")
def shop_form(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    shop = db.get(Shop, 1)
    values = _shop_values(shop)
    zones = list(COMMON_TIMEZONES)
    if shop and shop.timezone not in zones:
        zones.append(shop.timezone)
    return render(
        request,
        db,
        "admin/shop.html",
        values=values,
        errors={},
        timezones=zones,
        stripe=stripe_state(request.app.state.settings),
    )


@router.post("/shop")
async def shop_save(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    shop = db.get(Shop, 1)
    if shop is None:
        raise HTTPException(status_code=500)
    form = await posted(request)
    parsed, errors, raw = parse_shop_form(form)
    zones = list(COMMON_TIMEZONES)
    if parsed is None:
        if raw.get("timezone") and raw["timezone"] not in zones:
            zones.append(raw["timezone"])
        return render(
            request,
            db,
            "admin/shop.html",
            status_code=400,
            values=raw,
            errors=errors,
            timezones=zones,
            stripe=stripe_state(request.app.state.settings),
        )
    shop.business_name = parsed["business_name"]
    shop.phone = parsed["phone"]
    shop.email = parsed["email"]
    shop.address = parsed["address"]
    shop.timezone = parsed["timezone"]
    shop.tax_rate_bps = parsed["tax_rate_bps"]
    shop.tax_labor = parsed["tax_labor"]
    shop.job_prefix = parsed["job_prefix"]
    shop.invoice_prefix = parsed["invoice_prefix"]
    shop.updated_at = utcnow()
    flash(request, "Shop profile saved. New jobs pick up the tax settings.")
    return go("/admin/shop")


@router.get("/backup")
def backup_form(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return render(request, db, "admin/backup.html", sample_count=sample_customer_count(db))


@router.post("/export")
async def export_data(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    form = await posted(request)
    kind = str(form.get("format") or "json")
    stamp = datetime.now().date().isoformat()
    if kind == "jobs-csv":
        body = "\ufeff" + jobs_csv(db)
        return _download(body, f"daybook-jobs-{stamp}.csv", "text/csv; charset=utf-8")
    if kind == "customers-csv":
        body = "\ufeff" + customers_csv(db)
        return _download(body, f"daybook-customers-{stamp}.csv", "text/csv; charset=utf-8")
    if kind == "payments-csv":
        body = "\ufeff" + payments_csv(db)
        return _download(body, f"daybook-payments-{stamp}.csv", "text/csv; charset=utf-8")
    payload = export_payload(db)
    body = json.dumps(payload, indent=2)
    return _download(body, f"daybook-backup-{stamp}.json", "application/json")


@router.post("/import")
async def import_data(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    form = await posted(request, max_part_size=MAX_BACKUP_BYTES + 1024)
    upload = form.get("backup")
    filename = getattr(upload, "filename", "") or ""
    if not hasattr(upload, "read") or not filename.lower().endswith(".json"):
        flash(request, "Choose a Daybook JSON backup.", "error")
        return go("/admin/backup")
    raw = await upload.read(MAX_BACKUP_BYTES + 1)
    if len(raw) > MAX_BACKUP_BYTES:
        flash(request, "That file is larger than 5 MB.", "error")
        return go("/admin/backup")
    try:
        payload = json.loads(raw.decode("utf-8"))
        result = import_payload(db, payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        db.rollback()
        flash(request, "That file is not Daybook JSON. Nothing was changed.", "error")
        return go("/admin/backup")
    except BackupError as exc:
        db.rollback()
        flash(request, exc.message, "error")
        return go("/admin/backup")
    message = f"Imported {result['customers']} customers and {result['jobs']} jobs."
    if result["users_skipped"]:
        message += " User accounts in the file were left unchanged."
    flash(request, message)
    return go("/admin/backup")


@router.post("/sample/delete")
async def sample_delete(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    await posted(request)
    removed = remove_sample_data(db)
    flash(request, f"Removed {removed} sample customers and their jobs." if removed else "No sample customers were left.")
    return go("/admin/backup")


def _shop_values(shop: Shop | None) -> dict[str, str]:
    if shop is None:
        return {
            "business_name": "Daybook",
            "phone": "",
            "email": "",
            "address": "",
            "timezone": "America/New_York",
            "tax_rate": "0",
            "tax_labor": "1",
            "job_prefix": "JOB-",
            "invoice_prefix": "INV-",
        }
    return {
        "business_name": shop.business_name,
        "phone": shop.phone,
        "email": shop.email,
        "address": shop.address,
        "timezone": shop.timezone,
        "tax_rate": format_percent(shop.tax_rate_bps),
        "tax_labor": "1" if shop.tax_labor else "",
        "job_prefix": shop.job_prefix,
        "invoice_prefix": shop.invoice_prefix,
    }


def _download(body: str, filename: str, media_type: str) -> Response:
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
