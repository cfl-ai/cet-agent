import json
import re
import aiosqlite
import time
from agents.base import BaseAgent
from services.llm_client import llm_client
from models.database import DB_PATH

class GradingAgent(BaseAgent):
    """评分Agent — 客观题直接判分，主观题调用LLM评分"""

    WRITING_PROMPT = """你是四六级写作阅卷老师。请根据官方评分标准（内容完整、语言运用、组织连贯、切题程度）为以下作文打分。

题目要求：{topic}
学生作文：{essay}

请严格按JSON格式输出：
{{
    "score": 分数（0-15整数）,
    "level": "档次（14分档/11分档/8分档/5分档/2分档）",
    "content_score": 内容得分(0-5),
    "language_score": 语言得分(0-5),
    "organization_score": 组织得分(0-5),
    "feedback": "详细评语（150字内）",
    "ai_traces": ["检测到的模板套句或AI痕迹"],
    "improvements": ["具体改进建议1", "建议2"]
}}"""

    def __init__(self):
        super().__init__(name="GradingAgent", max_retries=2)

    async def _run(self, task: dict) -> dict:
        answers = task.get("answers", [])
        results = []
        correct_count = 0

        async with aiosqlite.connect(DB_PATH) as db:
            for ans in answers:
                qid = ans["question_id"]
                cursor = await db.execute(
                    "SELECT answer, explanation, knowledge_tags FROM questions WHERE question_id = ?",
                    (qid,)
                )
                row = await cursor.fetchone()
                if not row:
                    continue

                correct_answer, explanation, tags = row
                is_correct = ans["user_answer"].strip().upper() == correct_answer.strip().upper()

                # 记录答题
                await db.execute(
                    """INSERT INTO answer_records
                       (user_id, question_id, user_answer, correct_answer,
                        is_correct, duration, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (task["user_id"], qid, ans["user_answer"], correct_answer,
                     1 if is_correct else 0, ans.get("duration", 0), time.time())
                )

                if is_correct:
                    correct_count += 1
                    # 更新知识状态
                    await self._update_knowledge(db, task["user_id"], tags, True)
                else:
                    # 写入错题本
                    await db.execute(
                        """INSERT INTO wrong_questions
                           (user_id, question_id, wrong_count, error_type, last_wrong_at)
                           VALUES (?, ?, 1, ?, ?)
                           ON CONFLICT(user_id, question_id) DO UPDATE SET
                           wrong_count = wrong_count + 1,
                           last_wrong_at = excluded.last_wrong_at""",
                        (task["user_id"], qid, self._infer_error_type(ans), time.time())
                    )
                    await self._update_knowledge(db, task["user_id"], tags, False)

                results.append({
                    "question_id": qid,
                    "is_correct": is_correct,
                    "correct_answer": correct_answer,
                    "explanation": explanation,
                })

            await db.commit()

        return {
            "user_id": task["user_id"],
            "total": len(results),
            "correct": correct_count,
            "accuracy": round(correct_count / len(results), 3) if results else 0,
            "details": results,
        }

    async def _update_knowledge(self, db, user_id: str, tags_json: str, correct: bool):
        """简化版知识追踪 — 指数移动平均"""
        try:
            tags = json.loads(tags_json) if tags_json else []
        except Exception:
            tags = []

        for tag in tags:
            cursor = await db.execute(
                "SELECT mastery, attempts FROM knowledge_state WHERE user_id=? AND knowledge_point=?",
                (user_id, tag)
            )
            row = await cursor.fetchone()
            if row:
                mastery, attempts = row
                # EMA更新：0.3权重给新观察
                new_mastery = mastery * 0.7 + (1.0 if correct else 0.0) * 0.3
                await db.execute(
                    "UPDATE knowledge_state SET mastery=?, attempts=attempts+1, last_update=? WHERE user_id=? AND knowledge_point=?",
                    (new_mastery, time.time(), user_id, tag)
                )
            else:
                await db.execute(
                    """INSERT INTO knowledge_state
                       (user_id, knowledge_point, mastery, attempts, correct, last_update)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (user_id, tag, 1.0 if correct else 0.0, 1 if correct else 0, time.time())
                )

    def _infer_error_type(self, ans: dict) -> str:
        """简化错误类型推断 — 可扩展为LLM判断"""
        return "knowledge_gap"  # knowledge_gap / careless / logic_error

    async def grade_writing(self, topic: str, essay: str) -> dict:
        """主观题评分（写作/翻译）"""
        content = await llm_client.chat([
            {"role": "system", "content": "你是专业的四六级阅卷老师。"},
            {"role": "user", "content": self.WRITING_PROMPT.format(topic=topic, essay=essay)},
        ], temperature=0.3)

        content = self._clean_json(content)
        return json.loads(content)

    def _clean_json(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return text

    def _validate_output(self, result: dict):
        if "accuracy" not in result:
            raise ValueError("评分结果缺少accuracy字段")