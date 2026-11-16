"""Login en rollen voor de console. Staat uit zolang CONSOLE_USERS leeg is.

  CONSOLE_USERS      naam:rol:wachtwoord, gescheiden door komma's. Rollen: po (mag beslissen) en dev (alleen lezen).
  CONSOLE_API_TOKEN  wat pipeline en healer meesturen als Bearer-token op /api.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

ROLES = {"po": "Product owner", "dev": "Developer"}


def load_users(raw: str) -> dict[str, dict]:
    users = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        name, role, password = entry.split(":", 2)
        if role not in ROLES:
            raise ValueError(f"onbekende rol {role!r} voor {name}; kies po of dev")
        users[name] = {"name": name, "role": role, "password": password}
    return users


USERS = load_users(os.environ.get("CONSOLE_USERS", ""))
API_TOKEN = os.environ.get("CONSOLE_API_TOKEN", "")
ENABLED = bool(USERS)
# Zonder vast geheim vervallen sessies bij een herstart; voor een lokale opstelling is dat prima.
_SECRET = (os.environ.get("CONSOLE_SECRET") or secrets.token_hex(32)).encode()


def _sign(name: str) -> str:
    return hmac.new(_SECRET, name.encode(), hashlib.sha256).hexdigest()


def session_for(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode() + "." + _sign(name)


def user_from(cookie: str | None) -> dict | None:
    try:
        encoded, signature = (cookie or "").split(".", 1)
        name = base64.urlsafe_b64decode(encoded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return USERS.get(name) if hmac.compare_digest(signature, _sign(name)) else None


def check_password(name: str, password: str) -> dict | None:
    user = USERS.get(name)
    expected = user["password"] if user else secrets.token_hex(8)  # ook bij onbekende naam vergelijken
    return user if hmac.compare_digest(password.encode(), expected.encode()) and user else None


def token_ok(header: str | None) -> bool:
    return bool(API_TOKEN) and hmac.compare_digest((header or "").encode(), f"Bearer {API_TOKEN}".encode())
