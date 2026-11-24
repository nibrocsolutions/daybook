"""Stripe webhooks. The signature is the authentication."""

from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.services.stripe_billing import apply_checkout_session, construct_event, mark_checkout_closed
from app.web.deps import get_db

router = APIRouter()
log = logging.getLogger("fieldbook.stripe")


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    payload = await request.body()
    if len(payload) > 1_000_000:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    if not settings.stripe_webhook_secret:
        return JSONResponse({"error": "not configured"}, status_code=503)
    signature = request.headers.get("stripe-signature", "")
    try:
        event = construct_event(payload, signature, settings.stripe_webhook_secret)
    except stripe.SignatureVerificationError:
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except ValueError:
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    event_type = event["type"]
    data_object = event["data"]["object"]
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        apply_checkout_session(db, data_object)
    elif event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
        mark_checkout_closed(db, data_object, failed=True)
    else:
        log.info("ignored stripe event %s", event_type)
    return JSONResponse({"received": True})
