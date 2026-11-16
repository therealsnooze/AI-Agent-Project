"""Per-session sandbox: a private copy of the seed files and a freshly
generated store database, so visitors to a public deployment can't affect
each other and every demo starts from the same state."""

from __future__ import annotations

import random
import shutil
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    city TEXT NOT NULL,
    joined_on TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','shipped','delivered','cancelled'))
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
"""

PRODUCTS = [
    ("BEAN-001", "Sunrise Blend 12oz", "beans", 16.50, 84),
    ("BEAN-002", "Ethiopia Yirgacheffe 12oz", "beans", 19.00, 42),
    ("BEAN-003", "Colombia Huila 12oz", "beans", 17.50, 65),
    ("BEAN-004", "Midnight Espresso 12oz", "beans", 18.00, 7),
    ("BEAN-005", "Decaf Swiss Water 12oz", "beans", 17.00, 23),
    ("BEAN-006", "Guatemala Antigua 12oz", "beans", 18.50, 0),
    ("BEAN-007", "Holiday Spice Blend 12oz", "beans", 19.50, 31),
    ("GEAR-001", "Ceramic Pour-Over Dripper", "equipment", 28.00, 15),
    ("GEAR-002", "Burr Grinder Pro", "equipment", 129.00, 4),
    ("GEAR-003", "Gooseneck Kettle", "equipment", 45.00, 11),
    ("GEAR-004", "French Press 34oz", "equipment", 35.00, 9),
    ("GEAR-005", "Paper Filters (100)", "equipment", 7.50, 140),
    ("MERCH-001", "Brightside Mug", "merch", 14.00, 52),
    ("MERCH-002", "Canvas Tote Bag", "merch", 18.00, 3),
    ("SUB-001", "Monthly Bean Subscription", "subscription", 32.00, 999),
]

FIRST = ["Ava", "Liam", "Mia", "Noah", "Sofia", "Ethan", "Isabella", "Mateo", "Chloe", "Lucas", "Zoe", "Elijah",
         "Priya", "Kenji", "Amara", "Diego", "Hannah", "Omar", "Grace", "Ravi", "Lena", "Marcus", "Yuki", "Nadia"]
LAST = ["Garcia", "Nguyen", "Patel", "Kim", "Johnson", "Okafor", "Silva", "Cohen", "Martinez", "Rossi", "Chen",
        "Walker", "Haddad", "Novak", "Reyes", "Fischer"]
CITIES = ["Los Angeles", "Portland", "Austin", "Seattle", "Denver", "Chicago", "San Diego", "Phoenix", "Boston"]


def build_database(path: Path, today: date | None = None, seed: int = 7) -> None:
    """Deterministic sample data, with dates relative to today so questions like
    'last month' always have answers."""
    today = today or date.today()
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO products (sku, name, category, price, stock) VALUES (?,?,?,?,?)", PRODUCTS)
        customers, seen = [], set()
        while len(customers) < 40:
            first, last = rng.choice(FIRST), rng.choice(LAST)
            if (first, last) in seen:
                continue
            seen.add((first, last))
            joined = today - timedelta(days=rng.randint(20, 400))
            customers.append((f"{first} {last}", f"{first}.{last}@example.com".lower(), rng.choice(CITIES), joined.isoformat()))
        conn.executemany("INSERT INTO customers (name, email, city, joined_on) VALUES (?,?,?,?)", customers)

        product_rows = conn.execute("SELECT id, price FROM products").fetchall()
        weights = [8, 5, 6, 7, 3, 2, 4, 3, 1, 2, 2, 6, 3, 1, 4]
        order_id = 0
        for days_ago in range(120, -1, -1):
            for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
                order_id += 1
                d = today - timedelta(days=days_ago)
                if days_ago < 3:
                    status = rng.choice(["pending", "pending", "shipped"])
                elif days_ago < 10:
                    status = rng.choice(["shipped", "delivered", "delivered"])
                else:
                    status = rng.choice(["delivered"] * 9 + ["cancelled"])
                conn.execute("INSERT INTO orders (id, customer_id, order_date, status) VALUES (?,?,?,?)",
                             (order_id, rng.randint(1, 40), d.isoformat(), status))
                for pid, price in dict.fromkeys(rng.choices(product_rows, weights=weights, k=rng.randint(1, 3))):
                    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)",
                                 (order_id, pid, rng.randint(1, 3), price))
        conn.commit()
    finally:
        conn.close()


def create_workspace(session_id: str) -> Path:
    root = config.WORKSPACES_DIR / session_id
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(config.SEED_DIR / "files", root)
    build_database(root / "store.db")
    return root


def cleanup_old_workspaces(max_age_hours: float = 12) -> None:
    if not config.WORKSPACES_DIR.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for d in config.WORKSPACES_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
