import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_console import INCIDENT, PASSED, PROPOSAL

ROOT = Path(__file__).resolve().parents[2]
TOKEN = {"Authorization": "Bearer dienst-geheim"}


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACTS_DIR", str(ROOT / "contracts"))
    monkeypatch.setenv("CONSOLE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CONSOLE_USERS", "pieter:po:geheim1, sam:dev:geheim2")
    monkeypatch.setenv("CONSOLE_API_TOKEN", "dienst-geheim")
    sys.path.insert(0, str(ROOT / "console"))
    for m in ("app", "store", "auth"):
        sys.modules.pop(m, None)
    yield lambda: TestClient(importlib.import_module("app").app, follow_redirects=False)
    for m in ("app", "store", "auth"):      # andere tests draaien zonder login
        sys.modules.pop(m, None)


def login(client, name, password):
    return client.post("/login", data={"name": name, "password": password})


def validated(client):
    inc = client.post("/api/incidents", json=INCIDENT, headers=TOKEN).json()["id"]
    for step, body in (("claim", {}), ("proposal", PROPOSAL), ("validation", PASSED)):
        assert client.post(f"/api/incidents/{inc}/{step}", json=body, headers=TOKEN).status_code == 200
    return inc


def test_everything_needs_a_login_or_the_service_token(secured):
    c = secured()
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/api/incidents").status_code == 401
    assert c.get("/api/incidents", headers={"Authorization": "Bearer fout"}).status_code == 401
    assert c.get("/api/incidents", headers=TOKEN).status_code == 200
    assert c.get("/api/health").status_code == 200
    assert login(c, "pieter", "fout").headers["location"] == "/login?error=1"
    assert login(c, "bestaat-niet", "geheim1").headers["location"] == "/login?error=1"
    c.cookies.set("session", "cGlldGVy.vervalst")
    assert c.get("/").headers["location"] == "/login"


def test_product_owner_decides_under_own_name_and_developer_cannot(secured):
    svc = secured()
    inc = validated(svc)

    dev = secured()
    assert login(dev, "sam", "geheim2").status_code == 303
    page = dev.get(f"/incidents/{inc}").text
    assert "create or replace" in page                       # developer start in de developer-weergave
    assert "Alleen een product owner kan" in page and 'value="approve"' not in page
    assert dev.post(f"/incidents/{inc}/decision", data={"decision": "approve"}).status_code == 403
    assert dev.post(f"/api/incidents/{inc}/decision", json={"decision": "approve"}).status_code == 403
    assert dev.post("/policy", data={"enabled": "1"}).status_code == 403

    po = secured()
    login(po, "pieter", "geheim1")
    page = po.get(f"/incidents/{inc}").text
    assert 'value="approve"' in page and "create or replace" not in page and 'id="decided_by"' not in page
    assert po.post(f"/incidents/{inc}/decision", data={"decision": "approve", "decided_by": "iemand anders"}).status_code == 303
    doc = svc.get(f"/api/incidents/{inc}", headers=TOKEN).json()
    assert doc["status"] == "approved" and doc["decision"]["decided_by"] == "pieter"
    assert po.get("/logout").headers["location"] == "/login" 
