"""
数据库迁移脚本 — 对既有库做增量升级
用法：python scripts/migrate_db.py

★ schema 单一来源是 models/database.py 的 init_db()。
本脚本只负责把"旧版数据库"补齐到当前 schema。

历史教训：本脚本曾维护另一套 exam_sessions 建表语句（分项计分结构），
与 database.py 的真题套卷结构冲突，导致 /api/exam/paper/* 三个接口
全线报 "no such column"。现两处定义已统一，并由
tests/test_schema_consistency.py 强制守护一致性。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH, init_db


async def migrate():
    # 先执行权威 schema，确保新库结构完整
    await init_db()

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. vocabulary 表加 category 字段（旧库兼容）
        try:
            await db.execute(
                "ALTER TABLE vocabulary ADD COLUMN category TEXT DEFAULT 'frequent'"
            )
            print("✅ vocabulary.category 添加成功")
        except Exception as e:
            print(f"ℹ️ vocabulary.category 已存在或错误: {e}")

        # 2. exam_sessions 补齐真题套卷字段（旧库兼容）
        #    与 models/database.py 的定义保持一致。
        #    这里用显式语句而非循环，便于静态检查工具核对列名。
        alter_statements = [
            "ALTER TABLE exam_sessions ADD COLUMN year INTEGER",
            "ALTER TABLE exam_sessions ADD COLUMN month INTEGER",
            "ALTER TABLE exam_sessions ADD COLUMN set_number INTEGER",
            "ALTER TABLE exam_sessions ADD COLUMN duration_seconds INTEGER",
            "ALTER TABLE exam_sessions ADD COLUMN score INTEGER",
            "ALTER TABLE exam_sessions ADD COLUMN created_at REAL",
        ]
        for stmt in alter_statements:
            try:
                await db.execute(stmt)
                print(f"✅ {stmt.split('ADD COLUMN ')[1].split()[0]} 添加成功")
            except Exception as e:
                col = stmt.split("ADD COLUMN ")[1].split()[0]
                print(f"ℹ️ exam_sessions.{col} 已存在: {e}")

        # 3. 索引
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_exam_user ON exam_sessions(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vocab_category ON vocabulary(category)"
        )

        await db.commit()
        print("\n✅ 数据库迁移完成")


if __name__ == "__main__":
    asyncio.run(migrate())
