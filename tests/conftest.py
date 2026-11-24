"""Shared fixtures for the Daybook tests."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import clear_login_attempts

ADMIN_EMAIL = "owner@daybook.test"
ADMIN_PASSWORD = "correct-horse-battery"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        env="test",
        database_url=f"sqlite:///{tmp_path}/daybook.db",
        session_secret="test-session-secret-value-32chars",
        admin_email=ADMIN_EMAIL,
        admin_password=ADMIN_PASSWORD,
        admin_name="Avery Lang",
        seed_demo=False,
        demo_tech_password="sample-tech-password",
        stripe_secret_key="",
        stripe_webhook_secret="",
        app_base_url="http://testserver",
        default_timezone="America/New_York",
        secure_cookies=False,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _reset_login_attempts():
    clear_login_attempts()
    yield
    clear_login_attempts()


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def csrf(html: str) -> str:
    match = _CSRF.search(html)
    assert match, "csrf token missing"
    return match.group(1)


def login(client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, next_url="/"):
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": csrf(page.text),
            "next": next_url,
        },
        follow_redirects=False,
    )


def submit(client, url, data, page="/"):
    source = client.get(page)
    payload = {"csrf_token": csrf(source.text)}
    payload.update(data)
    return client.post(url, data=payload, follow_redirects=False)


@pytest.fixture
def admin(client):
    response = login(client)
    assert response.status_code == 303
    return client
