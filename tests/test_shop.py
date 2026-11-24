"""Sign-in, jobs, payments, and the admin desk."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import stripe
from fastapi.testclient import TestClient

from app.models import Payment, User
from app.services.stripe_billing import stripe as stripe_sdk
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, csrf, login, submit


def test_health_and_security_headers(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in response.headers["content-security-policy"]


def test_login_failure_does_not_reveal_the_account(client):
    missing = login(client, "nobody@daybook.test", "not-the-password")
    assert missing.status_code == 400
    assert "do not match" in missing.text
    assert "inactive" not in missing.text.lower()
    wrong = login(client, ADMIN_EMAIL, "not-the-password")
    assert wrong.status_code == 400
    assert "do not match" in wrong.text


def test_open_redirect_is_dropped(client):
    response = login(client, next_url="https://evil.example/phish")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_missing_csrf_is_rejected(admin):
    response = admin.post("/logout", data={}, follow_redirects=False)
    assert response.status_code == 403


def test_job_ledger_payment_and_invoice(admin):
    created = submit(
        admin,
        "/customers",
        {
            "name": "Helen Cho",
            "phone": "845-555-0148",
            "email": "helen@example.com",
            "address_line": "184 Orchard Street",
            "city": "Kingston",
            "region": "NY",
            "postal_code": "12401",
            "notes": "Dog in the yard",
        },
    )
    assert created.status_code == 303
    customer_id = created.headers["location"].rsplit("/", 1)[-1]
    job = submit(
        admin,
        "/jobs",
        {
            "customer_id": customer_id,
            "title": "Kitchen faucet",
            "category": "house",
            "description": "Replace the faucet.",
            "service_address": "",
            "priority": "normal",
            "status": "completed",
            "scheduled_start": "",
            "scheduled_end": "",
            "assigned_user_id": "",
            "tax_rate": "10",
            "discount": "",
            "customer_note": "Text on the way.",
            "line_description": "Labor",
            "line_kind": "labor",
            "line_quantity": "1",
            "line_unit_price": "100.00",
        },
    )
    assert job.status_code == 303
    job_url = job.headers["location"]
    submit(
        admin,
        job_url + "/lines",
        {"description": "Faucet", "kind": "material", "quantity": "1", "unit_price": "50.00"},
    )
    page = admin.get(job_url)
    assert page.status_code == 200
    assert "$155.00" in page.text
    assert "House" in page.text
    assert "Kitchen faucet" in admin.get("/jobs?category=house").text
    assert "Kitchen faucet" not in admin.get("/jobs?category=software").text
    tokens = re.findall(r'name="csrf_token" value="([^"]*)"', page.text)
    assert len(tokens) > 3
    assert all(len(token) >= 20 for token in tokens)
    assert len(set(tokens)) == 1
    submit(admin, job_url + "/notes", {"body": "INTERNAL-SHUTOFF-LEFT"})
    invoice = admin.get(job_url + "/invoice")
    assert "Text on the way." in invoice.text
    assert "INTERNAL-SHUTOFF-LEFT" not in invoice.text
    assert "Replace the faucet." in invoice.text
    partial = submit(
        admin,
        job_url + "/payments",
        {"amount": "40.00", "method": "cash", "note": "Deposit"},
    )
    assert partial.status_code == 303
    assert "$115.00" in admin.get(job_url).text
    rest = submit(admin, job_url + "/payments", {"amount": "115.00", "method": "check", "note": ""})
    assert rest.status_code == 303
    paid = admin.get(job_url)
    assert "Paid" in paid.text
    assert "$0.00" in paid.text


def test_technician_cannot_administer_or_refund(admin, app):
    submit(
        admin,
        "/admin/users",
        {
            "name": "Sam Rivera",
            "email": "sam@daybook.test",
            "role": "technician",
            "password": "field-tech-pass",
            "confirm": "field-tech-pass",
            "active": "1",
        },
    )
    with TestClient(app) as tech:
        assert login(tech, "sam@daybook.test", "field-tech-pass").status_code == 303
        assert tech.get("/admin/users").status_code == 403
        assert tech.get("/").status_code == 200
        denied = submit(tech, "/admin/export", {"format": "json"})
        assert denied.status_code == 403

    customer = submit(admin, "/customers", {"name": "Owen Briggs", "phone": "", "email": "", "address_line": "", "city": "", "region": "", "postal_code": "", "notes": ""})
    customer_id = customer.headers["location"].rsplit("/", 1)[-1]
    job = submit(
        admin,
        "/jobs",
        {
            "customer_id": customer_id,
            "title": "Patch a wall",
            "description": "",
            "service_address": "",
            "priority": "normal",
            "status": "completed",
            "scheduled_start": "",
            "scheduled_end": "",
            "assigned_user_id": "",
            "tax_rate": "0",
            "discount": "",
            "customer_note": "",
            "line_description": "Skim coat",
            "line_kind": "labor",
            "line_quantity": "1",
            "line_unit_price": "85.00",
        },
    )
    job_url = job.headers["location"]
    submit(admin, job_url + "/payments", {"amount": "85.00", "method": "cash", "note": ""})
    page = admin.get(job_url)
    assert "Refund" in page.text
    with TestClient(app) as tech:
        login(tech, "sam@daybook.test", "field-tech-pass")
        tech_page = tech.get(job_url)
        assert "Refund" not in tech_page.text
        payment_id = app.state.SessionLocal().query(Payment).one().id
        refused = submit(tech, f"{job_url}/payments/{payment_id}/refund", {})
        assert refused.status_code == 403
        assert submit(tech, job_url + "/delete", {}).status_code == 403
    refunded = submit(admin, f"{job_url}/payments/{payment_id}/refund", {})
    assert refunded.status_code == 303
    assert "Invoiced" in admin.get(job_url).text or "invoiced" in admin.get(job_url).text


def test_backup_round_trip_hides_secrets(admin, app):
    submit(admin, "/customers", {"name": "=cmd|'/c calc'!A0", "phone": "845-555-0100", "email": "formula@example.com", "address_line": "1 Main", "city": "Hudson", "region": "NY", "postal_code": "12534", "notes": ""})
    exported = submit(admin, "/admin/export", {"format": "json"})
    assert exported.status_code == 200
    payload = exported.json()
    raw = exported.text
    admin_user = app.state.SessionLocal().query(User).filter_by(email=ADMIN_EMAIL).one()
    assert admin_user.password_hash not in raw
    assert "pbkdf2" not in raw
    assert "password" not in payload["users"][0]
    customers = submit(admin, "/admin/export", {"format": "customers-csv"})
    assert customers.text.splitlines()[1].startswith("'=cmd")
    payload["customers"][0]["name"] = "Helen Cho"
    imported = submit_file(admin, payload)
    assert imported.status_code == 303
    assert "Helen Cho" in admin.get("/customers").text
    assert admin_user.password_hash == app.state.SessionLocal().query(User).filter_by(email=ADMIN_EMAIL).one().password_hash


def test_stripe_checkout_and_webhook(admin, app, monkeypatch):
    app.state.settings = replace(
        app.state.settings,
        stripe_secret_key="sk_test_example",
        stripe_webhook_secret="whsec_example",
    )
    shop = admin.get("/admin/shop")
    assert "sk_test_example" not in shop.text
    assert "whsec_example" not in shop.text
    assert "Saved on the server" in shop.text
    customer = submit(admin, "/customers", {"name": "Priya Shah", "phone": "", "email": "priya@example.com", "address_line": "", "city": "", "region": "", "postal_code": "", "notes": ""})
    customer_id = customer.headers["location"].rsplit("/", 1)[-1]
    job = submit(
        admin,
        "/jobs",
        {
            "customer_id": customer_id,
            "title": "Bath fan",
            "description": "",
            "service_address": "",
            "priority": "normal",
            "status": "invoiced",
            "scheduled_start": "",
            "scheduled_end": "",
            "assigned_user_id": "",
            "tax_rate": "0",
            "discount": "",
            "customer_note": "",
            "line_description": "Fan install",
            "line_kind": "labor",
            "line_quantity": "1",
            "line_unit_price": "120.00",
        },
    )
    job_url = job.headers["location"]
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 12000
        assert kwargs["customer_email"] == "priya@example.com"

        class Session:
            id = "cs_test_123"
            url = "https://checkout.stripe.com/c/pay/cs_test_123"

        return Session()

    monkeypatch.setattr(stripe_sdk.checkout.Session, "create", fake_create)
    started = submit(admin, job_url + "/checkout", {})
    assert started.status_code == 303
    assert started.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_123"
    again = submit(admin, job_url + "/checkout", {})
    assert again.headers["location"] == started.headers["location"]
    assert calls["count"] == 1

    db = app.state.SessionLocal()
    payment = db.query(Payment).filter_by(method="stripe").one()
    payment_id = payment.id
    job_id = payment.job_id
    db.close()

    def fake_event(payload, signature, secret):
        assert secret == "whsec_example"
        assert signature == "t=1,v1=test"
        return json.loads(payload)

    monkeypatch.setattr("app.web.routes.webhooks.construct_event", fake_event)
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_status": "paid",
                "amount_total": 12000,
                "payment_intent": "pi_test_123",
                "metadata": {"payment_id": payment_id, "job_id": job_id},
            }
        },
    }
    hooked = admin.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"stripe-signature": "t=1,v1=test", "content-type": "application/json"},
    )
    assert hooked.status_code == 200
    assert "Paid" in admin.get(job_url).text

    def reject(payload, signature, secret):
        raise stripe.SignatureVerificationError("bad", signature)

    monkeypatch.setattr("app.web.routes.webhooks.construct_event", reject)
    bad = admin.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "nope"})
    assert bad.status_code == 400


def test_checkout_without_stripe_stays_on_the_job(admin):
    customer = submit(admin, "/customers", {"name": "Lila Nguyen", "phone": "", "email": "", "address_line": "", "city": "", "region": "", "postal_code": "", "notes": ""})
    job = submit(
        admin,
        "/jobs",
        {
            "customer_id": customer.headers["location"].rsplit("/", 1)[-1],
            "title": "Door sweep",
            "description": "",
            "service_address": "",
            "priority": "low",
            "status": "completed",
            "scheduled_start": "",
            "scheduled_end": "",
            "assigned_user_id": "",
            "tax_rate": "0",
            "discount": "",
            "customer_note": "",
            "line_description": "Sweep",
            "line_kind": "material",
            "line_quantity": "1",
            "line_unit_price": "18.00",
        },
    )
    response = submit(admin, job.headers["location"] + "/checkout", {})
    assert response.status_code == 303
    assert "stripe.com" not in response.headers["location"]


def test_demo_seed_is_idempotent_and_removable(tmp_path):
    from app.main import create_app
    from app.models import Customer
    from tests.conftest import make_settings

    database = tmp_path / "demo.db"
    settings = make_settings(tmp_path, seed_demo=True)
    settings = replace(settings, database_url=f"sqlite:///{database}")
    first = create_app(settings)
    second = create_app(settings)
    db = second.state.SessionLocal()
    assert db.query(Customer).count() >= 5
    count = db.query(Customer).count()
    db.close()
    again = first.state.SessionLocal()
    assert again.query(Customer).count() == count
    again.close()


def submit_file(client, payload):
    page = client.get("/admin/backup")
    return client.post(
        "/admin/import",
        data={"csrf_token": csrf(page.text)},
        files={"backup": ("daybook.json", json.dumps(payload), "application/json")},
        follow_redirects=False,
    )

