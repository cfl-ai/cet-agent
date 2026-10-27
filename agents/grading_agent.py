import json
import re
import time
import logging
import unicodedata

import aiosqlite

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

    # 词汇类题型：错因优先归为词汇问题
    VOCAB_TYPES = {"阅读-选词填空", "词汇辨析"}
    # 主观题类型：不做 ABCD 硬判定
    SUBJECTIVE_TYPES = {"写作", "翻译-汉译英"}
    # 秒选阈值（秒）：低于此时长基本没读完题干
    RUSH_THRESHOLD = 5.0
    # 长时间犹豫阈值（秒）：超过此时长仍错，多为概念不清
    HESITATE_THRESHOLD = 60.0
    # 贝叶斯平滑先验强度 K：样本越少，掌握度越向先验收缩；
    # 样本充足时收敛到真实正确率，避免"答对1题即掌握度1.0"
    KT_PRIOR_STRENGTH = 2.0
    # 先验掌握概率（P(L0)）
    KT_PRIOR = 0.5
    # 错误类型中文映射（供学习方案与前端展示）
    ERROR_TYPE_CN = {
        "vocab": "词汇欠缺",
        "careless": "粗心失误",
        "concept": "概念不清",
        "knowledge_gap": "知识漏洞",
    }

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
                    "SELECT answer, explanation, knowledge_tags, question_type, "
                    "source, verified FROM questions WHERE question_id = ?",
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
                source = row[4] or ""
                verified = row[5] or 0
                ungradable_reason = self._ungradable_reason(
                    correct_answer, source, verified)

                # ★ 主观题：客观判分不适用，交给专门的批改接口按评分标准打分
                if qtype in self.SUBJECTIVE_TYPES:
                    results.append({
                        "question_id": qid,
                        "question_type": qtype,
                        "is_correct": None,
                        "gradable": False,
                        "grading": "subjective",
                        "user_answer": user_ans,
                        "correct_answer": correct_answer or "见解析",
                        "explanation": explanation or (
                            "主观题不做客观判分。写作请调用 /api/grade/writing，"
                            "翻译请调用 /api/grade/translation 获取按评分标准"
                            "给出的真实分数与逐句批注。"
                        ),
                        "skipped": True,
                    })
                    continue

                # ★ 无法可靠判分的题：跳过计分，避免把错误答案写进学情。
                #   真题导入时答案曾被硬编码为 "A" 且 verified=0（1090 道），
                #   拿它判定用户"做错"会系统性污染错题本与掌握度。
                if ungradable_reason:
                    results.append({
                        "question_id": qid,
                        "question_type": qtype,
                        "is_correct": None,
                        "gradable": False,
                        "grading": "ungradable",
                        "ungradable_reason": ungradable_reason,
                        "user_answer": user_ans,
                        "correct_answer": correct_answer,
                        "explanation": explanation or (
                            "该题答案尚未核验，暂不参与判分与学情统计。"
                        ),
                        "skipped": True,
                    })
                    continue

                # 客观题：答案归一化后比较（全角/空格/大小写/多选顺序）
                is_correct = self._answers_match(user_ans, correct_answer)

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
                          self._infer_error_type(ans, qtype), time.time()))
                    await self._update_knowledge(db, task["user_id"], tags, False)

                results.append({
                    "question_id": qid,
                    "question_type": qtype,
                    "is_correct": is_correct,
                    "gradable": True,
                    "grading": "objective",
                    "user_answer": user_ans,
                    "correct_answer": correct_answer,
                    "explanation": explanation,
                    # 错因诊断（答对为 None），供前端与学习方案使用
                    "error_type": None if is_correct
                                  else self._infer_error_type(ans, qtype),
                    "error_type_cn": None if is_correct
                                     else self.ERROR_TYPE_CN.get(
                                         self._infer_error_type(ans, qtype), "知识漏洞"),
                })

            await self._update_study_stats(
                db, task["user_id"], results, answers
            )
            await db.commit()

        # 只统计"真正判了分"的客观题：主观题与答案未核验题不计入正确率
        graded = [r for r in results if r.get("gradable") is True]
        total_obj = len(graded)
        accuracy = correct_count / total_obj if total_obj else 0
        ungradable = [r for r in results if r.get("grading") == "ungradable"]
        subjective = [r for r in results if r.get("grading") == "subjective"]

        return {
            "user_id": task["user_id"],
            "total": len(results),
            "objective_total": total_obj,
            "correct": correct_count,
            "accuracy": round(accuracy, 3),
            "ungradable_count": len(ungradable),
            "subjective_count": len(subjective),
            # 明确告诉前端/调用方：有多少题因答案未核验而未计分
            "grading_note": (
                "主观题与答案未核验题不计入正确率；"
                "写作/翻译请调用对应批改接口"
                if (subjective or ungradable) else None
            ),
            "details": results,
        }

    async def _update_study_stats(self, db, user_id: str,
                                  results: list, answers: list):
        """
        按日聚合学习统计。
        此前 study_stats 表从未被写入，导致无法做学习趋势、打卡坚持度分析。
        """
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        # 只统计真正判了分的客观题（主观题/答案未核验题不计）
        graded = [r for r in results if r.get("gradable") is True]
        done = len(graded)
        correct = sum(1 for r in graded if r.get("is_correct"))

        total_seconds = 0.0
        for a in answers:
            try:
                total_seconds += float(a.get("duration") or 0)
            except (TypeError, ValueError):
                continue
        minutes = int(round(total_seconds / 60))

        # 先确保当日记录存在，再做增量累加
        await db.execute(
            "INSERT OR IGNORE INTO study_stats (user_id, stat_date) VALUES (?, ?)",
            (user_id, today),
        )
        await db.execute(
            """
            UPDATE study_stats SET
                questions_done = questions_done + ?,
                correct_count = correct_count + ?,
                study_minutes = study_minutes + ?
            WHERE user_id = ? AND stat_date = ?
            """,
            (done, correct, minutes, user_id, today),
        )

    async def _update_knowledge(self, db, user_id: str, tags_json: str,
                                  correct: bool):
        """
        知识追踪 — 贝叶斯平滑的掌握度更新。

        此前为裸 EMA（mastery×0.7 + 结果×0.3），样本少时严重失真：
        只答对1题掌握度即冲到 1.0，模拟训练中出现过 78% 正确率的用户
        掌握度(0.90)反高于 88% 正确率用户(0.57)的倒挂。

        现改为 (correct + K·prior) / (attempts + K)：
          - 答对1题  → (1+1)/(1+2) = 0.67，不再盲目乐观
          - 样本充足 → 收敛到该知识点的真实正确率
        同时修复 correct 列插入后从未更新的隐性 bug。
        """
        try:
            tags = json.loads(tags_json) if tags_json else []
        except Exception:
            tags = []

        for tag in tags:
            cursor = await db.execute(
                "SELECT mastery, attempts, correct FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                (user_id, tag)
            )
            row = await cursor.fetchone()
            if row:
                _, attempts, correct_cnt = row
                new_attempts = (attempts or 0) + 1
                new_correct = (correct_cnt or 0) + (1 if correct else 0)
                new_mastery = (
                    (new_correct + self.KT_PRIOR_STRENGTH * self.KT_PRIOR)
                    / (new_attempts + self.KT_PRIOR_STRENGTH)
                )
                await db.execute(
                    "UPDATE knowledge_state SET mastery=?, attempts=?, "
                    "correct=?, last_update=? WHERE user_id=? AND knowledge_point=?",
                    (new_mastery, new_attempts, new_correct,
                     time.time(), user_id, tag)
                )
            else:
                new_correct = 1 if correct else 0
                new_mastery = (
                    (new_correct + self.KT_PRIOR_STRENGTH * self.KT_PRIOR)
                    / (1 + self.KT_PRIOR_STRENGTH)
                )
                await db.execute(
                    """INSERT INTO knowledge_state
                       (user_id, knowledge_point, mastery, attempts, correct, last_update)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (user_id, tag, new_mastery,
                     new_correct, time.time())
                )

    def _infer_error_type(self, ans: dict, qtype: str = "") -> str:
        """
        错误类型推断。

        此前该函数恒返回硬编码 "knowledge_gap"，导致学习方案中的
        "错题类型分布"永远是同一类，诊断完全失去意义。

        现基于两个真实信号推断：
          - 题型：词汇类题型错 → vocab（词汇欠缺）
          - 答题时长：秒选(<5s) → careless（粗心/未读题）
                     长时间犹豫(>60s)仍错 → concept（概念不清）
                     其余 → knowledge_gap（知识漏洞）
        """
        if qtype in self.VOCAB_TYPES:
            return "vocab"

        try:
            duration = float(ans.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        if 0 < duration < self.RUSH_THRESHOLD:
            return "careless"
        if duration > self.HESITATE_THRESHOLD:
            return "concept"
        return "knowledge_gap"

    # ==================== 判分基础：答案归一化与可信度 ====================
    @staticmethod
    def _normalize_answer(value: str) -> str:
        """答案归一化：全角→半角、去空白与标点、转大写、多选排序。

        真实判分不能只做 upper()：用户可能输入 "a "、"Ａ"、"B,A"，
        多选答案的书写顺序也不应影响判定。
        """
        if value is None:
            return ""
        text = unicodedata.normalize("NFKC", str(value)).strip().upper()
        text = re.sub(r"[\s,.，、;；/|\-]+", "", text)
        if re.fullmatch(r"[A-H]{2,}", text):
            text = "".join(sorted(set(text)))
        return text

    def _answers_match(self, user_ans: str, correct_ans: str) -> bool:
        """归一化后比较用户答案与标准答案。"""
        user_norm = self._normalize_answer(user_ans)
        correct_norm = self._normalize_answer(correct_ans)
        return bool(user_norm) and user_norm == correct_norm

    @staticmethod
    def _ungradable_reason(correct_answer: str, source: str,
                           verified) -> str | None:
        """判断一道客观题是否"不能可靠判分"。

        真实的判分必须先保证答案可信：
          - answer_missing：标准答案缺失 → 无法判错
          - answer_unverified：真题导入时答案曾被硬编码为 "A"（verified=0），
            这类题判对/判错都会把系统性错误写进错题本与掌握度
        """
        answer = (correct_answer or "").strip()
        if not answer:
            return "answer_missing"
        src = str(source or "")
        if not verified and src.startswith("real_"):
            return "answer_unverified"
        return None

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