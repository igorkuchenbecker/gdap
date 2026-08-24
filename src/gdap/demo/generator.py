"""Demo data generator.

Produces a small but *realistic* multi-table commercial dataset — customers, products, regions and
transactions — deliberately seeded with the defects real data has:

* missing values in a revenue column and in customer e-mails
* exact duplicate transactions (a retried API call)
* category spellings that differ only by case/whitespace
* a handful of extreme orders (genuine outliers, not errors)
* orphan foreign keys (a customer that was never loaded)
* a few timestamps in the future (timezone bug at the source)
* a real business event: one region's revenue collapses in the final weeks

The point is that the platform must *find* these on its own. Everything is seeded, so two runs
produce byte-identical files and the demo is reproducible (§42).
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import sin
from pathlib import Path
from typing import Any

from gdap.observability.logging import get_logger

log = get_logger(__name__)

REGIONS = [
    ("R1", "North", "United States", "A. Silva", 1.40),
    ("R2", "South", "Brazil", "M. Costa", 1.00),
    ("R3", "East", "Germany", "K. Müller", 0.75),
    ("R4", "West", "Japan", "T. Nakamura", 0.90),
]

CATEGORIES = ["Hardware", "Software", "Services", "Accessories"]
CHANNELS = ["online", "retail", "partner", "direct"]
SEGMENTS = ["enterprise", "mid-market", "smb", "public sector"]
STATUSES = ["completed", "completed", "completed", "completed", "refunded", "pending"]


@dataclass(slots=True)
class DemoDataset:
    """Where the generated files live, plus what was deliberately broken in them."""

    directory: Path
    files: dict[str, Path]
    stats: dict[str, Any]

    def path(self, name: str) -> Path:
        return self.files[name]


def generate_demo_files(
    target: Path,
    *,
    days: int = 540,
    seed: int = 42,
    customers: int = 220,
    products: int = 60,
    orders_per_day: int = 14,
) -> DemoDataset:
    """Write ``customers.csv``, ``products.csv``, ``regions.csv`` and ``transactions.csv``."""
    rng = random.Random(seed)
    target = Path(target).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    end = date.today()
    start = end - timedelta(days=days)

    customer_rows = _customers(rng, customers, start)
    product_rows = _products(rng, products)
    region_rows = _regions()
    transaction_rows, defects = _transactions(
        rng,
        customer_rows,
        product_rows,
        start=start,
        days=days,
        orders_per_day=orders_per_day,
    )

    files = {
        "customers": _write_csv(target / "customers.csv", customer_rows),
        "products": _write_csv(target / "products.csv", product_rows),
        "regions": _write_csv(target / "regions.csv", region_rows),
        "transactions": _write_csv(target / "transactions.csv", transaction_rows),
    }
    stats = {
        "customers": len(customer_rows),
        "products": len(product_rows),
        "regions": len(region_rows),
        "transactions": len(transaction_rows),
        "period": f"{start.isoformat()} → {end.isoformat()}",
        "seeded_defects": defects,
    }
    log.info(
        "demo_data_generated",
        directory=str(target),
        **{k: v for k, v in stats.items() if isinstance(v, int)},
    )
    return DemoDataset(directory=target, files=files, stats=stats)


# ─────────────────────────────────────────── tables ────────────────────────────────────────


def _customers(rng: random.Random, count: int, start: date) -> list[dict[str, Any]]:
    first = ["Ana", "Bruno", "Chen", "Dara", "Elif", "Farid", "Greta", "Hugo", "Ines", "Jonas"]
    last = ["Silva", "Costa", "Müller", "Nakamura", "Okafor", "Rossi", "Novak", "Haddad"]
    rows = []
    for index in range(1, count + 1):
        name = f"{rng.choice(first)} {rng.choice(last)}"
        handle = name.lower().replace(" ", ".").replace("ü", "u")
        # ~3% of e-mails are malformed at the point of capture
        email = (
            f"{handle}{index}@example.com" if rng.random() > 0.03 else f"{handle}{index}(at)example"
        )
        region = rng.choice(REGIONS)
        rows.append(
            {
                "customer_id": f"C{index:05d}",
                "customer_name": name,
                "email": email if rng.random() > 0.04 else "",  # ~4% missing
                "segment": rng.choice(SEGMENTS),
                "country": region[2],
                "region": region[1],
                "signup_date": (start - timedelta(days=rng.randint(0, 900))).isoformat(),
                "credit_limit": rng.choice([5000, 10000, 25000, 50000, 100000]),
                "is_active": rng.random() > 0.12,
            }
        )
    return rows


def _products(rng: random.Random, count: int) -> list[dict[str, Any]]:
    adjectives = ["Compact", "Pro", "Ultra", "Lite", "Max", "Edge", "Core", "Prime"]
    nouns = ["Router", "Sensor", "Gateway", "Licence", "Support", "Cable", "Module", "Dashboard"]
    rows = []
    for index in range(1, count + 1):
        category = CATEGORIES[index % len(CATEGORIES)]
        cost = round(rng.uniform(12, 780), 2)
        rows.append(
            {
                "product_id": f"P{index:04d}",
                "product_name": f"{rng.choice(adjectives)} {rng.choice(nouns)} {index}",
                "category": category,
                "unit_cost": cost,
                "list_price": round(cost * rng.uniform(1.25, 2.4), 2),
                "active": rng.random() > 0.08,
            }
        )
    return rows


def _regions() -> list[dict[str, Any]]:
    return [
        {
            "region_id": region_id,
            "region": name,
            "country": country,
            "manager": manager,
            "target_multiplier": multiplier,
        }
        for region_id, name, country, manager, multiplier in REGIONS
    ]


def _transactions(
    rng: random.Random,
    customers: list[dict[str, Any]],
    products: list[dict[str, Any]],
    *,
    start: date,
    days: int,
    orders_per_day: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    defects: dict[str, Any] = {
        "missing_revenue": 0,
        "duplicate_rows": 0,
        "case_variant_regions": 0,
        "extreme_orders": 0,
        "orphan_customers": 0,
        "future_timestamps": 0,
        "revenue_shock_region": "South",
        "revenue_shock_from_day": days - 55,
    }
    order_number = 0

    for day_index in range(days):
        day = start + timedelta(days=day_index)
        weekday_factor = 0.55 if day.weekday() >= 5 else 1.0
        seasonal = 1 + 0.22 * sin(day_index / 28)
        growth = 1 + day_index * 0.0016
        volume = max(1, int(orders_per_day * weekday_factor * seasonal * rng.uniform(0.7, 1.3)))

        for _ in range(volume):
            order_number += 1
            customer = rng.choice(customers)
            product = rng.choice(products)
            region_id, region_name, _country, _manager, multiplier = rng.choice(REGIONS)

            quantity = max(1, int(rng.lognormvariate(1.0, 0.6)))
            unit_price = round(product["list_price"] * rng.uniform(0.92, 1.08), 2)
            discount = round(rng.choice([0, 0, 0, 0.05, 0.1, 0.15]), 2)
            revenue = round(
                quantity * unit_price * (1 - discount) * multiplier * seasonal * growth, 2
            )

            # a genuine business event: one region collapses in the final weeks
            if (
                region_name == defects["revenue_shock_region"]
                and day_index >= defects["revenue_shock_from_day"]
            ):
                revenue = round(revenue * 0.45, 2)

            # ~0.4% of orders are genuinely enormous (a distributor bulk purchase)
            if rng.random() < 0.004:
                quantity *= rng.randint(30, 90)
                revenue = round(revenue * quantity / max(quantity, 1) * rng.uniform(25, 60), 2)
                defects["extreme_orders"] += 1

            recorded_region = region_name
            if rng.random() < 0.06:  # inconsistent casing/whitespace from a legacy exporter
                recorded_region = rng.choice(
                    [region_name.lower(), f"{region_name} ", region_name.upper()]
                )
                defects["case_variant_regions"] += 1

            customer_id = customer["customer_id"]
            if rng.random() < 0.008:  # customer never loaded into the master table
                customer_id = f"C9{rng.randint(1000, 9999)}"
                defects["orphan_customers"] += 1

            timestamp = datetime.combine(day, datetime.min.time()).replace(
                hour=rng.randint(7, 21), minute=rng.randint(0, 59), tzinfo=UTC
            )
            if rng.random() < 0.001:  # timezone bug: a few records land in the future
                timestamp = datetime.now(UTC) + timedelta(days=rng.randint(1, 20))
                defects["future_timestamps"] += 1

            revenue_field: Any = revenue
            if rng.random() < 0.035:  # revenue not captured by the source system
                revenue_field = ""
                defects["missing_revenue"] += 1

            row = {
                "order_id": f"ORD{order_number:07d}",
                "order_timestamp": timestamp.isoformat(),
                "order_date": day.isoformat(),
                "customer_id": customer_id,
                "product_id": product["product_id"],
                "region_id": region_id,
                "region": recorded_region,
                "channel": rng.choice(CHANNELS),
                "quantity": quantity,
                "unit_price": unit_price,
                "discount_pct": discount,
                "revenue": revenue_field,
                "status": rng.choice(STATUSES),
            }
            rows.append(row)

            if rng.random() < 0.006:  # retried API call wrote the same transaction twice
                rows.append(dict(row))
                defects["duplicate_rows"] += 1

    return rows, defects


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    if not rows:
        raise ValueError(f"refusing to write an empty demo file: {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path
