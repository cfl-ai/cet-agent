import json
import re
import aiosqlite
import time
import logging
from agents.base import BaseAgent
from services.llm_client import llm_client
from models.database import DB_PATH

logger = logging.getLogger(__name__)


class GradingAgent(BaseAgent):
    """评分Agent — 客观题判分，主观题跳过硬判定"""

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

    # ★ 主观题类型：不做 ABCD 硬判定
    SUBJECTIVE_TYPES = {"写作", "翻译-汉译英"}

    def __init__(self):
        super().__init__(name="GradingAgent", max_retries=2)

    async def _run(self, task: dict) -> dict:
        answers = task.get("answers", [])
        results = []
        correct_count = 0

        async with aiosqlite.connect(DB_PATH) as db:
            for ans in answers:
                qid = ans.get("question_id")
                user_ans = (ans.get("user_answer") or "").strip()

                if not qid:
                    continue

                cursor = await db.execute(
                    "SELECT answer, explanation, knowledge_tags, question_type "
                    "FROM questions WHERE question_id = ?",
                    (qid,)
                )
                row = await cursor.fetchone()
                if not row:
                    logger.warning(f"[GradingAgent] 题目不存在: {qid}")
                    continue

                correct_answer = row[0] or ""
                explanation = row[1] or ""
                tags = row[2] or "[]"
                qtype = row[3] or ""

                # ★ 主观题：不判对错，只展示参考答案
                if qtype in self.SUBJECTIVE_TYPES:
                    results.append({
                        "question_id": qid,
                        "question_type": qtype,
                        "is_correct": None,
                        "user_answer": user_ans,
                        "correct_answer": correct_answer or "见解析",
                        "explanation": explanation or "主观题请对照参考答案自查。",
                        "skipped": True,
                    })
                    continue

                # 客观题：正常判分
                is_correct = user_ans.upper() == correct_answer.strip().upper()

                # 记录答题
                await db.execute("""
                    INSERT INTO answer_records
                    (user_id, question_id, user_answer, correct_answer,
                     is_correct, duration, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    task["user_id"], qid, user_ans, correct_answer,
                    1 if is_correct else 0, ans.get("duration", 0), time.time()
                ))

                if is_correct:
                    correct_count += 1
                    await self._update_knowledge(db, task["user_id"], tags, True)
                else:
                    # 写入错题本
                    await db.execute("""
                        INSERT INTO wrong_questions
                        (user_id, question_id, wrong_count, error_type, last_wrong_at)
                        VALUES (?, ?, 1, ?, ?)
                        ON CONFLICT(user_id, question_id) DO UPDATE SET
                        wrong_count = wrong_count + 1,
                        last_wrong_at = excluded.last_wrong_at
                    """, (task["user_id"], qid,
                          self._infer_error_type(ans), time.time()))
                    await self._update_knowledge(db, task["user_id"], tags, False)

                results.append({
                    "question_id": qid,
                    "question_type": qtype,
                    "is_correct": is_correct,
                    "user_answer": user_ans,
                    "correct_answer": correct_answer,
                    "explanation": explanation,
                })

            await db.commit()

        # 只统计客观题
        objective = [r for r in results if not r.get("skipped")]
        total_obj = len(objective)
        accuracy = correct_count / total_obj if total_obj else 0

        return {
            "user_id": task["user_id"],
            "total": len(results),
            "objective_total": total_obj,
            "correct": correct_count,
            "accuracy": round(accuracy, 3),
            "details": results,
        }

    async def _update_knowledge(self, db, user_id: str, tags_json: str,
                                  correct: bool):
        """简化版知识追踪 — 指数移动平均"""
        try:
            tags = json.loads(tags_json) if tags_json else []
        except Exception:
            tags = []

        for tag in tags:
            cursor = await db.execute(
                "SELECT mastery, attempts FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                (user_id, tag)
            )
            row = await cursor.fetchone()
            if row:
                mastery, attempts = row
                new_mastery = mastery * 0.7 + (1.0 if correct else 0.0) * 0.3
                await db.execute(
                    "UPDATE knowledge_state SET mastery=?, attempts=attempts+1, "
                    "last_update=? WHERE user_id=? AND knowledge_point=?",
                    (new_mastery, time.time(), user_id, tag)
                )
            else:
                await db.execute(
                    """INSERT INTO knowledge_state
                       (user_id, knowledge_point, mastery, attempts, correct, last_update)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (user_id, tag, 1.0 if correct else 0.0,
                     1 if correct else 0, time.time())
                )

    def _infer_error_type(self, ans: dict) -> str:
        """简化错误类型推断"""
        return "knowledge_gap"

    async def grade_writing(self, topic: str, essay: str) -> dict:
        """写作评分（单独调用）"""
        content = await llm_client.chat([
            {"role": "system", "content": "你是专业的四六级阅卷老师。"},
            {"role": "user", "content": self.WRITING_PROMPT.format(
                topic=topic, essay=essay)},
        ], temperature=0.3)

        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return json.loads(content)

    def _validate_output(self, result: dict):
        if "accuracy" not in result:
            raise ValueError("评分结果缺少accuracy字段")