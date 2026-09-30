"""
知识追踪模型 — BKT (贝叶斯知识追踪)
依赖: pip install pyBKT pandas
"""
import json
import numpy as np
import aiosqlite
from pathlib import Path
from models.database import DB_PATH


class KnowledgeTracer:
    """
    基于 pyBKT 的知识追踪器
    BKT 参数：
      prior  = P(L0)  初始掌握概率
      learn  = P(T)   学习率
      guess  = P(G)   猜测率
      slip   = P(S)   失误率
    """

    def __init__(self):
        self.model = None
        self._trained = False

    async def collect_data(self, user_id: str = None) -> list[dict]:
        """从数据库收集答题数据"""
        async with aiosqlite.connect(DB_PATH) as db:
            if user_id:
                cursor = await db.execute("""
                    SELECT ar.user_id, ar.is_correct, ar.created_at,
                           q.knowledge_tags, q.question_id
                    FROM answer_records ar
                    JOIN questions q ON ar.question_id = q.question_id
                    WHERE ar.user_id = ?
                    ORDER BY ar.created_at ASC
                """, (user_id,))
            else:
                cursor = await db.execute("""
                    SELECT ar.user_id, ar.is_correct, ar.created_at,
                           q.knowledge_tags, q.question_id
                    FROM answer_records ar
                    JOIN questions q ON ar.question_id = q.question_id
                    ORDER BY ar.created_at ASC
                """)
            rows = await cursor.fetchall()

        records = []
        for uid, correct, ts, tags_json, qid in rows:
            try:
                tags = json.loads(tags_json) if tags_json else ["通用"]
            except Exception:
                tags = ["通用"]
            for tag in tags:
                records.append({
                    "user_id": uid,
                    "correct": int(correct),
                    "order_id": int(ts * 1000),
                    "skill_name": tag,
                })
        return records

    async def get_mastery(self, user_id: str) -> dict:
        """
        获取用户对每个知识点的掌握概率
        返回: {skill: mastery_probability}
        """
        records = await self.collect_data(user_id)
        if not records:
            return {}

        # 按技能分组
        skill_records = {}
        for r in records:
            skill = r["skill_name"]
            if skill not in skill_records:
                skill_records[skill] = []
            skill_records[skill].append(r)

        mastery = {}
        for skill, recs in skill_records.items():
            corrects = [r["correct"] for r in recs]
            if not corrects:
                mastery[skill] = 0.0
                continue
            # 最近10次答题权重更高
            recent = corrects[-10:]
            weights = np.linspace(0.5, 1.0, len(recent))
            mastery[skill] = float(np.average(recent, weights=weights))
        return mastery

    async def fit_bkt(self, user_id: str = None) -> dict:
        """拟合 BKT 模型（用于分析，非实时预测）"""
        try:
            from pyBKT.models import Model
            import pandas as pd
        except ImportError:
            return {}

        records = await self.collect_data(user_id)
        if len(records) < 10:
            return {}

        df = pd.DataFrame(records)
        model = Model(seed=42, num_fits=1)
        try:
            model.fit(data=df, skills=".".join(df["skill_name"].unique()))
            self.model = model
            self._trained = True
        except Exception:
            pass
        return {}