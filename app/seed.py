"""Create the shop row, the first admin, and optional sample work."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import WEAK_PASSWORDS, Settings
from app.formatting import clean_timezone, utcnow
from app.models import Customer, Job, JobEvent, JobLine, JobNote, Payment, Shop, User
from app.security import hash_password
from app.services.jobs import allocate_number, ensure_invoice_number, recalc_job

log = logging.getLogger("fieldbook.seed")


def seed(app) -> None:
    settings: Settings = app.state.settings
    db = app.state.SessionLocal()
    try:
        shop = db.get(Shop, 1)
        if shop is None:
            shop = Shop(
                id=1,
                business_name="Fieldbook",
                phone="",
                email="",
                address="",
                timezone=clean_timezone(settings.default_timezone),
                currency="usd",
                tax_rate_bps=0,
                tax_labor=True,
                job_prefix="JOB-",
                invoice_prefix="INV-",
                next_job_number=1001,
                next_invoice_number=1001,
                demo_seeded=False,
            )
            db.add(shop)
            db.flush()
        user_count = db.scalar(select(func.count()).select_from(User)) or 0
        if user_count == 0:
            if not settings.admin_email or not settings.admin_password:
                raise RuntimeError("Set ADMIN_EMAIL and ADMIN_PASSWORD before the first launch.")
            if settings.env == "production" and (
                settings.admin_password in WEAK_PASSWORDS or len(settings.admin_password) < 10
            ):
                raise RuntimeError(
                    "Refusing to create the admin with a weak ADMIN_PASSWORD while ENV=production."
                )
            db.add(
                User(
                    name=settings.admin_name,
                    email=settings.admin_email,
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                    active=True,
                )
            )
            log.info("Created the admin account for %s", settings.admin_email)
        if settings.seed_demo and settings.env != "production" and not shop.demo_seeded:
            _load_demo(db, shop, settings)
            shop.demo_seeded = True
            log.info("Loaded sample customers and jobs")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _at(tz_name: str, day_offset: int, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    day = datetime.now(tz).date() + timedelta(days=day_offset)
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _load_demo(db: Session, shop: Shop, settings: Settings) -> None:
    if shop.business_name == "Fieldbook":
        shop.business_name = "Northline Handyman"
        shop.phone = "(845) 555-0199"
        shop.email = "office@northline.example"
        shop.address = "18 Partition Street\nKingston, NY 12401"
        shop.tax_rate_bps = 800
        shop.tax_labor = False
    tech = db.scalars(select(User).where(User.email == "sam@fieldbook.local")).first()
    if tech is None:
        password = settings.demo_tech_password
        if len(password) < 10:
            password = "sample-tech-password"
        tech = User(
            name="Sam Rivera",
            email="sam@fieldbook.local",
            password_hash=hash_password(password),
            role="technician",
            active=True,
            is_sample=True,
        )
        db.add(tech)
        db.flush()

    helen = _customer(db, "Helen Cho", "845-555-0148", "helen@example.com", "184 Orchard Street", "Kingston", "NY", "12401", "Dog in the side yard. Text on arrival.")
    marcus = _customer(db, "Marcus Hale", "845-555-0172", "marcus@example.com", "42 Cedar Lane", "Saugerties", "NY", "12477", "Gate code is on the work order.")
    priya = _customer(db, "Priya Shah", "845-555-0114", "priya@example.com", "9 Mill Road", "Rhinebeck", "NY", "12572", "")
    owen = _customer(db, "Owen Briggs", "518-555-0166", "owen@example.com", "220 Main Street, Apt 4", "Hudson", "NY", "12534", "Buzzer 4.")
    lila = _customer(db, "Lila Nguyen", "518-555-0130", "lila@example.com", "15 Willow Court", "Catskill", "NY", "12414", "")
    ruth = _customer(db, "Ruth Adler", "845-555-0188", "ruth@example.com", "77 West Chestnut Street", "Kingston", "NY", "12401", "")

    lila_job = _job(
        db, shop, lila, "Door sweep and threshold",
        "Front door is dragging. Replace the sweep and the oak threshold.",
        "scheduled", "normal", _at(shop.timezone, -6, 10), tech,
        customer_note="Please take the old sweep with you.",
        when=_at(shop.timezone, -6, 10),
    )
    _line(db, lila_job, "labor", "Hang and trim the door", "1.50", 9500, 1)
    _line(db, lila_job, "material", "Oak threshold", "1", 4200, 2)
    _line(db, lila_job, "material", "Door sweep", "1", 1800, 3)
    _finish(db, shop, lila_job, tech, paid=True, at=_at(shop.timezone, -6, 12))

    _job(
        db, shop, marcus, "Mount a television",
        "Customer postponed until the new year.",
        "cancelled", "low", None, None,
        customer_note="",
        when=_at(shop.timezone, -4, 9),
    )

    priya_job = _job(
        db, shop, priya, "Bath fan replacement",
        "Existing fan is loud. Replace with a quiet 110 CFM unit and a timer switch.",
        "invoiced", "normal", _at(shop.timezone, -2, 9), tech,
        customer_note="The fan is in the hall bath.",
        when=_at(shop.timezone, -2, 9),
    )
    _line(db, priya_job, "labor", "Replace fan and switch", "2.50", 9500, 1)
    _line(db, priya_job, "material", "Bath fan", "1", 12600, 2)
    recalc_job(db, priya_job)
    ensure_invoice_number(db, shop, priya_job)
    db.add(Payment(
        job_id=priya_job.id,
        amount_cents=10000,
        method="cash",
        status="succeeded",
        note="Deposit at the door",
        recorded_by_id=tech.id,
        received_at=_at(shop.timezone, -2, 11),
        created_at=_at(shop.timezone, -2, 11),
    ))
    priya_job.status = "invoiced"
    priya_job.completed_at = _at(shop.timezone, -2, 12)

    ruth_job = _job(
        db, shop, ruth, "Sticking window sash",
        "Kitchen window will not stay up. Free the sash and add a brass stop.",
        "scheduled", "normal", _at(shop.timezone, -1, 11), tech,
        customer_note="Call from the porch. The bell is out.",
        when=_at(shop.timezone, -1, 11),
    )
    _line(db, ruth_job, "labor", "Free the sash and fit a stop", "1.25", 9500, 1)
    _line(db, ruth_job, "material", "Brass sash stop", "1", 1400, 2)
    recalc_job(db, ruth_job)

    helen_job = _job(
        db, shop, helen, "Kitchen faucet and supply lines",
        "Replace the dripping kitchen faucet and both supply lines. Customer has the faucet on site.",
        "scheduled", "normal", _at(shop.timezone, 0, 9), tech,
        customer_note="Please text when you are on the way.",
        end=_at(shop.timezone, 0, 11),
        when=_at(shop.timezone, 0, 8),
    )
    _line(db, helen_job, "labor", "Swap faucet and supply lines", "2", 9500, 1)
    _line(db, helen_job, "material", "Braided supply lines", "2", 1800, 2)
    _line(db, helen_job, "material", "Plumber's putty and tape", "1", 890, 3)
    recalc_job(db, helen_job)
    db.add(JobNote(body="Shutoff is under the sink, left side.", job=helen_job, author_id=tech.id))

    marcus_job = _job(
        db, shop, marcus, "Deck stair railing",
        "Rebuild the stair railing with cedar posts and a graspable handrail. Stairs are already sound.",
        "in_progress", "urgent", _at(shop.timezone, 0, 13), tech,
        customer_note="Leave the offcuts stacked by the shed.",
        end=_at(shop.timezone, 0, 16, 30),
        when=_at(shop.timezone, -1, 15),
    )
    _line(db, marcus_job, "labor", "Rebuild stair railing", "6", 9500, 1)
    _line(db, marcus_job, "material", "Cedar posts, rail, and hardware", "1", 24000, 2)
    recalc_job(db, marcus_job)

    owen_job = _job(
        db, shop, owen, "Drywall patch after a leak",
        "Patch the hallway ceiling where a supply line dripped. Prime only; the customer will paint.",
        "inquiry", "normal", None, None,
        customer_note="",
        when=utcnow(),
    )
    _line(db, owen_job, "labor", "Cut, tape, and skim", "3", 8500, 1)
    recalc_job(db, owen_job)


def _customer(db, name, phone, email, line, city, region, postal, notes) -> Customer:
    customer = Customer(
        name=name,
        phone=phone,
        email=email,
        address_line=line,
        city=city,
        region=region,
        postal_code=postal,
        notes=notes,
        is_sample=True,
    )
    db.add(customer)
    db.flush()
    return customer


def _job(db, shop, customer, title, description, status, priority, start, assignee, customer_note, end=None, when=None) -> Job:
    job = Job(
        number=allocate_number(db, shop, "job"),
        customer_id=customer.id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        scheduled_start=start,
        scheduled_end=end,
        assigned_user_id=assignee.id if assignee else None,
        tax_rate_bps=shop.tax_rate_bps,
        tax_labor=shop.tax_labor,
        customer_note=customer_note,
        is_sample=True,
        created_at=when or utcnow(),
    )
    if status in {"completed", "invoiced", "paid"} and when is not None:
        job.completed_at = when
    db.add(job)
    db.flush()
    db.add(JobEvent(job_id=job.id, actor_id=assignee.id if assignee else None, message="Opened this job.", created_at=job.created_at))
    return job


def _line(db, job: Job, kind: str, description: str, quantity: str, unit_price: int, sort: int) -> None:
    db.add(
        JobLine(
            job_id=job.id,
            kind=kind,
            description=description,
            quantity=Decimal(quantity),
            unit_price_cents=unit_price,
            sort_order=sort,
        )
    )
    db.flush()


def _finish(db, shop, job: Job, actor: User, paid: bool, at: datetime) -> None:
    recalc_job(db, job)
    ensure_invoice_number(db, shop, job)
    if paid:
        db.add(
            Payment(
                job_id=job.id,
                amount_cents=job.total_cents,
                method="cash",
                status="succeeded",
                note="Paid on site",
                recorded_by_id=actor.id,
                received_at=at,
                created_at=at,
            )
        )
        job.status = "paid"
        job.completed_at = at

