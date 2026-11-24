"""Request dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import RedirectResponse

from app.errors import AuthRedirect, PermissionDenied
from app.models import Job, JobEvent, JobNote, User
from app.security import safe_next


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.SessionLocal()
    request.state.db = db
    try:
        yield db
    except Exception:
        if not getattr(request.state, "db_done", False):
            db.rollback()
            request.state.db_done = True
        raise
    else:
        if not getattr(request.state, "db_done", False):
            db.commit()
            request.state.db_done = True
    finally:
        db.close()


def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not isinstance(uid, str):
        return None
    user = db.get(User, uid)
    if user is None or not user.active:
        if "uid" in request.session:
            request.session.pop("uid", None)
        return None
    return user


def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if user is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise AuthRedirect(target)
    return user


def require_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if user.role != "admin":
        raise PermissionDenied("Only an admin can do that.")
    return user


def flash(request: Request, message: str, level: str = "ok") -> None:
    if level not in {"ok", "warn", "error"}:
        level = "ok"
    items = list(request.session.get("flash") or [])
    items.append({"level": level, "message": message[:500]})
    request.session["flash"] = items


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(safe_next(url) if url.startswith("/") else "/", status_code=303)


def go(url: str) -> RedirectResponse:
    if not url.startswith("/"):
        url = "/"
    return RedirectResponse(url, status_code=303)


async def posted(request: Request, *, max_part_size: int = 1024 * 1024):
    from app.security import assert_csrf

    form = await request.form(max_files=2, max_fields=40, max_part_size=max_part_size)
    assert_csrf(request, str(form.get("csrf_token") or ""))
    return form


def load_job(db: Session, job_id: str) -> Job:
    from fastapi import HTTPException

    job = db.scalars(
        select(Job)
        .where(Job.id == job_id)
        .options(
            selectinload(Job.customer),
            selectinload(Job.assignee),
            selectinload(Job.lines),
            selectinload(Job.payments),
            selectinload(Job.notes).selectinload(JobNote.author),
            selectinload(Job.events).selectinload(JobEvent.actor),
        )
    ).first()
    if job is None:
        raise HTTPException(status_code=404)
    return job


def assignee_choices(db: Session, current_id: str | None = None) -> list[User]:
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)).all())
    if current_id and all(user.id != current_id for user in users):
        extra = db.get(User, current_id)
        if extra is not None:
            users.append(extra)
    return users
