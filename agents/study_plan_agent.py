"""
学情预测与学习方案 Agent
基于真实学情数据，由 LLM 生成可执行、可量化的备考方案。
"""
import json
import logging
import time
import re
import math
import aiosqlite
from agents.base import BaseAgent
from services.llm_client import llm_client
from models.database import DB_PATH

logger = logging.getLogger(__name__)


class StudyPlanAgent(BaseAgent):

    PROMPT_TEMPLATE = """你是一位有15年经验的四六级备考规划师，专精于基于数据诊断制定提分方案。

【考生真实学情数据】
- 目标级别：{level}
- 目标分数：{target_score} 分（合格线425）
- 备考剩余：{days_left} 天
- 累计答题：{total_questions} 道
- 整体正确率：{overall_accuracy}
- 当前预测分数：{predicted_score} 分
- 过线概率：{pass_probability}

【分题型正确率（真实统计）】
{type_accuracy}

【最薄弱知识点（掌握度最低5个）】
{weak_points}

【错题类型分布】
{error_types}

【最近10次答题表现（新→旧）】
{recent_performance}

【硬性要求 — 必须遵守】
1. 所有建议必须**引用上述具体数据**，禁止空泛套话
2. 必须针对薄弱知识点给出**具体训练动作**（做什么题、用什么材料、练多久）
3. 学习计划必须包含**每日任务量**（单词数、题目数、听力时长）
4. 必须给出**可量化的阶段性目标**（如"7天内将选词填空正确率从40%提升到65%"）
5. 若预测分数≥425，重点放在**冲高分**；若<425，重点放在**补短板**
6. 禁止推荐付费课程或具体产品，只给通用方法和资源类型
7. 计划要与剩余天数匹配，不要给出超过剩余时间的任务
8. 所有字段必须是**字符串类型**，禁止返回对象或数组（如 "how" 必须是字符串，不能是 {{"frequency": "xxx"}}）
【输出格式】严格输出以下 JSON，不要任何 markdown 标记：
{{
    "analysis": {{
        "current_level": "基于数据的真实水平定位（1-2句话，引用具体正确率）",
        "gap_analysis": "与目标的差距分析（1-2句话，量化差距）",
        "key_issues": ["最关键的3个问题，每条包含数据和现象"]
    }},
        "methods": [
        {{
            "name": "学习方法名称（字符串）",
            "why": "为什么适合该考生（字符串，必须引用具体数据）",
            "how": "具体怎么做（字符串，含频率、时长、材料类型，例如：'每天早读30分钟，使用真题听力材料，配合跟读训练'）"
         }}
    ],
    "tips": [
        {{
            "topic": "技巧主题",
            "content": "具体技巧（可立即执行）"
        }}
    ],
    "study_plan": {{
        "phase_1": {{
            "name": "阶段名称",
            "duration_days": 天数,
            "focus": "重点方向",
            "daily_tasks": [
                {{"task": "任务名", "amount": "数量/时长", "goal": "达成目标"}}
            ]
        }},
        "phase_2": {{
            "name": "阶段名称",
            "duration_days": 天数,
            "focus": "重点方向",
            "daily_tasks": []
        }},
        "phase_3": {{
            "name": "阶段名称",
            "duration_days": 天数,
            "focus": "重点方向",
            "daily_tasks": []
        }}
    }},
    "milestones": [
        {{"day": 天数, "target": "可量化的目标（含具体数字）"}}
    ]
}}"""

    def __init__(self):
        super().__init__(name="StudyPlanAgent", max_retries=2, idempotent=False)

    async def _run(self, task: dict) -> dict:
        user_id = task["user_id"]
        target_score = task.get("target_score", 425)
        days_left = task.get("days_left", 60)
        level = task.get("level", "CET4")

        # 1. 收集真实学情数据
        stats = await self._collect_stats(user_id)

        # 2. 数据不足时提示
        if stats["total_questions"] < 5:
            return {
                "ready": False,
                "message": f"当前仅答题 {stats['total_questions']} 道，数据不足。"
                           f"建议先完成至少 20 道题（含各题型），系统才能给出有依据的方案。",
                "stats": stats,
            }

        # 3. 构建 Prompt
        prompt = self.PROMPT_TEMPLATE.format(
            level=level,
            target_score=target_score,
            days_left=days_left,
            total_questions=stats["total_questions"],
            overall_accuracy=f"{stats['overall_accuracy']:.1%}",
            predicted_score=stats["predicted_score"],
            pass_probability=f"{stats['pass_probability']:.1%}",
            type_accuracy=self._format_type_accuracy(stats["type_accuracy"]),
            weak_points=self._format_weak_points(stats["weak_points"]),
            error_types=self._format_error_types(stats["error_types"]),
            recent_performance=self._format_recent(stats["recent_performance"]),
        )

        # 4. 调用 LLM
        content = await llm_client.chat([
            {"role": "system",
             "content": "你是资深四六级备考规划师，只输出 JSON，所有建议必须基于数据。"},
            {"role": "user", "content": prompt},
        ], temperature=0.4, max_tokens=2500)

        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n?", "", content)
            content = re.sub(r"\n?```$", "", content)

        plan = json.loads(content)

        return {
            "ready": True,
            "generated_at": time.time(),
            "stats": stats,
            "plan": plan,
        }

    async def _collect_stats(self, user_id: str) -> dict:
        """收集真实学情数据"""
        async with aiosqlite.connect(DB_PATH) as db:
            # 1. 总体统计
            cursor = await db.execute(
                "SELECT COUNT(*), SUM(is_correct) FROM answer_records WHERE user_id=?",
                (user_id,)
            )
            total, correct = await cursor.fetchone()
            total = total or 0
            correct = correct or 0
            overall_accuracy = correct / total if total else 0

            # 2. 分题型正确率
            cursor = await db.execute("""
                SELECT q.question_type,
                       COUNT(*) as cnt,
                       SUM(ar.is_correct) as correct_cnt
                FROM answer_records ar
                JOIN questions q ON ar.question_id = q.question_id
                WHERE ar.user_id=?
                GROUP BY q.question_type
            """, (user_id,))
            type_rows = await cursor.fetchall()
            type_accuracy = {}
            for qtype, cnt, corr in type_rows:
                type_accuracy[qtype or "未知"] = {
                    "total": cnt,
                    "correct": corr or 0,
                    "accuracy": (corr or 0) / cnt if cnt else 0,
                }

            # 3. 最薄弱知识点
            cursor = await db.execute("""
                SELECT knowledge_point, mastery, attempts
                FROM knowledge_state
                WHERE user_id=? AND attempts >= 2
                ORDER BY mastery ASC LIMIT 5
            """, (user_id,))
            weak_points = [
                {"point": r[0], "mastery": round(r[1], 2), "attempts": r[2]}
                for r in await cursor.fetchall()
            ]

            # 4. 错题类型分布
            cursor = await db.execute("""
                SELECT error_type, COUNT(*) FROM wrong_questions
                WHERE user_id=? AND resolved=0
                GROUP BY error_type
            """, (user_id,))
            error_types = {r[0] or "knowledge_gap": r[1]
                           for r in await cursor.fetchall()}

            # 5. 最近10次答题（新→旧）
            cursor = await db.execute("""
                SELECT is_correct FROM answer_records
                WHERE user_id=?
                ORDER BY created_at DESC LIMIT 10
            """, (user_id,))
            recent = [r[0] for r in await cursor.fetchall()]

            # 6. 未解决错题数
            cursor = await db.execute(
                "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND resolved=0",
                (user_id,)
            )
            (wrong_count,) = await cursor.fetchone()

        # 7. 预测分数（启发式）
        predicted_score = int(290 + overall_accuracy * 420)
        predicted_score = max(290, min(710, predicted_score))
        pass_prob = 1 / (1 + math.exp(-(predicted_score - 425) / 50))

        return {
            "total_questions": total,
            "correct": correct,
            "overall_accuracy": round(overall_accuracy, 3),
            "predicted_score": predicted_score,
            "pass_probability": round(pass_prob, 3),
            "type_accuracy": type_accuracy,
            "weak_points": weak_points,
            "error_types": error_types,
            "recent_performance": recent,
            "wrong_count": wrong_count,
        }

    def _format_type_accuracy(self, data: dict) -> str:
        if not data:
            return "  （暂无分题型数据）"
        lines = []
        for qtype, info in sorted(data.items(), key=lambda x: x[1]["accuracy"]):
            lines.append(
                f"  - {qtype}: {info['accuracy']:.0%} "
                f"({info['correct']}/{info['total']} 题)"
            )
        return "\n".join(lines)

    def _format_weak_points(self, points: list) -> str:
        if not points:
            return "  （数据不足，需更多练习）"
        return "\n".join([
            f"  - {p['point']}: 掌握度 {p['mastery']:.0%}（练习 {p['attempts']} 次）"
            for p in points
        ])

    def _format_error_types(self, data: dict) -> str:
        if not data:
            return "  （暂无错题）"
        label_map = {
            "knowledge_gap": "知识盲区",
            "careless": "粗心",
            "logic_error": "逻辑错误",
            "timeout": "时间不足",
        }
        return "\n".join([
            f"  - {label_map.get(k, k)}: {v} 道"
            for k, v in sorted(data.items(), key=lambda x: -x[1])
        ])

    def _format_recent(self, recent: list) -> str:
        if not recent:
            return "  （无记录）"
        marks = ["✓" if x else "✗" for x in recent]
        acc = sum(recent) / len(recent)
        return f"  {' '.join(marks)}  （最近10题正确率 {acc:.0%}）"

    def _validate_output(self, result: dict):
        if "ready" not in result:
            raise ValueError("缺少 ready 字段")
        if result["ready"] and "plan" not in result:
            raise ValueError("缺少 plan 字段")