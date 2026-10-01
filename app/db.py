"""SQLite access (stdlib only). One connection per request, autocommit; writes that
span several statements use utils.tx()."""
import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], isolation_level=None, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    con.executescript(SCHEMA)
    con.close()


def seed(path):
    """Rich demo data: 5 centres across 4 cities, 10 tests, realistic pricing. Safe to run repeatedly."""
    import hashlib, hmac as _hmac, os as _os
    con = sqlite3.connect(path)
    if con.execute("SELECT 1 FROM centres").fetchone():
        con.close()
        return False

    # ── Centres ──────────────────────────────────────────────────────────────
    centres = [
        ("Metro Diagnostics",      "Jamshedpur"),
        ("CityCare Labs",          "Ranchi"),
        ("Apollo Diagnostics",     "Mumbai"),
        ("HealthFirst Pathology",  "Delhi"),
        ("Sunrise Medical Centre", "Bangalore"),
    ]

    # ── Tests ─────────────────────────────────────────────────────────────────
    tests = [
        "Complete Blood Count",      # 1
        "Lipid Profile",             # 2
        "Thyroid Panel (T3/T4/TSH)", # 3
        "Blood Glucose Fasting",     # 4
        "HbA1c (Diabetes Screen)",   # 5
        "Liver Function Test",       # 6
        "Kidney Function Test",      # 7
        "Vitamin D3",                # 8
        "ECG (12-Lead)",             # 9
        "Full Body Checkup",         # 10
    ]

    # ── Prices: (centre_id, test_id) → price in paise ────────────────────────
    prices = {
        # Metro Diagnostics, Jamshedpur
        (1, 1): 35000,  (1, 2): 60000,  (1, 3): 75000,
        (1, 4): 18000,  (1, 6): 80000,  (1, 9): 45000,
        (1, 10): 250000,
        # CityCare Labs, Ranchi
        (2, 1): 30000,  (2, 2): 55000,  (2, 3): 70000,
        (2, 4): 15000,  (2, 5): 65000,  (2, 7): 90000,
        (2, 10): 220000,
        # Apollo Diagnostics, Mumbai
        (3, 1): 45000,  (3, 2): 85000,  (3, 3): 110000,
        (3, 4): 25000,  (3, 5): 95000,  (3, 6): 120000,
        (3, 7): 130000, (3, 8): 75000,  (3, 9): 60000,
        (3, 10): 350000,
        # HealthFirst Pathology, Delhi
        (4, 1): 40000,  (4, 2): 70000,  (4, 3): 85000,
        (4, 4): 20000,  (4, 5): 75000,  (4, 6): 95000,
        (4, 8): 80000,  (4, 9): 55000,  (4, 10): 290000,
        # Sunrise Medical Centre, Bangalore
        (5, 1): 38000,  (5, 2): 72000,  (5, 3): 90000,
        (5, 4): 22000,  (5, 5): 80000,  (5, 6): 100000,
        (5, 7): 110000, (5, 8): 85000,  (5, 9): 58000,
        (5, 10): 320000,
    }

    con.executemany("INSERT INTO centres(name, location) VALUES (?, ?)", centres)
    con.executemany("INSERT INTO tests(name) VALUES (?)", [(t,) for t in tests])
    con.executemany(
        "INSERT INTO centre_tests(centre_id, test_id, price) VALUES (?, ?, ?)",
        [(c, t, p) for (c, t), p in prices.items()],
    )
    con.commit()
    con.close()
    return True
