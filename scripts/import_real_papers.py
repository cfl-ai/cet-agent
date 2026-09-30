"""
真题导入脚本 — 支持TXT/MD格式
用法：
  1. 把真题TXT文件放入 data/raw/real_papers/ 目录
  2. python scripts/import_real_papers.py
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

PAPERS_DIR = Path("data/raw/real_papers")


def parse_translation(text: str, source_tag: str) -> list[dict]:
    """解析翻译题：匹配中文字符段落（30-200字）"""
    questions = []
    pattern = re.compile(r'([\u4e00-\u9fa5，。、；：""''（）【】\s]{30,200})')
    for i, m in enumerate(pattern.finditer(text)):
        chinese = m.group(1).strip()
        if len(chinese) < 20:
            continue
        questions.append({
            "question_id": f"real_{source_tag}_trans_{i:03d}",
            "question_type": "汉译英",
            "topic": "真题翻译",
            "difficulty": 4,
            "content": chinese,
            "options": json.dumps([], ensure_ascii=False),
            "answer": "",
            "explanation": "",
            "knowledge_tags": json.dumps(["真题", "翻译"], ensure_ascii=False),
            "source": f"real_{source_tag}",
            "verified": 0,
            "created_at": time.time(),
        })
    return questions


def parse_reading(text: str, source_tag: str) -> list[dict]:
    """解析阅读理解选择题"""
    questions = []
    pattern = re.compile(
        r'(\d{1,3})[\.\、\s]\s*(.+?)\s*\n'
        r'\s*A[\.\、\)]\s*(.+?)\s*\n'
        r'\s*B[\.\、\)]\s*(.+?)\s*\n'
        r'\s*C[\.\、\)]\s*(.+?)\s*\n'
        r'\s*D[\.\、\)]\s*(.+?)(?=\n\s*\d{1,3}[\.\、]|\Z)',
        re.DOTALL
    )
    for i, m in enumerate(pattern.finditer(text)):
        qnum = m.group(1)
        stem = m.group(2).strip().replace("\n", " ")
        options = [
            m.group(3).strip().replace("\n", " "),
            m.group(4).strip().replace("\n", " "),
            m.group(5).strip().replace("\n", " "),
            m.group(6).strip().replace("\n", " "),
        ]
        if len(stem) < 10:
            continue
        questions.append({
            "question_id": f"real_{source_tag}_read_{qnum:03d}",
            "question_type": "reading",
            "topic": "真题阅读",
            "difficulty": 4,
            "content": stem[:800],
            "options": json.dumps(options, ensure_ascii=False),
            "answer": "A",
            "explanation": "",
            "knowledge_tags": json.dumps(["真题", "阅读"], ensure_ascii=False),
            "source": f"real_{source_tag}",
            "verified": 0,
            "created_at": time.time(),
        })
    return questions


def parse_any(text: str, source_tag: str) -> list[dict]:
    """通用解析：自动判断题型"""
    all_qs = []
    if re.search(r'[\u4e00-\u9fa5]{20,}', text):
        all_qs.extend(parse_translation(text, source_tag))
    if re.search(r'A[\.\、\)]', text):
        all_qs.extend(parse_reading(text, source_tag))
    return all_qs


async def main():
    print("=" * 55)
    print("📥 真题TXT解析工具")
    print("=" * 55)

    await init_db()

    if not PAPERS_DIR.exists():
        PAPERS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"\n📁 已创建目录: {PAPERS_DIR}")
        print("  请把真题TXT文件放入此目录后重新运行")
        return

    txt_files = list(PAPERS_DIR.glob("**/*.txt"))
    if not txt_files:
        print(f"\n⚠️ {PAPERS_DIR} 下没有TXT文件")
        return

    print(f"\n📚 发现 {len(txt_files)} 个TXT文件")
    total_inserted = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for f in txt_files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                print(f"  ⚠️ 读取失败 {f.name}: {e}")
                continue

            source_tag = f.stem
            questions = parse_any(text, source_tag)
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
                    continue
            print(f"  ✅ {f.name}: 解析 {len(questions)} 题, 入库 {inserted} 题")

        await db.commit()

    print(f"\n✅ 总计入库 {total_inserted} 道真题")


if __name__ == "__main__":
    asyncio.run(main())