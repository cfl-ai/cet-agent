"""
在线学习闭环测试 — 让预测随真实数据越来越准，而不是复述公式。

背景（本轮要解决的根本缺陷）：
  旧版 scripts/train_predictor.py 的标签是自举的假标签
      true_score = 290 + avg(mastery) * 420
  模型学到的只是"复盘这条公式"，喂再多数据误差都很漂亮，
  但预测永远不会更准。本module 验证新闭环的三个关键性质：
    1. 标签来自真实分数（实考录入 / 模考折算）
    2. 特征只用"考试之前"的答题（因果正确，不泄漏未来）
    3. 训练后必须验证：**变准才晋升，变差就回滚**（防越训越差）
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _date(offset_days: int) -> str:
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


async def _seed_user(db, user_id: str, n_answers: int, correct_ratio: float,
                     base_ts: float, tag: str = "细节理解"):
    """为该用户插入答题记录（题目统一带一个知识点标签）"""
    await db.execute(
        "INSERT OR REPLACE INTO questions (question_id, question_type, content, "
        "answer, knowledge_tags, verified, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"q_{user_id}", "阅读-仔细阅读", "text", "A",
         json.dumps([tag], ensure_ascii=False), 1, 0),
    )
    n_correct = int(round(n_answers * correct_ratio))
    for i in range(n_answers):
        await db.execute(
            "INSERT INTO answer_records (user_id, question_id, user_answer, "
            "correct_answer, is_correct, duration, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, f"q_{user_id}", "A", "A",
             1 if i < n_correct else 0, 20.0, base_ts + i))
    await db.commit()


# ==================== 真实标签 ====================

class TestTrueLabels:

    @pytest.mark.asyncio
    async def test_no_labels_no_samples(self):
        """没有真实分数就不产生样本 —— 不再用自举标签凑数"""
        from models.database import init_db
        from services import learning_service

        await init_db()
        X, y, _ = await learning_service.build_training_samples()
        assert (X, y) == ([], [])

    @pytest.mark.asyncio
    async def test_real_exam_score_creates_sample(self):
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "real_user", 10, 0.8, base_ts=1000.0)
            await db.execute(
                "INSERT INTO user_exam_scores (user_id, level, score, "
                "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                ("real_user", "CET4", 520, _date(1), "real_exam", 1.0))
            await db.commit()

        X, y, y_pass = await learning_service.build_training_samples()
        assert len(X) == 1
        assert y[0] == 520
        assert y_pass[0] == 1

    @pytest.mark.asyncio
    async def test_mock_exam_score_is_also_a_label(self):
        """模考折算出的分数同样是监督信号（此前恒为 0，白丢）"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "mock_user", 8, 0.5, base_ts=1000.0)
            await db.execute(
                "INSERT INTO exam_sessions (session_id, user_id, level, year, "
                "month, set_number, duration_seconds, started_at, ended_at, "
                "status, answers, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s_mock", "mock_user", "cet4", 2024, 6, 1, 7500,
                 5000.0, 6000.0, "completed", "{}", 430, 5000.0))
            await db.commit()

        X, y, _ = await learning_service.build_training_samples()
        assert len(X) == 1 and y[0] == 430

    @pytest.mark.asyncio
    async def test_zero_score_session_excluded(self):
        """score=0 的历史脏会话不能当标签"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "zero_user", 8, 0.5, base_ts=1000.0)
            await db.execute(
                "INSERT INTO exam_sessions (session_id, user_id, level, year, "
                "month, set_number, duration_seconds, started_at, ended_at, "
                "status, answers, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s_zero", "zero_user", "cet4", 2024, 6, 1, 7500,
                 5000.0, 6000.0, "completed", "{}", 0, 5000.0))
            await db.commit()

        X, _, _ = await learning_service.build_training_samples()
        assert X == []


# ==================== 因果正确性（核心） ====================

class TestCausality:

    @pytest.mark.asyncio
    async def test_features_use_only_pre_exam_answers(self):
        """
        特征只能用考试之前的答题。
        若把考试后的答题也算进去，离线指标会很好看但在线预测必然失真。
        """
        from models.database import init_db, DB_PATH
        import aiosqlite
        from datetime import datetime as dt
        from services import learning_service
        from core.score_predictor import ScorePredictor

        await init_db()
        exam_date = _date(10)
        cutoff = dt.strptime(exam_date, "%Y-%m-%d").timestamp()

        async with aiosqlite.connect(DB_PATH) as db:
            # 考前：10 题全错 → 掌握度很低
            await _seed_user(db, "causal_user", 10, 0.0, base_ts=cutoff - 1000)
            # 考后：10 题全对（若泄漏进来，特征会完全不同）
            await _seed_user(db, "causal_user", 10, 1.0,
                             base_ts=cutoff + 100, tag="语法")
            await db.execute(
                "INSERT INTO user_exam_scores (user_id, level, score, "
                "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                ("causal_user", "CET4", 380, exam_date, "real_exam", 1.0))
            await db.commit()

        X, _, _ = await learning_service.build_training_samples()
        assert len(X) == 1

        # 期望特征：仅考前 10 题全错 → (0+1)/(10+2) = 0.0833
        expected = ScorePredictor().prepare_features(
            {"细节理解": (0 + 1.0) / (10 + 2.0)})[0]
        assert abs(float(X[0][0]) - float(expected[0])) < 1e-6, \
            "特征泄漏了考试之后的答题"

    @pytest.mark.asyncio
    async def test_too_few_pre_answers_is_skipped(self):
        """考前答题不足 5 道的标签，掌握度向量不可信，应丢弃"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "few_user", 3, 1.0, base_ts=1000.0)
            await db.execute(
                "INSERT INTO user_exam_scores (user_id, level, score, "
                "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                ("few_user", "CET4", 500, _date(1), "real_exam", 1.0))
            await db.commit()

        X, _, _ = await learning_service.build_training_samples()
        assert X == []


# ==================== 自动触发与择优晋升 ====================

class TestTrainingLoop:

    async def _seed_dataset(self, db, n_users: int, size=10):
        """造 n_users 份真实标签：答题正确率越高、分数越高"""
        for i in range(n_users):
            uid = f"loop_u{i}"
            ratio = 0.3 + (i / max(1, n_users - 1)) * 0.6
            await _seed_user(db, uid, size, ratio, base_ts=1000.0 + i)
            score = int(300 + ratio * 400)
            await db.execute(
                "INSERT INTO user_exam_scores (user_id, level, score, "
                "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                (uid, "CET4", score, _date(1), "real_exam", 1.0))
        await db.commit()

    @pytest.mark.asyncio
    async def test_insufficient_samples_blocks_training(self):
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 3)
        ok, reason = await learning_service.should_train()
        assert ok is False
        assert "不足" in reason

    @pytest.mark.asyncio
    async def test_first_training_promotes(self):
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 8)

        ok, reason = await learning_service.should_train()
        assert ok is True, reason

        result = await learning_service.train_if_needed()
        assert result["trained"] is True
        assert result["promoted"] is True, "无基线时首次训练应直接晋升"
        assert result["version"] == 1
        assert result["val_size"] >= 2 and result["train_size"] >= 3

        latest = await learning_service.get_latest_model()
        assert latest["version"] == 1
        assert latest["status"] == "promoted"
        assert latest["sample_count"] == 8

    @pytest.mark.asyncio
    async def test_no_retrain_when_samples_not_growing(self):
        """样本没增长就不重复训练，避免无意义的算力浪费"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 8)
        await learning_service.train_if_needed()

        ok, reason = await learning_service.should_train()
        assert ok is False
        assert reason.startswith("样本无显著增长")

    @pytest.mark.asyncio
    async def test_promotion_policy_rejects_worse_candidate(self, monkeypatch):
        """
        核心保护（确定性验证）：候选模型验证误差不优于线上模型时不晋升，
        线上模型文件保持原样 —— 保证"越用越准"而不是"越训越差"。
        """
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service
        from core.score_predictor import ScorePredictor

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 8)
        first = await learning_service.train_if_needed(force=True)
        assert first["promoted"] is True

        model_file = Path(DB_PATH).parent / "models" / "regressor.pkl"
        before = model_file.read_bytes()

        # 让候选模型的验证误差必然变差（第一次 evaluate 是候选，第二次是基线）
        real_eval = ScorePredictor.evaluate
        calls = {"n": 0}

        def fake_eval(self, X, y, yp):
            calls["n"] += 1
            res = real_eval(self, X, y, yp)
            if res is not None and calls["n"] == 1:
                res["mae"] = 9999.0
            return res

        monkeypatch.setattr(ScorePredictor, "evaluate", fake_eval)

        # 追加样本以满足"样本有增长"的触发条件
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(6):
                uid = f"grow_u{i}"
                ratio = 0.4 + (i / 5) * 0.4
                await _seed_user(db, uid, 10, ratio, base_ts=1000.0 + i)
                await db.execute(
                    "INSERT INTO user_exam_scores (user_id, level, score, "
                    "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, "CET4", int(300 + ratio * 400),
                     _date(1), "real_exam", 1.0))
            await db.commit()

        second = await learning_service.train_if_needed()
        assert second["trained"] is True
        assert second["status"] == "rejected", "候选更差时必须拒绝晋升"
        assert second["promoted"] is False
        assert second["version"] is None

        latest = await learning_service.get_latest_model()
        assert latest["version"] == 1, "线上模型版本不应变化"
        assert model_file.read_bytes() == before, "模型文件不应被覆盖"

    @pytest.mark.asyncio
    async def test_new_batches_are_recorded(self):
        """每批训练都会留下决策记录，趋势可追溯"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 8)
        await learning_service.train_if_needed(force=True)

        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(6):
                uid = f"batch_u{i}"
                ratio = 0.4 + (i / 5) * 0.4
                await _seed_user(db, uid, 10, ratio, base_ts=1000.0 + i)
                await db.execute(
                    "INSERT INTO user_exam_scores (user_id, level, score, "
                    "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, "CET4", int(300 + ratio * 400),
                     _date(1), "real_exam", 1.0))
            await db.commit()

        second = await learning_service.train_if_needed()
        assert second["trained"] is True
        assert second["status"] in ("promoted", "rejected")
        assert second["baseline_mae"] is not None, "必须有基线才能做择优判断"
        status = await learning_service.get_model_status()
        assert len(status["history"]) >= 2

    @pytest.mark.asyncio
    async def test_better_model_is_promoted(self):
        """数据质量好时确实会晋升新版本 —— 这才是"越来越准"的正向路径"""
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 6)
        first = await learning_service.train_if_needed(force=True)

        # 追加同分布的高质量样本
        async with aiosqlite.connect(DB_PATH) as db:
            for i in range(10):
                uid = f"good_u{i}"
                ratio = 0.35 + (i / 9) * 0.5
                await _seed_user(db, uid, 10, ratio, base_ts=1000.0 + i)
                await db.execute(
                    "INSERT INTO user_exam_scores (user_id, level, score, "
                    "exam_date, source, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, "CET4", int(300 + ratio * 400),
                     _date(1), "real_exam", 1.0))
            await db.commit()

        second = await learning_service.train_if_needed(force=True)
        assert second["trained"] is True
        assert second["sample_count"] > first["sample_count"]
        assert second["status"] in ("promoted", "rejected")
        if second["status"] == "promoted":
            assert second["version"] == 2
            assert second["baseline_mae"] is not None

    @pytest.mark.asyncio
    async def test_training_history_recorded(self):
        from models.database import init_db, DB_PATH
        import aiosqlite
        from services import learning_service

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await self._seed_dataset(db, 8)
        await learning_service.train_if_needed(force=True)

        status = await learning_service.get_model_status()
        assert status["current_model"]["version"] == 1
        assert len(status["history"]) >= 1
        assert status["available_labels"] == 8


# ==================== 热更新与置信度 ====================

def _train_tiny_model(p):
    """造一个已训练的最小可用模型（样本少时走小网络分支）"""
    import numpy as np
    X = np.array([[0.5] * 23] * 6)
    X[:, 0] = np.linspace(0.2, 0.9, 6)
    y = np.array([400.0, 430.0, 470.0, 510.0, 550.0, 600.0])
    p.train(X, y, np.array([0, 0, 1, 1, 1, 1]))
    return p


class TestHotReloadAndConfidence:

    @pytest.mark.asyncio
    async def test_reload_picks_up_new_version(self):
        """后台训练出新版后，运行中的 predictor 无需重启即可加载"""
        import time
        from core.score_predictor import ScorePredictor

        p = _train_tiny_model(ScorePredictor())
        p.save({"version": 1, "sample_count": 8, "val_mae": 30.0})
        consumer = ScorePredictor()
        assert consumer.load() is True and consumer.version == 1

        p.save({"version": 2, "sample_count": 20, "val_mae": 18.0})
        # Windows 文件系统时间戳分辨率较粗，显式推进 mtime 以确保可被检测
        meta_path = p.model_dir / "meta.pkl"
        os.utime(meta_path, (time.time() + 5, time.time() + 5))

        assert consumer.reload_if_newer() is True
        assert consumer.version == 2
        assert consumer.meta["sample_count"] == 20
        # 元信息缺失时不能把未训练模型当成可用
        assert ScorePredictor.reload_if_newer(consumer) is False

    def test_predict_returns_model_metadata(self):
        from core.score_predictor import ScorePredictor

        p = _train_tiny_model(ScorePredictor())
        p.save({"version": 3, "sample_count": 12, "val_mae": 25.5})
        p.load()
        out = p.predict({"细节理解": 0.8})
        assert out["model"] == "mlp"
        assert out["model_version"] == 3
        assert out["training_samples"] == 12
        assert 290 <= out["predicted_score"] <= 710

    def test_confidence_levels(self):
        """冷启动必须如实告知，不能把启发式猜测包装成精确预测"""
        from agents.review_agent import ReviewAgent

        assert ReviewAgent._confidence({"model": "heuristic"}) == "cold"
        assert ReviewAgent._confidence(
            {"model": "mlp", "training_samples": 8}) == "low"
        assert ReviewAgent._confidence(
            {"model": "mlp", "training_samples": 30}) == "medium"
        assert ReviewAgent._confidence(
            {"model": "mlp", "training_samples": 80}) == "high"


# ==================== 模考折算 ====================

class TestPaperScoring:
    """模考折算必须只认真实可判定的作答。

    旧实现的两个失真点：
      1. 字符串 upper() 直接比较，全角/空格/多选顺序都可能误判；
      2. 主观题固定给 65% 基准分——用户没做 AI 批改也能白拿分。
    现在：客观题归一化比较、未核验答案跳过、主观题只在真有批改分时折算。
    """

    def _qmap(self):
        return {
            "1": ("听力-长对话", "A", "ai_generated", 1),
            "2": ("听力-短文听力", "B", "ai_generated", 1),
            "3": ("阅读-仔细阅读", "C", "ai_generated", 1),
            "4": ("阅读-段落匹配", "D", "ai_generated", 1),
            "5": ("翻译-汉译英", "", "real_cet4_2020", 0),
            "6": ("写作", "", "real_cet4_2020", 0),
        }

    def test_full_correct_with_real_grades_scores_near_max(self):
        from main import _estimate_paper_score
        answers = {"1": "A", "2": "B", "3": "C", "4": "D",
                   "5": "我的译文", "6": "我的作文"}
        score, meta = _estimate_paper_score(
            answers, self._qmap(), {"写作", "翻译-汉译英"},
            latest_subjective={"writing": 15, "translation": 15})
        # 客观题全对 497 + 主观题满分 213 ≈ 710
        assert 700 <= score <= 710, (score, meta)
        assert meta["subjective_scored"] == ["translation", "writing"]

    def test_full_correct_without_grades_has_no_free_subjective_points(self):
        from main import _estimate_paper_score
        answers = {"1": "A", "2": "B", "3": "C", "4": "D",
                   "5": "我的译文", "6": "我的作文"}
        score, meta = _estimate_paper_score(
            answers, self._qmap(), {"写作", "翻译-汉译英"})
        # 主观题没有批改分 → 记 0，只剩客观题 497
        assert 490 <= score <= 500, (score, meta)
        assert meta["subjective_missing"], "应提示主观题未评分"

    def test_all_wrong_floor_at_220(self):
        from main import _estimate_paper_score
        qmap = {"1": ("听力-长对话", "A", "ai_generated", 1),
                "2": ("阅读-仔细阅读", "B", "ai_generated", 1)}
        score, _ = _estimate_paper_score({"1": "Z", "2": "Z"}, qmap, set())
        assert score == 220, "全错应保底 220（报道分下限）"

    def test_answer_normalization_counts_fullwidth_and_lowercase(self):
        from main import _estimate_paper_score
        qmap = {"1": ("阅读-仔细阅读", "A", "ai_generated", 1)}
        score, _ = _estimate_paper_score({"1": "ａ "}, qmap, set())
        # 全角小写 a 归一化后应判对；单题全对 → 阅读单项满分
        assert score > 220, f"归一化后应判对，实际 {score}"

    def test_unverified_real_answer_is_skipped(self):
        from main import _estimate_paper_score
        qmap = {"1": ("阅读-仔细阅读", "A", "real_cet4_2015", 0),
                "2": ("阅读-仔细阅读", "A", "ai_generated", 1)}
        score, meta = _estimate_paper_score({"1": "A", "2": "B"}, qmap, set())
        assert meta["skipped_unverified"] == 1
        # 未核验题不计入分母：唯一有效题答错 → 0 分 → 保底 220
        assert score == 220

    def test_blank_subjective_gets_nothing(self):
        from main import _estimate_paper_score
        qmap = {"5": ("写作", "", "real_cet4_2020", 0),
                "1": ("阅读-仔细阅读", "A", "ai_generated", 1)}
        answered, _ = _estimate_paper_score(
            {"5": "作文", "1": "A"}, qmap, {"写作"},
            latest_subjective={"writing": 12})
        blank, _ = _estimate_paper_score(
            {"5": "", "1": "A"}, qmap, {"写作"},
            latest_subjective={"writing": 12})
        assert answered > blank, "主观题空白不应拿到批改分"

    def test_unknown_question_id_ignored(self):
        from main import _estimate_paper_score
        score, _ = _estimate_paper_score({"nope": "A"}, {}, set())
        assert score == 220

# ==================== 实考录入 ====================

class TestExamScoreRecording:

    @pytest.mark.asyncio
    async def test_record_true_score(self):
        from models.database import init_db
        from services import learning_service

        await init_db()
        data = await learning_service.record_true_score(
            "score_user", 560, level="CET6", exam_date="2026-06-13")
        assert data["score"] == 560 and data["exam_date"] == "2026-06-13"
        assert await learning_service.count_labels() == 1

    @pytest.mark.asyncio
    async def test_score_clamped(self):
        from models.database import init_db
        from services import learning_service

        await init_db()
        data = await learning_service.record_true_score("s2", 9999)
        assert data["score"] == 710
        data = await learning_service.record_true_score("s2", -50)
        assert data["score"] == 0

    @pytest.mark.asyncio
    async def test_empty_user_rejected(self):
        from models.database import init_db
        from services import learning_service

        await init_db()
        with pytest.raises(ValueError):
            await learning_service.record_true_score("", 500)