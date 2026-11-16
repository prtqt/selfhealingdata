import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTRACT_PATH", str(ROOT / "contracts/landing_schema.yml"))
    monkeypatch.setenv("SOURCE_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("DATASET", "synthetic")
    monkeypatch.setenv("MAX_BATCHES", "3")
    sys.path.insert(0, str(ROOT / "source"))
    sys.modules.pop("source", None)
    return importlib.import_module("source")


def test_bootstrap_and_deliver(monkeypatch, tmp_path):
    src = load(monkeypatch, tmp_path)
    src.bootstrap()
    staged = sorted(p.name for p in (tmp_path / "work/staged").iterdir())
    assert staged == ["batch_20170206", "batch_20170213", "batch_20170220"]

    assert src.deliver_next() == "batch_20170206"
    batch = tmp_path / "out/batch_20170206"
    contract = src.load_contract()
    for table, spec in contract.items():
        header = (batch / f"{table}.csv").read_text().splitlines()[0].split(",")
        assert header == list(spec["columns"])
    assert json.loads((batch / "_manifest.json").read_text())["window_end"] == "2017-02-13"
    assert not any(p.name.startswith(".tmp") for p in (tmp_path / "out").iterdir())


def test_feed_is_ordered_and_finite(monkeypatch, tmp_path):
    src = load(monkeypatch, tmp_path)
    src.bootstrap()
    assert [src.deliver_next() for _ in range(4)] == ["batch_20170206", "batch_20170213", "batch_20170220", None]
