"""
数据库迁移脚本 — 添加新表和新字段
用法：python scripts/migrate_db.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH


async def migrate():
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. vocabulary 表加 category 字段
        try:
            await db.execute("ALTER TABLE vocabulary ADD COLUMN category TEXT DEFAULT 'frequent'")
            print("✅ vocabulary.category 添加成功")
        except Exception as e:
            print(f"ℹ️ vocabulary.category 已存在或错误: {e}")

        # 2. 新增 exam_sessions 表（真题演练记录）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS exam_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                level TEXT,
                status TEXT DEFAULT 'in_progress',
                started_at REAL,
                ended_at REAL,
                duration_limit INTEGER,
                score_writing INTEGER DEFAULT 0,
                score_listening INTEGER DEFAULT 0,
                score_reading INTEGER DEFAULT 0,
                score_translation INTEGER DEFAULT 0,
                total_score INTEGER DEFAULT 0,
                paper_data TEXT,
                answers TEXT
            )
        """)
        print("✅ exam_sessions 表创建成功")

        # 2b. 补齐 exam_sessions 真题套卷字段
        # 注意：main.py 的 /api/exam/paper/* 依赖这些列，
        # 而本脚本的建表语句是分项计分结构，两套 schema 必须共存，
        # 否则套卷接口会报 "no such column"。
        for col, typedef in [
            ("year", "INTEGER"),
            ("month", "INTEGER"),
            ("set_number", "INTEGER"),
            ("duration_seconds", "INTEGER"),
            ("score", "INTEGER"),
            ("created_at", "REAL"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE exam_sessions ADD COLUMN {col} {typedef}"
                )
                print(f"✅ exam_sessions.{col} 添加成功")
            except Exception as e:
                print(f"ℹ️ exam_sessions.{col} 已存在: {e}")

        # 2c. 新增 listening_assets 表（听力音频缓存）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listening_assets (
                asset_id TEXT PRIMARY KEY,
                question_id TEXT,
                transcript TEXT,
                audio_url TEXT,
                created_at REAL
            )
        """)
        print("✅ listening_assets 表创建成功")

        # 4. 索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_exam_user ON exam_sessions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_vocab_category ON vocabulary(category)")

        await db.commit()
        print("\n✅ 数据库迁移完成")


if __name__ == "__main__":
    asyncio.run(migrate())