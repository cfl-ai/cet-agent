"""验证导入结果 — 用法: python scripts/check_import.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH


async def check():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM questions WHERE source LIKE 'real_%'"
        )
        (real_q,) = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM questions WHERE source LIKE 'real_%' AND verified=1"
        )
        (verified_q,) = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM questions WHERE source IN ('ai_generated', 'seed')"
        )
        (ai_q,) = await cursor.fetchone()

        cursor = await db.execute("SELECT COUNT(*) FROM vocabulary")
        (total_v,) = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT level, COUNT(*) FROM vocabulary GROUP BY level ORDER BY level"
        )
        by_level = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT question_type, COUNT(*) FROM questions GROUP BY question_type"
        )
        by_type = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT word, meaning, frequency FROM vocabulary ORDER BY frequency ASC LIMIT 10"
        )
        top_words = await cursor.fetchall()

        # ★ 新增：错题统计
        cursor = await db.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE resolved=0"
        )
        (wrong_count,) = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM answer_records"
        )
        (answer_count,) = await cursor.fetchone()

    print("=" * 55)
    print("📊 数据导入验证报告")
    print("=" * 55)
    print(f"  真题题目:     {real_q} 道 (已核对 {verified_q})")
    print(f"  示例/AI题:    {ai_q} 道")
    print(f"  词汇总量:     {total_v} 词")
    print(f"  累计答题记录: {answer_count} 条")
    print(f"  未解决错题:   {wrong_count} 条")
    print()

    if by_level:
        print("  词汇级别分布:")
        for level, cnt in by_level:
            print(f"    {level}: {cnt}")
        print()

    if by_type:
        print("  题目题型分布:")
        for qtype, cnt in by_type:
            print(f"    {qtype}: {cnt}")
        print()

    if top_words:
        print("  Top 10 高频词 (COCA顺序，已排除初高中基础词):")
        for w, meaning, freq in top_words:
            meaning_short = (meaning[:35] + "...") if len(meaning) > 35 else meaning
            print(f"    [{freq:>5}] {w:<18} {meaning_short}")
        print()

    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(check())