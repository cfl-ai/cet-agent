"""
手动训练入口（CLI 版）。

⚠️ 本脚本以前自己拼训练数据，标签是 mastery 自举推导出来的假标签
   （true_score = 290 + avg(mastery)*420），模型只是学会了复述这条公式，
   喂再多数据也不会变准。该逻辑已废弃，现统一委托给 services.learning_service，
   走"真实分数标签 + 验证集择优晋升"的在线学习闭环。

用法：python scripts/train_predictor.py [--force]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import learning_service  # noqa: E402


async def main(force: bool = True):
    print("=" * 55)
    print("训练分数预测模型（在线学习闭环）")
    print("=" * 55)

    status = await learning_service.get_model_status()
    cur = status["current_model"]
    if cur:
        print(f"当前线上模型: v{cur['version']} | 样本 {cur['sample_count']} "
              f"| 验证MAE {cur['val_mae']}")
    else:
        print("当前线上模型: 无（预测走启发式兜底）")
    print(f"可用真实分数标签: {status['available_labels']} "
          f"（有效样本 {status['available_samples']}）")

    result = await learning_service.train_if_needed(force=force)
    if not result.get("trained"):
        print(f"\n未训练：{result.get('reason')}")
        print("提示：需要 ≥5 份有效样本（实考录入或模考折算，"
              "且该用户在考试前答题 ≥5 道）")
        return

    print(f"\n训练完成 | 样本 {result['sample_count']} "
          f"(训练 {result['train_size']} / 验证 {result['val_size']})")
    print(f"  训练 MAE {result['train_mae']} | 验证 MAE {result['val_mae']} "
          f"| 基线 MAE {result['baseline_mae']}")
    if result.get("promoted"):
        print(f"  已晋升为 v{result['version']}（验证误差优于旧模型）")
    else:
        print("  未晋升：候选误差 ≥ 线上模型，保留原模型")
        print("  → 说明这批数据没能提升预测能力，是模型的自我保护")


if __name__ == "__main__":
    asyncio.run(main("--force" in sys.argv))
