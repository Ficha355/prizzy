import os
from typing import Optional

import psycopg2
import psycopg2.extras


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    # Render fournit postgres://, psycopg2 attend postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
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
    conn.close()


def upsert_subscriber(
    email: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    status: str = "active",
    plan: Optional[str] = None,
):
    conn = get_db()
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
    conn.commit()
    conn.close()


def get_subscriber(email: str) -> Optional[dict]:
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (email,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def has_active_subscription(email: str) -> bool:
    sub = get_subscriber(email)
    return sub is not None and sub["status"] == "active"


def has_elite_subscription(email: str) -> bool:
    sub = get_subscriber(email)
    return sub is not None and sub["status"] == "active" and sub.get("plan") == "elite"
