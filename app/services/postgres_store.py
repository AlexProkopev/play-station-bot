from __future__ import annotations

import logging
import os
import queue
import threading
from contextlib import contextmanager
from copy import deepcopy

from psycopg import connect
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

USERS_DOC_KEY = "users_db"
USERS_BACKUP_DOC_KEY = "users_db_pre_table"


class _ConnectionPool:
    def __init__(self, dsn: str, max_size: int = 16):
        self._dsn = dsn
        self._max_size = max(1, max_size)
        self._created = 0
        self._lock = threading.Lock()
        self._available: queue.SimpleQueue = queue.SimpleQueue()

    def acquire(self):
        while True:
            try:
                conn = self._available.get_nowait()
            except queue.Empty:
                conn = None

            if conn is not None:
                if conn.closed:
                    with self._lock:
                        self._created = max(0, self._created - 1)
                    continue
                return conn

            with self._lock:
                if self._created < self._max_size:
                    self._created += 1
                    return connect(self._dsn, autocommit=True)

            conn = self._available.get()
            if conn.closed:
                with self._lock:
                    self._created = max(0, self._created - 1)
                continue
            return conn

    def release(self, conn) -> None:
        if conn is None or conn.closed:
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        self._available.put(conn)

    def close(self) -> None:
        while True:
            try:
                conn = self._available.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass
        with self._lock:
            self._created = 0


class PostgresDocumentStore:
    TABLE_NAME = "ad_app_documents"
    USERS_TABLE = "ad_app_users"
    _initialized = False
    _pool: _ConnectionPool | None = None
    _pool_lock = threading.Lock()
    _doc_cache: dict[str, tuple[object, object]] = {}
    _doc_cache_lock = threading.RLock()

    @staticmethod
    def is_enabled() -> bool:
        if os.getenv("PYTEST_CURRENT_TEST") and os.getenv("ALLOW_TEST_DATABASE") != "1":
            return False
        return bool(DATABASE_URL)

    @classmethod
    def _get_pool(cls) -> _ConnectionPool:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        if cls._pool is None:
            with cls._pool_lock:
                if cls._pool is None:
                    cls._pool = _ConnectionPool(DATABASE_URL, max_size=16)
        return cls._pool

    @classmethod
    @contextmanager
    def _connection(cls):
        pool = cls._get_pool()
        conn = pool.acquire()
        try:
            yield conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            with pool._lock:
                pool._created = max(0, pool._created - 1)
            raise
        else:
            pool.release(conn)

    @classmethod
    def close(cls) -> None:
        with cls._pool_lock:
            pool = cls._pool
            cls._pool = None
        if pool is not None:
            pool.close()
        with cls._doc_cache_lock:
            cls._doc_cache.clear()
        cls._initialized = False

    @classmethod
    def initialize(cls) -> None:
        if cls._initialized or not cls.is_enabled():
            return

        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {cls.TABLE_NAME} (
                        key TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {cls.USERS_TABLE} (
                        user_id TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        has_active_order BOOLEAN NOT NULL DEFAULT FALSE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {cls.USERS_TABLE}_active_order_idx
                    ON {cls.USERS_TABLE} (user_id)
                    WHERE has_active_order
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {cls.USERS_TABLE}_last_bot_idx
                    ON {cls.USERS_TABLE} ((payload->>'last_bot_id'))
                    """
                )

        cls._initialized = True
        try:
            migrated = cls._migrate_users_document()
            if migrated:
                logger.info("Migrated users_db document into %s", cls.USERS_TABLE)
        except Exception:
            logger.exception("Failed to migrate users_db document")
        cls.invalidate_document_cache(USERS_DOC_KEY)

    @classmethod
    def invalidate_document_cache(cls, key: str | None = None) -> None:
        with cls._doc_cache_lock:
            if key is None:
                cls._doc_cache.clear()
            else:
                cls._doc_cache.pop(key, None)

    @classmethod
    def _unwrap_cache_entry(cls, cached) -> tuple[object, object] | None:
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached[0], cached[1]
        return None

    @classmethod
    def get_document(cls, key: str, default):
        if not cls.is_enabled():
            return deepcopy(default)

        cls.initialize()
        with cls._doc_cache_lock:
            cached = cls._unwrap_cache_entry(cls._doc_cache.get(key))

        if cached is not None:
            cached_updated_at, cached_payload = cached
            with cls._connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"SELECT updated_at FROM {cls.TABLE_NAME} WHERE key = %s",
                        (key,),
                    )
                    row = cur.fetchone()
            db_updated_at = row.get("updated_at") if row else None
            if row is not None and db_updated_at == cached_updated_at:
                return cached_payload

        with cls._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT payload, updated_at FROM {cls.TABLE_NAME} WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()

        payload = deepcopy(default) if not row else row.get("payload", deepcopy(default))
        updated_at = None if not row else row.get("updated_at")
        with cls._doc_cache_lock:
            cls._doc_cache[key] = (updated_at, payload)
        return payload

    @classmethod
    def set_document(cls, key: str, payload, force: bool = False) -> None:
        if not cls.is_enabled():
            return

        cls.initialize()
        stored = deepcopy(payload)
        with cls._doc_cache_lock:
            cached = cls._unwrap_cache_entry(cls._doc_cache.get(key))
            expected_at = None if cached is None else cached[0]

        with cls._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if force or expected_at is None:
                    cur.execute(
                        f"""
                        INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key)
                        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                        RETURNING updated_at
                        """,
                        (key, Jsonb(stored)),
                    )
                    row = cur.fetchone()
                else:
                    cur.execute(
                        f"""
                        UPDATE {cls.TABLE_NAME}
                        SET payload = %s, updated_at = NOW()
                        WHERE key = %s AND updated_at IS NOT DISTINCT FROM %s
                        RETURNING updated_at
                        """,
                        (Jsonb(stored), key, expected_at),
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            f"SELECT payload, updated_at FROM {cls.TABLE_NAME} WHERE key = %s",
                            (key,),
                        )
                        fresh = cur.fetchone()
                        with cls._doc_cache_lock:
                            if fresh:
                                cls._doc_cache[key] = (fresh.get("updated_at"), fresh.get("payload"))
                            else:
                                cls._doc_cache.pop(key, None)
                        return

        with cls._doc_cache_lock:
            cls._doc_cache[key] = (None if not row else row.get("updated_at"), stored)

    @classmethod
    def mutate_document(cls, key: str, mutator, *, default=None):
        if not cls.is_enabled():
            return None
        cls.initialize()
        with cls._connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"SELECT payload, updated_at FROM {cls.TABLE_NAME} WHERE key = %s FOR UPDATE",
                        (key,),
                    )
                    row = cur.fetchone()
                    if row and isinstance(row.get("payload"), dict):
                        payload = deepcopy(row["payload"])
                    else:
                        payload = deepcopy(default) if isinstance(default, dict) else {}
                    mutator(payload)
                    cur.execute(
                        f"""
                        INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key)
                        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                        RETURNING payload, updated_at
                        """,
                        (key, Jsonb(payload)),
                    )
                    saved = cur.fetchone()
        stored = deepcopy(saved.get("payload") if saved else payload)
        with cls._doc_cache_lock:
            cls._doc_cache[key] = (
                None if not saved else saved.get("updated_at"),
                stored,
            )
        return stored

    @classmethod
    def _has_active_order(cls, payload) -> bool:
        if not isinstance(payload, dict):
            return False
        order = payload.get("active_order")
        return bool(order)

    @classmethod
    def _migrate_users_document(cls) -> bool:
        with cls._connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {cls.USERS_TABLE}")
                    count = int(cur.fetchone()[0] or 0)
                    if count > 0:
                        return False

                    cur.execute(
                        f"SELECT payload FROM {cls.TABLE_NAME} WHERE key = %s",
                        (USERS_DOC_KEY,),
                    )
                    row = cur.fetchone()
                    payload = row[0] if row else None
                    if not isinstance(payload, dict) or payload.get("migrated_to_app_users"):
                        return False

                    users = payload.get("users")
                    if not isinstance(users, dict) or not users:
                        cur.execute(
                            f"""
                            INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (key)
                            DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                            """,
                            (USERS_DOC_KEY, Jsonb({"migrated_to_app_users": True})),
                        )
                        return False

                    cur.execute(
                        f"""
                        INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key)
                        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                        """,
                        (USERS_BACKUP_DOC_KEY, Jsonb(payload)),
                    )
                    cur.executemany(
                        f"""
                        INSERT INTO {cls.USERS_TABLE} (user_id, payload, has_active_order, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        [
                            (str(user_id), Jsonb(user_payload), cls._has_active_order(user_payload))
                            for user_id, user_payload in users.items()
                            if isinstance(user_payload, dict)
                        ],
                    )
                    cur.execute(
                        f"""
                        INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (key)
                        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                        """,
                        (USERS_DOC_KEY, Jsonb({"migrated_to_app_users": True})),
                    )
                    return True

    @classmethod
    def load_all_users(cls, _retried: bool = False) -> dict:
        if not cls.is_enabled():
            return {}

        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id, payload FROM {cls.USERS_TABLE}")
                rows = cur.fetchall()
        users = {}

    @classmethod
    def set_document_if_missing(cls, key: str, payload) -> bool:
        if not cls.is_enabled():
            return False

        cls.initialize()
        stored = deepcopy(payload)
        with cls._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"""
                    INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO NOTHING
                    RETURNING updated_at
                    """,
                    (key, Jsonb(stored)),
                )
                row = cur.fetchone()

        if row is None:
            return False
        with cls._doc_cache_lock:
            cls._doc_cache[key] = (row.get("updated_at"), stored)
        return True
        for user_id, payload in rows:
            if isinstance(payload, dict):
                users[str(user_id)] = payload
        if users or _retried:
            return users

        try:
            if cls._migrate_users_document():
                return cls.load_all_users(_retried=True)
        except Exception:
            logger.exception("Retry migrate of users_db failed")

        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT payload FROM {cls.TABLE_NAME} WHERE key = %s",
                    (USERS_DOC_KEY,),
                )
                row = cur.fetchone()
        payload = row[0] if row else None
        if isinstance(payload, dict) and not payload.get("migrated_to_app_users"):
            blob_users = payload.get("users")
            if isinstance(blob_users, dict) and blob_users:
                logger.warning("app_users is empty, using users_db document fallback")
                return {
                    str(uid): data
                    for uid, data in blob_users.items()
                    if isinstance(data, dict)
                }
        return users

    _BALANCE_SQL = """
    COALESCE(
      CASE
        WHEN jsonb_typeof(payload -> 'balance') = 'number'
          THEN (payload ->> 'balance')::double precision
        ELSE 0::double precision
      END,
      0::double precision
    )
    """

    @classmethod
    def get_user(cls, user_id: str) -> dict | None:
        if not cls.is_enabled():
            return None
        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT payload FROM {cls.USERS_TABLE} WHERE user_id = %s",
                    (str(user_id),),
                )
                row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        return deepcopy(payload) if isinstance(payload, dict) else None

    @classmethod
    def load_all_user_ids(cls) -> list[str]:
        if not cls.is_enabled():
            return []
        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id FROM {cls.USERS_TABLE}")
                rows = cur.fetchall()
        return [str(row[0]) for row in rows if row and row[0] is not None]

    @classmethod
    def load_user_ids_for_broadcast(cls, bot_id, *, include_unassigned: bool = True) -> list[str]:
        if not cls.is_enabled():
            return []
        cls.initialize()
        bot_key = str(bot_id)
        with cls._connection() as conn:
            with conn.cursor() as cur:
                if include_unassigned:
                    cur.execute(
                        f"""
                        SELECT user_id
                        FROM {cls.USERS_TABLE}
                        WHERE coalesce(payload->>'last_bot_id', '') IN ('', %s)
                        """,
                        (bot_key,),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT user_id
                        FROM {cls.USERS_TABLE}
                        WHERE coalesce(payload->>'last_bot_id', '') = %s
                        """,
                        (bot_key,),
                    )
                rows = cur.fetchall()
        return [str(row[0]) for row in rows if row and row[0] is not None]

    @classmethod
    def find_user_id_by_username(cls, username: str) -> str | None:
        if not cls.is_enabled() or not username:
            return None
        cls.initialize()
        clean = str(username).lstrip("@").lower()
        if not clean:
            return None
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT user_id
                    FROM {cls.USERS_TABLE}
                    WHERE lower(ltrim(coalesce(payload->>'username', ''), '@')) = %s
                    LIMIT 1
                    """,
                    (clean,),
                )
                row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return str(row[0])

    @classmethod
    def mutate_user(cls, user_id: str, mutator, *, create_payload: dict | None = None) -> dict | None:
        if not cls.is_enabled():
            return None
        cls.initialize()
        uid = str(user_id)
        try:
            with cls._connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT payload FROM {cls.USERS_TABLE} WHERE user_id = %s FOR UPDATE",
                            (uid,),
                        )
                        row = cur.fetchone()
                        if not row:
                            if not isinstance(create_payload, dict):
                                return None
                            payload = deepcopy(create_payload)
                            mutator(payload)
                            cur.execute(
                                f"""
                                INSERT INTO {cls.USERS_TABLE} (user_id, payload, has_active_order, updated_at)
                                VALUES (%s, %s, %s, NOW())
                                """,
                                (uid, Jsonb(payload), cls._has_active_order(payload)),
                            )
                            return deepcopy(payload)
                        payload = deepcopy(row[0]) if isinstance(row[0], dict) else {}
                        mutator(payload)
                        cur.execute(
                            f"""
                            UPDATE {cls.USERS_TABLE}
                            SET payload = %s,
                                has_active_order = %s,
                                updated_at = NOW()
                            WHERE user_id = %s
                            """,
                            (Jsonb(payload), cls._has_active_order(payload), uid),
                        )
                        return deepcopy(payload)
        except UniqueViolation:
            return cls.mutate_user(uid, mutator, create_payload=None)

    @classmethod
    def load_users_with_active_orders(cls) -> dict:
        if not cls.is_enabled():
            return {}
        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT user_id, payload
                    FROM {cls.USERS_TABLE}
                    WHERE has_active_order
                    """
                )
                rows = cur.fetchall()
        users = {}
        for user_id, payload in rows:
            if isinstance(payload, dict):
                users[str(user_id)] = deepcopy(payload)
        return users

    @classmethod
    def load_users_with_late_ratecore_orders(cls) -> dict:
        if not cls.is_enabled():
            return {}
        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT user_id, payload
                    FROM {cls.USERS_TABLE}
                    WHERE jsonb_typeof(payload -> 'late_ratecore_order') = 'object'
                    """
                )
                rows = cur.fetchall()
        users = {}
        for user_id, payload in rows:
            if isinstance(payload, dict):
                users[str(user_id)] = deepcopy(payload)
        return users

    @classmethod
    def set_user_balance(cls, user_id: str, amount: float) -> dict | None:
        if not cls.is_enabled():
            return None
        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {cls.USERS_TABLE}
                    SET payload = jsonb_set(payload, '{{balance}}', to_jsonb(%s::double precision), true),
                        updated_at = NOW()
                    WHERE user_id = %s
                    RETURNING payload
                    """,
                    (float(amount), str(user_id)),
                )
                row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        return deepcopy(payload) if isinstance(payload, dict) else None

    @classmethod
    def add_user_balance(cls, user_id: str, delta: float, *, require_non_negative: bool = False) -> dict | None:
        if not cls.is_enabled():
            return None
        cls.initialize()
        uid = str(user_id)
        change = float(delta)
        balance_sql = cls._BALANCE_SQL
        with cls._connection() as conn:
            with conn.cursor() as cur:
                if require_non_negative:
                    cur.execute(
                        f"""
                        UPDATE {cls.USERS_TABLE}
                        SET payload = jsonb_set(payload, '{{balance}}', to_jsonb(({balance_sql}) + %s), true),
                            updated_at = NOW()
                        WHERE user_id = %s
                          AND ({balance_sql}) + %s >= 0
                        RETURNING payload
                        """,
                        (change, uid, change),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE {cls.USERS_TABLE}
                        SET payload = jsonb_set(payload, '{{balance}}', to_jsonb(({balance_sql}) + %s), true),
                            updated_at = NOW()
                        WHERE user_id = %s
                        RETURNING payload
                        """,
                        (change, uid),
                    )
                row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        return deepcopy(payload) if isinstance(payload, dict) else None

    @classmethod
    def upsert_user(cls, user_id: str, payload: dict) -> None:
        if not cls.is_enabled():
            return

        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {cls.USERS_TABLE} (user_id, payload, has_active_order, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        payload = EXCLUDED.payload,
                        has_active_order = EXCLUDED.has_active_order,
                        updated_at = NOW()
                    """,
                    (str(user_id), Jsonb(payload), cls._has_active_order(payload)),
                )

    @classmethod
    def delete_users(cls, user_ids: list[str]) -> None:
        if not cls.is_enabled() or not user_ids:
            return

        cls.initialize()
        with cls._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {cls.USERS_TABLE} WHERE user_id = ANY(%s)",
                    (list(user_ids),),
                )

    @classmethod
    def replace_all_users(cls, users: dict) -> None:
        if not cls.is_enabled():
            return

        cls.initialize()
        rows = [
            (str(user_id), Jsonb(payload), cls._has_active_order(payload))
            for user_id, payload in (users or {}).items()
            if isinstance(payload, dict)
        ]
        with cls._connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {cls.USERS_TABLE}")
                    if rows:
                        cur.executemany(
                            f"""
                            INSERT INTO {cls.USERS_TABLE} (user_id, payload, has_active_order, updated_at)
                            VALUES (%s, %s, %s, NOW())
                            """,
                            rows,
                        )
