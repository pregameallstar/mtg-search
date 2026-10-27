"""Minimal session CSRF token for browser form/multipart requests.

The app is loopback-first and has no user login, so this is not an auth system.
It protects state-changing browser posts from cross-site forms/fetches that
would otherwise be able to change config, upload a database, or restart MCP.
API clients that use JSON content types are already protected by the browser's
CORS preflight rules; they do not need this token.
"""

import hmac
import secrets

from flask import session

_TOKEN_KEY = "_csrf_token"


def csrf_token():
    """Return the current session token, creating one on first use."""
    token = session.get(_TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_TOKEN_KEY] = token
    return token


def csrf_ok(token):
    """Constant-time comparison of a submitted token with the session token."""
    expected = session.get(_TOKEN_KEY)
    if not expected or not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(expected, token)
