"""Integratietests over de componenten heen. Elke component heeft daarnaast eigen tests in zijn map."""
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for sub in ("source", "chaos", "pipeline", "healer", "console"):
    sys.path.insert(0, str(ROOT / sub))


@pytest.fixture(scope="session")
def batches(tmp_path_factory):
    """Zes schone weekbatches uit de synthetische bron."""
    base = tmp_path_factory.mktemp("src")
    os.environ.update(CONTRACT_PATH=str(ROOT / "contracts/landing_schema.yml"), SOURCE_WORK_DIR=str(base / "work"),
                      OUT_DIR=str(base / "out"), DATASET="synthetic", MAX_BATCHES="6")
    sys.modules.pop("source", None)
    src = importlib.import_module("source")
    src.bootstrap()
    while src.deliver_next():
        pass
    return sorted((base / "out").iterdir())


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Eigen datamap per test en vers geladen pipeline-modules."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTRACT_PATH", str(ROOT / "contracts/landing_schema.yml"))
    monkeypatch.setenv("CONSOLE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PARTIAL_RELEASE", "false")   # per test aan te zetten
    for leftover in ("QUARANTINE_DIR", "CANDIDATES_DIR", "LANDING_DIR", "PIPELINE_STATE_DIR", "WAREHOUSE_PATH"):
        monkeypatch.delenv(leftover, raising=False)
    for name in [m for m in sys.modules if m.startswith("sh_pipeline")]:
        del sys.modules[name]
    for d in ("landing", "quarantine", "candidates"):
        (tmp_path / d).mkdir()
    return tmp_path
