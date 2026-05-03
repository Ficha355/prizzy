import sqlite3
import os
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "prizzy.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            email                 TEXT UNIQUE NOT NULL,
            stripe_customer_id    TEXT,
            stripe_subscription_id TEXT,
            status                TEXT NOT NULL DEFAULT 'inactive',
            plan                  TEXT NOT NULL DEFAULT 'starter',
            created_at            TEXT DEFAULT (datetime('now')),
            updated_at            TEXT DEFAULT (datetime('now'))
        )
    """)
    try:
        conn.execute("ALTER TABLE subscribers ADD COLUMN plan TEXT NOT NULL DEFAULT 'starter'")
    except Exception:
        pass
    conn.commit()
    conn.close()


def upsert_subscriber(
    email: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    status: str = "active",
    plan: Optional[str] = None,
):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO subscribers (email, stripe_customer_id, stripe_subscription_id, status, plan, updated_at)
        VALUES (?, ?, ?, ?, COALESCE(?, 'starter'), datetime('now'))
        ON CONFLICT(email) DO UPDATE SET
            stripe_customer_id     = COALESCE(excluded.stripe_customer_id, stripe_customer_id),
            stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, stripe_subscription_id),
            status                 = excluded.status,
            plan                   = COALESCE(excluded.plan, plan),
            updated_at             = datetime('now')
        """,
        (email, stripe_customer_id, stripe_subscription_id, status, plan),
    )
    conn.commit()
    conn.close()


def get_subscriber(email: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM subscribers WHERE email = ?", (email,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def has_active_subscription(email: str) -> bool:
    sub = get_subscriber(email)
    return sub is not None and sub["status"] == "active"


def has_elite_subscription(email: str) -> bool:
    sub = get_subscriber(email)
    return sub is not None and sub["status"] == "active" and sub.get("plan") == "elite"
