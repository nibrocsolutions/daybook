"""Environment configuration.

Secrets are read from the environment only. Nothing in this module has a
production credential of its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

WEAK_SECRETS = {
    "dev-only-session-secret",
    "change-me",
    "secret",
    "password",
}

WEAK_PASSWORDS = {
    "change-this-password",
    "sample-tech-password",
    "password",
    "admin",
    "changeme",
}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    session_secret: str
    admin_email: str
    admin_password: str
    admin_name: str
    seed_demo: bool
    demo_tech_password: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    app_base_url: str
    default_timezone: str
    secure_cookies: bool

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get("ENV", "development").strip().lower() or "development"
        secret = os.environ.get("SESSION_SECRET", "").strip()
        if not secret and env != "production":
            secret = "dev-only-session-secret"
        base = os.environ.get("APP_BASE_URL", "http://localhost:8055").strip().rstrip("/")
        return cls(
            env=env,
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./daybook.db").strip(),
            session_secret=secret,
            admin_email=os.environ.get("ADMIN_EMAIL", "owner@daybook.local").strip().lower(),
            admin_password=os.environ.get("ADMIN_PASSWORD", "change-this-password"),
            admin_name=os.environ.get("ADMIN_NAME", "Avery Lang").strip() or "Avery Lang",
            seed_demo=env_bool("SEED_DEMO", False),
            demo_tech_password=os.environ.get("DEMO_TECH_PASSWORD", "sample-tech-password"),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
            app_base_url=base or "http://localhost:8055",
            default_timezone=os.environ.get("APP_TIMEZONE", "America/New_York").strip()
            or "America/New_York",
            secure_cookies=env == "production",
        )

    def validate(self) -> None:
        if self.env != "production":
            return
        if self.seed_demo:
            raise RuntimeError("SEED_DEMO cannot be enabled when ENV=production.")
        if len(self.session_secret) < 32 or self.session_secret in WEAK_SECRETS:
            raise RuntimeError(
                "Set SESSION_SECRET to a unique value of at least 32 characters."
            )
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("Set ADMIN_EMAIL and ADMIN_PASSWORD before the first launch.")
