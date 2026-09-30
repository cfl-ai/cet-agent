"""
补充词库 — 从本地缓存的 CET4/CET6 TXT 中导入所有未入库的词
用法：python scripts/supplement_vocab.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import init_db, DB_PATH

# 本地缓存路径
VOCAB_DIR = Path("data/raw/vocab")
CET4_FILE = VOCAB_DIR / "CET4_edited.txt"
CET6_FILE = VOCAB_DIR / "CET6_edited.txt"
COCA_FILE = VOCAB_DIR / "COCA_20000.txt"


def parse_coca_frequency(text: str) -> dict:
    """解析COCA词频表"""
    freq = {}
    if not text:
        return freq
    for idx, line in enumerate(text.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[\s\t]+", line)
        word = None
        rank = None
        for part in parts:
            part_clean = part.strip().strip(".")
            if part_clean.isdigit():
                rank = int(part_clean)
            elif re.match(r"^[a-zA-Z\-']+$", part_clean):
                word = part_clean.lower()
        if word and rank:
            freq[word] = rank
        elif word and not rank:
            freq[word] = idx + 1
    return freq


def parse_cet_txt(text: str, level: str, coca_freq: dict) -> list[dict]:
    """解析 CET TXT 词库，返回全部词条（不筛选）"""
    words = []
    if not text:
        return words

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("大学英语") or line.startswith("CET") or line.startswith("#"):
            continue

        # 匹配：单词 [音标] 释义
        match = re.match(r"^([a-zA-Z][a-zA-Z\-']*)\s*\[([^\]]+)\]\s*(.+)$", line)
        if match:
            word = match.group(1).strip()
            phonetic = f"[{match.group(2).strip()}]"
            meaning = match.group(3).strip()
        else:
            match = re.match(r"^([a-zA-Z][a-zA-Z\-']*)\s+(.+)$", line)
            if match:
                word = match.group(1).strip()
                phonetic = ""
                meaning = match.group(2).strip()
            else:
                continue

        if not word or len(word) < 2 or not meaning:
            continue

        freq_rank = coca_freq.get(word.lower(), 99999)

        words.append({
            "word": word,
            "phonetic": phonetic,
            "meaning": meaning[:200],
            "level": level.upper(),
            "frequency": freq_rank,
            "topic": "通用",
            "examples": json.dumps([], ensure_ascii=False),
        })

    return words


async def main():
    print("=" * 55)
    print("📚 词库补充工具")
    print("=" * 55)

    await init_db()

    # 1. 检查本地文件
    if not CET4_FILE.exists() or not CET6_FILE.exists():
        print(f"❌ 找不到本地词库文件")
        print(f"  期望路径: {CET4_FILE}")
        print(f"           {CET6_FILE}")
        print(f"  请先运行 import_vocab.py 下载词库")
        return

    # 2. 加载COCA词频
    coca_freq = {}
    if COCA_FILE.exists():
        coca_freq = parse_coca_frequency(COCA_FILE.read_text(
            encoding="utf-8", errors="ignore"))
        print(f"✅ COCA词频表: {len(coca_freq)} 词")

    # 3. 解析全部词条
    cet4_text = CET4_FILE.read_text(encoding="utf-8", errors="ignore")
    cet6_text = CET6_FILE.read_text(encoding="utf-8", errors="ignore")

    cet4_words = parse_cet_txt(cet4_text, "CET4", coca_freq)
    cet6_words = parse_cet_txt(cet6_text, "CET6", coca_freq)

    print(f"✅ CET4 解析: {len(cet4_words)} 词")
    print(f"✅ CET6 解析: {len(cet6_words)} 词")

    # 4. 合并去重（同词保留六级版本）
    all_words = {}
    for w in cet4_words:
        all_words[w["word"]] = w
    for w in cet6_words:
        # 六级词优先
        all_words[w["word"]] = w

    print(f"✅ 合并去重后: {len(all_words)} 词")

    # 5. 查询数据库已有词
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT word FROM vocabulary")
        existing = set(r[0] for r in await cursor.fetchall())

        print(f"📊 数据库中已有: {len(existing)} 词")

        # 6. 找出新增词
        new_words = [w for word, w in all_words.items() if word not in existing]
        print(f"📥 需要新增: {len(new_words)} 词")

        if not new_words:
            print("✅ 词库已是最全状态，无需补充")
            return

        # 7. 批量插入
        inserted = 0
        for w in new_words:
            try:
                # 分类：按COCA排名
                freq = w["frequency"]
                if freq is None or freq >= 99999:
                    category = "少见"
                elif freq <= 5000:
                    category = "高频"
                elif freq <= 8000:
                    category = "日常"
                else:
                    category = "少见"

                await db.execute("""
                    INSERT OR IGNORE INTO vocabulary
                    (word, phonetic, meaning, level, frequency, topic, examples, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    w["word"], w["phonetic"], w["meaning"],
                    w["level"], w["frequency"], w["topic"], w["examples"],
                    category
                ))
                inserted += 1
            except Exception:
                continue

        await db.commit()
        print(f"✅ 新增入库: {inserted} 词")

    # 8. 统计
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM vocabulary")
        (total,) = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT level, COUNT(*) FROM vocabulary GROUP BY level"
        )
        by_level = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT category, COUNT(*) FROM vocabulary GROUP BY category"
        )
        by_cat = await cursor.fetchall()

    print("\n📊 补充后统计:")
    print(f"  词汇总量: {total} 词")
    print(f"  按级别:")
    for lvl, cnt in by_level:
        print(f"    {lvl}: {cnt}")
    print(f"  按分类:")
    for cat, cnt in by_cat:
        print(f"    {cat or '高频'}: {cnt}")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())