"""Tests zonder Claude: de werkruimte, de vangrails en de agent-lus met een nagespeeld model."""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "healer"))

import agents  # noqa: E402
import llm  # noqa: E402
from workspace import RecipeError, Workspace  # noqa: E402

ROUTE = llm.Route(model="test-model", max_turns=6, price_per_mtok=(1.0, 2.0))


@pytest.fixture
def bundle(tmp_path):
    b = tmp_path / "quarantine" / "batch_1"
    b.mkdir(parents=True)
    (b / "orders.csv").write_text(
        "order_id,customer_id,order_status,order_purchase_timestamp,order_approved_at,order_delivered_carrier_date,"
        "order_delivered_customer_date,order_estimated_delivery_date\n"
        "o1,c1,delivered,2017-02-06 10:00:00,,,,\no2,c2,delivered,2017-02-07 10:00:00,,,,\no2,c2,delivered,2017-02-07 10:00:00,,,,\n")
    (b / "order_payments.csv").write_text("order_id,payment_sequential,payment_type,payment_installments,amount\no1,1,boleto,1,\"10,50\"\n")
    (b / "_manifest.json").write_text(json.dumps({"batch_id": "batch_1", "window_start": "2017-02-06", "window_end": "2017-02-13"}))
    (b / "history_stats.csv").write_text("batch_id,table_name,row_count\nbatch_0,orders,2\n")
    shutil.copy(ROOT / "contracts/landing_schema.yml", b / "landing_schema.yml")
    return b


def test_workspace_loads_copy_and_reports_conformance(bundle):
    ws = Workspace(bundle)
    assert ws.row_counts() == {"order_payments": 1, "orders": 3}
    assert "work.products: ONTBREEKT" in ws.describe()
    assert any("mist kolommen: payment_value" in p for p in ws.conformance())
    assert "batch_0" in ws.query("select * from original.history_stats")


@pytest.mark.parametrize("sql", [
    "select * from read_csv('/etc/passwd', all_varchar=true)",   # geen bestanden lezen
    "select * from glob('/*')",
])
def test_queries_cannot_reach_the_filesystem(bundle, sql):
    with pytest.raises(Exception, match="(?i)permission|disabled|not allowed|cannot access"):
        Workspace(bundle).query(sql)


@pytest.mark.parametrize("sql", ["delete from work.orders", "select 1; select 2", "attach '/tmp/x.db' as x"])
def test_run_sql_only_accepts_one_select(bundle, sql):
    with pytest.raises(RecipeError):
        Workspace(bundle).query(sql)


@pytest.mark.parametrize("recipe", [
    ["delete from original.orders"],                                   # original is alleen-lezen
    ["drop table work.orders"],
    ["create table main.x as select 1"],
    ["update work.orders set order_status = 'x'; delete from work.orders"],  # één statement per regel
    ["copy work.orders to '/tmp/out.csv'"],
    ["set enable_external_access = true"],
    [],
])
def test_recipe_outside_the_work_copy_is_refused(bundle, recipe):
    ws = Workspace(bundle)
    with pytest.raises(RecipeError):
        ws.run_recipe(recipe, commit=True)
    assert ws.row_counts("original")["orders"] == 3


def test_recipe_that_tries_to_write_a_file_fails_and_rolls_back(bundle, tmp_path):
    ws = Workspace(bundle)
    out = tmp_path / "leak.csv"
    with pytest.raises(RecipeError):
        ws.run_recipe(["delete from work.orders where order_id = 'o1'",
                       f"create or replace table work.orders as select * from read_csv('{bundle}/orders.csv')"], commit=True)
    assert ws.row_counts()["orders"] == 3 and not out.exists()


def test_trial_run_rolls_back_and_commit_persists(bundle):
    ws = Workspace(bundle)
    recipe = ["create or replace table work.orders as select distinct * from work.orders"]
    assert "orders: 3 -> 2 rijen" in ws.run_recipe(recipe, commit=False)
    assert ws.row_counts()["orders"] == 3
    ws.run_recipe(recipe, commit=True)
    assert ws.row_counts()["orders"] == 2


def test_export_writes_candidate_atomically(bundle, tmp_path):
    ws = Workspace(bundle)
    ws.run_recipe(["alter table work.order_payments rename amount to payment_value",
                   "update work.order_payments set payment_value = replace(payment_value, ',', '.')"], commit=True)
    target = tmp_path / "candidates" / "inc-1-a1"
    ws.export(target)
    assert (target / "order_payments.csv").read_text().splitlines() == [
        "order_id,payment_sequential,payment_type,payment_installments,payment_value", "o1,1,boleto,1,10.50"]
    assert (target / "_manifest.json").exists() and not list(target.parent.glob(".tmp-*"))


# ---------------------------------------------------------------- agent-lus met nagespeeld model

class FakeClient:
    """Speelt een vooraf bepaald gesprek af en onthoudt wat het model te zien kreeg."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # momentopname
        step = self.script.pop(0)
        blocks = [SimpleNamespace(type="tool_use", id=f"t{len(self.calls)}_{i}", name=n, input=inp)
                  for i, (n, inp) in enumerate(step)]
        return SimpleNamespace(content=blocks, stop_reason="tool_use" if blocks else "end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


DIAGNOSIS = {"root_cause": "export draaide twee keer", "evidence": ["o2 staat er twee keer identiek in"],
             "confidence": "high", "repair_advice": "kopieën verwijderen, geen verlies"}
FINDING = {"issue_type": "duplicates", "table_name": "orders", "column_name": "order_id", "check_name": "unique_key",
           "n_affected": 1, "n_total": 3, "details": "dubbel"}


def test_heal_issue_runs_both_agents_and_applies_recipe(bundle):
    dedupe = ["create or replace table work.orders as select distinct * from work.orders"]
    client = FakeClient([
        [("run_sql", {"sql": "select order_id, count(*) from work.orders group by 1"})],
        [("submit_diagnosis", DIAGNOSIS)],
        [("test_recipe", {"recipe_sql": dedupe})],
        [("submit_fix", {"action": "repair", "recipe_sql": dedupe, "rationale": "1 kopie weg"})],
    ])
    ws = Workspace(bundle)
    fix = agents.heal_issue(client, ROUTE, ws, "duplicates", [FINDING], [FINDING], [], [])

    assert ws.row_counts()["orders"] == 2
    assert fix["recipe_sql"] == dedupe and fix["root_cause"] == DIAGNOSIS["root_cause"]
    assert fix["usage"] == {"input_tokens": 4000, "output_tokens": 400, "turns": 4, "cost_usd": 0.0048}
    # Het queryresultaat ging terug naar het model, en de fixer kreeg de diagnose mee.
    assert "o2 | 2" in client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "export draaide twee keer" in client.calls[2]["messages"][0]["content"]
    assert "sleutels die meer dan één keer" in client.calls[0]["system"]


def test_bad_recipe_goes_back_to_the_model_instead_of_being_applied(bundle):
    good = ["delete from work.orders where order_id = 'o1'"]
    client = FakeClient([
        [("submit_diagnosis", DIAGNOSIS)],
        [("submit_fix", {"action": "repair", "recipe_sql": ["delete from original.orders"], "rationale": "x"})],
        [("submit_fix", {"action": "repair", "recipe_sql": good, "rationale": "y"})],
    ])
    ws = Workspace(bundle)
    fix = agents.heal_issue(client, ROUTE, ws, "duplicates", [FINDING], [FINDING], [], [])
    assert fix["recipe_sql"] == good and ws.row_counts("original")["orders"] == 3
    rejected = client.calls[2]["messages"][-1]["content"][0]
    assert rejected["is_error"] and "moet beginnen met" in rejected["content"]


def test_feedback_and_discard(bundle):
    client = FakeClient([
        [("submit_diagnosis", DIAGNOSIS)],
        [("submit_fix", {"action": "discard", "recipe_sql": [], "rationale": "niet te redden"})],
    ])
    ws = Workspace(bundle)
    feedback = [{"attempt": 1, "source": "mens", "text": "bewaar de nieuwste rij"}]
    fix = agents.heal_issue(client, ROUTE, ws, "duplicates", [FINDING], [FINDING], feedback, [])
    assert fix["action"] == "discard" and fix["recipe_sql"] == [] and ws.row_counts()["orders"] == 3
    assert "bewaar de nieuwste rij" in client.calls[0]["messages"][0]["content"]


def test_agent_that_never_finishes_raises(bundle):
    client = FakeClient([[("run_sql", {"sql": "select 1"})]] * ROUTE.max_turns)
    with pytest.raises(agents.AgentError, match="geen resultaat"):
        agents.heal_issue(client, ROUTE, Workspace(bundle), "duplicates", [FINDING], [FINDING], [], [])
    last = client.calls[-1]["messages"][-1]["content"][-1]
    assert last["type"] == "text" and "laatste stap" in last["text"]


def test_routing_file_covers_every_issue_type_with_a_price():
    import worker
    routing = worker.load_routing(ROOT / "healer/routing.yml")
    assert set(routing) == set(worker.ORDER)
    assert routing["schema_drift"].model == "claude-opus-5-5" and routing["schema_drift"].fallbacks
    assert not routing["duplicates"].fallbacks and all(r.price_per_mtok[1] > 0 for r in routing.values())
    assert all((ROOT / "healer/prompts" / f"{t}.md").exists() for t in routing)
