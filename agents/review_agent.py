import aiosqlite
import time
import json
from agents.base import BaseAgent
from services.llm_client import llm_client
from models.database import DB_PATH
from core.kt_model import KnowledgeTracer
from core.score_predictor import ScorePredictor


class ReviewAgent(BaseAgent):
    """学情诊断 — 知识追踪 + MLP 过线预测"""

    PASS_SCORE = 425

    def __init__(self):
        super().__init__(name="ReviewAgent", max_retries=2)
        self.kt = KnowledgeTracer()
        self.predictor = ScorePredictor()
        self.predictor.load()

    async def _run(self, task: dict) -> dict:
        user_id = task["user_id"]

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND resolved=0",
                (user_id,)
            )
            (wrong_count,) = await cursor.fetchone()

            cursor = await db.execute(
                """SELECT knowledge_point, mastery FROM knowledge_state
                   WHERE user_id=? ORDER BY mastery ASC LIMIT 5""",
                (user_id,)
            )
            weak_points = [
                {"point": r[0], "mastery": round(r[1], 2)}
                for r in await cursor.fetchall()
            ]

        # 知识追踪获取掌握度
        mastery = await self.kt.get_mastery(user_id)

        # MLP 预测
        prediction = self.predictor.predict(mastery)

        # 生成备考建议
        suggestion = await self._generate_suggestion(
            user_id, mastery, weak_points, wrong_count,
            prediction["predicted_score"]
        )

        return {
            "user_id": user_id,
            "predicted_score": prediction["predicted_score"],
            "pass_probability": prediction["pass_probability"],
            "prediction_model": prediction.get("model", "heuristic"),
            "mastery_vector": {k: round(v, 3) for k, v in mastery.items()},
            "wrong_count": wrong_count,
            "weak_points": weak_points,
            "suggestion": suggestion,
            "timestamp": time.time(),
        }

    async def _generate_suggestion(self, user_id, mastery, weak_points,
                                     wrong_count, predicted) -> str:
        weak_str = "、".join([w["point"] for w in weak_points[:3]]) or "暂无"
        avg = sum(mastery.values()) / len(mastery) if mastery else 0.5
        prompt = f"""基于以下学情数据，给出50字内的针对性备考建议：
- 整体掌握度：{avg:.0%}
- 预测分数：{predicted}
- 薄弱知识点：{weak_str}
- 待解决错题数：{wrong_count}
直接输出建议文本，不要任何前缀。"""
        try:
            return await llm_client.chat([
                {"role": "system", "content": "你是四六级备考规划师。"},
                {"role": "user", "content": prompt},
            ], temperature=0.6, max_tokens=200)
        except Exception:
            return f"建议优先攻克{weak_str}，每日复习错题{wrong_count}道。"

    def _validate_output(self, result: dict):
        if not (0 <= result["pass_probability"] <= 1):
            raise ValueError("过线概率超出范围")