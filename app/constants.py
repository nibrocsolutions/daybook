"""Shared labels and allowed values."""

from __future__ import annotations

STATUSES = (
    "inquiry",
    "scheduled",
    "in_progress",
    "completed",
    "invoiced",
    "paid",
    "cancelled",
)

STATUS_LABELS = {
    "inquiry": "Inquiry",
    "scheduled": "Scheduled",
    "in_progress": "In progress",
    "completed": "Completed",
    "invoiced": "Invoiced",
    "paid": "Paid",
    "cancelled": "Cancelled",
}

PRIORITIES = ("low", "normal", "urgent")

PRIORITY_LABELS = {
    "low": "Low",
    "normal": "Normal",
    "urgent": "Urgent",
}

ROLES = ("admin", "technician")

ROLE_LABELS = {
    "admin": "Admin",
    "technician": "Technician",
}

LINE_KINDS = ("labor", "material", "other")

KIND_LABELS = {
    "labor": "Labor",
    "material": "Material",
    "other": "Other",
}

MANUAL_METHODS = ("cash", "check", "other")

METHOD_LABELS = {
    "stripe": "Card",
    "cash": "Cash",
    "check": "Check",
    "other": "Other",
}

PAYMENT_STATUSES = ("pending", "succeeded", "failed", "refunded")

COMMON_TIMEZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "UTC",
)

OPEN_BOARD_STATUSES = ("inquiry", "scheduled", "in_progress")
COLLECT_STATUSES = ("completed", "invoiced")
MAX_CHECKOUT_CENTS = 10_000_000
MIN_CHECKOUT_CENTS = 50
MAX_MONEY_CENTS = 100_000_000_00
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 5 * 1024 * 1024
