"""
学习分析服务 — 把分散在各表中的学习记录聚合成可读的学情视图。

覆盖四类查询（均按 user_id 隔离，跨设备可续）：
  1. get_weak_points   薄弱知识点排序（掌握度 + 未解决错题归集）
  2. get_learning_trend 最近 N 天每日趋势（缺省日补零，便于画连续曲线）
  3. get_streak        连续打卡天数（当前 + 历史最长）
  4. get_exam_history  真题模考历史（用时、状态、得分）
  5. get_dashboard     以上汇总，供个人主页/仪表盘一次拉取

背景：这些表（answer_records / knowledge_state / study_stats /
exam_sessions）此前只有写入没有聚合读取，前端拿不到趋势与排名，
学习情况"记了却看不见"。
"""
import json
import time
from datetime import datetime, timedelta

import aiosqlite

from models.database import DB_PATH
from services import user_service


def _parse_tags(tags_json) -> list:
    """knowledge_tags 容错解析，坏数据不拖垮聚合"""
    try:
        tags = json.loads(tags_json) if tags_json else []
    except Exception:
        return []
    return tags if isinstance(tags, list) else []


async def get_weak_points(user_id: str, limit: int = 5,
                          min_attempts: int = 2) -> list[dict]:
    """
    薄弱知识点：掌握度升序，同水平按练习次数降序（练得多还错更值得优先补）。
    同时归集每个知识点的未解决错题数，让"薄弱"有错题佐证。
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT knowledge_point, mastery, attempts, correct "
            "FROM knowledge_state WHERE user_id=? AND attempts>=? "
            "ORDER BY mastery ASC, attempts DESC LIMIT ?",
            (user_id, min_attempts, limit),
        )
        rows = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT q.knowledge_tags FROM wrong_questions w "
            "JOIN questions q ON w.question_id = q.question_id "
            "WHERE w.user_id=? AND w.resolved=0",
            (user_id,),
        )
        tag_rows = await cursor.fetchall()

    wrong_by_point: dict[str, int] = {}
    for (tags_json,) in tag_rows:
        for tag in _parse_tags(tags_json):
            wrong_by_point[tag] = wrong_by_point.get(tag, 0) + 1

    points = []
    for point, mastery, attempts, correct in rows:
        attempts = attempts or 0
        correct = correct or 0
        points.append({
            "point": point,
            "mastery": round(mastery or 0.0, 3),
            "attempts": attempts,
            "correct": correct,
            "accuracy": round(correct / attempts, 3) if attempts else 0.0,
            "open_wrong": wrong_by_point.get(point, 0),
        })
    return points


async def get_learning_trend(user_id: str, days: int = 30) -> list[dict]:
    """
    最近 N 天每日学习统计。没有记录的日期补零（accuracy 为 None），
    保证前端画出的是连续曲线而不是稀疏点。
    """
    days = max(1, min(90, days))
    start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT stat_date, questions_done, correct_count, "
            "words_reviewed, study_minutes FROM study_stats "
            "WHERE user_id=? AND stat_date>=? ORDER BY stat_date ASC",
            (user_id, start),
        )
        rows = await cursor.fetchall()

    by_date = {r[0]: r for r in rows}
    trend = []
    for i in range(days):
        date = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        r = by_date.get(date)
        done = (r[1] or 0) if r else 0
        correct = (r[2] or 0) if r else 0
        trend.append({
            "date": date,
            "questions_done": done,
            "correct_count": correct,
            "accuracy": round(correct / done, 3) if done else None,
            "words_reviewed": (r[3] or 0) if r else 0,
            "study_minutes": (r[4] or 0) if r else 0,
        })
    return trend


async def get_streak(user_id: str) -> dict:
    """
    连续打卡统计。
    current_streak：截至今天的连续学习天数（今天还没学不算断，
    从最近一天往前数）；longest_streak：历史最长连续。
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT stat_date FROM study_stats "
            "WHERE user_id=? ORDER BY stat_date DESC",
            (user_id,),
        )
        dates = [r[0] for r in await cursor.fetchall()]

    if not dates:
        return {"current_streak": 0, "longest_streak": 0,
                "total_days": 0, "last_study_date": None}

    date_set = set(dates)
    today = datetime.now()
    # 今天已学则从今天起数，否则从昨天起数（容忍"今天还没开始学"）
    anchor = today if today.strftime("%Y-%m-%d") in date_set \
        else today - timedelta(days=1)
    current = 0
    d = anchor
    while d.strftime("%Y-%m-%d") in date_set:
        current += 1
        d -= timedelta(days=1)

    longest, run, prev = 0, 0, None
    for ds in sorted(date_set):
        if prev is not None:
            gap = (datetime.strptime(ds, "%Y-%m-%d")
                   - datetime.strptime(prev, "%Y-%m-%d")).days
            run = run + 1 if gap == 1 else 1
        else:
            run = 1
        longest = max(longest, run)
        prev = ds

    return {
        "current_streak": current,
        "longest_streak": longest,
        "total_days": len(date_set),
        "last_study_date": dates[0],
    }


async def get_exam_history(user_id: str, limit: int = 20) -> list[dict]:
    """真题模考历史（新→旧），附带真实用时"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_id, level, year, month, set_number, "
            "duration_seconds, started_at, ended_at, status, score "
            "FROM exam_sessions WHERE user_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()

    history = []
    for r in rows:
        started, ended = r[6], r[7]
        used = int(ended - started) if (started and ended) else None
        history.append({
            "session_id": r[0],
            "level": r[1],
            "year": r[2],
            "month": r[3],
            "set_number": r[4],
            "duration_seconds": r[5],
            "started_at": started,
            "ended_at": ended,
            "used_seconds": used,
            "status": r[8],
            "score": r[9],
        })
    return history


async def record_words_reviewed(user_id: str, count: int = 1) -> dict:
    """
    记录单词复习量（此前 study_stats.words_reviewed 从未被写入，
    词汇学习在统计上是黑洞）。
    """
    if not user_id:
        raise ValueError("user_id 不能为空")
    count = max(1, min(500, int(count or 1)))
    await user_service.touch(user_id)

    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO study_stats (user_id, stat_date) VALUES (?, ?)",
            (user_id, today),
        )
        await db.execute(
            "UPDATE study_stats SET words_reviewed = words_reviewed + ? "
            "WHERE user_id=? AND stat_date=?",
            (count, user_id, today),
        )
        await db.commit()

    return {"user_id": user_id, "stat_date": today, "words_reviewed": count}


async def get_dashboard(user_id: str, days: int = 30) -> dict:
    """个人主页仪表盘：一次拉齐档案、趋势、薄弱点、打卡、模考记录"""
    overview = await user_service.get_study_overview(user_id)
    trend = await get_learning_trend(user_id, days)
    return {
        "profile": overview,
        "trend": trend,
        "last_7d_questions": sum(t["questions_done"] for t in trend[-7:]),
        "last_7d_minutes": sum(t["study_minutes"] for t in trend[-7:]),
        "weak_points": await get_weak_points(user_id),
        "streak": await get_streak(user_id),
        "recent_exams": await get_exam_history(user_id, limit=5),
        "generated_at": time.time(),
    }
