"""Sign-in, sign-out, and the signed-in person's password."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.formatting import utcnow
from app.forms import parse_account_password, parse_email
from app.models import User
from app.security import (
    clear_login_failure,
    ensure_csrf,
    hash_password,
    login_blocked,
    password_matches,
    record_login_failure,
    safe_next,
)
from app.web.deps import current_user, get_db, go, posted, require_user
from app.web.rendering import render

router = APIRouter()


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if current_user(request, db) is not None:
        return go(safe_next(request.query_params.get("next")))
    ensure_csrf(request)
    return render(
        request,
        db,
        "login.html",
        email="",
        error="",
        next_url=safe_next(request.query_params.get("next")),
    )


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await posted(request)
    email, email_error = parse_email(str(form.get("email") or ""), required=True)
    password = str(form.get("password") or "")
    next_url = safe_next(str(form.get("next") or request.query_params.get("next") or ""))
    if email_error or login_blocked(email or str(form.get("email") or "")):
        if email and login_blocked(email):
            error = "Too many sign-in attempts. Wait a few minutes and try again."
        elif email_error and not email:
            error = "Enter the email and password on the account."
        else:
            record_login_failure(email or "unknown")
            error = "Those credentials do not match."
        return render(request, db, "login.html", status_code=400, email=email, error=error, next_url=next_url)
    user = db.scalars(select(User).where(User.email == email)).first()
    stored = user.password_hash if user is not None and user.active else None
    if not password_matches(password, stored) or user is None:
        record_login_failure(email)
        return render(
            request,
            db,
            "login.html",
            status_code=400,
            email=email,
            error="Those credentials do not match.",
            next_url=next_url,
        )
    clear_login_failure(email)
    user.last_login_at = utcnow()
    request.session.clear()
    request.session["uid"] = user.id
    ensure_csrf(request)
    return go(next_url)


@router.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    await posted(request)
    request.session.clear()
    return go("/login")


@router.get("/account")
def account_form(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    return render(request, db, "account.html", error="")


@router.post("/account")
async def account_submit(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    form = await posted(request)
    new_password, error = parse_account_password(form)
    if error or new_password is None:
        return render(request, db, "account.html", status_code=400, error=error or "Check the password.")
    if not password_matches(str(form.get("current_password") or ""), user.password_hash):
        return render(request, db, "account.html", status_code=400, error="The current password does not match.")
    user.password_hash = hash_password(new_password)
    user.updated_at = utcnow()
    from app.web.deps import flash

    flash(request, "Password updated.")
    return go("/account")
