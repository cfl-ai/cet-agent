"""
构建向量索引 — 把数据库中的题目批量导入 ChromaDB
用法：python scripts/build_vector_index.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH
from services.vector_service import vector_service


async def main():
    print("=" * 55)
    print("🔍 构建向量索引")
    print("=" * 55)

    if not vector_service.enabled:
        print("❌ VectorService 未启用（ChromaDB 未安装或初始化失败）")
        print("   请先安装：pip install chromadb")
        return

    # 查询所有题目
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT question_id, question_type, topic, difficulty,
                   content, source
            FROM questions
            WHERE content IS NOT NULL AND LENGTH(content) > 20
        """)
        rows = await cursor.fetchall()

    questions = [{
        "question_id": r[0],
        "question_type": r[1] or "",
        "topic": r[2] or "",
        "difficulty": r[3] or 3,
        "content": r[4],
        "source": r[5] or "",
    } for r in rows]

    print(f"📊 数据库中待索引: {len(questions)} 题")

    if not questions:
        print("⚠️ 无题目可索引")
        return

    # 分批写入，每批 200 条
    inserted = 0
    batch_size = 200
    for i in range(0, len(questions), batch_size):
        batch = questions[i:i + batch_size]
        n = vector_service.add_questions(batch)
        inserted += n
        print(f"  ✅ 批次 {i // batch_size + 1}: {n}/{len(batch)} 写入")

    print("\n" + "=" * 55)
    print(f"✅ 完成！向量库现有: {vector_service.count()} 条")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())