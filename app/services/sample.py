"""Remove the fictional shop that ships with a first look."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Job, JobEvent, JobNote, Payment, User


def sample_customer_count(db: Session) -> int:
    return len(db.scalars(select(Customer.id).where(Customer.is_sample.is_(True))).all())


def remove_sample_data(db: Session) -> int:
    users = db.scalars(select(User).where(User.is_sample.is_(True), User.role != "admin")).all()
    user_ids = [user.id for user in users]
    if user_ids:
        for job in db.scalars(select(Job).where(Job.assigned_user_id.in_(user_ids))).all():
            job.assigned_user_id = None
        for event in db.scalars(select(JobEvent).where(JobEvent.actor_id.in_(user_ids))).all():
            event.actor_id = None
        for note in db.scalars(select(JobNote).where(JobNote.author_id.in_(user_ids))).all():
            note.author_id = None
        for payment in db.scalars(select(Payment).where(Payment.recorded_by_id.in_(user_ids))).all():
            payment.recorded_by_id = None
    customers = db.scalars(select(Customer).where(Customer.is_sample.is_(True))).all()
    count = len(customers)
    for customer in customers:
        db.delete(customer)
    db.flush()
    for user in users:
        db.delete(user)
    return count
