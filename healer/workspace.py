"""De werkruimte van de healer: een kopie van de batch in een in-memory DuckDB.

  original.*   de batch zoals aangeleverd, alleen-lezen
  work.*       de werkkopie waarop het herstelrecept draait

Na het laden staat externe toegang uit en is de configuratie vergrendeld. Daardoor kan
SQL van een agent niets buiten deze database lezen of schrijven, wat er ook in staat.
"""
from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import duckdb
import yaml

TABLE_NAME = re.compile(r"^[a-z_]+$")
WRITE_TARGET = re.compile(
    r"^\s*(create\s+or\s+replace\s+table\s+work\.\w+\s+as\b|update\s+work\.\w+\b|delete\s+from\s+work\.\w+\b"
    r"|insert\s+into\s+work\.\w+\b|alter\s+table\s+work\.\w+\b)", re.IGNORECASE)
MAX_CELL, MAX_ROWS = 80, 40


class RecipeError(Exception):
    pass


class Workspace:
    def __init__(self, bundle: Path):
        self.bundle = bundle
        self.contract = yaml.safe_load((bundle / "landing_schema.yml").read_text())["tables"]
        self.con = duckdb.connect(":memory:")
        self.con.execute("create schema original; create schema work")
        self.tables = []
        for f in sorted(bundle.glob("*.csv")):
            if f.stem in ("history_stats", "history_metrics") or not TABLE_NAME.match(f.stem) or f.stat().st_size == 0:
                continue
            self.con.execute(
                f"create table original.{f.stem} as select * from read_csv(?, all_varchar=true, header=true, null_padding=true)",
                [str(f)])
            self.con.execute(f"create table work.{f.stem} as select * from original.{f.stem}")
            self.tables.append(f.stem)
        self.con.execute("create table original.history_stats (batch_id varchar, table_name varchar, row_count bigint)")
        if (bundle / "history_stats.csv").exists():
            self.con.execute("insert into original.history_stats select * from read_csv(?, header=true)",
                             [str(bundle / "history_stats.csv")])
        self.con.execute("create table original.history_metrics (batch_id varchar, metric varchar, value double)")
        if (bundle / "history_metrics.csv").exists():
            self.con.execute("insert into original.history_metrics select * from read_csv(?, header=true)",
                             [str(bundle / "history_metrics.csv")])
        self.con.execute("set enable_external_access = false")
        self.con.execute("set lock_configuration = true")

    # ------------------------------------------------------------ lezen

    def _statements(self, sql: str) -> list:
        try:
            return self.con.extract_statements(sql)
        except duckdb.Error as exc:
            raise RecipeError(f"SQL is niet te lezen: {exc}")

    def query(self, sql: str) -> str:
        """Eén SELECT; geeft de eerste rijen als tekst terug."""
        statements = self._statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise RecipeError("run_sql accepteert precies één SELECT-statement")
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
        lines = [" | ".join(cols)]
        for row in rows[:MAX_ROWS]:
            lines.append(" | ".join("NULL" if v is None else str(v)[:MAX_CELL] for v in row))
        if len(rows) > MAX_ROWS:
            lines.append(f"... (alleen de eerste {MAX_ROWS} rijen getoond; verfijn de query of aggregeer)")
        return "\n".join(lines)

    def row_counts(self, schema: str = "work") -> dict[str, int]:
        return {t: self.con.execute(f"select count(*) from {schema}.{t}").fetchone()[0] for t in self._tables(schema)}

    def _tables(self, schema: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "select table_name from information_schema.tables where table_schema = ? order by 1", [schema]).fetchall()]

    def _columns(self, schema: str, table: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "select column_name from information_schema.columns where table_schema = ? and table_name = ? order by ordinal_position",
            [schema, table]).fetchall()]

    def describe(self) -> str:
        """Overzicht van contract en werkkopie, als startpunt voor een agent."""
        out = []
        for table, spec in self.contract.items():
            expected = ", ".join(f"{c} {v['type']}{' verplicht' if v.get('required') else ''}" for c, v in spec["columns"].items())
            out.append(f"{table} (sleutel: {', '.join(spec['key'])})\n  contract: {expected}")
            if table in self._tables("work"):
                n = self.con.execute(f"select count(*) from work.{table}").fetchone()[0]
                out.append(f"  work.{table}: {n} rijen, kolommen: {', '.join(self._columns('work', table))}")
            else:
                out.append(f"  work.{table}: ONTBREEKT in de aanlevering")
        return "\n".join(out)

    def conformance(self) -> list[str]:
        """Waar de werkkopie qua tabellen en kolommen nog afwijkt van het contract."""
        problems = []
        for table, spec in self.contract.items():
            if table not in self._tables("work"):
                problems.append(f"tabel work.{table} ontbreekt")
                continue
            have, want = self._columns("work", table), list(spec["columns"])
            if missing := [c for c in want if c not in have]:
                problems.append(f"work.{table} mist kolommen: {', '.join(missing)}")
            if extra := [c for c in have if c not in want]:
                problems.append(f"work.{table} heeft kolommen buiten het contract: {', '.join(extra)}")
        return problems

    # ------------------------------------------------------------ schrijven

    def check_recipe(self, statements: list[str]) -> None:
        """Een recept mag alleen tabellen in work wijzigen, met één statement per regel."""
        if not statements:
            raise RecipeError("het recept is leeg")
        for sql in statements:
            parsed = self._statements(sql)
            if len(parsed) != 1:
                raise RecipeError(f"precies één statement per recept-regel: {sql[:120]}")
            if not WRITE_TARGET.match(sql):
                raise RecipeError(
                    "een recept-regel moet beginnen met CREATE OR REPLACE TABLE work.<tabel> AS, UPDATE work.<tabel>, "
                    f"DELETE FROM work.<tabel>, INSERT INTO work.<tabel> of ALTER TABLE work.<tabel>: {sql[:120]}")

    def run_recipe(self, statements: list[str], commit: bool) -> str:
        """Voert het recept uit; zonder commit is het een proef die wordt teruggedraaid."""
        self.check_recipe(statements)
        before = self.row_counts()
        self.con.execute("begin")
        try:
            for sql in statements:
                try:
                    self.con.execute(sql)
                except duckdb.Error as exc:
                    raise RecipeError(f"fout in recept-regel `{sql[:120]}`: {exc}")
            after, problems = self.row_counts(), self.conformance()
            self.con.execute("commit" if commit else "rollback")
        except Exception:
            self.con.execute("rollback")
            raise
        lines = [f"{t}: {before.get(t, 0)} -> {after.get(t, 0)} rijen" for t in sorted(set(before) | set(after))]
        lines.append("afwijkingen van het contract: " + ("; ".join(problems) if problems else "geen"))
        return "\n".join(lines)

    def export(self, target: Path) -> None:
        """Schrijft de werkkopie atomair als kandidaat-batch."""
        tmp = target.parent / f".tmp-{target.name}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for table in self._tables("work"):
            cur = self.con.execute(f"select * from work.{table}")
            with open(tmp / f"{table}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([d[0] for d in cur.description])
                for row in cur.fetchall():
                    w.writerow(["" if v is None else v for v in row])
        shutil.copy(self.bundle / "_manifest.json", tmp / "_manifest.json")
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
