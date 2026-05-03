import os
import sqlite3
from typing import Optional

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = "postgresql://" + _DATABASE_URL[len("postgres://"):]

USE_PG = bool(_DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras

DB_PATH = "/tmp/prizzy.db"


def get_db():
    if USE_PG:
        return psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    if USE_PG:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id                     SERIAL PRIMARY KEY,
                    email                  TEXT UNIQUE NOT NULL,
                    stripe_customer_id     TEXT,
                    stripe_subscription_id TEXT,
                    status                 TEXT NOT NULL DEFAULT 'inactive',
                    plan                   TEXT NOT NULL DEFAULT 'starter',
                    created_at             TIMESTAMPTZ DEFAULT NOW(),
                    updated_at             TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                email                  TEXT UNIQUE NOT NULL,
                stripe_customer_id     TEXT,
                stripe_subscription_id TEXT,
                status                 TEXT NOT NULL DEFAULT 'inactive',
                plan                   TEXT NOT NULL DEFAULT 'starter',
                created_at             TEXT DEFAULT (datetime('now')),
                updated_at             TEXT DEFAULT (datetime('now'))
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
    if USE_PG:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscribers
                    (email, stripe_customer_id, stripe_subscription_id, status, plan, updated_at)
                VALUES (%s, %s, %s, %s, COALESCE(%s, 'starter'), NOW())
                ON CONFLICT (email) DO UPDATE SET
                    stripe_customer_id     = COALESCE(EXCLUDED.stripe_customer_id,     subscribers.stripe_customer_id),
                    stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id, subscribers.stripe_subscription_id),
                    status                 = EXCLUDED.status,
                    plan                   = COALESCE(EXCLUDED.plan, subscribers.plan),
                    updated_at             = NOW()
                """,
                (email, stripe_customer_id, stripe_subscription_id, status, plan),
            )
    else:
        conn.execute(
            """
            INSERT INTO subscribers
                (email, stripe_customer_id, stripe_subscription_id, status, plan, updated_at)
            VALUES (?, ?, ?, ?, COALESCE(?, 'starter'), datetime('now'))
            ON CONFLICT (email) DO UPDATE SET
                stripe_customer_id     = COALESCE(excluded.stripe_customer_id,     stripe_customer_id),
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
    if USE_PG:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM subscribers WHERE email = %s", (email,))
            row = cur.fetchone()
    else:
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
