"""source: speelt het bronsysteem na.

  bootstrap  haalt de Olist-data op (Kaggle of synthetisch) en knipt die in weekbatches
  feed       levert elke FEED_INTERVAL seconden de volgende batch af in OUT_DIR
  run        bootstrap (indien nodig) gevolgd door feed

Kent alleen het landing-contract en zijn eigen mappen.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import yaml

CONTRACT = Path(os.environ.get("CONTRACT_PATH", "/contracts/landing_schema.yml"))
WORK_DIR = Path(os.environ.get("SOURCE_WORK_DIR", "/work"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
DATASET = os.environ.get("DATASET", "kaggle")
START_DATE = os.environ.get("START_DATE", "2017-02-06")
MAX_BATCHES = int(os.environ.get("MAX_BATCHES", "0"))
FEED_INTERVAL = int(os.environ.get("FEED_INTERVAL", "120"))

KAGGLE_FILES = {
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "order_payments": "olist_order_payments_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "products": "olist_products_dataset.csv",
}


def log(msg: str) -> None:
    print(f"[source] {msg}", flush=True)


def load_contract(path: Path = CONTRACT) -> dict:
    return yaml.safe_load(path.read_text())["tables"]


# ---------------------------------------------------------------- brondata

def download_kaggle(raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    if all((raw_dir / f).exists() for f in KAGGLE_FILES.values()):
        return
    if not (os.environ.get("KAGGLE_API_TOKEN") or (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"))):
        sys.exit(
            "[source] Geen Olist-bestanden en geen Kaggle-token gevonden.\n"
            "  - zet KAGGLE_USERNAME en KAGGLE_KEY in .env, of\n"
            f"  - zet de CSV's van kaggle.com/datasets/olistbr/brazilian-ecommerce in {raw_dir}, of\n"
            "  - zet DATASET=synthetic om met nagemaakte data te draaien."
        )
    log("download olistbr/brazilian-ecommerce van Kaggle")
    subprocess.run(
        ["kaggle", "datasets", "download", "-d", "olistbr/brazilian-ecommerce", "-p", str(raw_dir), "--unzip"],
        check=True,
    )


def generate_synthetic(raw_dir: Path, weeks: int = 30, orders_per_week: int = 250, seed: int = 7) -> None:
    """Maakt CSV's met dezelfde namen en kolommen als Olist, zodat de rest gelijk blijft."""
    import csv

    raw_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    hexid = lambda: "%032x" % rng.getrandbits(128)
    start = datetime.fromisoformat(START_DATE)
    fmt = "%Y-%m-%d %H:%M:%S"
    categories = ["beleza_saude", "esporte_lazer", "informatica_acessorios", "moveis_decoracao", "brinquedos", ""]
    states = ["SP", "RJ", "MG", "RS", "PR", "BA"]
    products = [
        [hexid(), rng.choice(categories), rng.randint(100, 9000), rng.randint(10, 80), rng.randint(2, 40), rng.randint(10, 60)]
        for _ in range(400)
    ]
    sellers = [hexid() for _ in range(40)]
    orders, items, payments, customers = [], [], [], []
    for w in range(weeks):
        n = int(orders_per_week * (1 + 0.01 * w) * rng.uniform(0.9, 1.1))
        for _ in range(n):
            oid, cid = hexid(), hexid()
            ts = start + timedelta(weeks=w, seconds=rng.randint(0, 7 * 86400 - 1))
            status = rng.choices(["delivered", "shipped", "canceled", "invoiced"], [90, 5, 3, 2])[0]
            delivered = (ts + timedelta(days=rng.randint(3, 20))).strftime(fmt) if status == "delivered" else ""
            orders.append([oid, cid, status, ts.strftime(fmt), (ts + timedelta(hours=1)).strftime(fmt),
                           (ts + timedelta(days=2)).strftime(fmt), delivered, (ts + timedelta(days=25)).strftime(fmt)])
            customers.append([cid, hexid(), "%05d" % rng.randint(1000, 99999), "cidade", rng.choice(states)])
            total = 0.0
            for i in range(rng.choices([1, 2, 3], [80, 15, 5])[0]):
                price = round(rng.lognormvariate(4.3, 0.8), 2)
                freight = round(rng.uniform(5, 45), 2)
                total += price + freight
                items.append([oid, i + 1, rng.choice(products)[0], rng.choice(sellers),
                              (ts + timedelta(days=5)).strftime(fmt), f"{price:.2f}", f"{freight:.2f}"])
            payments.append([oid, 1, rng.choice(["credit_card", "boleto", "voucher", "debit_card"]),
                             rng.randint(1, 10), f"{total:.2f}"])
    contract = load_contract()
    for table, rows in [("orders", orders), ("order_items", items), ("order_payments", payments),
                        ("customers", customers), ("products", products)]:
        with open(raw_dir / KAGGLE_FILES[table], "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(list(contract[table]["columns"]))
            wr.writerows(rows)
    log(f"synthetische dataset gemaakt: {len(orders)} orders over {weeks} weken")


# ---------------------------------------------------------------- batches

def split_into_batches(raw_dir: Path, staged_dir: Path) -> int:
    contract = load_contract()
    con = duckdb.connect()
    for table, fname in KAGGLE_FILES.items():
        cols = ", ".join(f'"{c}"' for c in contract[table]["columns"])
        con.execute(
            f"create table {table} as select {cols} from read_csv(?, all_varchar=true, header=true)",
            [str(raw_dir / fname)],
        )
    first = date.fromisoformat(START_DATE)
    last = con.execute("select max(try_cast(order_purchase_timestamp as timestamp))::date from orders").fetchone()[0]
    staged_dir.mkdir(parents=True, exist_ok=True)
    n, ws = 0, first
    while ws + timedelta(days=7) <= last and (not MAX_BATCHES or n < MAX_BATCHES):
        we = ws + timedelta(days=7)
        batch_id = f"batch_{ws:%Y%m%d}"
        tmp = staged_dir / f".tmp-{batch_id}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        con.execute(
            "create or replace temp table b_orders as select * from orders "
            "where try_cast(order_purchase_timestamp as timestamp) >= ? and try_cast(order_purchase_timestamp as timestamp) < ?",
            [ws, we],
        )
        selects = {
            "orders": "select * from b_orders",
            "order_items": "select * from order_items where order_id in (select order_id from b_orders)",
            "order_payments": "select * from order_payments where order_id in (select order_id from b_orders)",
            "customers": "select * from customers where customer_id in (select customer_id from b_orders)",
            "products": "select * from products where product_id in (select product_id from order_items "
                        "where order_id in (select order_id from b_orders))",
        }
        for table, sql in selects.items():
            con.execute(f"copy ({sql}) to '{tmp / (table + '.csv')}' (header, delimiter ',')")
        (tmp / "_manifest.json").write_text(json.dumps(
            {"batch_id": batch_id, "window_start": ws.isoformat(), "window_end": we.isoformat()}))
        shutil.rmtree(staged_dir / batch_id, ignore_errors=True)
        tmp.rename(staged_dir / batch_id)
        n, ws = n + 1, we
    return n


def bootstrap() -> None:
    raw_dir, staged_dir = WORK_DIR / "raw", WORK_DIR / "staged"
    if (WORK_DIR / "_bootstrapped").exists():
        return
    if DATASET == "synthetic":
        generate_synthetic(raw_dir)
    else:
        download_kaggle(raw_dir)
    n = split_into_batches(raw_dir, staged_dir)
    (WORK_DIR / "_bootstrapped").write_text(str(n))
    log(f"{n} weekbatches klaargezet vanaf {START_DATE}")


def deliver_next() -> str | None:
    """Verplaatst de oudste klaargezette batch atomair naar OUT_DIR."""
    staged = sorted(p for p in (WORK_DIR / "staged").iterdir() if p.is_dir() and not p.name.startswith("."))
    if not staged:
        return None
    batch = staged[0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / f".tmp-{batch.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(batch, tmp)
    tmp.rename(OUT_DIR / batch.name)
    shutil.rmtree(batch)
    return batch.name


def feed() -> None:
    log(f"lever elke {FEED_INTERVAL}s een batch af in {OUT_DIR}")
    while True:
        name = deliver_next()
        if name is None:
            log("alle batches afgeleverd")
            return
        log(f"afgeleverd: {name}")
        time.sleep(FEED_INTERVAL)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("bootstrap", "run"):
        bootstrap()
    if cmd in ("feed", "run"):
        feed()
    if cmd == "once":
        bootstrap()
        log(f"afgeleverd: {deliver_next()}")
