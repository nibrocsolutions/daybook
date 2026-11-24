"""Request failures that become pages rather than tracebacks."""

from __future__ import annotations


class AuthRedirect(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


class PermissionDenied(Exception):
    def __init__(self, message: str = "You do not have access to that."):
        self.message = message


class FormExpired(Exception):
    def __init__(self, message: str = "Refresh the page and try again."):
        self.message = message


class BackupError(Exception):
    def __init__(self, message: str):
        self.message = message
