"""
数据巡检清理脚本 — 定期执行，防止存储爆表
用法：
  python scripts/janitor.py              # 只巡检，不清理
  python scripts/janitor.py --cleanup    # 巡检 + 自动清理
  python scripts/janitor.py --report     # 输出JSON格式报告
Windows任务计划：每天凌晨3点执行
Linux cron：0 3 * * * python /opt/cet-agent/scripts/janitor.py --cleanup
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.storage_guard import StorageGuard


def main():
    parser = argparse.ArgumentParser(description="数据巡检清理工具")
    parser.add_argument("--cleanup", action="store_true", help="执行自动清理")
    parser.add_argument("--report", action="store_true", help="输出JSON报告")
    args = parser.parse_args()

    guard = StorageGuard()

    # 1. 巡检
    report = guard.inspect()

    if args.report:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # 2. 人类可读输出
    print("=" * 50)
    print("🔍 数据存储巡检报告")
    print("=" * 50)
    print(f"  Raw目录:      {report['raw']['size_mb']}MB / {report['raw']['limit_mb']}MB "
          f"({report['raw']['usage_pct']}%)")
    print(f"  Raw文件数:    {report['raw']['file_count']}")
    print(f"  Parsed目录:   {report['parsed']['size_mb']}MB / {report['parsed']['limit_mb']}MB")
    print(f"  数据库:       {report['database']['size_mb']}MB / {report['database']['limit_mb']}MB")
    print(f"  去重指纹数:   {report['checksums_count']}")
    print(f"  URL索引数:    {report['url_index_count']}")
    print(f"  存储状态:     {'✅ 健康' if report['status'] == 'healthy' else '⚠️ 警告'}")

    # 3. 清理
    if args.cleanup:
        print("\n🧹 执行自动清理...")
        cleanup_report = guard.auto_cleanup()
        print(f"  删除raw文件:    {cleanup_report['deleted_raw']} 个")
        print(f"  删除parsed文件: {cleanup_report['deleted_parsed']} 个")
        print(f"  释放空间:       {cleanup_report['freed_bytes'] / 1024 / 1024:.2f}MB")

        if cleanup_report["details"]:
            print("  详情:")
            for d in cleanup_report["details"][:10]:
                print(f"    - {d}")

        # 清理后再次巡检
        print("\n📊 清理后状态:")
        report2 = guard.inspect()
        print(f"  Raw目录:  {report2['raw']['size_mb']}MB ({report2['raw']['usage_pct']}%)")
        print(f"  状态:     {'✅ 健康' if report2['status'] == 'healthy' else '⚠️ 警告'}")

    print("=" * 50)


if __name__ == "__main__":
    main()