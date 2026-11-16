"""De poort op een handgemaakte minibatch, zonder andere componenten."""
import csv
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline"))
CONTRACT = yaml.safe_load((ROOT / "contracts/landing_schema.yml").read_text())["tables"]
MANIFEST = {"batch_id": "batch_20170206", "window_start": "2017-02-06", "window_end": "2017-02-13"}


def make_batch(folder: Path, n: int = 40) -> Path:
    folder.mkdir(parents=True)
    ts = "2017-02-07 10:00:00"
    rows = {
        "orders": [[f"o{i}", f"c{i}", "delivered", ts, ts, ts, ts, ts] for i in range(n)],
        "order_items": [[f"o{i}", "1", f"p{i % 5}", "s1", ts, "50.00", "10.00"] for i in range(n)],
        "order_payments": [[f"o{i}", "1", "boleto", "1", "60.00"] for i in range(n)],
        "customers": [[f"c{i}", f"u{i}", "01000", "sao paulo", "SP"] for i in range(n)],
        "products": [[f"p{i}", "cat", "100", "10", "10", "10"] for i in range(5)],
    }
    for table, spec in CONTRACT.items():
        with open(folder / f"{table}.csv", "w", newline="") as f:
            csv.writer(f).writerows([list(spec["columns"])] + rows[table])
    (folder / "_manifest.json").write_text(json.dumps(MANIFEST))
    return folder


def edit(batch: Path, table: str, fn) -> None:
    with open(batch / f"{table}.csv", newline="") as f:
        rows = list(csv.reader(f))
    rows = fn(rows)
    with open(batch / f"{table}.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTRACT_PATH", str(ROOT / "contracts/landing_schema.yml"))
    for name in [m for m in sys.modules if m.startswith("sh_pipeline")]:
        del sys.modules[name]
    from sh_pipeline import gate
    return gate


def kinds(findings):
    return {(f["issue_type"], f["table_name"], f["check_name"]) for f in findings}


def test_clean_batch_passes(gate, tmp_path):
    findings, counts = gate.run_gate(make_batch(tmp_path / "b"), MANIFEST, [])
    assert findings == [] and counts == {"orders": 40, "order_items": 40, "order_payments": 40, "customers": 40, "products": 5}


def test_each_kind_of_damage_is_reported_in_one_run(gate, tmp_path):
    batch = make_batch(tmp_path / "b")
    edit(batch, "orders", lambda r: r + r[1:4])                                               # 3 dubbele orders
    edit(batch, "customers", lambda r: [r[0]] + [[x[0], x[1], x[2], x[3], ""] for x in r[1:11]] + r[11:])  # lege staat
    edit(batch, "order_items", lambda r: [r[0]] + [x[:5] + ["-5.00", x[6]] for x in r[1:3]] + r[3:])        # negatieve prijs
    edit(batch, "order_payments", lambda r: r + [["o999", "1", "boleto", "1", "10.00"]])       # betaling zonder order
    findings, _ = gate.run_gate(batch, MANIFEST, [])
    assert kinds(findings) == {
        ("duplicates", "orders", "unique_key"),
        ("null_spike", "customers", "required_not_null"),
        ("invalid_values", "order_items", "price_out_of_range"),
        ("referential_integrity", "order_payments", "fk_order_payments_orders"),
    }
    dup = next(f for f in findings if f["issue_type"] == "duplicates")
    assert (dup["n_affected"], dup["n_total"], len(dup["sample"])) == (3, 43, 3)


def test_renamed_column_is_drift_and_silences_noise_on_that_table(gate, tmp_path):
    batch = make_batch(tmp_path / "b")
    edit(batch, "orders", lambda r: [[c if c != "order_id" else "id" for c in r[0]]] + r[1:])
    findings, _ = gate.run_gate(batch, MANIFEST, [])
    # Geen null_spike op orders.order_id en geen kapotte verwijzingen naar orders: dat is ruis van dezelfde oorzaak.
    assert kinds(findings) == {("schema_drift", "orders", "missing_column"), ("schema_drift", "orders", "unexpected_column")}


def test_unreadable_values_are_drift_not_invalid_values(gate, tmp_path):
    batch = make_batch(tmp_path / "b")
    edit(batch, "order_items", lambda r: [r[0]] + [x[:5] + ["50,00", x[6]] for x in r[1:]])
    findings, _ = gate.run_gate(batch, MANIFEST, [])
    assert kinds(findings) == {("schema_drift", "order_items", "type_mismatch")}
    assert findings[0]["sample"][0] == "50,00"


def test_volume_is_judged_against_history(gate, tmp_path):
    batch = make_batch(tmp_path / "b")
    normal = [(f"batch_{i}", t, n) for i in range(4) for t, n in [("orders", 40), ("order_items", 40), ("order_payments", 40), ("customers", 40), ("products", 5)]]
    big = [(b, t, n * 10) for b, t, n in normal]
    assert gate.run_gate(batch, MANIFEST, normal)[0] == []
    assert gate.run_gate(batch, MANIFEST, big[:10])[0] == []          # te weinig historie om te oordelen
    findings, _ = gate.run_gate(batch, MANIFEST, big)
    assert {f["check_name"] for f in findings} == {"row_count_deviation"} and len(findings) == 5

    (batch / "products.csv").unlink()
    findings, _ = gate.run_gate(batch, MANIFEST, normal)
    assert kinds(findings) == {("volume_anomaly", "products", "missing_table")}
