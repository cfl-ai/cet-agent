"""
学习记录链路改良测试 — 贝叶斯平滑掌握度 + 学习分析服务。

覆盖本轮改良：
  1. knowledge_state 掌握度改为 (correct+K·prior)/(attempts+K)，
     修复裸 EMA 在稀疏样本下的失真与 correct 列从不更新的隐性 bug
  2. kt_model.get_mastery 与实时链路口径一致
  3. analytics_service：薄弱点、趋势补零、连续打卡、模考历史、词汇复习计数
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _seed_question(db, qid, qtype="阅读-仔细阅读",
                         tags='["细节理解"]', answer="A"):
    await db.execute(
        "INSERT OR REPLACE INTO questions (question_id, question_type, content, "
        "answer, knowledge_tags, verified, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (qid, qtype, "test content", answer, tags, 1, 0),
    )


# ==================== 贝叶斯平滑掌握度 ====================

class TestBayesianMastery:
    """裸 EMA 曾导致：答对1题掌握度即1.0，样本少时排名倒挂"""

    @pytest.mark.asyncio
    async def test_first_correct_is_shrunk_toward_prior(self):
        """答对1题 → (1+1)/(1+2)=0.667，不再是盲目的 1.0"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_question(db, "q_bayes1")
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()
        await g.execute(
            {"user_id": "bayes_user", "answers": [
                {"question_id": "q_bayes1", "user_answer": "A", "duration": 30.0}
            ]},
            task_id="t_bayes1",
        )

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT mastery, attempts, correct FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                ("bayes_user", "细节理解"))
            row = await cursor.fetchone()

        assert row is not None, "knowledge_state 未写入"
        mastery, attempts, correct = row
        assert attempts == 1
        assert correct == 1
        assert abs(mastery - 2 / 3) < 1e-6, f"首题答对应收缩到0.667，实际 {mastery}"

    @pytest.mark.asyncio
    async def test_wrong_after_correct_gives_half(self):
        """对1题再错1题 → (1+1)/(2+2)=0.5，且 correct 列保持1不虚增"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_question(db, "q_bayes2")
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()
        for ans in ("A", "B"):
            await g.execute(
                {"user_id": "bayes_user2", "answers": [
                    {"question_id": "q_bayes2", "user_answer": ans, "duration": 30.0}
                ]},
                task_id=f"t_bayes2_{ans}",
            )

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT mastery, attempts, correct FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                ("bayes_user2", "细节理解"))
            mastery, attempts, correct = await cursor.fetchone()

        assert attempts == 2
        assert correct == 1, "correct 列应在错答时不增加"
        assert abs(mastery - 0.5) < 1e-6, f"一胜一负应得0.5，实际 {mastery}"

    @pytest.mark.asyncio
    async def test_correct_column_accumulates(self):
        """回归：correct 列此前插入后从不更新，连对应保持累加"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_question(db, "q_bayes3")
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()
        for i in range(3):
            # 每次耗时不同：GradingAgent 按任务内容哈希做幂等，
            # 完全相同的重复提交会命中缓存（防双击重复计数），
            # 真实重复练习的时长必然不同。
            await g.execute(
                {"user_id": "bayes_user3", "answers": [
                    {"question_id": "q_bayes3", "user_answer": "A",
                     "duration": 20.0 + i}
                ]},
                task_id=f"t_bayes3_{i}",
            )

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT mastery, attempts, correct FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                ("bayes_user3", "细节理解"))
            mastery, attempts, correct = await cursor.fetchone()

        assert attempts == 3
        assert correct == 3, "连对3题 correct 应为3"
        assert abs(mastery - (3 + 1) / (3 + 2)) < 1e-6, f"应收敛到0.8，实际 {mastery}"

    @pytest.mark.asyncio
    async def test_mastery_ordering_no_longer_inverted(self):
        """核心场景复现：高正确率用户的掌握度必须高于低正确率用户"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_question(db, "q_cmp")
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()

        async def _practice(user_id, correct_flags):
            for i, ok in enumerate(correct_flags):
                await g.execute(
                    {"user_id": user_id, "answers": [
                        {"question_id": "q_cmp",
                         "user_answer": "A" if ok else "B",
                         "duration": 25.0 + i}
                    ]},
                    task_id=f"t_cmp_{user_id}_{i}",
                )

        # 用户A：10题对7（70%）；用户B：10题对9（90%）
        await _practice("cmp_low", [True] * 7 + [False] * 3)
        await _practice("cmp_high", [True] * 9 + [False])

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT mastery FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                ("cmp_low", "细节理解"))
            low = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT mastery FROM knowledge_state "
                "WHERE user_id=? AND knowledge_point=?",
                ("cmp_high", "细节理解"))
            high = (await cursor.fetchone())[0]

        assert high > low, f"90%正确率({high}) 应高于 70%({low})"
        assert abs(low - (7 + 1) / (10 + 2)) < 1e-6
        assert abs(high - (9 + 1) / (10 + 2)) < 1e-6

    @pytest.mark.asyncio
    async def test_kt_model_consistent_with_realtime(self):
        """kt_model.get_mastery 与实时链路同口径：单次答对应得 0.667"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_question(db, "q_kt", tags='["阅读理解"]')
            await db.execute(
                "INSERT INTO answer_records (user_id, question_id, user_answer, "
                "correct_answer, is_correct, duration, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("kt_user", "q_kt", "A", "A", 1, 20.0, 1000.0))
            await db.commit()

        from core.kt_model import KnowledgeTracer
        kt = KnowledgeTracer()
        mastery = await kt.get_mastery("kt_user")

        assert "阅读理解" in mastery
        assert abs(mastery["阅读理解"] - 2 / 3) < 1e-6, \
            f"与实时链路口径不一致: {mastery['阅读理解']}"


# ==================== 薄弱知识点 ====================

class TestWeakPoints:

    @pytest.mark.asyncio
    async def test_ordering_and_open_wrong_merge(self):
        """薄弱点按掌握度升序，并正确归集未解决错题数"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO knowledge_state (user_id, knowledge_point, mastery, "
                "attempts, correct, last_update) VALUES (?,?,?,?,?,?)",
                ("wp_user", "听力长对话", 0.3, 10, 3, 0))
            await db.execute(
                "INSERT INTO knowledge_state (user_id, knowledge_point, mastery, "
                "attempts, correct, last_update) VALUES (?,?,?,?,?,?)",
                ("wp_user", "段落匹配", 0.6, 10, 6, 0))
            await db.execute(
                "INSERT INTO knowledge_state (user_id, knowledge_point, mastery, "
                "attempts, correct, last_update) VALUES (?,?,?,?,?,?)",
                ("wp_user", "只练一次", 0.1, 1, 0, 0))
            await _seed_question(db, "q_wp", tags='["听力长对话"]')
            await db.execute(
                "INSERT INTO wrong_questions (user_id, question_id, wrong_count, "
                "error_type, resolved, last_wrong_at) VALUES (?,?,?,?,?,?)",
                ("wp_user", "q_wp", 2, "knowledge_gap", 0, 0))
            await db.commit()

        from services import analytics_service
        points = await analytics_service.get_weak_points("wp_user")

        assert [p["point"] for p in points] == ["听力长对话", "段落匹配"]
        assert "只练一次" not in [p["point"] for p in points], \
            "练习次数不足的知识点应被过滤"
        assert points[0]["open_wrong"] == 1, "未解决错题数应归集到知识点"
        assert points[0]["attempts"] == 10 and points[0]["correct"] == 3
        assert points[0]["accuracy"] == 0.3

    @pytest.mark.asyncio
    async def test_empty_returns_empty(self):
        from models.database import init_db
        from services import analytics_service

        await init_db()
        assert await analytics_service.get_weak_points("nobody") == []


# ==================== 学习趋势 ====================

class TestLearningTrend:

    @pytest.mark.asyncio
    async def test_missing_days_are_zero_filled(self):
        """没有学习的日期必须补零，前端才能画连续曲线"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        today = datetime.now()
        async with aiosqlite.connect(DB_PATH) as db:
            for offset, done, correct in [(0, 10, 7), (2, 5, 5)]:
                d = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
                await db.execute(
                    "INSERT INTO study_stats (user_id, stat_date, questions_done, "
                    "correct_count, words_reviewed, study_minutes) "
                    "VALUES (?,?,?,?,?,?)",
                    ("trend_user", d, done, correct, 20, 30))
            await db.commit()

        from services import analytics_service
        trend = await analytics_service.get_learning_trend("trend_user", days=7)

        assert len(trend) == 7, "应返回完整7天"
        assert trend[-1]["date"] == today.strftime("%Y-%m-%d")
        assert trend[-1]["questions_done"] == 10
        assert trend[-1]["accuracy"] == 0.7
        assert trend[-1]["words_reviewed"] == 20
        # 中间缺一天（offset=1）：补零且 accuracy 为 None（区分"没学"和"全错"）
        assert trend[-2]["questions_done"] == 0
        assert trend[-2]["accuracy"] is None
        assert trend[-3]["questions_done"] == 5
        assert trend[-3]["accuracy"] == 1.0

    @pytest.mark.asyncio
    async def test_days_clamped(self):
        """days 参数收敛到 1-90，防止异常值拖垮查询"""
        from models.database import init_db
        from services import analytics_service

        await init_db()
        assert len(await analytics_service.get_learning_trend("x", days=0)) == 1
        assert len(await analytics_service.get_learning_trend("x", days=999)) == 90


# ==================== 连续打卡 ====================

class TestStreak:

    async def _seed_dates(self, db, user_id, offsets):
        today = datetime.now()
        for off in offsets:
            d = (today - timedelta(days=off)).strftime("%Y-%m-%d")
            await db.execute(
                "INSERT OR IGNORE INTO study_stats (user_id, stat_date) "
                "VALUES (?, ?)", (user_id, d))

    @pytest.mark.asyncio
    async def test_current_and_longest(self):
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            # 今天、昨天、前天连续；再往前断档；5天前另起一段2天
            await self._seed_dates(db, "streak_user", [0, 1, 2, 5, 6])
            await db.commit()

        from services import analytics_service
        s = await analytics_service.get_streak("streak_user")

        assert s["current_streak"] == 3
        assert s["longest_streak"] == 3
        assert s["total_days"] == 5
        assert s["last_study_date"] == datetime.now().strftime("%Y-%m-%d")

    @pytest.mark.asyncio
    async def test_today_not_studied_keeps_yesterday_streak(self):
        """今天还没学不算断档，从昨天继续数"""
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dates(db, "streak_user2", [1, 2, 3])
            await db.commit()

        from services import analytics_service
        s = await analytics_service.get_streak("streak_user2")
        assert s["current_streak"] == 3

    @pytest.mark.asyncio
    async def test_gap_over_one_day_resets(self):
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dates(db, "streak_user3", [0, 3])
            await db.commit()

        from services import analytics_service
        s = await analytics_service.get_streak("streak_user3")
        assert s["current_streak"] == 1
        assert s["longest_streak"] == 1

    @pytest.mark.asyncio
    async def test_no_records(self):
        from models.database import init_db
        from services import analytics_service

        await init_db()
        s = await analytics_service.get_streak("ghost")
        assert s["current_streak"] == 0
        assert s["longest_streak"] == 0
        assert s["last_study_date"] is None


# ==================== 模考历史 ====================

class TestExamHistory:

    @pytest.mark.asyncio
    async def test_history_with_used_seconds(self):
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO exam_sessions (session_id, user_id, level, year, "
                "month, set_number, duration_seconds, started_at, ended_at, "
                "status, answers, score, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s1", "exam_user", "cet4", 2024, 6, 1, 7500, 1000.0, 4600.0,
                 "completed", "{}", 480, 1000.0))
            await db.execute(
                "INSERT INTO exam_sessions (session_id, user_id, level, year, "
                "month, set_number, duration_seconds, started_at, ended_at, "
                "status, answers, score, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s2", "exam_user", "cet4", 2023, 12, 2, 7500, 2000.0, None,
                 "timeout", "{}", 0, 2000.0))
            await db.commit()

        from services import analytics_service
        history = await analytics_service.get_exam_history("exam_user")

        assert len(history) == 2
        assert history[0]["session_id"] == "s2", "应按开始时间倒序"
        assert history[0]["used_seconds"] is None, "未结束的会话无用时"
        assert history[1]["used_seconds"] == 3600
        assert history[1]["score"] == 480
        assert history[1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_empty_history(self):
        from models.database import init_db
        from services import analytics_service

        await init_db()
        assert await analytics_service.get_exam_history("nobody") == []


# ==================== 词汇复习计数 ====================

class TestWordsReviewed:
    """words_reviewed 此前从未被写入，词汇学习是统计黑洞"""

    @pytest.mark.asyncio
    async def test_words_reviewed_accumulates(self):
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        from services import analytics_service

        await analytics_service.record_words_reviewed("vocab_user", 15)
        await analytics_service.record_words_reviewed("vocab_user", 10)

        today = datetime.now().strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT words_reviewed FROM study_stats "
                "WHERE user_id=? AND stat_date=?",
                ("vocab_user", today))
            (words,) = await cursor.fetchone()

        assert words == 25, "两次复习量应累加"

    @pytest.mark.asyncio
    async def test_words_reviewed_creates_profile(self):
        """记录复习的同时应确保用户档案存在（数据有归属）"""
        from models.database import init_db
        from services import analytics_service
        from services import user_service

        await init_db()
        await analytics_service.record_words_reviewed("vocab_new", 5)
        profile = await user_service.get_profile("vocab_new")
        assert profile is not None

    @pytest.mark.asyncio
    async def test_count_clamped(self):
        from models.database import init_db
        from services import analytics_service

        await init_db()
        data = await analytics_service.record_words_reviewed("vocab_clamp", 0)
        assert data["words_reviewed"] >= 1, "计数至少为1"


# ==================== 仪表盘 ====================

class TestDashboard:

    @pytest.mark.asyncio
    async def test_dashboard_structure(self):
        from models.database import init_db
        from services import analytics_service

        await init_db()
        data = await analytics_service.get_dashboard("dash_user", days=7)

        for key in ("profile", "trend", "weak_points", "streak",
                    "recent_exams", "last_7d_questions", "last_7d_minutes",
                    "generated_at"):
            assert key in data, f"仪表盘缺少字段: {key}"
        assert len(data["trend"]) == 7
        assert data["profile"]["user_id"] == "dash_user"
