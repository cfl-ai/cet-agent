"""
解析手动下载的真题TXT
用法：
  1. 把真题TXT文件放入 data/raw/zhenti/ 目录
  2. python scripts/import_zhenti_txt.py
"""
import asyncio
import json
import re
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

from models.database import init_db, DB_PATH
import aiosqlite

ZHENTI_DIR = Path("data/raw/zhenti")


def parse_reading_questions(text: str, source_tag: str) -> list[dict]:
    """
    解析阅读理解选择题
    匹配格式：
        46. What does the author say about ...?
        A. xxx
        B. xxx
        C. xxx
        D. xxx
    """
    questions = []
    # 匹配 "数字. 题干" 后跟 A/B/C/D 四个选项
    pattern = re.compile(
        r'(\d{1,3})[\.\、\s]\s*(.+?)\s*\n'
        r'\s*A[\.\、\)]\s*(.+?)\s*\n'
        r'\s*B[\.\、\)]\s*(.+?)\s*\n'
        r'\s*C[\.\、\)]\s*(.+?)\s*\n'
        r'\s*D[\.\、\)]\s*(.+?)(?=\n\s*\d{1,3}[\.\、]|\Z)',
        re.DOTALL
    )

    for i, m in enumerate(pattern.finditer(text)):
        try:
            qnum = int(m.group(1))
            stem = m.group(2).strip().replace("\n", " ")
            options = [
                m.group(3).strip().replace("\n", " "),
                m.group(4).strip().replace("\n", " "),
                m.group(5).strip().replace("\n", " "),
                m.group(6).strip().replace("\n", " "),
            ]
            if len(stem) < 10 or any(len(o) < 1 for o in options):
                continue

            questions.append({
                "question_id": f"real_{source_tag}_{qnum:03d}",
                "question_type": "reading",
                "topic": "真题",
                "difficulty": 4,
                "content": stem[:800],
                "options": json.dumps(options, ensure_ascii=False),
                "answer": "A",     # 待人工标注
                "explanation": "", # 待人工标注
                "knowledge_tags": json.dumps(["真题"], ensure_ascii=False),
                "source": f"real_{source_tag}",
                "verified": 0,
                "created_at": time.time(),
            })
        except Exception:
            continue

    return questions


def parse_vocab_questions(text: str, source_tag: str) -> list[dict]:
    """
    解析词汇/选词填空题（形如 "36. xxx" 后跟四个选项）
    与阅读题解析逻辑相同，只是标记题型不同
    """
    questions = parse_reading_questions(text, source_tag)
    for q in questions:
        q["question_type"] = "vocab"
        q["difficulty"] = 3
    return questions


def parse_any_txt(text: str, source_tag: str) -> list[dict]:
    """通用解析：尝试所有可能的题型"""
    all_qs = []
    # 先尝试识别题型关键词
    if "reading" in text.lower() or "passage" in text.lower():
        all_qs = parse_reading_questions(text, source_tag)
    elif "vocabulary" in text.lower() or "cloze" in text.lower():
        all_qs = parse_vocab_questions(text, source_tag)
    else:
        # 默认按阅读题解析
        all_qs = parse_reading_questions(text, source_tag)
    return all_qs


async def main():
    print("=" * 55)
    print("📥 真题TXT解析工具")
    print("=" * 55)

    await init_db()

    # 检查目录
    if not ZHENTI_DIR.exists():
        ZHENTI_DIR.mkdir(parents=True, exist_ok=True)
        print(f"\n📁 已创建目录: {ZHENTI_DIR}")
        print("  请把真题TXT文件放入此目录后重新运行")
        return

    txt_files = list(ZHENTI_DIR.glob("**/*.txt"))
    if not txt_files:
        print(f"\n⚠️ {ZHENTI_DIR} 下没有TXT文件")
        print("  用法：")
        print("   1. 从真题PDF提取文本，另存为TXT")
        print(f"   2. 放入 {ZHENTI_DIR}/ 目录")
        print("   3. 重新运行本脚本")
        return

    print(f"\n📚 发现 {len(txt_files)} 个TXT文件")

    total_inserted = 0
    total_skipped = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for f in txt_files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                print(f"  ⚠️ 读取失败 {f.name}: {e}")
                continue

            source_tag = f.stem
            questions = parse_any_txt(text, source_tag)

            if not questions:
                print(f"  ⚠️ {f.name}: 未解析到题目")
                continue

            inserted = 0
            for q in questions:
                try:
                    await db.execute("""
                        INSERT OR IGNORE INTO questions
                        (question_id, question_type, topic, difficulty,
                         content, options, answer, explanation,
                         knowledge_tags, source, verified, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        q["question_id"], q["question_type"], q["topic"],
                        q["difficulty"], q["content"], q["options"],
                        q["answer"], q["explanation"], q["knowledge_tags"],
                        q["source"], q["verified"], q["created_at"]
                    ))
                    inserted += 1
                    total_inserted += 1
                except Exception:
                    total_skipped += 1
                    continue

            print(f"  ✅ {f.name}: 解析 {len(questions)} 题, 入库 {inserted} 题")

        await db.commit()

    print("\n" + "=" * 55)
    print("📊 导入报告")
    print(f"  入库题目: {total_inserted} 道")
    print(f"  跳过重复: {total_skipped} 道")
    print()
    print("⚠️ 注意：自动解析的真题标记为 verified=0（待验证）")
    print("   答案默认为 A，需要人工核对后通过以下SQL修正：")
    print(f"   UPDATE questions SET answer='B', verified=1")
    print(f"   WHERE question_id='real_xxx_001';")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())