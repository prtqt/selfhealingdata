import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

FINDING = {"issue_type": "duplicates", "table_name": "orders", "column_name": "order_id", "check_name": "unique_key",
           "n_affected": 5, "n_total": 100, "pct_affected": 5.0, "sample": ["a"], "details": "dubbel"}
INCIDENT = {"batch_id": "batch_1", "quarantine_path": "batch_1", "row_counts": {"orders": 100}, "findings": [FINDING]}
FIX = {"issue_type": "duplicates", "model": "claude-haiku-5-5", "root_cause": "export draaide twee keer",
       "evidence": ["5 sleutels dubbel"], "confidence": "high", "action": "repair",
       "recipe_sql": ["create or replace table work.orders as select distinct * from work.orders"],
       "rationale": "exacte kopieën verwijderen",
       "business_summary": "Een deel van de bestellingen is twee keer aangeleverd; de kopieën worden verwijderd.", "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "turns": 2}}
PROPOSAL = {"action": "repair", "summary": "duplicaten verwijderd", "candidate_path": "inc-a1", "fixes": [FIX]}
PASSED = {"passed": True, "remaining_findings": [], "row_counts_before": {"orders": 100}, "row_counts_after": {"orders": 95}}
FAILED = {"passed": False, "remaining_findings": [FINDING], "row_counts_before": {"orders": 100}, "row_counts_after": {"orders": 100}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACTS_DIR", str(ROOT / "contracts"))
    monkeypatch.setenv("CONSOLE_STATE_DIR", str(tmp_path))
    log = tmp_path / "chaos_log.jsonl"
    log.write_text(json.dumps({"batch_id": "batch_1", "injections": [
        {"issue_type": "duplicates", "variant": "duplicate_rows", "table": "orders", "column": None, "n_affected": 5}]}) + "\n")
    monkeypatch.setenv("CHAOS_LOG_PATH", str(log))
    sys.path.insert(0, str(ROOT / "console"))
    for m in ("app", "store"):
        sys.modules.pop(m, None)
    return TestClient(importlib.import_module("app").app, follow_redirects=False)


def post(client, path, body=None, code=200):
    r = client.post(f"/api/incidents{path}", json=body or {})
    assert r.status_code == code, r.text
    return r.json()


def to_validated(client):
    inc = post(client, "", INCIDENT)["id"]
    post(client, f"/{inc}/claim")
    post(client, f"/{inc}/proposal", PROPOSAL)
    post(client, f"/{inc}/validation", PASSED)
    return inc


def test_happy_path_and_pages(client):
    inc = to_validated(client)
    client.get("/?view=dev")  # kiest de developer-weergave en onthoudt die in een cookie
    assert "Goedkeuren" in client.get(f"/incidents/{inc}").text
    assert "chaos-agent werkelijk" not in client.get(f"/incidents/{inc}").text  # waarheid pas na afloop

    r = client.post(f"/incidents/{inc}/decision", data={"decision": "approve", "comment": "ok", "decided_by": "Pieter"})
    assert r.status_code == 303
    doc = client.get(f"/api/incidents/{inc}").json()
    assert (doc["status"], doc["final_action"]) == ("approved", "repair")
    assert [i["id"] for i in client.get("/api/incidents", params={"status": "approved"}).json()] == [inc]

    assert post(client, f"/{inc}/resolution", {"outcome": "released", "detail": "ingelezen"})["status"] == "resolved"
    page = client.get(f"/incidents/{inc}").text
    assert "chaos-agent werkelijk" in page and "Goedkeuren" not in page
    assert "Opgelost" in client.get("/").text
    report = client.get("/export").text
    assert "export draaide twee keer" in report and "<button" not in report


def test_invalid_transitions_are_refused(client):
    inc = post(client, "", INCIDENT)["id"]
    post(client, f"/{inc}/proposal", PROPOSAL, code=409)        # eerst claimen
    post(client, f"/{inc}/decision", {"decision": "approve"}, code=409)
    post(client, f"/{inc}/claim")
    post(client, f"/{inc}/claim", code=409)                      # geen twee healers op één incident
    post(client, f"/{inc}/resolution", {"outcome": "released"}, code=409)
    post(client, "/bestaat-niet/claim", code=404)


def test_payloads_must_match_contract(client):
    post(client, "", {"batch_id": "x"}, code=422)
    inc = post(client, "", INCIDENT)["id"]
    post(client, f"/{inc}/claim")
    post(client, f"/{inc}/proposal", {**PROPOSAL, "action": "yolo"}, code=422)


def test_same_batch_gives_same_open_incident(client):
    assert post(client, "", INCIDENT)["id"] == post(client, "", INCIDENT)["id"]


def test_reject_and_failed_validation_retry_then_need_human(client):
    inc = to_validated(client)
    doc = post(client, f"/{inc}/decision", {"decision": "reject", "comment": "bewaar de nieuwste rij"})
    assert (doc["status"], doc["attempt"]) == ("detected", 2)
    assert doc["feedback"][-1]["text"] == "bewaar de nieuwste rij" and "proposal" not in doc

    for attempt in (2, 3):
        post(client, f"/{inc}/claim")
        post(client, f"/{inc}/proposal", PROPOSAL)
        doc = post(client, f"/{inc}/validation", FAILED)
    assert doc["status"] == "needs_human" and len(doc["previous_attempts"]) == 3
    post(client, f"/{inc}/claim", code=409)

    doc = post(client, f"/{inc}/decision", {"decision": "discard", "decided_by": "Pieter"})
    assert (doc["status"], doc["final_action"]) == ("approved", "discard")
    assert post(client, f"/{inc}/resolution", {"outcome": "discarded"})["status"] == "discarded"


def test_human_can_override_before_any_proposal(client):
    inc = post(client, "", INCIDENT)["id"]
    doc = post(client, f"/{inc}/decision", {"decision": "release_original"})
    assert doc["final_action"] == "release_original"


def test_product_owner_and_developer_see_different_pages(client):
    inc = to_validated(client)
    po = client.get(f"/incidents/{inc}").text            # standaard: product owner
    assert "Goedkeuren" in po and "twee keer aangeleverd" in po and "5 bestellingen zijn dubbel aangeleverd" in po
    assert "Bestellingen" in po and "100 → <b>95</b>" in po
    for jargon in ("create or replace", "unique_key", "claude-haiku", "tokens", "orders.order_id"):
        assert jargon not in po, jargon

    dev = client.get(f"/incidents/{inc}?view=dev").text
    for detail in ("create or replace table work.orders", "unique_key", "claude-haiku-5-5", "data/quarantine/batch_1/", "Goedkeuren"):
        assert detail in dev, detail
    assert "twee keer aangeleverd" not in dev

    assert "create or replace" in client.get(f"/incidents/{inc}").text       # keuze is onthouden
    assert "create or replace" not in client.get(f"/incidents/{inc}?view=po").text


def test_overviews_per_audience(client):
    inc = to_validated(client)
    po = client.get("/?view=po").text
    assert "1 levering wacht op jouw besluit" in po and "Bekijken en beslissen" in po and "Dubbele gegevens" in po
    assert "zonder verlies" not in po and "vervallen 5 regels" in po
    dev = client.get("/?view=dev").text
    assert "orders.unique_key" in dev and inc in dev and "tokens" in dev

    post(client, f"/{inc}/decision", {"decision": "approve", "decided_by": "Pieter"})
    post(client, f"/{inc}/resolution", {"outcome": "released", "detail": "ok"})
    po = client.get("/?view=po").text
    assert "Niets wacht op jou" in po and "Hersteld en verwerkt" in po and "1/1" in po and "Pieter" in po


def test_approved_recipe_can_become_a_rule_that_auto_approves_next_time(client):
    inc = to_validated(client)
    r = client.post(f"/incidents/{inc}/decision", data={"decision": "approve", "decided_by": "Pieter", "save_as_rule": "1"})
    assert r.status_code == 303
    rule = client.get("/api/rules").json()[0]
    assert rule["signature"] == ["orders.order_id:unique_key"] and rule["recipe_sql"] == FIX["recipe_sql"]
    post(client, f"/{inc}/resolution", {"outcome": "released"})

    # Dezelfde afwijking in een volgende batch: de healer krijgt de regel mee bij het claimen.
    nxt = post(client, "", {**INCIDENT, "batch_id": "batch_2", "quarantine_path": "batch_2"})["id"]
    claimed = post(client, f"/{nxt}/claim")
    assert claimed["rule"]["id"] == rule["id"]
    post(client, f"/{nxt}/proposal", {**PROPOSAL, "rule_id": rule["id"]})
    doc = post(client, f"/{nxt}/validation", PASSED)
    assert doc["status"] == "approved" and doc["decision"]["auto"] and rule["id"] in doc["decision"]["decided_by"]
    assert client.get("/api/rules").json()[0]["applied"] == 1
    assert "Blijvende regels" in client.get("/rules").text and "1 keer toegepast" in client.get("/rules").text


def test_failing_rule_is_not_retried_and_can_be_switched_off(client):
    inc = to_validated(client)
    post(client, f"/{inc}/decision", {"decision": "approve", "save_as_rule": True})
    post(client, f"/{inc}/resolution", {"outcome": "released"})
    rule_id = client.get("/api/rules").json()[0]["id"]

    nxt = post(client, "", {**INCIDENT, "batch_id": "batch_2", "quarantine_path": "batch_2"})["id"]
    post(client, f"/{nxt}/claim")
    post(client, f"/{nxt}/proposal", {**PROPOSAL, "rule_id": rule_id})
    assert post(client, f"/{nxt}/validation", FAILED)["status"] == "detected"
    assert post(client, f"/{nxt}/claim")["rule"] is None           # tweede poging gaat naar de agents
    assert client.get("/api/rules").json()[0]["failed"] == 1

    assert client.post(f"/rules/{rule_id}/deactivate").status_code == 303
    other = post(client, "", {**INCIDENT, "batch_id": "batch_3", "quarantine_path": "batch_3"})["id"]
    assert post(client, f"/{other}/claim")["rule"] is None


def test_different_findings_do_not_match_a_rule(client):
    inc = to_validated(client)
    post(client, f"/{inc}/decision", {"decision": "approve", "save_as_rule": True})
    post(client, f"/{inc}/resolution", {"outcome": "released"})
    other = {**INCIDENT, "batch_id": "batch_2", "findings": [{**FINDING, "table_name": "order_payments"}]}
    assert post(client, f"/{post(client, '', other)['id']}/claim")["rule"] is None


def test_product_owner_sees_business_impact_and_biggest_first(client):
    def waiting(batch, revenue):
        inc = post(client, "", {**INCIDENT, "batch_id": batch, "quarantine_path": batch, "window_start": "2017-03-06",
                               "impact": {"orders": 246, "revenue": revenue, "affected_rows": 5, "waiting_reports": ["Omzet per dag"]}})["id"]
        post(client, f"/{inc}/claim"); post(client, f"/{inc}/proposal", PROPOSAL); post(client, f"/{inc}/validation", PASSED)
        return inc
    small, big = waiting("batch_a", 1200.0), waiting("batch_b", 48250.4)
    po = client.get("/?view=po").text
    assert "R$ 48.250 omzet" in po and "R$ 49.450 omzet" in po          # per levering en opgeteld
    assert po.index(f"/incidents/{big}") < po.index(f"/incidents/{small}")
    detail = client.get(f"/incidents/{big}?view=po").text
    assert "Dat raakt: Omzet per dag" in detail and "246 bestellingen" in detail
