"""Build the fictional sample database.

Every name, city, figure, and date is invented and generated from a fixed
random seed, so the database is byte-for-byte reproducible. No real data.
Run:  python make_db.py [output_path]
"""
import random
import sqlite3
import sys
from datetime import date, timedelta

SEED = 42
SCHEMA_PATH = "schema.sql"

FIRST_NAMES = [
    "Aria", "Blake", "Cleo", "Dario", "Elif", "Farah", "Gus", "Hana",
    "Ivo", "Juno", "Kira", "Lena", "Milo", "Nadia", "Omar", "Priya",
    "Quinn", "Rhea", "Sana", "Tariq", "Uma", "Vera", "Wren", "Yusuf",
    "Zara",
]
LAST_NAMES = [
    "Alder", "Bexley", "Corvin", "Dawlish", "Elmsworth", "Fenwick",
    "Grimsby", "Halloway", "Inkwell", "Juniper", "Kestrel", "Larkspur",
    "Marlowe", "Nightingale", "Osborne", "Pemberton", "Quill", "Ravenshaw",
    "Sutter", "Thistledown", "Underwood", "Vesper", "Whitlock", "Yardley",
    "Zephyr",
]
CITIES = [
    "Austin", "Denver", "Portland", "Seattle", "Chicago",
    "Boston", "Miami", "Nashville", "Phoenix", "Minneapolis",
]
CHANNELS = ["Email", "Social", "Search", "Display"]
CATEGORIES = ["Apparel", "Electronics", "Home", "Beauty", "Sports"]
# Typical basket sizes per category (fictional).
CATEGORY_PRICE = {
    "Apparel": (25, 120),
    "Electronics": (80, 900),
    "Home": (30, 400),
    "Beauty": (12, 90),
    "Sports": (20, 250),
}

DATA_START = date(2026, 1, 1)
DATA_END = date(2026, 9, 20)  # fixed end date: no dependence on "today"


def build(db_path: str, seed: int = SEED) -> str:
    rng = random.Random(seed)

    customers = []
    for i in range(1, 201):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        city = rng.choice(CITIES)
        signup = DATA_START + timedelta(days=rng.randint(0, 200))
        customers.append((i, name, city, signup.isoformat()))

    campaigns = []
    cid = 1
    for channel in CHANNELS:
        for n in range(1, 4):
            start = DATA_START + timedelta(days=rng.randint(0, 120))
            end = start + timedelta(days=rng.randint(30, 120))
            spend = round(rng.uniform(4000, 30000), 2)
            campaigns.append(
                (cid, f"{channel} Push {n}", channel,
                 start.isoformat(), end.isoformat(), spend)
            )
            cid += 1

    orders = []
    n_days = (DATA_END - DATA_START).days
    for i in range(1, 1501):
        customer_id = rng.randint(1, 200)
        campaign_id = rng.choice([c[0] for c in campaigns] + [None])
        order_date = (DATA_START + timedelta(days=rng.randint(0, n_days))).isoformat()
        category = rng.choice(CATEGORIES)
        lo, hi = CATEGORY_PRICE[category]
        amount = round(rng.uniform(lo, hi), 2)
        orders.append((i, customer_id, campaign_id, order_date, category, amount))

    with open(SCHEMA_PATH) as f:
        schema = f.read()

    import os
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)", customers)
    conn.executemany(
        "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?)", campaigns)
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", orders)
    conn.commit()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("customers", "campaigns", "orders")
    }
    conn.close()
    print(f"Wrote {db_path}: {counts} (SIMULATED DATA, seed={seed})")
    return db_path


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "sample.db")
