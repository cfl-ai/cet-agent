"""个性化提分方案服务 — 让"每天练多少、练什么"有据可依、可复算。

生成逻辑：
  1. 收集真实学情：users 档案 / knowledge_state 掌握度 / wrong_attributions 归因 /
     writing_grades / translation_grades / user_exam_scores / 近 14 天 study_stats。
  2. current_score：优先最新真实考试分；否则用透明公式 290 + avg_mastery*420
     （标注 source=heuristic）；两者都没有则 None，并建议先完成 20 题测评。
  3. gap = target - current（clamp >= 0）；horizon 默认按 level（CET4 42 天 / CET6 56 天），
     显式传入 days_left 时以它为准。
  4. focus_points 按「低掌握度 × 高频考点」排序，每条带 reason；
     weekly_schedule 的周数 = ceil(horizon/7)，题量按每天 10-20 题、每周 1-2 篇批改推导，
     并写入 volume 便于核对"一周不会出现 500 题"。
  5. LLM 只负责 narrative（150-250 字）与 actions（3-6 条），
     失败或输出不合规时用模板兜底，model_meta 记录是否 fallback。
  6. 落库 improvement_plans，source_snapshot 固化各数据源实际数值，便于复算与解释。

表结构对齐 models/database.py：
  improvement_plans(plan_id, user_id, level, current_score, target_score, gap,
                    horizon_days, focus_points, weekly_schedule, actions, narrative,
                    source_snapshot, model_meta, created_at)
"""
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from models.database import DB_PATH
from services import user_service
from services.llm_client import llm_client

logger = logging.getLogger(__name__)

PROMPT_VERSION = "improvement_plan_v1"

DEFAULT_TARGET_SCORE = 425
DEFAULT_HORIZON_DAYS = {"CET4": 42, "CET6": 56}
MIN_TARGET_SCORE, MAX_TARGET_SCORE = 220, 710
MIN_HORIZON_DAYS, MAX_HORIZON_DAYS = 1, 180

# 透明评分公式：无真实考试分时用掌握度估算（写入 source_snapshot）
SCORE_BASE = 290
SCORE_SPAN = 420

# 题量约束：每天 10-20 题，避免"一周 500 题"这种不可执行方案
MIN_DAILY_QUESTIONS, MAX_DAILY_QUESTIONS = 10, 20
MIN_WEEKLY_WRITING, MAX_WEEKLY_WRITING = 1, 2
TASK_TYPES = ("practice", "writing", "translation", "vocabulary", "review")
FALLBACK_FOCUS_POINTS = ("听力理解", "仔细阅读", "写作", "翻译", "词汇")


# ==================== 通用工具 ====================

def _require_user_id(user_id) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id 不能为空")
    return uid


def _to_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _loads_json_lenient(text) -> Any:
    """容错解析落库 JSON（含 LLM 输出）：去代码围栏、截取片段、修复尾逗号"""
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = [text.strip(), _strip_code_fences(text)]
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    for candidate in candidates:
        for attempt_text in (candidate, _repair_json_text(candidate)):
            try:
                return json.loads(attempt_text)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return None


def _strip_code_fences(text: str) -> str:
    text = text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_json_text(text: str) -> str:
    text = _strip_code_fences(text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text


def _normalize_level(level, fallback: str = "CET4") -> str:
    text = str(level or "").strip().upper()
    if text in {"CET6", "CET-6", "六级", "6"}:
        return "CET6"
    if text in {"CET4", "CET-4", "四级", "4"}:
        return "CET4"
    fallback_text = str(fallback or "").strip().upper()
    return "CET6" if fallback_text in {"CET6", "CET-6"} else "CET4"


def _normalize_target(value, fallback: int = DEFAULT_TARGET_SCORE) -> int:
    number = _to_float(value if value is not None else fallback, default=None)
    if number is None:
        number = DEFAULT_TARGET_SCORE
    return int(max(MIN_TARGET_SCORE, min(MAX_TARGET_SCORE, round(number))))


def _normalize_horizon(days_left, level: str) -> int:
    if days_left is not None:
        number = _to_float(days_left, default=None)
        if number is not None:
            return int(max(MIN_HORIZON_DAYS, min(MAX_HORIZON_DAYS, round(number))))
    return DEFAULT_HORIZON_DAYS.get(level, 42)


def _agent_identity() -> dict:
    """记录本次可能使用的 provider / model（无可用 key 时为 None）"""
    try:
        for provider in llm_client.providers:
            if provider.get("api_key"):
                return {"provider": provider.get("name"),
                        "model": provider.get("model")}
    except Exception:
        pass
    return {"provider": None, "model": None}


# ==================== 数据采集 ====================

async def _load_learning_data(user_id: str) -> dict:
    """一次读取方案所需的全部真实数据源（只读，不写库）"""
    start_date = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT knowledge_point, mastery, attempts, correct "
            "FROM knowledge_state WHERE user_id=? ORDER BY mastery ASC",
            (user_id,),
        )
        knowledge_state = [dict(row) for row in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT knowledge_point, error_type, confidence "
            "FROM wrong_attributions WHERE user_id=?",
            (user_id,),
        )
        attributions = [dict(row) for row in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT COUNT(*) AS count FROM wrong_questions "
            "WHERE user_id=? AND resolved=0",
            (user_id,),
        )
        wrong_open = int((await cursor.fetchone())["count"] or 0)

        cursor = await db.execute(
            "SELECT score, level, exam_date, created_at FROM user_exam_scores "
            "WHERE user_id=? "
            "ORDER BY COALESCE(created_at, 0) DESC, id DESC LIMIT 1",
            (user_id,),
        )
        exam_row = await cursor.fetchone()
        latest_exam = dict(exam_row) if exam_row else None

        cursor = await db.execute(
            "SELECT score, score_710, level_band, feedback, created_at "
            "FROM writing_grades WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        writing_row = await cursor.fetchone()
        writing_grade = dict(writing_row) if writing_row else None

        cursor = await db.execute(
            "SELECT score, score_710, level_band, issues, created_at "
            "FROM translation_grades WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        translation_row = await cursor.fetchone()
        translation_grade = dict(translation_row) if translation_row else None

        cursor = await db.execute(
            "SELECT COALESCE(SUM(questions_done), 0) AS questions_done, "
            "COALESCE(SUM(correct_count), 0) AS correct_count, "
            "COALESCE(SUM(study_minutes), 0) AS study_minutes, "
            "COUNT(DISTINCT stat_date) AS active_days "
            "FROM study_stats WHERE user_id=? AND stat_date>=?",
            (user_id, start_date),
        )
        study_stats = dict(await cursor.fetchone())

    return {
        "knowledge_state": knowledge_state,
        "attributions": attributions,
        "wrong_open": wrong_open,
        "latest_exam": latest_exam,
        "writing_grade": writing_grade,
        "translation_grade": translation_grade,
        "study_stats": study_stats,
        "stats_start_date": start_date,
    }


# ==================== 当前分 / 分差 ====================

def _avg_mastery(knowledge_state: list[dict]) -> tuple[float | None, list[dict]]:
    """有效掌握度简单平均（每个知识点等权），只统计练过的知识点"""
    points = [row for row in knowledge_state if int(row.get("attempts") or 0) > 0]
    if not points:
        return None, []
    masteries = [float(row["mastery"]) if row["mastery"] is not None else 0.0
                 for row in points]
    avg = sum(masteries) / len(masteries)
    detail = [{"knowledge_point": row["knowledge_point"],
               "mastery": round(float(row["mastery"] or 0.0), 3),
               "attempts": int(row.get("attempts") or 0)} for row in points]
    return round(avg, 3), detail


def _derive_current_score(data: dict) -> dict:
    """当前分推导：真实考试分 > 掌握度启发式 > 无数据"""
    avg, detail = _avg_mastery(data["knowledge_state"])
    latest_exam = data.get("latest_exam")
    if latest_exam and latest_exam.get("score") is not None:
        # 历史脏数据可能超出 0-710，收敛后再参与分差计算
        exam_score = max(0.0, min(710.0, float(latest_exam["score"])))
        return {
            "method": "real_exam",
            "value": round(exam_score, 1),
            "avg_mastery": avg,
            "formula": None,
            "inputs": {"latest_exam_score": latest_exam},
        }
    if avg is not None:
        value = round(SCORE_BASE + avg * SCORE_SPAN, 1)
        return {
            "method": "heuristic",
            "value": value,
            "avg_mastery": avg,
            "formula": "290 + avg_mastery * 420",
            "inputs": {"points": len(detail), "avg_mastery": avg,
                       "mastery_points": detail},
        }
    return {
        "method": "none",
        "value": None,
        "avg_mastery": None,
        "formula": None,
        "inputs": {"reason": "无真实考试成绩，且 knowledge_state 无练习记录"},
    }


# ==================== 重点考点 ====================

def _build_focus_points(data: dict, limit: int = 5) -> list[dict]:
    """按「低掌握度 × 高频考点」排序；无数据时按题型均衡兜底"""
    point_stats: dict[str, dict] = {}

    for row in data["knowledge_state"]:
        point = row.get("knowledge_point")
        if not point:
            continue
        point_stats[point] = {
            "knowledge_point": point,
            "mastery": row.get("mastery"),
            "attempts": int(row.get("attempts") or 0),
            "wrong_count": 0,
            "error_types": [],
        }

    for row in data["attributions"]:
        point = row.get("knowledge_point") or "未标注知识点"
        stat = point_stats.setdefault(point, {
            "knowledge_point": point, "mastery": None, "attempts": 0,
            "wrong_count": 0, "error_types": [],
        })
        stat["wrong_count"] += 1
        error_type = row.get("error_type")
        if error_type and error_type not in stat["error_types"]:
            stat["error_types"].append(error_type)

    points: list[dict] = []
    for stat in point_stats.values():
        mastery = stat["mastery"] if stat["mastery"] is not None else 0.5
        attempts = stat["attempts"]
        # 透明度量：掌握度越低、练习题量越大（高频考点），优先级越高；错题再加重
        priority = round((1.0 - mastery) * max(attempts, 1)
                         + 2.0 * stat["wrong_count"], 3)
        mastery_text = (f"{float(stat['mastery']):.2f}"
                        if stat["mastery"] is not None else "未知（按 0.5 先验）")
        points.append({
            "knowledge_point": stat["knowledge_point"],
            "mastery": stat["mastery"],
            "attempts": attempts,
            "wrong_count": stat["wrong_count"],
            "error_types": stat["error_types"],
            "priority": priority,
            "reason": (f"掌握度 {mastery_text}，已练 {attempts} 题，"
                       f"归因错题 {stat['wrong_count']} 道；"
                       f"按「低掌握度 × 高频考点」优先。"),
        })

    points.sort(key=lambda item: (-item["priority"],
                                  item["mastery"] if item["mastery"] is not None else 1.0,
                                  item["knowledge_point"]))
    points = points[:limit]

    if not points:
        return [{
            "knowledge_point": section,
            "mastery": None,
            "attempts": 0,
            "wrong_count": 0,
            "error_types": [],
            "priority": 0.0,
            "reason": "暂无学情数据，按四六级题型均衡分配；"
                      "先完成 20 题测评可重新定位薄弱项。",
        } for section in FALLBACK_FOCUS_POINTS]

    # 数据点不足时用题型兜底补齐，保证方案覆盖听力/阅读/写作/翻译/词汇
    existing = {point["knowledge_point"] for point in points}
    for section in FALLBACK_FOCUS_POINTS:
        if len(points) >= limit:
            break
        if section in existing:
            continue
        points.append({
            "knowledge_point": section,
            "mastery": None,
            "attempts": 0,
            "wrong_count": 0,
            "error_types": [],
            "priority": 0.0,
            "reason": "补充题型：暂无该模块学情数据，按均衡覆盖纳入计划。",
        })
    return points


# ==================== 周计划 ====================

def _phase_for_week(week: int, weeks: int) -> str:
    if weeks <= 1:
        return "考前冲刺"
    ratio = week / weeks
    if ratio <= 0.4:
        return "基础补漏"
    if ratio <= 0.75:
        return "专项突破"
    if week < weeks:
        return "真题强化"
    return "真题冲刺"


def _build_weekly_schedule(horizon_days: int, gap: float | None, target_score: int,
                           focus_points: list[dict], avg_mastery: float | None,
                           data: dict) -> tuple[list[dict], dict]:
    """周计划：题量由"每天 10-20 题"推导，周数 = ceil(horizon/7)"""
    weeks = max(1, math.ceil(horizon_days / 7))
    gap_value = gap if gap is not None else 60.0

    # 分差越大每天题量越高，但封顶 20 题；分差小也保底 10 题
    daily_questions = MIN_DAILY_QUESTIONS + min(
        MAX_DAILY_QUESTIONS - MIN_DAILY_QUESTIONS,
        int(round(gap_value / 30)),
    )
    daily_questions = max(MIN_DAILY_QUESTIONS,
                          min(MAX_DAILY_QUESTIONS, daily_questions))
    practice_weekly = daily_questions * 7
    words_weekly = 70 + (daily_questions - MIN_DAILY_QUESTIONS) * 5  # 70-120 词/周

    wrong_open = int(data.get("wrong_open") or 0)
    review_weekly = max(5, min(20, 5 + wrong_open // max(1, weeks)))
    gap_large = gap_value >= 100
    writing_weekly = 2 if (gap_large and weeks > 1) else 1
    translation_weekly = 2 if (gap_value >= 150 and weeks > 1) else 1

    base_mastery = avg_mastery if avg_mastery is not None else 0.3
    target_mastery = min(0.9, max(0.6, base_mastery + 0.35))

    schedule: list[dict] = []
    for week in range(1, weeks + 1):
        focus = focus_points[(week - 1) % len(focus_points)] if focus_points else None
        point_name = focus["knowledge_point"] if focus else "综合能力"
        if week == weeks and weeks > 1:
            theme = "真题冲刺｜全真模考与错题收官"
        else:
            theme = f"{_phase_for_week(week, weeks)}｜主攻「{point_name}」"

        tasks = [
            {"type": "practice", "count": practice_weekly, "unit": "题",
             "detail": f"专项练习：每天约 {daily_questions} 题，优先「{point_name}」，"
                       f"错题当天订正。"},
            {"type": "vocabulary", "count": words_weekly, "unit": "词",
             "detail": "高频词 + 错题生词，重点记搭配与例句。"},
            {"type": "writing", "count": writing_weekly, "unit": "篇",
             "detail": "写作批改：按逐句问题修改后重写一遍。"},
            {"type": "translation", "count": translation_weekly, "unit": "篇",
             "detail": "翻译批改：对照参考译文标注漏译/错译/语法问题。"},
            {"type": "review", "count": review_weekly, "unit": "题",
             "detail": "错题复盘：重做本周错题，按归因类型整理根因。"},
        ]

        if week == weeks:
            if gap is not None:
                milestone = (f"完成 1 套全真模考，总分达到 {target_score} 分附近，"
                             f"错题复盘清零。")
            else:
                milestone = "完成 1 套全真模考并建立完整学情基线。"
        else:
            progress = round(base_mastery
                             + (target_mastery - base_mastery) * week / weeks, 2)
            accuracy = min(85, 60 + week * 3)
            milestone = (f"「{point_name}」掌握度达到 {progress:.2f}，"
                         f"本周练习正确率不低于 {accuracy}%。")

        schedule.append({
            "week": week,
            "theme": theme,
            "tasks": tasks,
            "milestone": milestone,
        })

    volume = {
        "weeks": weeks,
        "daily_questions": daily_questions,
        "practice_total": practice_weekly * weeks,
        "vocabulary_total": words_weekly * weeks,
        "writing_total": writing_weekly * weeks,
        "translation_total": translation_weekly * weeks,
        "review_total": review_weekly * weeks,
        "rule": "每天 10-20 题、每周 1-2 篇写作/翻译批改，题量与剩余天数匹配",
    }
    return schedule, volume


# ==================== LLM 叙述（可降级） ====================

PLAN_SYSTEM_PROMPT = "你是四六级备考规划师。只输出 JSON 对象，不要输出解释文字。"

PLAN_PROMPT = """请根据以下学情数据，为考生写一段 150-250 字、鼓励且具体的提分规划叙述，并给出 3-6 条可执行动作。

考生层级：{level}
当前水平：{current}
目标分数：{target}
分差：{gap}
剩余天数：{horizon} 天（{weeks} 周）
重点关注：{focus}
近 14 天学习：{stats}
每周安排：{schedule}

要求：
1. narrative 具体到考点与动作，避免空话，语气鼓励；
2. actions 每条 15-40 字、当天可执行；
3. 只输出 JSON 对象：{{"narrative": "...", "actions": ["...", "..."]}}
"""


def _template_actions(daily_questions: int, current_score: float | None) -> list[str]:
    actions = [
        f"每天完成 {daily_questions} 道专项题，当天订正并把错因写进错题本。",
        "每天用 15 分钟复习 20 个高频词，重点记固定搭配和例句。",
        "每周完成 1-2 篇写作或翻译并提交批改，按逐句问题修改后重写一遍。",
        "周末用 30 分钟复盘错题归因，把概念不清/知识漏洞类错题重做一遍。",
        "每两周做一套全真模考，记录总分与各模块正确率并对照目标调整。",
    ]
    if current_score is None:
        actions.insert(0, "先完成 20 题测评（建议 10 道阅读 + 10 道听力），建立初始学情基线。")
    return actions[:6]


def _template_narrative(level: str, current_score: float | None, target_score: int,
                        gap: float | None, horizon_days: int,
                        focus_points: list[dict], daily_questions: int,
                        weeks: int) -> str:
    current_text = (f"当前水平约 {current_score} 分" if current_score is not None
                    else "目前尚无足够的考试成绩与掌握度数据")
    gap_text = (f"距离目标还差 {gap} 分" if gap is not None
                else "先建立基线再定冲刺节奏")
    focus_names = "、".join(point["knowledge_point"]
                            for point in focus_points[:3]) or "听力、阅读与写作"
    return (f"你的目标是 {level} 考到 {target_score} 分，{current_text}，{gap_text}。"
            f"接下来 {horizon_days} 天按 {weeks} 周推进：先把精力集中在{focus_names}上，"
            f"每天保持 {daily_questions} 道题和 20 个高频词的节奏，"
            f"每周完成 1-2 篇写作或翻译批改，周末用错题归因复盘本周失分点。"
            f"提分不靠一次刷很多，而靠每天可完成的小目标；坚持两周你会看到正确率的变化，"
            f"四周后进入全真模考，把稳定得分段先拿满，再逐步冲击更高分。")


def _stats_text(data: dict) -> str:
    stats = data.get("study_stats") or {}
    return (f"完成 {int(stats.get('questions_done') or 0)} 题，"
            f"正确 {int(stats.get('correct_count') or 0)} 题，"
            f"学习 {int(stats.get('study_minutes') or 0)} 分钟，"
            f"活跃 {int(stats.get('active_days') or 0)} 天")


async def _compose_narrative(level: str, target_score: int,
                             current_score: float | None, current_method: str,
                             gap: float | None, horizon_days: int,
                             focus_points: list[dict], data: dict,
                             volume: dict) -> tuple[str, list[str], dict]:
    """LLM 生成 narrative + actions；失败/不合规时模板兜底，并记录 model_meta"""
    started = time.time()
    weeks = volume["weeks"]
    daily_questions = volume["daily_questions"]
    fallback_narrative = _template_narrative(
        level, current_score, target_score, gap, horizon_days,
        focus_points, daily_questions, weeks)
    fallback_actions = _template_actions(daily_questions, current_score)

    identity = _agent_identity()
    meta = {
        "prompt_version": PROMPT_VERSION,
        **identity,
        "llm_attempted": True,
        "fallback": True,
        "narrative_source": "template",
        "actions_source": "template",
        "attempts": 0,
        "error": None,
        "elapsed_ms": 0,
    }

    if current_score is None:
        current_text = "暂无（缺少考试成绩与掌握度数据）"
    elif current_method == "real_exam":
        current_text = f"{current_score} 分（最近一次真实考试）"
    else:
        current_text = f"{current_score} 分（按掌握度公式 290 + avg_mastery*420 估算）"
    gap_text = f"{gap} 分" if gap is not None else "未知（需先建立基线）"
    focus_text = "、".join(
        f"{point['knowledge_point']}（{point['reason']}）"
        for point in focus_points[:3]) or "暂无"
    schedule_text = (f"共 {weeks} 周，每天约 {daily_questions} 题，"
                     f"每周写作 {volume['writing_total'] // weeks} 篇、"
                     f"翻译 {volume['translation_total'] // weeks} 篇，"
                     f"错题复盘 {volume['review_total'] // weeks} 题")

    prompt = PLAN_PROMPT.format(
        level=level, current=current_text, target=target_score, gap=gap_text,
        horizon=horizon_days, weeks=weeks, focus=focus_text,
        stats=_stats_text(data), schedule=schedule_text,
    )
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    narrative, actions = fallback_narrative, fallback_actions
    for attempt in range(2):
        meta["attempts"] = attempt + 1
        try:
            content = await llm_client.chat(
                messages, temperature=0.6, max_tokens=900)
        except Exception as exc:  # 无 key / 超时 / 网络错误
            meta["error"] = str(exc)
            logger.warning(f"[提分方案] LLM 调用失败，使用模板兜底: {exc}")
            break

        parsed = _loads_json_lenient(content)
        if isinstance(parsed, dict):
            raw_narrative = parsed.get("narrative")
            raw_actions = parsed.get("actions")
            narrative_ok = (isinstance(raw_narrative, str)
                            and len(raw_narrative.strip()) >= 60)
            cleaned_actions: list[str] = []
            if isinstance(raw_actions, list):
                cleaned_actions = [str(item).strip()
                                   for item in raw_actions if str(item).strip()][:6]
            actions_ok = 3 <= len(cleaned_actions) <= 6

            if narrative_ok:
                narrative = raw_narrative.strip()[:600]
                meta["narrative_source"] = "llm"
            if actions_ok:
                actions = cleaned_actions
                meta["actions_source"] = "llm"
            meta["fallback"] = not (narrative_ok and actions_ok)
            if narrative_ok or actions_ok:
                if not (narrative_ok and actions_ok):
                    meta["error"] = "LLM 输出仅部分可用，缺失部分用模板补齐"
                break

        meta["error"] = "LLM 输出不是合法 JSON 对象或缺少 narrative/actions"
        logger.warning(f"[提分方案] LLM 输出不可用（第 {attempt + 1} 次）")
        messages = messages + [
            {"role": "assistant", "content": str(content)[:1000]},
            {"role": "user",
             "content": "上面的输出不是合法 JSON 对象或字段不合规。"
                        "请严格只输出 JSON 对象，包含 150-250 字的 narrative "
                        "和 3-6 条 actions 字符串，不要输出任何解释文字。"},
        ]

    meta["elapsed_ms"] = int((time.time() - started) * 1000)
    return narrative, actions, meta


# ==================== 落库 ====================

async def _save_plan(plan: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO improvement_plans
            (plan_id, user_id, level, current_score, target_score, gap,
             horizon_days, focus_points, weekly_schedule, actions, narrative,
             source_snapshot, model_meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan["plan_id"],
                plan["user_id"],
                plan["level"],
                plan["current_score"],
                plan["target_score"],
                plan["gap"],
                plan["horizon_days"],
                json.dumps(plan["focus_points"], ensure_ascii=False),
                json.dumps(plan["weekly_schedule"], ensure_ascii=False),
                json.dumps(plan["actions"], ensure_ascii=False),
                plan["narrative"],
                json.dumps(plan["source_snapshot"], ensure_ascii=False),
                json.dumps(plan["model_meta"], ensure_ascii=False),
                plan["created_at"],
            ),
        )
        await db.commit()


def _row_to_plan(row: aiosqlite.Row) -> dict:
    def _parse(raw, default):
        parsed = _loads_json_lenient(raw)
        return parsed if parsed is not None else default

    snapshot = _parse(row["source_snapshot"], {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "plan_id": row["plan_id"],
        "user_id": row["user_id"],
        "level": row["level"],
        "current_score": row["current_score"],
        "target_score": row["target_score"],
        "gap": row["gap"],
        "horizon_days": row["horizon_days"],
        "current_score_source": (snapshot.get("current_score") or {}).get("method"),
        "focus_points": _parse(row["focus_points"], []),
        "weekly_schedule": _parse(row["weekly_schedule"], []),
        "actions": _parse(row["actions"], []),
        "narrative": row["narrative"],
        "source_snapshot": snapshot,
        "model_meta": _parse(row["model_meta"], {}),
        "created_at": row["created_at"],
        "note": snapshot.get("note"),
    }


# ==================== 对外入口 ====================

async def generate_improvement_plan(user_id: str, level: str | None = None,
                                    target_score: int | None = None,
                                    days_left: int | None = None) -> dict:
    """生成个性化提分方案：学情快照 → 当前分/分差 → 重点考点 → 周计划 → LLM 叙述 → 落库"""
    started = time.time()
    uid = _require_user_id(user_id)

    profile = await user_service.get_profile(uid)
    level = _normalize_level(
        level if level is not None else (profile or {}).get("level"))
    target = _normalize_target(
        target_score if target_score is not None
        else (profile or {}).get("target_score"))
    horizon_days = _normalize_horizon(days_left, level)

    data = await _load_learning_data(uid)
    score_info = _derive_current_score(data)
    current_score = score_info["value"]
    gap = (round(max(0.0, target - current_score), 1)
           if current_score is not None else None)

    focus_points = _build_focus_points(data)
    weekly_schedule, volume = _build_weekly_schedule(
        horizon_days, gap, target, focus_points,
        score_info.get("avg_mastery"), data)

    narrative, actions, model_meta = await _compose_narrative(
        level, target, current_score, score_info["method"], gap,
        horizon_days, focus_points, data, volume)

    plan_id = f"plan_{uuid.uuid4().hex[:12]}"
    created_at = time.time()
    note = None
    if current_score is None:
        note = ("暂无考试成绩与掌握度数据，请先完成 20 题测评"
                "（建议 10 道阅读 + 10 道听力），系统会据此重算提分方案。")

    source_snapshot = {
        "generated_at": created_at,
        "user_id": uid,
        "level": level,
        "profile": profile,
        "target_score": target,
        "horizon_days": horizon_days,
        "days_left_input": days_left,
        "knowledge_state": data["knowledge_state"],
        "wrong_open": data["wrong_open"],
        "attribution": {
            "total": len(data["attributions"]),
            "by_error_type": dict(Counter(
                row.get("error_type") for row in data["attributions"]
                if row.get("error_type"))),
            "knowledge_points": sorted({
                row.get("knowledge_point")
                for row in data["attributions"]
                if row.get("knowledge_point")}),
        },
        "latest_exam_score": data["latest_exam"],
        "writing_grade": data["writing_grade"],
        "translation_grade": data["translation_grade"],
        "study_stats_14d": data["study_stats"],
        "stats_start_date": data["stats_start_date"],
        "current_score": score_info,
        "focus_weight_rule": "priority = (1 - mastery) * max(attempts, 1) + 2 * wrong_count",
        "volume": volume,
        "note": note,
    }

    model_meta = dict(model_meta)
    model_meta["total_elapsed_ms"] = int((time.time() - started) * 1000)

    plan = {
        "plan_id": plan_id,
        "user_id": uid,
        "level": level,
        "current_score": current_score,
        "target_score": target,
        "gap": gap,
        "horizon_days": horizon_days,
        "current_score_source": score_info["method"],
        "focus_points": focus_points,
        "weekly_schedule": weekly_schedule,
        "actions": actions,
        "narrative": narrative,
        "source_snapshot": source_snapshot,
        "model_meta": model_meta,
        "created_at": created_at,
        "note": note,
    }

    await _save_plan(plan)
    return plan


async def get_latest_plan(user_id: str) -> dict | None:
    """读取该用户最近一次生成的提分方案；从未生成返回 None"""
    uid = _require_user_id(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT plan_id, user_id, level, current_score, target_score, gap, "
            "horizon_days, focus_points, weekly_schedule, actions, narrative, "
            "source_snapshot, model_meta, created_at "
            "FROM improvement_plans WHERE user_id=? "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 1",
            (uid,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_plan(row)
