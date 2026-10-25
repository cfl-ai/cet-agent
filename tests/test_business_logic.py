"""
业务逻辑测试 — 用户档案、错误类型推断、答题时长。

这些用例覆盖本次改良新增/修复的功能，防止回归。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ==================== 用户档案 ====================

class TestUserProfile:
    """users 表此前从未被写入，导致个人设置无法长期保存"""

    @pytest.mark.asyncio
    async def test_auto_create_profile_on_first_visit(self):
        """首次访问自动建档，使用默认 CET4 / 425"""
        from models.database import init_db
        from services import user_service

        await init_db()

        profile = await user_service.get_study_overview("newbie")
        assert profile["user_id"] == "newbie"
        assert profile["level"] == "CET4"
        assert profile["target_score"] == 425
        assert profile["total_answered"] == 0

    @pytest.mark.asyncio
    async def test_profile_persists_across_reads(self):
        """设置后再次读取应得到相同值（长期保存的核心）"""
        from models.database import init_db
        from services import user_service

        await init_db()

        await user_service.upsert_profile(
            "persist_user", level="CET6", target_score=550, username="小李"
        )
        first = await user_service.get_profile("persist_user")

        # 模拟"刷新/换设备"：重新读取
        second = await user_service.get_profile("persist_user")

        assert first == second, "档案未持久化"
        assert second["level"] == "CET6"
        assert second["target_score"] == 550
        assert second["username"] == "小李"
        # created_at 不应被后续更新改变
        assert first["created_at"] == second["created_at"]

    @pytest.mark.asyncio
    async def test_partial_update_keeps_other_fields(self):
        """部分更新不能覆盖未提供的字段"""
        from models.database import init_db
        from services import user_service

        await init_db()

        await user_service.upsert_profile(
            "partial_user", level="CET6", target_score=500
        )
        # 只改 username
        await user_service.upsert_profile("partial_user", username="改名")

        p = await user_service.get_profile("partial_user")
        assert p["username"] == "改名"
        assert p["level"] == "CET6", "level 被意外覆盖"
        assert p["target_score"] == 500, "target_score 被意外覆盖"

    @pytest.mark.parametrize("raw,expected", [
        ("CET4", "CET4"),
        ("cet4", "CET4"),
        ("四级", "CET4"),
        ("CET-4", "CET4"),
        ("CET6", "CET6"),
        ("六级", "CET6"),
        ("CET-6", "CET6"),
        ("乱码", "CET4"),   # 无法识别回落默认
        ("", "CET4"),
        (None, "CET4"),
    ])
    def test_level_normalization(self, raw, expected):
        """级别输入容错：大小写、中文、连字符均可识别"""
        from services.user_service import _normalize_level
        assert _normalize_level(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        (425, 425),
        (550, 550),
        (9999, 710),    # 超出上限收敛
        (100, 220),     # 低于下限收敛
        (None, 425),    # 缺省
        ("abc", 425),   # 非法回落
    ])
    def test_target_score_normalization(self, raw, expected):
        """目标分数收敛到 220-710 合理区间"""
        from services.user_service import _normalize_target
        assert _normalize_target(raw) == expected

    @pytest.mark.asyncio
    async def test_touch_creates_without_overwriting(self):
        """touch 只负责建档，不覆盖已有设置"""
        from models.database import init_db
        from services import user_service

        await init_db()

        await user_service.upsert_profile(
            "touch_user", level="CET6", target_score=600
        )
        await user_service.touch("touch_user")

        p = await user_service.get_profile("touch_user")
        assert p["level"] == "CET6"
        assert p["target_score"] == 600


# ==================== 错误类型推断 ====================

class TestErrorTypeInference:
    """_infer_error_type 曾恒返回 knowledge_gap，导致错题诊断失效"""

    def setup_method(self):
        from agents.grading_agent import GradingAgent
        self.g = GradingAgent()

    def test_rush_answer_is_careless(self):
        """秒选（<5s）答错判为粗心"""
        assert self.g._infer_error_type({"duration": 3.0}, "阅读-仔细阅读") == "careless"

    def test_hesitation_is_concept(self):
        """长时间犹豫（>60s）仍错判为概念不清"""
        assert self.g._infer_error_type({"duration": 90.0}, "阅读-仔细阅读") == "concept"

    def test_normal_duration_is_knowledge_gap(self):
        """常规时长答错判为知识漏洞"""
        assert self.g._infer_error_type({"duration": 30.0}, "阅读-仔细阅读") == "knowledge_gap"

    def test_vocab_question_type(self):
        """词汇类题型优先判为词汇欠缺"""
        assert self.g._infer_error_type({"duration": 20.0}, "阅读-选词填空") == "vocab"
        assert self.g._infer_error_type({"duration": 2.0}, "词汇辨析") == "vocab"

    def test_missing_duration_falls_back(self):
        """未上报时长不应崩溃，回落知识漏洞"""
        assert self.g._infer_error_type({}, "阅读-仔细阅读") == "knowledge_gap"

    def test_invalid_duration_does_not_crash(self):
        """非法时长值需容错"""
        assert self.g._infer_error_type({"duration": "abc"}, "阅读-仔细阅读") == "knowledge_gap"

    def test_inference_is_discriminative(self):
        """核心断言：必须能区分多种错因，不能再退化成单一类型"""
        cases = [
            ({"duration": 3.0}, "阅读-仔细阅读"),
            ({"duration": 90.0}, "阅读-仔细阅读"),
            ({"duration": 30.0}, "阅读-仔细阅读"),
            ({"duration": 20.0}, "阅读-选词填空"),
        ]
        results = {self.g._infer_error_type(a, q) for a, q in cases}
        assert len(results) >= 3, f"推断结果仍过于单一: {results}"

    def test_error_type_cn_mapping_complete(self):
        """所有错误类型都应有中文映射"""
        for et in ["vocab", "careless", "concept", "knowledge_gap"]:
            assert et in self.g.ERROR_TYPE_CN


# ==================== 答题时长字段 ====================

class TestAnswerDuration:
    """AnswerItem 曾缺 duration 字段，导致前端传值被 Pydantic 静默丢弃"""

    def test_duration_is_accepted(self):
        from main import AnswerItem
        item = AnswerItem(
            question_id="q1", user_answer="A", duration=45.5
        )
        assert item.duration == 45.5

    def test_duration_defaults_to_zero(self):
        from main import AnswerItem
        item = AnswerItem(question_id="q1", user_answer="A")
        assert item.duration == 0.0

    def test_duration_is_float(self):
        from main import AnswerItem
        item = AnswerItem(question_id="q1", user_answer="A", duration=30)
        assert isinstance(item.duration, float)


# ==================== 学习统计 ====================

class TestStudyStats:
    """study_stats 表此前从未被写入"""

    @pytest.mark.asyncio
    async def test_stats_written_after_grading(self):
        """判分后应按日聚合写入 study_stats"""
        import asyncio
        from datetime import datetime
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()

        # 预置一道题
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO questions (question_id, question_type, content, "
                "answer, knowledge_tags, verified, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("q_stat", "阅读-仔细阅读", "test content", "A", '["细节理解"]', 1, 0),
            )
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()
        answers = [
            {"question_id": "q_stat", "user_answer": "A", "duration": 45.0},
            {"question_id": "q_stat", "user_answer": "B", "duration": 75.0},
        ]
        result = await g.execute(
            {"user_id": "stat_user", "answers": answers}, task_id="t1"
        )

        assert result["total"] == 2
        assert result["correct"] == 1

        today = datetime.now().strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT questions_done, correct_count, study_minutes "
                "FROM study_stats WHERE user_id=? AND stat_date=?",
                ("stat_user", today),
            )
            row = await cursor.fetchone()

        assert row is not None, "study_stats 未写入"
        done, correct, minutes = row
        assert done == 2
        assert correct == 1
        assert minutes == 2, "45+75=120秒应记为2分钟"

    @pytest.mark.asyncio
    async def test_answer_records_duration_persisted(self):
        """答题时长必须真实入库（此前恒为0）"""
        import asyncio
        from models.database import init_db, DB_PATH
        import aiosqlite

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO questions (question_id, question_type, content, "
                "answer, knowledge_tags, verified, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("q_dur", "阅读-仔细阅读", "test", "A", '["细节理解"]', 1, 0),
            )
            await db.commit()

        from agents.grading_agent import GradingAgent
        g = GradingAgent()
        await g.execute(
            {"user_id": "dur_user", "answers": [
                {"question_id": "q_dur", "user_answer": "A", "duration": 42.5}
            ]},
            task_id="t2",
        )

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT duration FROM answer_records WHERE user_id=?",
                ("dur_user",),
            )
            row = await cursor.fetchone()

        assert row is not None and row[0] == 42.5, f"时长未正确入库: {row}"
