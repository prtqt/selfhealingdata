"""De enige manier waarop de pipeline met de buitenwereld praat: de console-API."""
from __future__ import annotations

import os

import requests

from . import config

TIMEOUT = 10
# Alleen nodig als de console login aan heeft staan.
HEADERS = {"Authorization": f"Bearer {os.environ['CONSOLE_API_TOKEN']}"} if os.environ.get("CONSOLE_API_TOKEN") else {}


def _url(path: str) -> str:
    return f"{config.CONSOLE_URL}{path}"


def report_incident(payload: dict) -> str:
    r = requests.post(_url("/api/incidents"), json=payload, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    return r.json()["id"]


def incidents(status: str) -> list[dict]:
    r = requests.get(_url("/api/incidents"), params={"status": status}, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def post(incident_id: str, what: str, payload: dict) -> None:
    r = requests.post(_url(f"/api/incidents/{incident_id}/{what}"), json=payload, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
