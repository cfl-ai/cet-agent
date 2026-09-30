"""
真题导入脚本 — 适配 cet-skill 实际仓库结构
数据源：Liuxiangjian-ai/cet-skill
用法：python scripts/import_cet_real.py
"""
import asyncio
import json
import sys
import time
import warnings
import httpx
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.storage_guard import StorageGuard
from models.database import init_db, DB_PATH
import aiosqlite

GHPROXY = "https://ghproxy.net"
RAW_GITHUB = "https://raw.githubusercontent.com"

REPO_BASE = {
    "cdn": f"{GHPROXY}/https://raw.githubusercontent.com/Liuxiangjian-ai/cet-skill/main",
    "raw": f"{RAW_GITHUB}/Liuxiangjian-ai/cet-skill/main",
}

# ★ 已知的仓库路径（基于 cet-skill 的实际目录结构）
# 每一类对应一个目录，脚本会尝试枚举目录下的 1.json ~ 30.json
CANDIDATE_PATHS = [
    # (路径, 题型, 难度)
    ("CET4/reading", "reading", 3),
    ("CET4/listening", "listening", 3),
    ("CET4/vocabulary", "vocab", 2),
    ("CET4/cloze", "cloze", 3),
    ("CET6/reading", "reading", 4),
    ("CET6/listening", "listening", 4),
    ("CET6/vocabulary", "vocab", 3),
    ("CET6/cloze", "cloze", 4),
]

guard = StorageGuard()


async def try_fetch(client: httpx.AsyncClient, path: str) -> tuple[str | None, str]:
    """尝试从 cdn 或 raw 拉取文本"""
    for label in ["cdn", "raw"]:
        prefix = REPO_BASE.get(label)
        if not prefix:
            continue
        url = f"{prefix}/{path}"
        try:
            resp = await client.get(url, timeout=60)
            if resp.status_code == 200 and len(resp.text) > 50:
                return resp.text, url
        except Exception:
            continue
    return None, ""


def parse_questions_from_json(data, qtype: str, difficulty: int,
                               source_tag: str) -> list[dict]:
    """通用题目解析"""
    questions = []
    if not isinstance(data, (list, dict)):
        return questions

    items = data if isinstance(data, list) else (
        data.get("questions") or data.get("items") or []
    )

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        stem = item.get("stem") or item.get("question") or item.get("content") or item.get("text")
        options = item.get("options") or []
        answer = (item.get("answer") or "").strip().upper()

        if not stem:
            continue
        # 有些题目可能没有选项（如听力填空题）
        if options and len(options) != 4:
            continue
        if options and answer not in ["A", "B", "C", "D"]:
            continue

        qid = f"real_{source_tag}_{i:03d}"
        questions.append({
            "question_id": qid,
            "question_type": qtype,
            "topic": item.get("topic", "通用"),
            "difficulty": item.get("difficulty", difficulty),
            "content": stem[:1000],  # 限制长度
            "options": json.dumps(options, ensure_ascii=False),
            "answer": answer if answer else "A",
            "explanation": item.get("explanation", "")[:500],
            "knowledge_tags": json.dumps(
                item.get("tags", item.get("knowledge_tags", [])),
                ensure_ascii=False
            ),
            "source": f"real_{source_tag}",
            "verified": 1,
            "created_at": time.time(),
        })
    return questions


async def main():
    print("=" * 55)
    print("📥 四六级真题导入工具 (适配 cet-skill 结构)")
    print("=" * 55)

    await init_db()

    total_inserted = 0
    total_skipped = 0
    total_files = 0
    found_paths = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 CET-Agent/1.0"},
        follow_redirects=True,
        verify=False,
        timeout=60,
    ) as client:
        # 1. 探测哪些路径可用
        print("\n🔎 探测可用的题目路径...")
        for path, qtype, diff in CANDIDATE_PATHS:
            # 尝试 1.json
            text, url = await try_fetch(client, f"{path}/1.json")
            if text:
                print(f"  ✅ {path} 可用")
                found_paths.append((path, qtype, diff))
            else:
                # 尝试 index.json
                text, url = await try_fetch(client, f"{path}/index.json")
                if text:
                    print(f"  ✅ {path}/index.json 可用")
                    found_paths.append((path, qtype, diff))

        if not found_paths:
            print("\n❌ 未找到可用的题目路径")
            print("\n💡 提示：cet-skill 仓库结构可能已变更，请访问以下链接查看实际结构：")
            print(f"   {RAW_GITHUB}/Liuxiangjian-ai/cet-skill/main/")
            return

        # 2. 遍历每个路径下的 1.json ~ 50.json
        print(f"\n📚 开始下载（共 {len(found_paths)} 个路径）")
        async with aiosqlite.connect(DB_PATH) as db:
            for path, qtype, diff in found_paths:
                print(f"\n  处理 {path}...")
                path_count = 0

                for i in range(1, 51):  # 尝试 1.json ~ 50.json
                    file_path = f"{path}/{i}.json"
                    text, file_url = await try_fetch(client, file_path)
                    if not text:
                        continue

                    total_files += 1

                    # 去重 + 写 raw
                    result = guard.safe_write(
                        text.encode("utf-8"),
                        f"raw/zhenti/{path.replace('/', '_')}_{i}.json",
                        url=file_url,
                        source_type="zhenti"
                    )
                    if not result["written"]:
                        total_skipped += 1
                        continue

                    # 解析
                    try:
                        data = json.loads(text)
                    except Exception:
                        continue

                    source_tag = path.replace("/", "_") + f"_{i}"
                    questions = parse_questions_from_json(data, qtype, diff, source_tag)

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
                            total_inserted += 1
                            path_count += 1
                        except Exception:
                            continue

                    await asyncio.sleep(0.2)

                await db.commit()
                if path_count:
                    print(f"    ✅ 入库 {path_count} 题")

    # 3. 最终报告
    print("\n" + "=" * 55)
    print("📊 导入报告")
    print(f"  扫描文件: {total_files} 个")
    print(f"  入库题目: {total_inserted} 道")
    print(f"  跳过重复: {total_skipped} 个")

    report = guard.inspect()
    print(f"  Raw目录:  {report['raw']['size_mb']}MB ({report['raw']['usage_pct']}%)")
    print(f"  数据库:   {report['database']['size_mb']}MB")
    print(f"  状态:     {report['status']}")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())