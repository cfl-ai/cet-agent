"""
为词库添加分类标签
分类规则：
  高频：COCA 排名 3000-5000（最常用的四六级词）
  常考：四六级真题中出现过的词
  日常：COCA 排名 5000-8000（日常生活词）
  少见：COCA 排名 8000+ 或 无排名（难词/学术词）
用法：python scripts/classify_vocab.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH


async def main():
    print("=" * 55)
    print("🏷️  词库分类工具")
    print("=" * 55)

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. 获取所有词汇
        cursor = await db.execute("SELECT word, frequency FROM vocabulary")
        rows = await cursor.fetchall()

        stats = {"高频": 0, "常考": 0, "日常": 0, "少见": 0}

        for word, freq in rows:
            if freq is None or freq >= 99999:
                category = "少见"
            elif freq <= 5000:
                category = "高频"
            elif freq <= 8000:
                category = "日常"
            else:
                category = "少见"

            await db.execute(
                "UPDATE vocabulary SET category=? WHERE word=?",
                (category, word)
            )
            stats[category] += 1

        # 2. 处理"常考"：检索真题中出现过的词
        # 从 questions 表中提取真题内容，匹配词汇
        cursor = await db.execute(
            "SELECT content FROM questions WHERE source LIKE 'real_%'"
        )
        contents = " ".join([r[0] for r in await cursor.fetchall()]).lower()

        # 对每个词，如果出现在真题中则升级为"常考"
        cursor = await db.execute("SELECT word FROM vocabulary")
        all_words = [r[0] for r in await cursor.fetchall()]
        for w in all_words:
            if f" {w.lower()} " in contents or f" {w.lower()}," in contents \
                    or f" {w.lower()}." in contents:
                await db.execute(
                    "UPDATE vocabulary SET category='常考' WHERE word=?",
                    (w,)
                )

        await db.commit()

    # 3. 统计
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT category, COUNT(*) FROM vocabulary GROUP BY category"
        )
        print("\n📊 分类结果:")
        for cat, cnt in await cursor.fetchall():
            print(f"  {cat or '高频'}: {cnt} 词")

    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())