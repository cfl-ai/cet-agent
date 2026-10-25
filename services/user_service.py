"""
用户档案服务 — 长期保存个人学习画像

解决问题：users 表此前从未被写入，导致考生的 level / target_score
只存在于前端传参中，刷新或换设备即丢失，学习方案无法真正针对个人。
本模块提供档案的持久化读写，并在答题等行为发生时自动建档。
"""
import time
import aiosqlite
from models.database import DB_PATH

DEFAULT_LEVEL = "CET4"
DEFAULT_TARGET_SCORE = 425

_VALID_LEVELS = {"CET4", "CET6"}


def _normalize_level(level: str | None) -> str:
    if not level:
        return DEFAULT_LEVEL
    lv = level.strip().upper()
    if lv in {"CET4", "CET-4", "四级", "4"}:
        return "CET4"
    if lv in {"CET6", "CET-6", "六级", "6"}:
        return "CET6"
    return DEFAULT_LEVEL


def _normalize_target(target: int | None) -> int:
    """目标分数合理区间 220-710（四六级总分710，过线425）"""
    if target is None:
        return DEFAULT_TARGET_SCORE
    try:
        t = int(target)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_SCORE
    return max(220, min(710, t))


async def get_profile(user_id: str) -> dict | None:
    """读取用户档案；不存在返回 None"""
    if not user_id:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, username, level, target_score, "
            "created_at, last_active FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "username": row[1],
        "level": row[2],
        "target_score": row[3],
        "created_at": row[4],
        "last_active": row[5],
    }


async def upsert_profile(
    user_id: str,
    username: str | None = None,
    level: str | None = None,
    target_score: int | None = None,
) -> dict:
    """
    创建或更新档案（部分更新：只覆盖显式传入的字段）。
    首次访问自动建档，并刷新 last_active。
    """
    if not user_id:
        raise ValueError("user_id 不能为空")

    now = time.time()
    norm_level = _normalize_level(level) if level is not None else None
    norm_target = (
        _normalize_target(target_score) if target_score is not None else None
    )

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE user_id=?", (user_id,)
        )
        exists = await cursor.fetchone() is not None

        if not exists:
            await db.execute(
                "INSERT INTO users "
                "(user_id, username, level, target_score, created_at, last_active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username or user_id,
                    norm_level or DEFAULT_LEVEL,
                    norm_target if norm_target is not None else DEFAULT_TARGET_SCORE,
                    now,
                    now,
                ),
            )
        else:
            fields, params = [], []
            if username is not None:
                fields.append("username=?")
                params.append(username)
            if norm_level is not None:
                fields.append("level=?")
                params.append(norm_level)
            if norm_target is not None:
                fields.append("target_score=?")
                params.append(norm_target)
            fields.append("last_active=?")
            params.append(now)
            await db.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE user_id=?",
                (*params, user_id),
            )
        await db.commit()

    return await get_profile(user_id)


async def touch(user_id: str) -> None:
    """
    答题等学习行为发生时调用：确保档案存在并刷新活跃时间。
    不覆盖已有用户的 level / target_score。
    """
    if not user_id:
        return
    profile = await get_profile(user_id)
    if profile is None:
        await upsert_profile(user_id)


async def get_study_overview(user_id: str) -> dict:
    """档案 + 累计学习统计，供个人主页/仪表盘使用"""
    profile = await get_profile(user_id)
    if profile is None:
        profile = await upsert_profile(user_id)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_correct), 0) "
            "FROM answer_records WHERE user_id=?",
            (user_id,),
        )
        total, correct = await cursor.fetchone()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND resolved=0",
            (user_id,),
        )
        wrong_open = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(DISTINCT stat_date) FROM study_stats WHERE user_id=?",
            (user_id,),
        )
        active_days = (await cursor.fetchone())[0]

    total = total or 0
    correct = correct or 0
    return {
        **profile,
        "total_answered": total,
        "total_correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "open_wrong": wrong_open,
        "active_days": active_days,
    }
