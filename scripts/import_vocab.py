"""
词库导入脚本 — 修复筛选逻辑
数据源：
1. mahavivo/english-wordlists (CET4/CET6 TXT词库)
2. dtMndas/english-wordlists (COCA 20000词频表)
用法：python scripts/import_vocab.py
"""
import asyncio
import json
import sys
import time
import re
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

VOCAB_SOURCES = {
    "cet4": {
        "ghproxy": f"{GHPROXY}/https://raw.githubusercontent.com/mahavivo/english-wordlists/master/CET4_edited.txt",
        "raw": f"{RAW_GITHUB}/mahavivo/english-wordlists/master/CET4_edited.txt",
        "local": "data/raw/vocab/CET4_edited.txt",
    },
    "cet6": {
        "ghproxy": f"{GHPROXY}/https://raw.githubusercontent.com/mahavivo/english-wordlists/master/CET6_edited.txt",
        "raw": f"{RAW_GITHUB}/mahavivo/english-wordlists/master/CET6_edited.txt",
        "local": "data/raw/vocab/CET6_edited.txt",
    },
    "coca": {
        "ghproxy": f"{GHPROXY}/https://raw.githubusercontent.com/dtMndas/english-wordlists/master/COCA_20000.txt",
        "raw": f"{RAW_GITHUB}/dtMndas/english-wordlists/master/COCA_20000.txt",
        "local": "data/raw/vocab/COCA_20000.txt",
    },
}

# ★ 排除初高中基础词 —— COCA 排名前 3000 的词大多是初高中词汇，
# 这些词不需要在四六级复习中重复出现
BASE_WORD_RANK_THRESHOLD = 3000

# ★ 词汇最小长度（太短的词多为功能词）
MIN_WORD_LENGTH = 4

guard = StorageGuard()


async def fetch_text(client: httpx.AsyncClient, url_key: str) -> str | None:
    """优先本地缓存，其次 ghproxy，最后 raw"""
    sources = VOCAB_SOURCES.get(url_key, {})

    # 1. 本地缓存
    local_path = sources.get("local")
    if local_path and Path(local_path).exists():
        print(f"  ✅ [本地] 读取: {local_path}")
        return Path(local_path).read_text(encoding="utf-8", errors="ignore")

    # 2. 网络拉取
    for label in ["ghproxy", "raw"]:
        url = sources.get(label)
        if not url:
            continue
        try:
            resp = await client.get(url, timeout=120)
            if resp.status_code == 200 and len(resp.text) > 100:
                print(f"  ✅ [{label}] 成功: {url[:70]}...")
                if local_path:
                    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(local_path).write_text(resp.text, encoding="utf-8")
                return resp.text
            print(f"  ⚠️ [{label}] {url[:70]} 返回 {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️ [{label}] {url[:70]} 失败: {type(e).__name__}")
            continue
    return None


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
    """解析 mahavivo 格式的 TXT 词库"""
    words = []
    if not text:
        return words

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("大学英语") or line.startswith("CET") or line.startswith("#"):
            continue

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

        meaning = meaning[:200]
        freq_rank = coca_freq.get(word.lower(), 99999)

        words.append({
            "word": word,
            "phonetic": phonetic,
            "meaning": meaning,
            "level": level.upper(),
            "frequency": freq_rank,
            "topic": "通用",
            "examples": json.dumps([], ensure_ascii=False),
        })

    return words


def select_high_freq(words: list[dict], top_n: int = 2000) -> list[dict]:
    """
    智能筛选高频词：
    1. 排除 COCA 排名前 3000 的基础词（初高中水平）
    2. 排除过短的词（<4 字符多为功能词）
    3. 按 COCA 频率排序（词频值越大越难，即越靠后越难）
       —— 改为按"词频值从小到大"排序，但起点是 3000 之后
    """
    filtered = []
    for w in words:
        word = w["word"].lower()
        # 排除基础词
        if w["frequency"] <= BASE_WORD_RANK_THRESHOLD:
            continue
        # 排除过短词
        if len(word) < MIN_WORD_LENGTH:
            continue
        # 排除纯数字/符号词
        if not re.match(r"^[a-zA-Z\-']+$", word):
            continue
        filtered.append(w)

    # 按 COCA 频率升序（越靠前越常用），选出最常用的 N 个
    sorted_words = sorted(filtered, key=lambda w: w["frequency"])
    return sorted_words[:top_n]


async def main():
    print("=" * 55)
    print("📚 四六级词库导入工具 (修复版)")
    print("=" * 55)

    await init_db()

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 CET-Agent/1.0"},
        follow_redirects=True,
        verify=False,
        timeout=120,
    ) as client:
        # 1. COCA词频表
        print("\n📊 拉取COCA词频表...")
        coca_text = await fetch_text(client, "coca")
        coca_freq = parse_coca_frequency(coca_text) if coca_text else {}
        print(f"  ✅ COCA词频表加载: {len(coca_freq)} 词")

        # 2. 四级词库
        print("\n📖 拉取四级词库...")
        cet4_text = await fetch_text(client, "cet4")
        cet4_words = parse_cet_txt(cet4_text, "CET4", coca_freq) if cet4_text else []
        print(f"  ✅ 四级词库解析: {len(cet4_words)} 词")

        # 3. 六级词库
        print("\n📖 拉取六级词库...")
        cet6_text = await fetch_text(client, "cet6")
        cet6_words = parse_cet_txt(cet6_text, "CET6", coca_freq) if cet6_text else []
        print(f"  ✅ 六级词库解析: {len(cet6_words)} 词")

        if not cet4_words and not cet6_words:
            print("\n❌ 词库源均拉取失败")
            return

        # 4. 智能筛选高频词
        print("\n🎯 智能筛选高频词（排除初高中基础词）...")
        cet4_top = select_high_freq(cet4_words, 2000)
        cet6_top = select_high_freq(cet6_words, 2000)
        all_words = cet4_top + cet6_top
        print(f"  四级高频: {len(cet4_top)} 词 (原始 {len(cet4_words)})")
        print(f"  六级高频: {len(cet6_top)} 词 (原始 {len(cet6_words)})")

        # 预览前 10 个筛选结果
        print("\n  📋 四级高频词预览 (前10):")
        for w in cet4_top[:10]:
            print(f"    [{w['frequency']:>5}] {w['word']:<20} {w['meaning'][:40]}")

        # 5. 写入raw暂存区
        print("\n💾 写入raw暂存区...")
        raw_data = {
            "cet4_high_freq": cet4_top,
            "cet6_high_freq": cet6_top,
            "generated_at": time.time(),
        }
        result = guard.safe_write_json(
            raw_data,
            "raw/vocab/cet4_cet6_high_freq.json",
            url=VOCAB_SOURCES["cet4"]["ghproxy"],
            source_type="vocab"
        )
        if result["written"]:
            print(f"  ✅ 写入: {result['path']}")
        else:
            print(f"  ⚠️ 跳过 (原因: {result['reason']})")

        # 6. 清空旧词库并重新写入
        print("\n💾 写入数据库...")
        async with aiosqlite.connect(DB_PATH) as db:
            # ★ 清空旧词库，避免之前的错误数据残留
            await db.execute("DELETE FROM vocabulary")
            await db.commit()
            print("  🧹 已清空旧词库")

            inserted = 0
            for w in all_words:
                try:
                    await db.execute("""
                        INSERT INTO vocabulary
                        (word, phonetic, meaning, level, frequency, topic, examples)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        w["word"], w["phonetic"], w["meaning"],
                        w["level"], w["frequency"], w["topic"], w["examples"]
                    ))
                    inserted += 1
                except Exception:
                    continue
            await db.commit()

        print(f"  ✅ 新增 {inserted} 词")

    # 7. 巡检
    report = guard.inspect()
    print("\n" + "=" * 55)
    print("📊 存储巡检")
    print(f"  Raw目录:   {report['raw']['size_mb']}MB ({report['raw']['usage_pct']}%)")
    print(f"  数据库:    {report['database']['size_mb']}MB")
    print(f"  状态:      {report['status']}")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())