"""Het warehouse: alleen de pipeline komt hier."""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

import duckdb

from . import config, gate

LIVE = ("clean", "healed")  # tellen mee voor de volume-historie


@contextlib.contextmanager
def pipeline_lock():
    """Eén pipeline-stap tegelijk: DuckDB staat één schrijver toe en dbt deelt één doelmap."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.STATE_DIR / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def connect():
    config.WAREHOUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(config.WAREHOUSE_PATH))
    try:
        init(con)
        yield con
    finally:
        con.close()


def init(con) -> None:
    con.execute("create schema if not exists raw")
    con.execute("create schema if not exists meta")
    con.execute("""create table if not exists meta.batch_registry (
        batch_id varchar primary key, status varchar, window_start varchar, window_end varchar,
        incident_id varchar, updated_at timestamptz default now())""")
    con.execute("create table if not exists meta.batch_stats (batch_id varchar, table_name varchar, row_count bigint)")
    con.execute("create table if not exists meta.batch_metrics (batch_id varchar, metric varchar, value double)")
    for table, spec in gate.load_contract().items():
        cols = ", ".join(f'"{c}" varchar' for c in spec["columns"])
        con.execute(f"create table if not exists raw.{table} ({cols}, batch_id varchar)")


def known_batches(con) -> dict[str, dict]:
    rows = con.execute("select batch_id, status, incident_id from meta.batch_registry").fetchall()
    return {b: {"status": s, "incident_id": i} for b, s, i in rows}


def set_status(con, manifest: dict, status: str, incident_id: str | None = None) -> None:
    con.execute(
        """insert into meta.batch_registry (batch_id, status, window_start, window_end, incident_id)
           values (?, ?, ?, ?, ?)
           on conflict (batch_id) do update set status = excluded.status,
             incident_id = coalesce(excluded.incident_id, meta.batch_registry.incident_id),
             updated_at = now()""",
        [manifest["batch_id"], status, manifest["window_start"], manifest["window_end"], incident_id],
    )


def history(con, before_batch: str) -> list[tuple]:
    """Rijaantallen van goede batches vóór deze batch, als vergelijking voor het volume."""
    return con.execute(
        f"""select s.batch_id, s.table_name, s.row_count from meta.batch_stats s
            join meta.batch_registry r using (batch_id)
            where r.status in {LIVE} and s.batch_id < ? order by s.batch_id""",
        [before_batch],
    ).fetchall()


def metrics_history(con, before_batch: str) -> list[tuple]:
    """Kengetallen van goede batches vóór deze batch, als vergelijking voor verschuivingen."""
    return con.execute(
        f"""select m.batch_id, m.metric, m.value from meta.batch_metrics m
            join meta.batch_registry r using (batch_id)
            where r.status in {LIVE} and m.batch_id < ? and m.value is not null order by m.batch_id""",
        [before_batch],
    ).fetchall()


def load_batch(con, batch_dir: Path, manifest: dict, status: str) -> dict[str, int]:
    """Laadt een batch in raw (idempotent per batch_id) en zet de status."""
    batch_id, counts = manifest["batch_id"], {}
    con.execute("begin")
    try:
        con.execute("delete from meta.batch_stats where batch_id = ?", [batch_id])
        for table, spec in gate.load_contract().items():
            con.execute(f"delete from raw.{table} where batch_id = ?", [batch_id])
            csv_path = batch_dir / f"{table}.csv"
            if csv_path.exists() and csv_path.stat().st_size > 0:
                select, _ = gate.conformed_select(con, csv_path, list(spec["columns"]))
                con.execute(f"insert into raw.{table} select *, ? from ({select})", [batch_id])
            counts[table] = con.execute(f"select count(*) from raw.{table} where batch_id = ?", [batch_id]).fetchone()[0]
            con.execute("insert into meta.batch_stats values (?, ?, ?)", [batch_id, table, counts[table]])
        con.execute("delete from meta.batch_metrics where batch_id = ?", [batch_id])
        con.execute("""insert into meta.batch_metrics
                       select ?, 'avg_price', avg(try_cast(price as double)) from raw.order_items where batch_id = ?""",
                    [batch_id, batch_id])
        set_status(con, manifest, status)
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    return counts


def build_marts() -> None:
    gate.run_dbt(["build", "--target", "warehouse", "--select", "tag:warehouse"],
                 {"WAREHOUSE_PATH": str(config.WAREHOUSE_PATH)})
