"""Stripe Checkout and refunds.

Card numbers never touch Daybook. The browser is sent to Stripe, and Stripe
tells us the result through a signed webhook or a session lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import stripe

from app.config import Settings
from app.constants import MAX_CHECKOUT_CENTS, MIN_CHECKOUT_CENTS
from app.formatting import format_money, utcnow
from app.models import Payment
from app.services.jobs import (
    add_event,
    balance_cents,
    ensure_invoice_number,
    recent_pending_checkout,
    sync_payment_status,
    touch,
)


class StripeNotReady(Exception):
    def __init__(self, message: str):
        self.message = message


@dataclass
class StripeState:
    ready: bool
    problem: str | None
    test_mode: bool
    live_mode: bool


def stripe_problem(settings: Settings) -> str | None:
    key = settings.stripe_secret_key
    secret = settings.stripe_webhook_secret
    if not key and not secret:
        return (
            "Stripe is not configured yet. Add STRIPE_SECRET_KEY and "
            "STRIPE_WEBHOOK_SECRET to the server environment. Cash, check, and "
            "other payments still work."
        )
    if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        return "STRIPE_SECRET_KEY is missing or does not look like a Stripe secret key."
    if not secret.startswith("whsec_"):
        return "STRIPE_WEBHOOK_SECRET is missing or does not look like a Stripe webhook secret."
    if not settings.app_base_url.startswith(("http://", "https://")):
        return "APP_BASE_URL must include http:// or https://."
    return None


def stripe_state(settings: Settings) -> StripeState:
    key = settings.stripe_secret_key
    problem = stripe_problem(settings)
    return StripeState(
        ready=problem is None,
        problem=problem,
        test_mode=key.startswith(("sk_test_", "rk_test_")),
        live_mode=key.startswith(("sk_live_", "rk_live_")),
    )


def safe_checkout_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host != "checkout.stripe.com":
        raise StripeNotReady("Stripe returned an unexpected checkout address. Nothing was charged.")
    return url


def construct_event(payload: bytes, signature: str, secret: str):
    return stripe.Webhook.construct_event(payload, signature, secret)


def _field(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_mapping(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    try:
        return dict(obj)
    except Exception:
        return {}


def _intent_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:255]
    if isinstance(value, dict):
        found = value.get("id")
        return str(found)[:255] if found else None
    found = getattr(value, "id", None)
    return str(found)[:255] if found else None


def start_checkout(db, settings: Settings, shop, job, actor) -> str:
    problem = stripe_problem(settings)
    if problem:
        raise StripeNotReady(problem)
    due = balance_cents(job)
    if due <= 0:
        raise StripeNotReady("This job has nothing due.")
    if due < MIN_CHECKOUT_CENTS:
        raise StripeNotReady("Stripe cannot charge less than $0.50. Record this balance manually.")
    if due > MAX_CHECKOUT_CENTS:
        raise StripeNotReady(
            "Card checkout in Daybook is limited to $100,000. Record this payment in parts or use another method."
        )
    existing = recent_pending_checkout(job, due)
    if existing and existing.checkout_url:
        try:
            return safe_checkout_url(existing.checkout_url)
        except StripeNotReady:
            existing.status = "failed"

    ensure_invoice_number(db, shop, job)
    payment = Payment(
        job_id=job.id,
        amount_cents=due,
        method="stripe",
        status="pending",
        note="",
        recorded_by_id=actor.id,
    )
    db.add(payment)
    job.payments.append(payment)
    db.flush()
    product_name = f"{job.invoice_number or job.number} {job.title}".strip()[:120]
    base = settings.app_base_url.rstrip("/")
    from app.formatting import mailto_href

    customer_email = job.customer.email if job.customer and mailto_href(job.customer.email) else None
    checkout_args = {
        "api_key": settings.stripe_secret_key,
        "mode": "payment",
        "client_reference_id": job.id,
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": due,
                    "product_data": {"name": product_name},
                },
            }
        ],
        "metadata": {
            "job_id": job.id,
            "payment_id": payment.id,
            "job_number": job.number,
        },
        "success_url": f"{base}/payments/return?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/jobs/{job.id}?payment=cancelled",
        "idempotency_key": payment.id,
    }
    if customer_email:
        checkout_args["customer_email"] = customer_email
    try:
        session = stripe.checkout.Session.create(**checkout_args)
    except stripe.StripeError:
        payment.status = "failed"
        raise StripeNotReady("Stripe did not start the checkout. Nothing was charged.") from None
    session_id = str(_field(session, "id") or "")
    session_url = str(_field(session, "url") or "")
    try:
        safe_url = safe_checkout_url(session_url)
    except StripeNotReady:
        payment.status = "failed"
        raise
    payment.stripe_checkout_session_id = session_id[:255]
    payment.checkout_url = safe_url
    add_event(db, job, actor, f"Started a card checkout for {format_money(due)}.")
    touch(job)
    return safe_url


@dataclass
class ApplyResult:
    status: str
    job_id: str | None


def apply_checkout_session(db, session_obj) -> ApplyResult:
    session_id = str(_field(session_obj, "id") or "")
    metadata = _as_mapping(_field(session_obj, "metadata"))
    payment = None
    payment_id = metadata.get("payment_id")
    if payment_id:
        payment = db.get(Payment, str(payment_id))
    if payment is None and session_id:
        payment = db.scalars(
            select_payment_by_session(session_id)
        ).first()
    if payment is None:
        return ApplyResult("missing", None)
    job = payment.job
    if payment.stripe_checkout_session_id and session_id and payment.stripe_checkout_session_id != session_id:
        return ApplyResult("mismatch", job.id if job else None)
    payment_status = str(_field(session_obj, "payment_status") or "")
    if payment_status != "paid":
        return ApplyResult("unpaid", job.id if job else None)
    amount = _field(session_obj, "amount_total")
    if amount is not None and int(amount) != payment.amount_cents:
        notice = "Amount from Stripe did not match this charge."
        if payment.note != notice:
            payment.note = notice
            if job is not None:
                add_event(db, job, None, "Stripe reported a different amount than the ledger. The charge was left pending.")
        return ApplyResult("mismatch", job.id if job else None)
    if payment.status == "succeeded":
        return ApplyResult("exists", job.id if job else None)
    payment.status = "succeeded"
    payment.received_at = payment.received_at or utcnow()
    payment.stripe_payment_intent_id = _intent_id(_field(session_obj, "payment_intent"))
    if session_id and not payment.stripe_checkout_session_id:
        payment.stripe_checkout_session_id = session_id[:255]
    if job is not None:
        sync_payment_status(job)
        add_event(db, job, None, f"Stripe collected {format_money(payment.amount_cents)}.")
        touch(job)
    return ApplyResult("paid", job.id if job else None)


def select_payment_by_session(session_id: str):
    from sqlalchemy import select

    return select(Payment).where(Payment.stripe_checkout_session_id == session_id)


def mark_checkout_closed(db, session_obj, failed: bool) -> None:
    session_id = str(_field(session_obj, "id") or "")
    if not session_id:
        return
    payment = db.scalars(select_payment_by_session(session_id)).first()
    if payment is None or payment.status != "pending":
        return
    payment.status = "failed" if failed else "failed"
    if payment.job is not None:
        add_event(db, payment.job, None, "The card checkout was closed before payment.")


def retrieve_and_apply(db, settings: Settings, session_id: str) -> ApplyResult:
    if stripe_problem(settings):
        raise StripeNotReady("Stripe is not configured, so this return could not be confirmed.")
    if not session_id.startswith("cs_") or len(session_id) > 255 or not session_id.replace("_", "").isalnum():
        return ApplyResult("missing", None)
    try:
        session = stripe.checkout.Session.retrieve(session_id, api_key=settings.stripe_secret_key)
    except stripe.StripeError:
        raise StripeNotReady("Stripe could not confirm that checkout yet.") from None
    return apply_checkout_session(db, session)


def refund_payment(db, settings: Settings, job, payment: Payment, actor) -> None:
    if payment.status != "succeeded":
        raise StripeNotReady("Only collected payments can be refunded.")
    if payment.method == "stripe":
        if not payment.stripe_payment_intent_id:
            raise StripeNotReady("This card payment has no Stripe reference to refund.")
        if stripe_problem(settings):
            raise StripeNotReady(
                "Stripe is not configured, so this card payment cannot be refunded from Daybook."
            )
        try:
            stripe.Refund.create(
                api_key=settings.stripe_secret_key,
                payment_intent=payment.stripe_payment_intent_id,
                amount=payment.amount_cents,
                idempotency_key=f"refund-{payment.id}",
            )
        except stripe.StripeError as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            raise StripeNotReady(f"Stripe did not complete the refund ({code}).") from None
    from app.services.jobs import refund_local

    refund_local(db, job, payment, actor)
