"""
Word真题文档解析器
依赖: pip install python-docx
用法：python scripts/parse_word_papers.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.database import init_db, DB_PATH
import aiosqlite

PAPERS_DIR = Path("data/raw/real_papers")

# ★ 中文字符范围（用 unicode 转义避免 SyntaxWarning）
# \u4e00-\u9fa5 基本汉字，\u3000-\u303f 中文标点，\uff00-\uffef 全角符号
CN_CHARS = r'\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef'


def extract_text_from_docx(file_path: Path) -> str:
    """从Word文档提取纯文本"""
    try:
        from docx import Document
        doc = Document(str(file_path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        print("⚠️ 请安装 python-docx: pip install python-docx")
        return ""
    except Exception as e:
        print(f"⚠️ 解析失败 {file_path.name}: {e}")
        return ""


def parse_translation(text: str, source_tag: str) -> list[dict]:
    """解析翻译题：匹配中文字符段落"""
    questions = []
    # ★ 用 unicode 转义构建正则，避免 SyntaxWarning
    pattern = re.compile(
        rf'([{CN_CHARS}\s]{{30,300}})',
        re.DOTALL
    )
    seen = set()
    for i, m in enumerate(pattern.finditer(text)):
        chinese = m.group(1).strip()
        # 过滤：长度 20-250 字，且包含中文标点
        if len(chinese) < 20 or len(chinese) > 250:
            continue
        # 过滤：必须包含中文标点（句号/逗号）
        if not re.search(r'[，。；：]', chinese):
            continue
        # 去重（前30字相同视为重复）
        key = chinese[:30]
        if key in seen:
            continue
        seen.add(key)

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
    """
    解析阅读理解选择题 — 带完整异常保护
    """
    questions = []
    try:
        pattern = re.compile(
            r'(\d{1,3})[\.\、\s]\s*(.+?)\s*\n'
            r'\s*A[\.\、\)]\s*(.+?)\s*\n'
            r'\s*B[\.\、\)]\s*(.+?)\s*\n'
            r'\s*C[\.\、\)]\s*(.+?)\s*\n'
            r'\s*D[\.\、\)]\s*(.+?)(?=\n\s*\d{1,3}[\.\、]|\Z)',
            re.DOTALL
        )

        seen_stems = set()
        for m in pattern.finditer(text):
            try:
                # ★ 用 try/except 保护每一道题的解析
                qnum = m.group(1)
                stem = (m.group(2) or "").strip().replace("\n", " ")
                if len(stem) < 10:
                    continue

                options = []
                for idx in (3, 4, 5, 6):
                    try:
                        opt = (m.group(idx) or "").strip().replace("\n", " ")
                        if opt:
                            options.append(opt)
                    except IndexError:
                        break

                if len(options) != 4:
                    continue

                # 题干去重
                key = stem[:40]
                if key in seen_stems:
                    continue
                seen_stems.add(key)

                questions.append({
                    "question_id": f"real_{source_tag}_read_{qnum:>03}",
                    "question_type": "reading",
                    "topic": "真题阅读",
                    "difficulty": 4,
                    "content": stem[:800],
                    "options": json.dumps(options, ensure_ascii=False),
                    "answer": "A",     # 待人工核对
                    "explanation": "",
                    "knowledge_tags": json.dumps(["真题", "阅读"], ensure_ascii=False),
                    "source": f"real_{source_tag}",
                    "verified": 0,
                    "created_at": time.time(),
                })
            except Exception as e:
                # 单题解析失败不影响整体
                continue

    except Exception as e:
        print(f"    ⚠️ 阅读解析异常: {e}")

    return questions


async def main():
    print("=" * 55)
    print("📄 Word真题文档解析工具")
    print("=" * 55)

    await init_db()

    # ★ 递归查找所有docx（包括子目录）
    docx_files = list(PAPERS_DIR.glob("**/*.docx"))
    if not docx_files:
        print(f"\n⚠️ {PAPERS_DIR} 下没有Word文件")
        return

    print(f"\n📚 发现 {len(docx_files)} 个Word文件")
    total_inserted = 0
    total_files_ok = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for f in docx_files:
            try:
                text = extract_text_from_docx(f)
                if not text:
                    continue

                source_tag = f.stem.replace(" ", "_")  # 文件名空格转下划线
                questions = []

                # 翻译题
                try:
                    if re.search(rf'[{CN_CHARS}]{{20,}}', text):
                        questions.extend(parse_translation(text, source_tag))
                except Exception as e:
                    print(f"  ⚠️ {f.name} 翻译解析失败: {e}")

                # 阅读题
                try:
                    if re.search(r'A[\.\、\)]', text):
                        questions.extend(parse_reading(text, source_tag))
                except Exception as e:
                    print(f"  ⚠️ {f.name} 阅读解析失败: {e}")

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

                total_files_ok += 1
                print(f"  ✅ {f.name}: 解析 {len(questions)} 题, 入库 {inserted} 题")

            except Exception as e:
                # ★ 单个文件失败不影响整体流程
                print(f"  ❌ {f.name} 整体失败: {e}")
                continue

        await db.commit()

    print("\n" + "=" * 55)
    print(f"✅ 总计入库 {total_inserted} 道真题")
    print(f"📊 成功处理 {total_files_ok}/{len(docx_files)} 个文件")
    print("⚠️ 自动解析的题目标记为 verified=0，需人工核对答案")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())