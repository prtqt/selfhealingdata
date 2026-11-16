"""De hele lus met echte componenten: bron -> chaos -> pipeline -> console -> healer -> mens -> pipeline.

Alleen Claude is nagespeeld; de console draait als echte HTTP-server.
"""
import importlib
import json
import random
import shutil
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import requests
import uvicorn

from conftest import ROOT
from injectors import inject


@pytest.fixture
def stack(env, monkeypatch):
    """Console op een vrije poort, pipeline en healer wijzend naar dezelfde datamap."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    (env / "chaos").mkdir()
    for k, v in {"CONSOLE_URL": url, "CONTRACTS_DIR": str(ROOT / "contracts"), "CONSOLE_STATE_DIR": str(env / "console"),
                 "CHAOS_LOG_PATH": str(env / "chaos/chaos_log.jsonl"), "QUARANTINE_DIR": str(env / "quarantine"),
                 "CANDIDATES_DIR": str(env / "candidates")}.items():
        monkeypatch.setenv(k, v)
    for name in ("app", "store", "worker"):
        sys.modules.pop(name, None)
    server = uvicorn.Server(uvicorn.Config(importlib.import_module("app").app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            requests.get(f"{url}/api/health", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.1)
    yield SimpleNamespace(dir=env, url=url, worker=importlib.import_module("worker"),
                          flow=importlib.import_module("sh_pipeline.flow"),
                          warehouse=importlib.import_module("sh_pipeline.warehouse"))
    server.should_exit = True


class ScriptedClaude:
    """Speelt per agent één diagnose en één recept af."""

    def __init__(self, recipe, action="repair"):
        self.recipe, self.action, self.n = recipe, action, 0
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=self.messages)

    def create(self, **kwargs):
        self.n += 1
        if self.n % 2:
            name, inp = "submit_diagnosis", {"root_cause": "export draaide twee keer", "evidence": ["identieke rijen"],
                                             "confidence": "high", "repair_advice": "kopieën verwijderen"}
        else:
            name, inp = "submit_fix", {"action": self.action, "recipe_sql": self.recipe, "rationale": "toelichting"}
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=f"t{self.n}", name=name, input=inp)],
                               stop_reason="tool_use", usage=SimpleNamespace(input_tokens=100, output_tokens=10))


def deliver(stack, batches, broken=None):
    """Zet batches in landing; de laatste eventueel verstoord, met een regel in het chaos-log."""
    for b in batches:
        shutil.copytree(b, stack.dir / "landing" / b.name)
    if broken:
        target = stack.dir / "landing" / batches[-1].name
        done = inject(target, broken[0], random.Random(3), broken[1])
        (stack.dir / "chaos/chaos_log.jsonl").write_text(json.dumps({"batch_id": target.name, "injections": [done]}) + "\n")
        return done


def incident(stack, status):
    found = requests.get(f"{stack.url}/api/incidents", params={"status": status}).json()
    assert len(found) == 1, f"verwacht één incident met status {status}"
    return found[0]


def orders_in_marts(stack):
    with stack.warehouse.connect() as con:
        return con.execute("select count(*), count(distinct order_id) from marts.fct_orders").fetchone()


def csv_rows(path):
    return len(path.read_text().splitlines()) - 1


def test_duplicate_batch_is_healed_approved_and_released(stack, batches):
    done = deliver(stack, batches, broken=("duplicates", "duplicate_rows"))
    table = done["table"]
    clean_orders = sum(csv_rows(b / "orders.csv") for b in batches)

    assert stack.flow.ingest_landing() == {"clean": [b.name for b in batches[:5]], "quarantined": [batches[5].name]}
    assert orders_in_marts(stack)[0] == clean_orders - csv_rows(batches[5] / "orders.csv")  # foute batch niet in de marts
    inc = incident(stack, "detected")
    assert {f["issue_type"] for f in inc["findings"]} == {"duplicates"}

    claude = ScriptedClaude([f"create or replace table work.{table} as select distinct * from work.{table}"])
    assert stack.worker.tick(claude, stack.worker.load_routing(ROOT / "healer/routing.yml")) == 1
    inc = incident(stack, "proposed")
    assert (stack.dir / "candidates" / inc["proposal"]["candidate_path"] / "recipe.sql").exists()

    assert stack.flow.validate_candidates() == [inc["id"]]
    inc = incident(stack, "validated")
    v = inc["validation"]
    assert v["passed"] and v["row_counts_before"][table] - v["row_counts_after"][table] == done["n_affected"]
    assert stack.flow.release_approved() == []  # zonder goedkeuring gebeurt er niets

    r = requests.post(f"{stack.url}/incidents/{inc['id']}/decision", data={"decision": "approve", "decided_by": "test"})
    assert r.ok
    assert stack.flow.release_approved() == [inc["id"]]
    assert incident(stack, "resolved")["resolution"]["outcome"] == "released"
    assert orders_in_marts(stack) == (clean_orders, clean_orders)  # gelijk aan een run zonder chaos

    page = requests.get(f"{stack.url}/incidents/{inc['id']}", params={"view": "dev"}).text
    assert "Detectie: <b class=\"ok\">juist" in page and "claude-haiku-5-5" in page


def test_useless_fix_fails_validation_and_goes_back_with_feedback(stack, batches):
    deliver(stack, batches[:1], broken=("schema_drift", "decimal_comma"))
    stack.flow.ingest_landing()
    routing = stack.worker.load_routing(ROOT / "healer/routing.yml")
    stack.worker.tick(ScriptedClaude(["delete from work.orders where false"]), routing)
    stack.flow.validate_candidates()
    inc = incident(stack, "detected")
    assert inc["attempt"] == 2 and "schema_drift" in inc["feedback"][0]["text"]
    assert "proposal" not in inc and len(inc["previous_attempts"]) == 1


def test_human_can_discard_or_release_original_without_healer(stack, batches):
    deliver(stack, batches[:2], broken=("invalid_values", "negative_price"))
    stack.flow.ingest_landing()
    inc = incident(stack, "detected")
    requests.post(f"{stack.url}/api/incidents/{inc['id']}/decision", json={"decision": "release_original"}).raise_for_status()
    assert stack.flow.release_approved() == [inc["id"]]
    assert incident(stack, "resolved")["final_action"] == "release_original"
    assert orders_in_marts(stack)[0] == sum(csv_rows(b / "orders.csv") for b in batches[:2])
    with stack.warehouse.connect() as con:
        assert con.execute("select status from meta.batch_registry order by batch_id").fetchall() == [("clean",), ("released_unchecked",)]


def test_discard_proposal_keeps_batch_out(stack, batches):
    deliver(stack, batches[:1], broken=("volume_anomaly", "missing_table"))
    stack.flow.ingest_landing()
    stack.worker.tick(ScriptedClaude([], action="discard"), stack.worker.load_routing(ROOT / "healer/routing.yml"))
    stack.flow.validate_candidates()
    inc = incident(stack, "validated")
    assert inc["proposal"]["action"] == "discard"
    requests.post(f"{stack.url}/api/incidents/{inc['id']}/decision", json={"decision": "approve"}).raise_for_status()
    stack.flow.release_approved()
    assert incident(stack, "discarded")
    with stack.warehouse.connect() as con:
        assert con.execute("select status from meta.batch_registry").fetchall() == [("discarded",)]
        assert con.execute("select count(*) from raw.orders").fetchone()[0] == 0


def test_pipeline_keeps_working_when_console_is_down(env, batches, monkeypatch):
    flow = importlib.import_module("sh_pipeline.flow")
    for b in batches[:2]:
        shutil.copytree(b, env / "landing" / b.name)
    inject(env / "landing" / batches[1].name, "duplicates", random.Random(1))
    assert flow.ingest_landing() == {"clean": [batches[0].name], "quarantined": [batches[1].name]}
    assert (env / "quarantine" / batches[1].name / "findings.json").exists()
    assert flow.validate_candidates() == [] and flow.release_approved() == []
    assert flow.ingest_landing() == {"clean": [], "quarantined": []}  # niets dubbel verwerkt


def test_recurring_drift_is_healed_by_a_rule_without_claude_or_human(stack, batches):
    routing = stack.worker.load_routing(ROOT / "healer/routing.yml")
    done = deliver(stack, batches[:1], broken=("schema_drift", "rename_column"))
    stack.flow.ingest_landing()
    rename = [f"alter table work.{done['table']} rename {done['renamed_to']} to {done['column']}"]
    stack.worker.tick(ScriptedClaude(rename), routing)
    stack.flow.validate_candidates()
    inc = incident(stack, "validated")
    requests.post(f"{stack.url}/api/incidents/{inc['id']}/decision",
                  json={"decision": "approve", "decided_by": "Pieter", "save_as_rule": True}).raise_for_status()
    stack.flow.release_approved()

    # Volgende week dezelfde hernoemde kolom.
    nxt = stack.dir / "landing" / batches[1].name
    shutil.copytree(batches[1], nxt)
    inject(nxt, "schema_drift", random.Random(3), "rename_column")
    stack.flow.ingest_landing()

    class NoClaude:
        messages = beta = SimpleNamespace(messages=None, create=None)
    stack.worker.tick(NoClaude(), routing)                  # zou crashen als een agent werd aangeroepen
    stack.flow.validate_candidates()
    auto = incident(stack, "approved")
    assert auto["batch_id"] == batches[1].name and auto["decision"]["auto"]
    stack.flow.release_approved()
    with stack.warehouse.connect() as con:
        assert con.execute("select status from meta.batch_registry order by batch_id").fetchall() == [("healed",), ("healed",)]


def test_clean_orders_are_released_while_only_affected_orders_wait(env, batches, monkeypatch):
    monkeypatch.setenv("PARTIAL_RELEASE", "true")
    flow, warehouse = importlib.import_module("sh_pipeline.flow"), importlib.import_module("sh_pipeline.warehouse")
    broken = env / "landing" / batches[0].name
    shutil.copytree(batches[0], broken)
    done = inject(broken, "invalid_values", random.Random(2), "negative_price", fraction=0.1)
    total = csv_rows(batches[0] / "orders.csv")

    flow.ingest_landing()
    with warehouse.connect() as con:
        assert con.execute("select status from meta.batch_registry").fetchall() == [("partial",)]
        released, bad_prices = con.execute("select count(*), count(*) filter (where items_value < 0) from marts.fct_orders").fetchone()
    assert 0 < total - released <= done["n_affected"] and bad_prices == 0     # geraakte orders buiten de marts
    assert (env / "quarantine" / batches[0].name / "order_items.csv").read_text() == (broken / "order_items.csv").read_text()


def test_structural_problems_hold_the_whole_batch(env, batches, monkeypatch):
    monkeypatch.setenv("PARTIAL_RELEASE", "true")
    flow, warehouse = importlib.import_module("sh_pipeline.flow"), importlib.import_module("sh_pipeline.warehouse")
    broken = env / "landing" / batches[0].name
    shutil.copytree(batches[0], broken)
    inject(broken, "schema_drift", random.Random(2), "decimal_comma")
    flow.ingest_landing()
    with warehouse.connect() as con:
        assert con.execute("select status from meta.batch_registry").fetchall() == [("quarantined",)]
        assert con.execute("select count(*) from raw.orders").fetchone()[0] == 0
