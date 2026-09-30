"""
真题自动更新一键脚本
爬取 → 下载 → 解析 → 入库
用法：python scripts/auto_update_papers.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main():
    print("=" * 55)
    print("🔄 真题自动更新流水线")
    print("=" * 55)

    # 1. 爬取下载
    print("\n[1/2] 爬取懒笔记真题...")
    result = subprocess.run(
        [sys.executable, "scripts/crawl_lazynote.py"],
        capture_output=False,
    )
    if result.returncode != 0:
        print("⚠️ 爬取过程出现问题，继续执行解析...")

    # 2. 解析入库
    print("\n[2/2] 解析Word文档并入库...")
    result = subprocess.run(
        [sys.executable, "scripts/parse_word_papers.py"],
        capture_output=False,
    )

    print("\n✅ 自动更新完成")


if __name__ == "__main__":
    asyncio.run(main())