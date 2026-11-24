"""Price math, redirects, and spreadsheet safety."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Settings
from app.formatting import csv_safe
from app.main import create_app
from app.security import password_matches, safe_next
from app.services.stripe_billing import StripeNotReady, safe_checkout_url
from app.services.totals import compute_totals
from tests.conftest import make_settings


class Line:
    def __init__(self, kind, quantity, unit_price_cents):
        self.kind = kind
        self.quantity = Decimal(quantity)
        self.unit_price_cents = unit_price_cents


def test_tax_can_skip_labor():
    totals = compute_totals(
        [Line("labor", "1", 10000), Line("material", "1", 5000)],
        tax_rate_bps=1000,
        tax_labor=False,
        discount_cents=0,
    )
    assert totals.subtotal_cents == 15000
    assert totals.tax_cents == 500
    assert totals.total_cents == 15500


def test_discount_cannot_make_a_negative_total():
    totals = compute_totals([Line("labor", "1", 1000)], 0, True, 5000)
    assert totals.total_cents == 0
    assert totals.discount_cents == 1000


def test_safe_next_rejects_offsite_urls():
    assert safe_next("https://evil.example") == "/"
    assert safe_next("//evil.example") == "/"
    assert safe_next("/\\evil") == "/"
    assert safe_next("/jobs/abc") == "/jobs/abc"
    assert safe_next("/jobs?status=paid") == "/jobs?status=paid"


def test_csv_cells_cannot_start_a_formula():
    assert csv_safe("=cmd|' /c calc'!A0").startswith("'")
    assert csv_safe("Helen Cho") == "Helen Cho"


def test_checkout_url_must_be_stripe():
    assert safe_checkout_url("https://checkout.stripe.com/c/pay/cs_test_123").startswith("https://")
    with pytest.raises(StripeNotReady):
        safe_checkout_url("https://evil.example/phish")
    with pytest.raises(StripeNotReady):
        safe_checkout_url("http://checkout.stripe.com/c/pay/cs_test_123")


def test_password_round_trip():
    from app.security import hash_password

    stored = hash_password("correct-horse-battery")
    assert password_matches("correct-horse-battery", stored)
    assert not password_matches("wrong-password", stored)
    assert "pbkdf2" not in "correct-horse-battery"


def test_production_refuses_a_weak_session_secret():
    settings = Settings(
        env="production",
        database_url="sqlite:///:memory:",
        session_secret="dev-only-session-secret",
        admin_email="owner@example.com",
        admin_password="a-real-password",
        admin_name="Owner",
        seed_demo=False,
        demo_tech_password="sample-tech-password",
        stripe_secret_key="",
        stripe_webhook_secret="",
        app_base_url="https://shop.example",
        default_timezone="America/New_York",
        secure_cookies=True,
    )
    with pytest.raises(RuntimeError):
        settings.validate()


def test_production_will_not_seed_the_demo(tmp_path):
    settings = make_settings(tmp_path, env="production", seed_demo=True, session_secret="x" * 40, admin_password="a-real-password-1")
    with pytest.raises(RuntimeError):
        create_app(settings)
