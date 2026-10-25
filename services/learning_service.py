"""
在线学习服务 — 让 Agent 的预测随真实数据越用越准。

此前的问题（必须解决才能真正"变准"）：
  scripts/train_predictor.py 的训练标签是自己推导出来的假标签
      true_score = 290 + avg(mastery) * 420
  模型学到的只是"复述这条公式"，不管喂多少数据，误差永远漂亮但
  预测不会更准；而且训练只能手动跑脚本。

本模块实现闭环：
  真实分数标签（实考录入 + 模考折算）
    → 按"截止考试时刻"的答题快照构造因果正确的特征
    → 样本增长自动触发训练
    → 训练/验证切分评估 MAE
    → 只有比线上模型更准才晋升覆盖（防止越训越差）
    → 预测实例热更新 → 下次答题就能用上新模型
"""
import asyncio
import json
import logging
import time
from datetime import datetime

import aiosqlite
import numpy as np

from models.database import DB_PATH

logger = logging.getLogger(__name__)

MODEL_DIR = "data/models"

# 最少需要多少份真实分数样本才值得训练
MIN_SAMPLES = 5
# 相比上次训练，新增样本数达到多少就自动重训
TRAIN_TRIGGER_DELTA = 5
# 单个样本至少要有多少道答题，掌握度向量才有意义
MIN_ANSWERS_PER_SAMPLE = 5
# 训练锁超时（秒），防止异常退出留下死锁
LOCK_TIMEOUT = 600


async def record_true_score(user_id: str, score: int, level: str = "CET4",
                            exam_date: str | None = None,
                            source: str = "real_exam") -> dict:
    """录入真实考试成绩 — 预测模型最可靠的监督信号"""
    if not user_id:
        raise ValueError("user_id 不能为空")
    score = max(0, min(710, int(score)))
    if exam_date is None:
        exam_date = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_exam_scores "
            "(user_id, level, score, exam_date, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, level, score, exam_date, source, time.time()),
        )
        await db.commit()
    return {"user_id": user_id, "score": score, "exam_date": exam_date}


def _mastery_snapshot(rows: list) -> dict:
    """
    按答题序列计算掌握度（与实时链路同一贝叶斯口径）。
    rows: [(is_correct, knowledge_tags_json), ...] 按时间升序
    """
    correct_cnt, attempts = {}, {}
    for is_correct, tags_json in rows:
        try:
            tags = json.loads(tags_json) if tags_json else []
        except Exception:
            tags = []
        for tag in tags:
            attempts[tag] = attempts.get(tag, 0) + 1
            if is_correct:
                correct_cnt[tag] = correct_cnt.get(tag, 0) + 1
    return {
        tag: (correct_cnt.get(tag, 0) + 1.0) / (n + 2.0)
        for tag, n in attempts.items()
    }


async def build_training_samples() -> tuple[list, list, list]:
    """
    构造训练样本。核心约束：特征只能用"该次考试之前"的答题，
    避免把未来信息泄漏进标签（否则离线好看、在线失真）。
    """
    from core.score_predictor import ScorePredictor

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. 收集所有真实分数标签
        labels = []
        cursor = await db.execute(
            "SELECT user_id, score, exam_date, created_at FROM user_exam_scores"
        )
        for uid, score, exam_date, created_at in await cursor.fetchall():
            try:
                cutoff = datetime.strptime(exam_date, "%Y-%m-%d").timestamp()
            except Exception:
                cutoff = created_at or 0
            labels.append((uid, float(score), cutoff, "real_exam"))

        cursor = await db.execute(
            "SELECT user_id, score, started_at FROM exam_sessions "
            "WHERE status IN ('completed','timeout') AND score IS NOT NULL "
            "AND score > 0"
        )
        for uid, score, started_at in await cursor.fetchall():
            labels.append((uid, float(score), float(started_at or 0),
                           "mock_exam"))

        if not labels:
            return [], [], []

        # 2. 逐个标签，用截止时刻之前的答题算掌握度
        X_list, y_score, y_pass = [], [], []
        predictor = ScorePredictor()
        for uid, score, cutoff, source in labels:
            cursor = await db.execute(
                "SELECT ar.is_correct, q.knowledge_tags "
                "FROM answer_records ar JOIN questions q "
                "ON ar.question_id = q.question_id "
                "WHERE ar.user_id=? AND ar.created_at < ? "
                "ORDER BY ar.created_at ASC",
                (uid, cutoff),
            )
            rows = await cursor.fetchall()
            if len(rows) < MIN_ANSWERS_PER_SAMPLE:
                continue
            mastery = _mastery_snapshot(rows)
            if not mastery:
                continue
            X_list.append(predictor.prepare_features(mastery)[0])
            y_score.append(score)
            y_pass.append(1 if score >= 425 else 0)

    return X_list, y_score, y_pass


async def count_labels() -> int:
    """可用的真实分数标签数（与样本数不同：有些标签答题量不足会被过滤）"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM user_exam_scores")
        real = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM exam_sessions "
            "WHERE status IN ('completed','timeout') AND score > 0"
        )
        mock = (await cursor.fetchone())[0]
    return real + mock


async def get_latest_model() -> dict | None:
    """当前线上生效的模型版本信息"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT version, trained_at, sample_count, train_mae, val_mae, "
            "baseline_mae, status, note FROM model_registry "
            "WHERE status='promoted' ORDER BY version DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "version": row[0], "trained_at": row[1], "sample_count": row[2],
        "train_mae": row[3], "val_mae": row[4], "baseline_mae": row[5],
        "status": row[6], "note": row[7],
    }


async def should_train() -> tuple[bool, str]:
    """判断是否该重训：样本不足不训，样本没增长不训"""
    latest = await get_latest_model()
    X, _, _ = await build_training_samples()
    n = len(X)
    if n < MIN_SAMPLES:
        return False, f"有效样本不足 ({n} < {MIN_SAMPLES})"
    if latest is None:
        return True, "尚无已晋升模型，执行首次训练"
    growth = n - (latest.get("sample_count") or 0)
    if growth >= TRAIN_TRIGGER_DELTA:
        return True, f"样本增长 {growth}（≥{TRAIN_TRIGGER_DELTA}）"
    return False, f"样本无显著增长（{growth} < {TRAIN_TRIGGER_DELTA}）"


def _acquire_lock() -> bool:
    """跨进程训练锁（MVP：文件锁 + 超时释放）"""
    from pathlib import Path
    lock = Path(MODEL_DIR) / ".train.lock"
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except Exception:
            age = LOCK_TIMEOUT + 1
        if age < LOCK_TIMEOUT:
            return False
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time()))
    return True


def _release_lock() -> None:
    from pathlib import Path
    try:
        (Path(MODEL_DIR) / ".train.lock").unlink(missing_ok=True)
    except Exception:
        pass


async def _record(version_result: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO model_registry (trained_at, sample_count, train_mae, "
            "val_mae, baseline_mae, status, note) VALUES (?,?,?,?,?,?,?)",
            (time.time(), version_result.get("sample_count"),
             version_result.get("train_mae"), version_result.get("val_mae"),
             version_result.get("baseline_mae"), version_result.get("status"),
             version_result.get("note")),
        )
        await db.commit()


async def train_if_needed(force: bool = False) -> dict:
    """
    训练闭环主流程。返回决策详情，供管理接口与日志追踪。

    晋升规则：只有当候选模型在验证集上的 MAE 严格优于当前线上
    模型时才覆盖（第一次训练无基线，直接晋升）。这样即使某批
    数据质量差，模型也不会退化。
    """
    from core.score_predictor import ScorePredictor

    if not force:
        ok, reason = await should_train()
        if not ok:
            return {"trained": False, "reason": reason}

    if not _acquire_lock():
        return {"trained": False, "reason": "已有训练任务在进行中"}
    try:
        X_list, y_score, y_pass = await build_training_samples()
        if len(X_list) < MIN_SAMPLES:
            return {"trained": False,
                    "reason": f"有效样本不足 ({len(X_list)} < {MIN_SAMPLES})"}

        X = np.array(X_list, dtype=float)
        y_score = np.array(y_score, dtype=float)
        y_pass = np.array(y_pass, dtype=int)

        # 确定性切分，保证两次训练结果可比
        n = len(X)
        rng = np.random.default_rng(42)
        idx = rng.permutation(n)
        n_val = max(2, min(n - 3, int(round(n * 0.25))))
        if n_val < 1 or n - n_val < 3:
            return {"trained": False, "reason": f"样本太少无法切分 (n={n})"}
        val_idx, train_idx = idx[:n_val], idx[n_val:]
        X_tr, X_va = X[train_idx], X[val_idx]
        y_tr, y_va = y_score[train_idx], y_score[val_idx]
        yp_tr, yp_va = y_pass[train_idx], y_pass[val_idx]

        # 1. 训练候选模型
        candidate = ScorePredictor(MODEL_DIR)
        train_metrics = candidate.train(X_tr, y_tr, yp_tr)
        cand_eval = candidate.evaluate(X_va, y_va, yp_va) or {}

        # 2. 当前线上模型在同一验证集上的表现
        baseline = ScorePredictor(MODEL_DIR)
        baseline_loaded = baseline.load()
        baseline_mae = None
        if baseline_loaded:
            base_eval = baseline.evaluate(X_va, y_va, yp_va)
            baseline_mae = base_eval["mae"] if base_eval else None

        # 3. 择优晋升
        cand_mae = cand_eval.get("mae")
        promote = baseline_mae is None or (
            cand_mae is not None and cand_mae < baseline_mae)

        latest = await get_latest_model()
        next_version = (latest["version"] if latest else 0) + 1
        result = {
            "trained": True,
            "sample_count": n,
            "train_size": int(len(X_tr)),
            "val_size": int(len(X_va)),
            "train_mae": round(train_metrics["train_mae"], 3),
            "val_mae": round(cand_mae, 3) if cand_mae is not None else None,
            "baseline_mae": (round(baseline_mae, 3)
                             if baseline_mae is not None else None),
            "version": next_version if promote else None,
        }

        if promote:
            meta = {
                "version": next_version,
                "trained_at": time.time(),
                "sample_count": n,
                "train_mae": result["train_mae"],
                "val_mae": result["val_mae"],
            }
            candidate.save(meta)
            await _record({**result, "status": "promoted",
                           "note": "自动训练晋升"})
            result["status"] = "promoted"
            result["promoted"] = True
            logger.info(f"[在线学习] 模型晋升 v{next_version} "
                        f"样本{n} MAE {baseline_mae} → {cand_mae}")
        else:
            await _record({**result, "status": "rejected",
                           "note": f"验证误差未优于线上模型 "
                                   f"({cand_mae} ≥ {baseline_mae})，保留旧模型"})
            result["status"] = "rejected"
            result["promoted"] = False
            logger.info(f"[在线学习] 候选未晋升 MAE {cand_mae} "
                        f"≥ 线上 {baseline_mae}，保留旧模型")
        return result
    finally:
        _release_lock()


async def trigger_training_background(reason: str = "") -> None:
    """后台触发训练检查，不阻塞请求；失败只记日志不影响主流程"""
    try:
        result = await train_if_needed()
        if result.get("trained"):
            logger.info(f"[在线学习] 触发来源={reason} 结果={result}")
    except Exception as e:
        logger.warning(f"[在线学习] 自动训练失败({reason}): {e}")


def schedule_training(reason: str = "") -> None:
    """在 FastAPI 请求线程中调度后台训练任务"""
    try:
        asyncio.get_running_loop().create_task(
            trigger_training_background(reason))
    except RuntimeError:
        pass


async def get_model_status() -> dict:
    """模型当前状态 + 训练历史，供管理接口查看"""
    latest = await get_latest_model()
    X, _, _ = await build_training_samples()
    ok, reason = await should_train()

    history = []
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT version, trained_at, sample_count, val_mae, status "
            "FROM model_registry ORDER BY version DESC LIMIT 10"
        )
        for v, ts, cnt, mae, status in await cursor.fetchall():
            history.append({"version": v, "trained_at": ts,
                            "sample_count": cnt, "val_mae": mae,
                            "status": status})
    label_count = await count_labels()

    return {
        "current_model": latest,
        "available_samples": len(X),
        "available_labels": label_count,
        "next_trainable": ok,
        "next_train_reason": reason,
        "history": history,
    }
