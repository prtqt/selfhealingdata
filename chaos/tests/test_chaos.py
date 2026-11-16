import csv
import importlib
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "chaos"))

from injectors import VARIANTS, inject, read  # noqa: E402

HEADERS = {
    "orders": ["order_id", "customer_id", "order_status", "order_purchase_timestamp"],
    "order_items": ["order_id", "order_item_id", "product_id", "seller_id", "price", "freight_value"],
    "order_payments": ["order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"],
    "customers": ["customer_id", "customer_unique_id", "customer_state"],
    "products": ["product_id", "product_category_name"],
}


def make_batch(folder: Path, n: int = 100) -> Path:
    folder.mkdir(parents=True)
    rows = {
        "orders": [[f"o{i}", f"c{i}", "delivered", f"2017-02-0{6 + i % 3} 10:00:00"] for i in range(n)],
        "order_items": [[f"o{i}", "1", f"p{i % 10}", "s1", "50.00", "10.00"] for i in range(n)],
        "order_payments": [[f"o{i}", "1", "boleto", "1", "60.00"] for i in range(n)],
        "customers": [[f"c{i}", f"u{i}", "SP"] for i in range(n)],
        "products": [[f"p{i}", "cat"] for i in range(10)],
    }
    for table, header in HEADERS.items():
        with open(folder / f"{table}.csv", "w", newline="") as f:
            csv.writer(f).writerows([header] + rows[table])
    (folder / "_manifest.json").write_text(json.dumps({"batch_id": folder.name}))
    return folder


def snapshot(batch: Path) -> dict:
    return {f.name: f.read_text() for f in sorted(batch.glob("*.csv"))}


@pytest.mark.parametrize("issue_type,variant", [(t, v) for t, vs in VARIANTS.items() for v in vs])
def test_every_variant_changes_the_batch_and_reports_it(tmp_path, issue_type, variant):
    batch = make_batch(tmp_path / "b1")
    before = snapshot(batch)
    done = inject(batch, issue_type, random.Random(5), variant)
    assert done["issue_type"] == issue_type and done["variant"] == variant and done["table"] in HEADERS
    assert snapshot(batch) != before


def test_same_seed_gives_same_damage(tmp_path):
    a, b = make_batch(tmp_path / "a"), make_batch(tmp_path / "b")
    inject(a, "null_spike", random.Random(9))
    inject(b, "null_spike", random.Random(9))
    assert snapshot(a) == snapshot(b)


def test_duplicate_rows_are_exact_copies(tmp_path):
    batch = make_batch(tmp_path / "b1")
    done = inject(batch, "duplicates", random.Random(2), fraction=0.1)
    _, rows = read(batch, done["table"])
    assert len(rows) == 110 and len({tuple(r) for r in rows}) == 100


@pytest.fixture
def courier(tmp_path, monkeypatch):
    for k, v in {"OUTBOX_DIR": "outbox", "LANDING_DIR": "landing", "CHAOS_STATE_DIR": "state"}.items():
        monkeypatch.setenv(k, str(tmp_path / v))
    monkeypatch.setenv("CHAOS_PROBABILITY", "1.0")
    monkeypatch.setenv("CHAOS_WARMUP_BATCHES", "1")
    sys.modules.pop("chaos", None)
    mod = importlib.import_module("chaos")
    mod.STATE.mkdir()
    mod.LANDING.mkdir()
    return mod


def test_courier_warms_up_then_injects_and_logs_the_truth(courier, tmp_path):
    rng = random.Random(1)
    first = courier.deliver(make_batch(tmp_path / "outbox/b1"), rng)
    second = courier.deliver(make_batch(tmp_path / "outbox/b2"), rng)
    assert first["injections"] == [] and len(second["injections"]) == 1
    assert snapshot(tmp_path / "landing/b1") == snapshot(tmp_path / "outbox/b1")   # warmup: ongewijzigd
    assert snapshot(tmp_path / "landing/b2") != snapshot(tmp_path / "outbox/b2")
    assert snapshot(tmp_path / "outbox/b2") == snapshot(make_batch(tmp_path / "ref"))  # outbox blijft onaangeroerd
    assert [e["batch_id"] for e in courier.history()] == ["b1", "b2"]
    assert not list((tmp_path / "landing").glob(".tmp-*"))


def test_force_applies_to_next_batch_only(courier, tmp_path, monkeypatch):
    monkeypatch.setattr(courier, "PROBABILITY", 0.0)
    courier.FORCE.write_text(json.dumps({"issue_type": "schema_drift", "variant": "rename_column"}))
    forced = courier.deliver(make_batch(tmp_path / "outbox/b1"), random.Random(1))
    after = courier.deliver(make_batch(tmp_path / "outbox/b2"), random.Random(1))
    assert forced["injections"][0]["variant"] == "rename_column" and "renamed_to" in forced["injections"][0]
    assert after["injections"] == [] and not courier.FORCE.exists()


def test_llm_choice_is_validated_and_falls_back(courier, tmp_path, monkeypatch):
    import chaos_agent
    from types import SimpleNamespace

    def fake(picks):
        blocks = [SimpleNamespace(type="tool_use", name="inject", input=p) for p in picks]
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(content=blocks, stop_reason="tool_use")))

    ok = {"issue_type": "duplicates", "variant": "duplicate_rows", "fraction": 0.1, "reason": "job draaide dubbel"}
    assert chaos_agent.choose_llm("x", [], sorted(VARIANTS), client=fake([ok])) == [ok]
    with pytest.raises(RuntimeError):  # variant hoort niet bij het type
        chaos_agent.choose_llm("x", [], sorted(VARIANTS), client=fake([{**ok, "variant": "rename_column"}]))

    monkeypatch.setattr(courier, "MODE", "llm")
    monkeypatch.setattr(chaos_agent, "choose_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("geen sleutel")))
    courier.deliver(make_batch(tmp_path / "outbox/b0"), random.Random(1))
    entry = courier.deliver(make_batch(tmp_path / "outbox/b1"), random.Random(1))
    assert len(entry["injections"]) == 1  # willekeurige keuze als Claude niet bereikbaar is
