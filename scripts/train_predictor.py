"""
训练分数预测模型
用法：python scripts/train_predictor.py
"""
import asyncio
import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite
from models.database import DB_PATH
from core.kt_model import KnowledgeTracer
from core.score_predictor import ScorePredictor


async def main():
    print("=" * 55)
    print("🧠 训练分数预测模型")
    print("=" * 55)

    kt = KnowledgeTracer()
    predictor = ScorePredictor()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM answer_records"
        )
        users = [r[0] for r in await cursor.fetchall()]

    if len(users) < 5:
        print(f"⚠️ 用户数不足（{len(users)} < 5），先积累更多答题数据")
        return

    print(f"📊 发现 {len(users)} 个用户")

    X_list, y_score_list, y_pass_list = [], [], []

    for uid in users:
        mastery = await kt.get_mastery(uid)
        if not mastery:
            continue
        features = predictor.prepare_features(mastery)
        X_list.append(features[0])

        avg = float(np.mean(list(mastery.values())))
        true_score = 290 + avg * 420
        y_score_list.append(true_score)
        y_pass_list.append(1 if true_score >= 425 else 0)

    if len(X_list) < 5:
        print("⚠️ 有效样本不足，无法训练")
        return

    X = np.array(X_list)
    y_score = np.array(y_score_list)
    y_pass = np.array(y_pass_list)

    print(f"📊 训练样本: {len(X)} 条")
    predictor.train(X, y_score, y_pass)
    print("✅ 模型训练完成并保存到 data/models/")

    result = predictor.predict(await kt.get_mastery(users[0]))
    print(f"📊 用户 {users[0]} 预测: 分数={result['predicted_score']}, "
          f"过线概率={result['pass_probability']}")


if __name__ == "__main__":
    asyncio.run(main())