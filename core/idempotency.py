import aiosqlite
import json
import time

class IdempotencyStore:
    """基于SQLite的幂等性保证 — 三态状态机"""

    def __init__(self, db_path: str = "data/cet_agent.db"):
        self.db_path = db_path

    async def _ensure_table(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    result TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    expires_at REAL
                )
            """)
            await db.commit()

    async def get(self, key: str) -> dict | None:
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT result, status FROM idempotency WHERE key = ? AND expires_at > ?",
                (key, time.time())
            )
            row = await cursor.fetchone()
            if row and row[1] == "completed":
                return json.loads(row[0])
            return None

    async def set(self, key: str, result: dict):
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO idempotency
                   (key, result, status, created_at, expires_at)
                   VALUES (?, ?, 'completed', ?, ?)""",
                (key, json.dumps(result, ensure_ascii=False),
                 time.time(), time.time() + 86400)  # 24小时过期
            )
            await db.commit()