import asyncio
import json
import time
import random
import logging
import aiosqlite
from agents.question_agent import (
    QuestionAgent, TOPIC_POOL, QUESTION_COUNTS
)
from agents.grading_agent import GradingAgent
from agents.review_agent import ReviewAgent
from models.database import DB_PATH
from config import settings

logger = logging.getLogger(__name__)


class Orchestrator:

    def __init__(self):
        self.question_agent = QuestionAgent()
        self.grading_agent = GradingAgent()
        self.review_agent = ReviewAgent()
        self.semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_AGENTS)

    async def _run_with_semaphore(self, agent, task, task_id):
        async with self.semaphore:
            return await agent.execute(task, task_id)

    async def generate_exam(self, config: dict) -> dict:
        num_questions = config.get("num_questions", 1)
        qtype = config.get("question_type", "reading_careful")
        base_ts = int(time.time() * 1000)

        topics = random.sample(TOPIC_POOL, min(num_questions, len(TOPIC_POOL)))
        while len(topics) < num_questions:
            topics.append(random.choice(TOPIC_POOL))

        tasks = []
        for i in range(num_questions):
            sub_task = {
                "question_type": qtype,
                "knowledge_point": config.get("knowledge_point") or qtype,
                "difficulty": config.get("difficulty", 3),
                "topic": topics[i],
                "seed": f"{base_ts}_{i}_{random.randint(100000, 999999)}",
            }
            tasks.append(self._run_with_semaphore(
                self.question_agent, sub_task,
                task_id=f"exam_{base_ts}_{i}"
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        questions, errors = [], []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                errors.append({"index": i, "error": str(r)})
                logger.error(f"题目{i}生成失败: {r}")
            else:
                questions.append(r)

        if len(questions) == 0:
            raise RuntimeError(f"出题全部失败 ({len(errors)}/{num_questions})")

        return {
            "questions": questions,
            "partial_errors": errors if errors else None,
            "total": len(questions),
        }

    async def generate_from_wrong(self, user_id: str, num: int = 3) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT q.knowledge_tags, q.question_type, q.difficulty
                FROM wrong_questions w
                JOIN questions q ON w.question_id = q.question_id
                WHERE w.user_id=? AND w.resolved=0
                ORDER BY w.wrong_count DESC, w.last_wrong_at DESC
                LIMIT ?
            """, (user_id, num))
            rows = await cursor.fetchall()

        if not rows:
            return await self.generate_exam({
                "num_questions": num,
                "question_type": "reading_careful",
                "difficulty": 3,
            })

        type_cn_to_key = {
            "写作": "writing",
            "听力-短篇新闻": "listening_news",
            "听力-长对话": "listening_conversation",
            "听力-听力篇章": "listening_passage",
            "阅读-选词填空": "reading_selection",
            "阅读-长篇匹配": "reading_matching",
            "阅读-仔细阅读": "reading_careful",
            "翻译-汉译英": "translation",
            "综合训练": "comprehensive",
        }

        base_ts = int(time.time() * 1000)
        tasks = []
        for i, (tags_json, qtype_cn, diff) in enumerate(rows):
            try:
                tags = json.loads(tags_json) if tags_json else []
            except Exception:
                tags = []
            knowledge_point = tags[0] if tags else "通用"
            qtype_key = type_cn_to_key.get(qtype_cn, "reading_careful")

            sub_task = {
                "question_type": qtype_key,
                "knowledge_point": knowledge_point,
                "difficulty": diff or 3,
                "topic": random.choice(TOPIC_POOL),
                "seed": f"wrong_{user_id}_{base_ts}_{i}_{random.randint(1000, 9999)}",
            }
            tasks.append(self._run_with_semaphore(
                self.question_agent, sub_task,
                task_id=f"wrong_exam_{user_id}_{base_ts}_{i}"
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        questions = [r for r in results if not isinstance(r, Exception)]

        if not questions:
            raise RuntimeError("错题变式题生成全部失败")

        return {
            "questions": questions,
            "total": len(questions),
            "based_on_wrong": len(rows),
        }

    async def evaluate_and_review(self, user_id: str,
                                   answers: list[dict]) -> dict:
        grading_task = self._run_with_semaphore(
            self.grading_agent,
            {"user_id": user_id, "answers": answers},
            task_id=f"grade_{user_id}"
        )
        review_task = self._run_with_semaphore(
            self.review_agent,
            {"user_id": user_id, "answers": answers},
            task_id=f"review_{user_id}"
        )

        grading_result, review_result = await asyncio.gather(
            grading_task, review_task, return_exceptions=True
        )

        if isinstance(grading_result, Exception):
            raise RuntimeError(f"评分Agent失败: {grading_result}")
        if isinstance(review_result, Exception):
            raise RuntimeError(f"诊断Agent失败: {review_result}")

        return {"grading": grading_result, "review": review_result}