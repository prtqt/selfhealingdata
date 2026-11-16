"""De poort: beoordeelt één batch in een wegwerp-DuckDB met de dbt-controles.

Invoer is een map met CSV's, het manifest en de historie van eerdere goede batches.
Uitvoer is de lijst bevindingen. De poort weet niet waar de batch vandaan komt
(landing of kandidaat van de healer); daardoor gelden voor beide dezelfde regels.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import duckdb
import yaml

from . import config


def load_contract() -> dict:
    return yaml.safe_load(config.CONTRACT_PATH.read_text())["tables"]


def conformed_select(con: duckdb.DuckDBPyConnection, csv_path: Path, columns: list[str]) -> tuple[str, list[str]]:
    """SELECT die een CSV als tekst leest in de kolommen van het contract.

    Ontbrekende kolommen worden NULL, extra kolommen vallen weg. Geeft ook de
    werkelijk aangeleverde kolommen terug, voor de schema-driftcontrole.
    """
    reader = f"read_csv('{csv_path}', all_varchar=true, header=true, null_padding=true)"
    actual = [r[0] for r in con.execute(f"describe select * from {reader}").fetchall()]
    exprs = [f'"{c}"' if c in actual else f'null::varchar as "{c}"' for c in columns]
    return f"select {', '.join(exprs)} from {reader}", actual


def stage_batch(con, batch_dir: Path, manifest: dict, history: list[tuple], contract: dict) -> dict[str, int]:
    con.execute("create schema if not exists landing")
    con.execute("create or replace table landing.columns_actual (table_name varchar, column_name varchar)")
    con.execute("create or replace table landing.files (table_name varchar, present boolean, row_count bigint)")
    con.execute("create or replace table landing.contract (table_name varchar, column_name varchar, data_type varchar, required boolean)")
    con.execute("create or replace table landing.history (batch_id varchar, table_name varchar, row_count bigint)")
    con.execute("create or replace table landing.manifest as select ?::varchar as batch_id, ?::varchar as window_start, ?::varchar as window_end",
                [manifest["batch_id"], manifest["window_start"], manifest["window_end"]])
    if history:
        con.executemany("insert into landing.history values (?, ?, ?)", history)
    row_counts = {}
    for table, spec in contract.items():
        columns = list(spec["columns"])
        for col, c in spec["columns"].items():
            con.execute("insert into landing.contract values (?, ?, ?, ?)", [table, col, c["type"], bool(c.get("required"))])
        csv_path = batch_dir / f"{table}.csv"
        present = csv_path.exists() and csv_path.stat().st_size > 0
        if present:
            select, actual = conformed_select(con, csv_path, columns)
            con.execute(f"create or replace table landing.{table} as {select}")
            con.executemany("insert into landing.columns_actual values (?, ?)", [[table, a] for a in actual])
        else:
            cols = ", ".join(f'"{c}" varchar' for c in columns)
            con.execute(f"create or replace table landing.{table} ({cols})")
        n = con.execute(f"select count(*) from landing.{table}").fetchone()[0]
        con.execute("insert into landing.files values (?, ?, ?)", [table, present, n])
        row_counts[table] = n
    return row_counts


def batch_impact(batch_dir: Path, findings: list[dict]) -> dict:
    """Wat er zakelijk in deze batch zit: bestellingen, omzet en de rapporten die erop wachten."""
    contract, con = load_contract(), duckdb.connect()
    def scalar(table: str, expr: str):
        path = batch_dir / f"{table}.csv"
        if not path.exists() or path.stat().st_size == 0:
            return None
        select, _ = conformed_select(con, path, list(contract[table]["columns"]))
        return con.execute(f"select {expr} from ({select})").fetchone()[0]
    revenue = scalar("order_payments", "sum(try_cast(payment_value as double))")
    return {"orders": int(scalar("orders", "count(distinct order_id)") or 0),
            "revenue": round(float(revenue), 2) if revenue is not None else None,
            "affected_rows": max((f["n_affected"] for f in findings), default=0),
            "waiting_reports": WAITING_REPORTS}


# Rapporten die uit de marts worden gevoed en dus wachten zolang een batch in quarantaine staat.
WAITING_REPORTS = ["Omzet per dag", "Bestellingen per klantregio", "Productassortiment"]


def run_dbt(args: list[str], env_extra: dict[str, str]) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(config.DBT_DIR),
        "DBT_TARGET_PATH": str(config.STATE_DIR / "dbt_target"),
        "DBT_LOG_PATH": str(config.STATE_DIR / "dbt_logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        **env_extra,
    }
    cmd = ["dbt", "--no-use-colors", "--quiet", *args, "--project-dir", str(config.DBT_DIR),
           "--vars", json.dumps({"contract": load_contract()})]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"dbt {' '.join(args)} mislukt:\n{res.stdout[-4000:]}\n{res.stderr[-2000:]}")


def run_gate(batch_dir: Path, manifest: dict, history: list[tuple]) -> tuple[list[dict], dict[str, int]]:
    """Geeft (bevindingen, rijaantallen per tabel). Geen bevindingen betekent: batch is goed."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    db_path = config.STATE_DIR / "gate.duckdb"
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        row_counts = stage_batch(con, batch_dir, manifest, history, load_contract())
    finally:
        con.close()
    run_dbt(["run", "--target", "gate", "--select", "tag:gate"], {"GATE_DB_PATH": str(db_path)})
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cur = con.execute("select * from dq.dq_findings")
        names = [d[0] for d in cur.description]
        findings = [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        con.close()
    for f in findings:
        f["sample"] = [str(s) for s in json.loads(f["sample"] or "[]")]
        f["n_affected"], f["n_total"] = int(f["n_affected"]), int(f["n_total"])
        f["pct_affected"] = float(f["pct_affected"]) if f["pct_affected"] is not None else None
    db_path.unlink(missing_ok=True)
    return findings, row_counts
