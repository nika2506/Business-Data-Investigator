import json
import shutil
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
SEED_DB = DATA_DIR / "seed.sqlite"
EXPANDED_DB = DATA_DIR / "expanded.sqlite"
ADDITIONAL_DATA = DATA_DIR / "additional-records.json"

shutil.copy2(SEED_DB, EXPANDED_DB)

with ADDITIONAL_DATA.open(encoding="utf-8") as f:
    data = json.load(f)

with sqlite3.connect(EXPANDED_DB) as conn:
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executemany(
        """
        INSERT INTO customers (customer_id, segment)
        VALUES (:customer_id, :segment)
        """,
        data["customers"],
    )

    conn.executemany(
        """
        INSERT INTO orders
            (order_id, customer_id, order_date, amount_cents)
        VALUES
            (:order_id, :customer_id, :order_date, :amount_cents)
        """,
        data["orders"],
    )

    conn.executemany(
        """
        INSERT INTO refunds
            (refund_id, order_id, refund_date, amount_cents)
        VALUES
            (:refund_id, :order_id, :refund_date, :amount_cents)
        """,
        data["refunds"],
    )

    # Проверяем внешние ключи
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValueError(f"Foreign key violations: {violations}")

    # Проверяем, что возвраты не превышают сумму заказа
    invalid_refunds = conn.execute("""
        SELECT o.order_id
        FROM orders o
        LEFT JOIN refunds r ON r.order_id = o.order_id
        GROUP BY o.order_id, o.amount_cents
        HAVING COALESCE(SUM(r.amount_cents), 0) > o.amount_cents
    """).fetchall()

    if invalid_refunds:
        raise ValueError(f"Refunds exceed order amounts: {invalid_refunds}")

print(f"Expanded database created: {EXPANDED_DB}")