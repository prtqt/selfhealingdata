"""Alle paden en instellingen van de pipeline komen uit de omgeving."""
import os
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
LANDING_DIR = Path(os.environ.get("LANDING_DIR", DATA / "landing"))
QUARANTINE_DIR = Path(os.environ.get("QUARANTINE_DIR", DATA / "quarantine"))
CANDIDATES_DIR = Path(os.environ.get("CANDIDATES_DIR", DATA / "candidates"))
STATE_DIR = Path(os.environ.get("PIPELINE_STATE_DIR", DATA / "pipeline"))
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", STATE_DIR / "warehouse.duckdb"))
DBT_DIR = Path(os.environ.get("DBT_DIR", Path(__file__).resolve().parents[1] / "dbt"))
CONTRACT_PATH = Path(os.environ.get("CONTRACT_PATH", "/contracts/landing_schema.yml"))
CONSOLE_URL = os.environ.get("CONSOLE_URL", "http://console:8000").rstrip("/")
# Bestellingen zonder bevinding alvast doorlaten als hoogstens dit aandeel van de batch is geraakt.
PARTIAL_RELEASE = os.environ.get("PARTIAL_RELEASE", "true").lower() == "true"
PARTIAL_MAX_SHARE = float(os.environ.get("PARTIAL_MAX_SHARE", "0.5"))
