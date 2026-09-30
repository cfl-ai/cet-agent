"""
懒笔记真题自动爬取器
数据源：https://english-exam.lazynote.cn
用法：
  python scripts/crawl_lazynote.py              # 爬取全部
  python scripts/crawl_lazynote.py --level cet4 # 只爬四级
  python scripts/crawl_lazynote.py --since 2020 # 只爬2020年以后
"""
import asyncio
import json
import re
import sys
import time
import warnings
import argparse
from pathlib import Path
from urllib.parse import urljoin

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from core.storage_guard import StorageGuard

# ==================== 配置 ====================
BASE_URL = "https://english-exam.lazynote.cn"
CET4_LIST_URL = f"{BASE_URL}/cet4/"
CET6_LIST_URL = f"{BASE_URL}/cet6/"

# 请求头（模拟浏览器）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

# 下载目录
DOWNLOAD_DIR = Path("data/raw/real_papers")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

guard = StorageGuard()


async def fetch_page(client: httpx.AsyncClient, url: str) -> str | None:
    """拉取HTML页面"""
    try:
        resp = await client.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.text
        print(f"  ⚠️ {url} 返回 {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ 拉取失败 {url}: {type(e).__name__}")
    return None


def extract_paper_links(html: str, base_url: str) -> list[dict]:
    """
    从列表页提取试卷链接
    懒笔记页面结构：每套真题有标题链接和下载按钮
    """
    papers = []

    # 匹配试卷标题和链接
    # 格式可能是 <a href="/cet4/2024-06-1/">2024年6月英语四级真题（第1套）</a>
    title_pattern = re.compile(
        r'<a[^>]+href="([^"]*cet[46][^"]*)"[^>]*>'
        r'([^<]*\d{4}[^<]*)</a>',
        re.IGNORECASE
    )

    for match in title_pattern.finditer(html):
        href = match.group(1)
        title = match.group(2).strip()

        # 从标题提取年份和月份
        year_match = re.search(r'(\d{4})', title)
        month_match = re.search(r'(\d{1,2})月', title)
        set_match = re.search(r'第(\d+)套', title)

        if not year_match:
            continue

        year = int(year_match.group(1))
        month = int(month_match.group(1)) if month_match else 6
        set_num = int(set_match.group(1)) if set_match else 1

        papers.append({
            "title": title,
            "detail_url": urljoin(base_url, href),
            "year": year,
            "month": month,
            "set_number": set_num,
        })

    return papers


def extract_download_links(html: str, base_url: str) -> dict:
    """
    从试卷详情页提取下载链接
    返回：{"pdf": "...", "pdf_answer": "...", "word": "..."}
    """
    downloads = {}

    # 匹配PDF下载链接
    pdf_pattern = re.compile(
        r'<a[^>]+href="([^"]*\.pdf)"[^>]*>.*?'
        r'(?:真题|试卷|PDF|下载|整卷).*?</a>',
        re.IGNORECASE | re.DOTALL
    )
    for m in pdf_pattern.finditer(html):
        url = urljoin(base_url, m.group(1))
        if "answer" in url.lower() or "解析" in m.group(0):
            downloads["pdf_answer"] = url
        else:
            downloads.setdefault("pdf", url)

    # 匹配Word下载链接
    word_pattern = re.compile(
        r'<a[^>]+href="([^"]*\.(?:docx?|word))"[^>]*>.*?'
        r'(?:Word|可编辑|下载).*?</a>',
        re.IGNORECASE | re.DOTALL
    )
    for m in word_pattern.finditer(html):
        downloads["word"] = urljoin(base_url, m.group(1))
        break

    return downloads


async def download_file(client: httpx.AsyncClient, url: str,
                        save_path: Path) -> bool:
    """下载文件并保存到本地"""
    try:
        resp = await client.get(url, timeout=120)
        if resp.status_code == 200 and len(resp.content) > 1000:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(resp.content)
            return True
        print(f"  ⚠️ 下载失败 {url}: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ 下载异常 {url}: {type(e).__name__}")
    return False


async def process_paper(client: httpx.AsyncClient, paper: dict,
                        level: str) -> dict:
    """处理单套真题：拉详情页 → 下载文件 → 去重"""
    result = {"title": paper["title"], "downloaded": [], "skipped": []}

    # 1. 拉取详情页
    html = await fetch_page(client, paper["detail_url"])
    if not html:
        return result

    # 2. 提取下载链接
    downloads = extract_download_links(html, BASE_URL)
    if not downloads:
        # 如果详情页没有直接链接，尝试从列表页HTML中提取
        print(f"    ⚠️ 未找到下载链接: {paper['title']}")
        return result

    # 3. 构造文件名
    year, month, set_num = paper["year"], paper["month"], paper["set_number"]
    base_name = f"{level}_{year}_{month:02d}_set{set_num}"

    # 4. 下载每种格式
    for fmt, url in downloads.items():
        ext = ".pdf" if "pdf" in fmt else ".docx"
        save_path = DOWNLOAD_DIR / f"{base_name}_{fmt}{ext}"

        # 检查是否已下载（URL去重）
        is_dup, existing = guard.is_duplicate_url(url)
        if is_dup:
            result["skipped"].append(fmt)
            continue

        # 下载
        ok = await download_file(client, url, save_path)
        if ok:
            # 注册到去重索引
            content = save_path.read_bytes()
            guard.register_content(
                content, str(save_path), url=url, source_type="real_paper"
            )
            result["downloaded"].append(fmt)
            print(f"    ✅ 下载 {fmt}: {save_path.name}")
        else:
            result["skipped"].append(fmt)

        await asyncio.sleep(0.5)  # 限速

    return result


async def crawl_level(client: httpx.AsyncClient, level: str,
                      since_year: int = 2015):
    """爬取一个级别的全部真题"""
    list_url = CET4_LIST_URL if level == "cet4" else CET6_LIST_URL
    print(f"\n📚 爬取 {level.upper()}...")

    # 1. 拉取列表页
    html = await fetch_page(client, list_url)
    if not html:
        print(f"  ❌ 列表页拉取失败")
        return

    # 2. 提取试卷链接
    papers = extract_paper_links(html, BASE_URL)
    print(f"  ✅ 发现 {len(papers)} 套试卷")

    # 3. 过滤年份
    papers = [p for p in papers if p["year"] >= since_year]
    print(f"  ✅ 筛选后 {len(papers)} 套 (since {since_year})")

    # 4. 逐套处理
    total_downloaded = 0
    total_skipped = 0

    for i, paper in enumerate(papers):
        print(f"\n  [{i+1}/{len(papers)}] {paper['title']}")
        result = await process_paper(client, paper, level)
        total_downloaded += len(result["downloaded"])
        total_skipped += len(result["skipped"])

        # 每套间隔1秒，避免触发反爬
        await asyncio.sleep(1.0)

    print(f"\n  ✅ {level.upper()} 完成: 下载 {total_downloaded} 文件, "
          f"跳过 {total_skipped} 个")


async def main():
    parser = argparse.ArgumentParser(description="懒笔记真题自动爬取")
    parser.add_argument("--level", choices=["cet4", "cet6", "all"],
                        default="all", help="爬取级别")
    parser.add_argument("--since", type=int, default=2015,
                        help="起始年份（默认2015）")
    args = parser.parse_args()

    print("=" * 55)
    print("📥 懒笔记真题自动爬取工具")
    print("=" * 55)

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        verify=False,
        timeout=120,
    ) as client:
        levels = ["cet4", "cet6"] if args.level == "all" else [args.level]
        for level in levels:
            await crawl_level(client, level, args.since)

    # 最终巡检
    report = guard.inspect()
    print("\n" + "=" * 55)
    print("📊 存储巡检")
    print(f"  Raw目录:   {report['raw']['size_mb']}MB ({report['raw']['usage_pct']}%)")
    print(f"  状态:      {report['status']}")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())