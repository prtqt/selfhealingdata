"""Deterministische verstoringen op een batchmap met CSV's.

Elke injector verandert bestanden in de map en geeft terug wat hij deed.
Geen kennis van de pipeline: alleen bestandsnamen en kolomnamen uit het landing-contract.
"""
from __future__ import annotations

import csv
import random
from datetime import datetime
from pathlib import Path


def read(batch: Path, table: str) -> tuple[list[str], list[list[str]]]:
    with open(batch / f"{table}.csv", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def write(batch: Path, table: str, header: list[str], rows: list[list[str]]) -> None:
    with open(batch / f"{table}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def pick(rng: random.Random, rows: list, fraction: float) -> list[int]:
    n = max(1, round(len(rows) * fraction))
    return rng.sample(range(len(rows)), min(n, len(rows)))


def _edit_column(batch, rng, table, column, fraction, fn, only=lambda v: True):
    header, rows = read(batch, table)
    i = header.index(column)
    eligible = [r for r in range(len(rows)) if only(rows[r][i])]
    chosen = [eligible[k] for k in pick(rng, eligible, fraction)] if eligible else []
    for r in chosen:
        rows[r][i] = fn(rows[r][i])
    write(batch, table, header, rows)
    return {"table": table, "column": column, "n_affected": len(chosen)}


# ------------------------------------------------------------ null_spike

def nulls(batch, rng, fraction):
    table, column = rng.choice([("orders", "customer_id"), ("orders", "order_purchase_timestamp"),
                                ("order_items", "price"), ("order_items", "product_id"),
                                ("order_payments", "payment_value")])
    return _edit_column(batch, rng, table, column, max(fraction, 0.05), lambda v: "")


# ------------------------------------------------------------ duplicates

def duplicate_rows(batch, rng, fraction):
    table = rng.choice(["orders", "order_items", "order_payments"])
    header, rows = read(batch, table)
    extra = [list(rows[i]) for i in pick(rng, rows, fraction)]
    rows += extra
    rng.shuffle(rows)
    write(batch, table, header, rows)
    return {"table": table, "column": None, "n_affected": len(extra)}


# ------------------------------------------------------------ invalid_values

def negative_price(batch, rng, fraction):
    return _edit_column(batch, rng, "order_items", "price", fraction, lambda v: f"-{v}")


def amount_x1000(batch, rng, fraction):
    table, column, floor = rng.choice([("order_items", "price", 10.0), ("order_payments", "payment_value", 20.0)])
    return _edit_column(batch, rng, table, column, fraction, lambda v: f"{float(v) * 1000:.2f}",
                        only=lambda v: _num(v) is not None and _num(v) > floor)


def future_dates(batch, rng, fraction):
    def shift(v):
        d = datetime.fromisoformat(v)
        return d.replace(year=d.year + 3).strftime("%Y-%m-%d %H:%M:%S")
    return _edit_column(batch, rng, "orders", "order_purchase_timestamp", fraction, shift, only=lambda v: bool(v))


def unknown_status(batch, rng, fraction):
    return _edit_column(batch, rng, "orders", "order_status", fraction, lambda v: rng.choice(["entregue", "UNKNOWN", "9"]))


def _num(v):
    try:
        return float(v)
    except ValueError:
        return None


# ------------------------------------------------------------ referential_integrity

def drop_parent_orders(batch, rng, fraction):
    header, rows = read(batch, "orders")
    gone = set(pick(rng, rows, fraction))
    write(batch, "orders", header, [r for i, r in enumerate(rows) if i not in gone])
    return {"table": "orders", "column": "order_id", "n_affected": len(gone)}


def scramble_product_ids(batch, rng, fraction):
    return _edit_column(batch, rng, "order_items", "product_id", fraction, lambda v: "%032x" % rng.getrandbits(128))


# ------------------------------------------------------------ volume_anomaly

def truncate_batch(batch, rng, fraction):
    """Houdt ongeveer 15% van de orders over, met de bijbehorende regels, zodat alleen het volume afwijkt."""
    header, orders = read(batch, "orders")
    keep = [orders[i] for i in pick(rng, orders, rng.uniform(0.08, 0.2))]
    order_ids = {r[header.index("order_id")] for r in keep}
    customer_ids = {r[header.index("customer_id")] for r in keep}
    write(batch, "orders", header, keep)
    product_ids = set()
    for table, col, allowed in [("order_items", "order_id", order_ids), ("order_payments", "order_id", order_ids),
                                ("customers", "customer_id", customer_ids)]:
        h, rows = read(batch, table)
        rows = [r for r in rows if r[h.index(col)] in allowed]
        if table == "order_items":
            product_ids = {r[h.index("product_id")] for r in rows}
        write(batch, table, h, rows)
    h, rows = read(batch, "products")
    write(batch, "products", h, [r for r in rows if r[0] in product_ids])
    return {"table": "orders", "column": None, "n_affected": len(orders) - len(keep)}


def missing_table(batch, rng, fraction):
    table = rng.choice(["products", "order_payments", "customers"])
    (batch / f"{table}.csv").unlink()
    return {"table": table, "column": None, "n_affected": 0}


# ------------------------------------------------------------ schema_drift

def rename_column(batch, rng, fraction):
    table, column, new = rng.choice([("order_payments", "payment_value", "amount"),
                                     ("orders", "order_purchase_timestamp", "purchase_ts"),
                                     ("order_items", "price", "unit_price"),
                                     ("customers", "customer_state", "state")])
    header, rows = read(batch, table)
    header[header.index(column)] = new
    write(batch, table, header, rows)
    return {"table": table, "column": column, "n_affected": len(rows), "renamed_to": new}


def decimal_comma(batch, rng, fraction):
    table, column = rng.choice([("order_items", "price"), ("order_items", "freight_value"),
                                ("order_payments", "payment_value")])
    return _edit_column(batch, rng, table, column, 1.0, lambda v: v.replace(".", ","))


def date_format(batch, rng, fraction):
    def fmt(v):
        return datetime.fromisoformat(v).strftime("%d/%m/%Y %H:%M") if v else v
    return _edit_column(batch, rng, "orders", "order_purchase_timestamp", 1.0, fmt)


# ------------------------------------------------------------ distribution_shift

def price_inflation(batch, rng, fraction):
    """Alle prijzen een vaste factor hoger of lager: elke waarde blijft geldig, het gemiddelde niet."""
    factor = rng.choice([2.0, 2.5, 0.4])
    done = _edit_column(batch, rng, "order_items", "price", 1.0, lambda v: f"{float(v) * factor:.2f}",
                        only=lambda v: _num(v) is not None)
    return {**done, "factor": factor}


VARIANTS = {
    "null_spike": {"nulls": nulls},
    "duplicates": {"duplicate_rows": duplicate_rows},
    "invalid_values": {"negative_price": negative_price, "amount_x1000": amount_x1000,
                       "future_dates": future_dates, "unknown_status": unknown_status},
    "referential_integrity": {"drop_parent_orders": drop_parent_orders, "scramble_product_ids": scramble_product_ids},
    "volume_anomaly": {"truncate_batch": truncate_batch, "missing_table": missing_table},
    "schema_drift": {"rename_column": rename_column, "decimal_comma": decimal_comma, "date_format": date_format},
    "distribution_shift": {"price_inflation": price_inflation},
}


def inject(batch: Path, issue_type: str, rng: random.Random, variant: str | None = None,
           fraction: float | None = None) -> dict:
    variants = VARIANTS[issue_type]
    variant = variant if variant in variants else rng.choice(sorted(variants))
    fraction = min(max(fraction if fraction is not None else rng.uniform(0.05, 0.25), 0.03), 0.5)
    result = variants[variant](batch, rng, fraction)
    return {"issue_type": issue_type, "variant": variant, "fraction": round(fraction, 3), **result}
