"""错题归因服务 — 从"错了"到"为什么错"的可解释诊断。

设计要点：
  1. 规则归因先给证据链：题型、答题时长、重复错次数、掌握度、选项相似度、
     解析关键词，每条依据都写进 evidence（JSON），保证结论可解释、可复核，
     而不是"看题型拍脑袋"。
  2. LLM 只做精修：一次最多 5 题，输出经服务端严格校验
     （question_id 必须在输入集合内、error_type 在枚举内、confidence 0-1），
     非法项丢弃并用规则结果补齐；调用异常 / 脏 JSON 一律回退规则，绝不 500。
  3. 每次归因 upsert 到 wrong_attributions（UNIQUE(user_id, question_id)），
     source 记录 rules / hybrid / llm，重复归因不会重复插入。

表结构对齐 models/database.py：
  wrong_attributions(user_id, question_id, knowledge_point, error_type,
                     confidence, root_cause, remediation, evidence, source, created_at)
"""
import difflib
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Any

import aiosqlite

from models.database import DB_PATH
from services.llm_client import llm_client

logger = logging.getLogger(__name__)

# prompt 版本号：写入报告 meta，便于以后复核"当时用的是哪版提示词"
PROMPT_VERSION = "wrong_attribution_v1"

# 错误类型枚举（与需求约定一致，前后端共用）
ERROR_TYPES = ("vocab", "careless", "concept", "knowledge_gap", "inference", "grammar")
ERROR_TYPE_CN = {
    "vocab": "词汇欠缺",
    "careless": "粗心失误",
    "concept": "概念不清",
    "knowledge_gap": "知识漏洞",
    "inference": "推理偏差",
    "grammar": "语法薄弱",
}
# 同分时的确定性优先级，保证规则结果可复现
_TYPE_PRIORITY = {et: i for i, et in enumerate(ERROR_TYPES)}

# ---- 规则阈值（全部集中在这里，便于解释与调参）----
RUSH_SECONDS = 5.0          # 低于此时长视为秒选 → careless
HESITATE_SECONDS = 60.0     # 超过此时长仍错 → concept
REPEAT_WRONG_THRESHOLD = 3  # 同题累计错 >= 3 次 → concept
LOW_MASTERY_THRESHOLD = 0.4 # 掌握度低于此值 → knowledge_gap
OPTION_SIMILARITY_THRESHOLD = 0.6  # 选项文本相似度阈值
LLM_BATCH_LIMIT = 5         # 单次 LLM 精修最多 5 题（控制成本）

VOCAB_QUESTION_KEYWORDS = ("选词填空", "词汇", "单词", "词义", "词形")
INFERENCE_QUESTION_KEYWORDS = ("推断", "推理", "主旨", "态度", "意图", "标题", "隐含")
GRAMMAR_QUESTION_KEYWORDS = ("语法", "时态", "从句", "语态", "虚拟", "非谓语", "主谓一致")
VOCAB_EXPLANATION_KEYWORDS = ("词义", "搭配", "词汇", "单词", "近义", "形近", "词形")
GRAMMAR_EXPLANATION_KEYWORDS = ("语法", "时态", "从句", "语态", "虚拟", "非谓语", "主谓一致")
INFERENCE_EXPLANATION_KEYWORDS = ("推断", "主旨", "态度", "意图", "隐含", "作者观点")

# 根因 / 补救模板：保证每条归因都有"为什么"和"接下来做什么"
ROOT_CAUSE_TEMPLATES = {
    "vocab": "「{kp}」相关词汇的词义/固定搭配掌握不牢（本题累计错 {wrong} 次），"
             "题干或选项中出现未能准确识别的词汇，导致理解偏差。",
    "knowledge_gap": "「{kp}」掌握度偏低（{mastery_hint}，累计错 {wrong} 次），"
                     "属于知识漏洞，需要系统性补学而不是继续机械刷题。",
    "inference": "「{kp}」推断/主旨类题目中，定位到原文后没有完成同义替换与逻辑推断，"
                 "证据链未落到正确选项。",
    "grammar": "「{kp}」涉及语法规则，规则边界（时态/从句/非谓语等）掌握不牢固，导致误判。",
}
REMEDIATION_TEMPLATES = {
    "vocab": "把本题及选项中不认识的词加入生词本，整理 3 个搭配例句，"
             "再完成 5 道「{kp}」同类选词/词汇题。",
    "careless": "针对「{kp}」做 3 道同类型题，强制圈画题干关键词，"
                "答完留 10 秒复核选项，记录每天的粗心率。",
    "concept": "回到「{kp}」的定义/规则笔记，用费曼法复述一遍，"
               "再做 5 道变式题并写出每步理由。",
    "knowledge_gap": "制定「{kp}」7 天补漏清单：看讲解→做 10 题→错题复盘，"
                     "掌握度回到 0.6 以上再进入下一考点。",
    "inference": "精读 2 篇真题，逐题标出定位句与选项改写点，"
                 "练习「同义替换 + 排除法」。",
    "grammar": "整理「{kp}」语法规则卡（结构+例句+易错点），"
               "完成 10 道语法专项题并逐题标注考点。",
}


# ==================== 通用工具 ====================

def _require_user_id(user_id) -> str:
    """user_id 为空的请求属于参数错误，统一抛 ValueError（路由层映射 400）"""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id 不能为空")
    return uid


def _loads_json_lenient(text) -> Any:
    """容错解析 JSON：去代码围栏、截取最外层片段、修复尾逗号。

    LLM 输出常见问题：JSON 代码围栏、前后夹带解释文字、对象/数组末尾多逗号。
    这里逐级降级解析，返回 None 表示确实无法修复。
    """
    if not isinstance(text, str) or not text.strip():
        return None

    candidates = [text.strip(), _strip_code_fences(text)]
    # 截取最外层 JSON 片段，容忍"好的，结果如下：..."这类前后缀
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        try:
            return json.loads(_repair_json_text(candidate))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _strip_code_fences(text: str) -> str:
    text = text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_json_text(text: str) -> str:
    """修复常见脏 JSON：代码围栏、尾逗号、Python 字面量 True/False/None"""
    text = _strip_code_fences(text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    return text


def _to_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _parse_tags(raw) -> list[str]:
    """knowledge_tags 容错解析：只接受字符串列表，坏数据不拖垮归因"""
    data = _loads_json_lenient(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        return []
    return [str(tag).strip() for tag in data if str(tag).strip()]


def _parse_options(raw) -> dict[str, str]:
    """选项统一解析为 {标签: 文本}。

    兼容 ["A. xxx", ...]、[{"A": "xxx"}, ...]、{"A": "xxx"} 三种历史格式。
    """
    data = _loads_json_lenient(raw) if isinstance(raw, str) else raw
    options: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            options[str(key).strip().upper()] = str(value).strip()
        return options
    if not isinstance(data, list):
        return options

    letters = "ABCDEFGH"
    for index, item in enumerate(data):
        if isinstance(item, dict):
            for key, value in item.items():
                options[str(key).strip().upper()] = str(value).strip()
            continue
        text = str(item).strip()
        matched = re.match(r"^([A-Ha-h])[\.、:：\)]\s*(.*)$", text)
        if matched:
            options[matched.group(1).upper()] = matched.group(2).strip()
        elif index < len(letters):
            options[letters[index]] = text
    return options


def _as_letter(value) -> str | None:
    """把 "A" / "A. xxx" / "（A）" 归一为单字母选项标签"""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 1 and text.upper() in "ABCDEFGH":
        return text.upper()
    matched = re.match(r"^[（(]?([A-Ha-h])[）)]?[\.、:：\s]", text)
    return matched.group(1).upper() if matched else None


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def _option_text(answer: str, options: dict[str, str]) -> str:
    letter = _as_letter(answer)
    if letter and letter in options:
        return options[letter]
    return str(answer or "").strip()


def _option_signals(user_answer, correct_answer, options: dict[str, str]) -> list[dict]:
    """选项层面的证据：相邻选项 → careless；文本高度相似 → concept"""
    signals: list[dict] = []
    if not user_answer or not correct_answer:
        return signals

    user_letter = _as_letter(user_answer)
    correct_letter = _as_letter(correct_answer)
    if user_letter and correct_letter and user_letter != correct_letter:
        distance = abs(ord(user_letter) - ord(correct_letter))
        if distance == 1:
            signals.append({
                "signal": "adjacent_option",
                "error_type": "careless",
                "weight": 0.40,
                "detail": f"所选 {user_letter} 与正确项 {correct_letter} 相邻，"
                          f"排除法使用不彻底 / 审题不细",
            })

    user_text = _option_text(user_answer, options)
    correct_text = _option_text(correct_answer, options)
    if user_letter != correct_letter and user_text and correct_text:
        ratio = difflib.SequenceMatcher(
            None, _normalize_text(user_text), _normalize_text(correct_text)
        ).ratio()
        if ratio >= OPTION_SIMILARITY_THRESHOLD:
            signals.append({
                "signal": "similar_option",
                "error_type": "concept",
                "weight": 0.40,
                "detail": f"所选与正确项文本相似度 {ratio:.2f}，属近义/形近概念混淆",
            })
    return signals


def _pick_knowledge_point(tags: list[str], question_type: str,
                          mastery_map: dict[str, float]) -> tuple[str, float | None]:
    """知识点取"最弱标签"：多个标签时优先掌握度最低的那个（更值得补）"""
    if tags:
        chosen = min(
            tags,
            key=lambda tag: (mastery_map.get(tag)
                             if mastery_map.get(tag) is not None else 1.0,
                             tags.index(tag)),
        )
        return chosen, mastery_map.get(chosen)
    if question_type:
        return question_type, None
    return "未标注知识点", None


def _root_cause(error_type: str, knowledge_point: str, duration: float | None,
                wrong_count: int, mastery: float | None,
                signals: list[dict]) -> str:
    if error_type == "careless":
        if duration is not None and 0 < duration < RUSH_SECONDS:
            hint = f"本题仅用 {duration:.1f} 秒秒选"
        elif any(s["signal"] == "adjacent_option" for s in signals):
            hint = "所选项与正确项相邻"
        else:
            hint = "读题与复核不充分"
        return (f"「{knowledge_point}」本身具备作答能力，但{hint}，"
                f"属于流程性失分而非能力缺口。")

    if error_type == "concept":
        parts = []
        if duration is not None and duration > HESITATE_SECONDS:
            parts.append(f"单题耗时 {duration:.1f} 秒仍答错")
        if wrong_count >= REPEAT_WRONG_THRESHOLD:
            parts.append(f"同题累计错 {wrong_count} 次")
        hint = "，".join(parts) or "对规则边界把握不清"
        return (f"「{knowledge_point}」的核心概念/规则理解不到位（{hint}），"
                f"需要回到定义与典型例题重建理解，而不是继续机械刷题。")

    mastery_hint = (f"当前掌握度 {mastery:.2f}" if mastery is not None
                    else "暂无掌握度记录")
    template = ROOT_CAUSE_TEMPLATES.get(error_type,
                                        ROOT_CAUSE_TEMPLATES["knowledge_gap"])
    return template.format(kp=knowledge_point, wrong=wrong_count,
                           mastery_hint=mastery_hint)


def _remediation(error_type: str, knowledge_point: str) -> str:
    template = REMEDIATION_TEMPLATES.get(error_type,
                                         REMEDIATION_TEMPLATES["knowledge_gap"])
    return template.format(kp=knowledge_point)


def _agent_identity() -> dict:
    """记录本次可能使用的 provider / model（无可用 key 时为 None）"""
    try:
        for provider in llm_client.providers:
            if provider.get("api_key"):
                return {"provider": provider.get("name"),
                        "model": provider.get("model")}
    except Exception:  # 测试桩/配置异常都不能影响主流程
        pass
    return {"provider": None, "model": None}


# ==================== 规则归因 ====================

def _build_rule_item(row: aiosqlite.Row,
                     mastery_map: dict[str, float]) -> dict:
    """单题规则归因：收集全部证据信号 → 加权投票 → 生成根因与补救动作"""
    qtype = (row["question_type"] or "").strip()
    tags = _parse_tags(row["knowledge_tags"])
    knowledge_point, mastery = _pick_knowledge_point(tags, qtype, mastery_map)
    wrong_count = int(row["wrong_count"] or 1)
    duration = _to_float(row["answer_duration"], default=None)
    user_answer = (row["latest_user_answer"] or "").strip() or None
    correct_answer = (row["answer"] or row["latest_correct_answer"] or "").strip()
    explanation = row["explanation"] or ""
    options = _parse_options(row["options"])

    signals: list[dict] = []

    def add(signal: str, error_type: str, weight: float, detail: str):
        signals.append({"signal": signal, "error_type": error_type,
                        "weight": weight, "detail": detail})

    # 1) 题型证据
    if any(keyword in qtype for keyword in VOCAB_QUESTION_KEYWORDS):
        add("question_type_vocab", "vocab", 0.60,
            f"题型「{qtype}」属于词汇类，错误优先归因词汇欠缺")
    if any(keyword in qtype for keyword in INFERENCE_QUESTION_KEYWORDS):
        add("question_type_inference", "inference", 0.45,
            f"题型「{qtype}」考查推断/主旨，错误多源于逻辑推断")
    if any(keyword in qtype for keyword in GRAMMAR_QUESTION_KEYWORDS):
        add("question_type_grammar", "grammar", 0.50,
            f"题型「{qtype}」属于语法类，错误与语法规则掌握相关")

    # 2) 答题时长证据
    if duration is not None and 0 < duration < RUSH_SECONDS:
        add("duration_rush", "careless", 0.55,
            f"本题仅用 {duration:.1f} 秒完成，低于 {RUSH_SECONDS:.0f} 秒秒选阈值，"
            f"基本没有读完题干")
    if duration is not None and duration > HESITATE_SECONDS:
        add("duration_hesitate", "concept", 0.50,
            f"本题耗时 {duration:.1f} 秒仍答错，超过 {HESITATE_SECONDS:.0f} 秒犹豫阈值，"
            f"概念/规则不清")

    # 3) 重复错误证据
    if wrong_count >= REPEAT_WRONG_THRESHOLD:
        add("repeat_wrong", "concept", 0.50,
            f"同题已累计答错 {wrong_count} 次（阈值 {REPEAT_WRONG_THRESHOLD}），"
            f"说明不是偶然失误")

    # 4) 掌握度证据
    if mastery is not None and mastery < LOW_MASTERY_THRESHOLD:
        add("low_mastery", "knowledge_gap", 0.55,
            f"「{knowledge_point}」掌握度仅 {mastery:.2f}，"
            f"低于 {LOW_MASTERY_THRESHOLD}，属于知识漏洞")

    # 5) 选项相似/相邻证据
    signals.extend(_option_signals(user_answer, correct_answer, options))

    # 6) 解析关键词证据
    for keyword in VOCAB_EXPLANATION_KEYWORDS:
        if keyword in explanation:
            add("explanation_vocab", "vocab", 0.35,
                f"解析中出现「{keyword}」，考点指向词义/固定搭配")
            break
    for keyword in GRAMMAR_EXPLANATION_KEYWORDS:
        if keyword in explanation:
            add("explanation_grammar", "grammar", 0.45,
                f"解析中出现「{keyword}」，考点指向语法规则")
            break
    for keyword in INFERENCE_EXPLANATION_KEYWORDS:
        if keyword in explanation:
            add("explanation_inference", "inference", 0.35,
                f"解析中出现「{keyword}」，考点指向逻辑推断")
            break

    # 加权投票：同分时按固定枚举优先级，保证可复现
    score_by_type: dict[str, float] = {}
    for signal in signals:
        error_type = signal["error_type"]
        score_by_type[error_type] = round(
            score_by_type.get(error_type, 0.0) + signal["weight"], 3)

    if signals:
        total_weight = sum(score_by_type.values())
        winner = max(score_by_type,
                     key=lambda et: (score_by_type[et],
                                     -_TYPE_PRIORITY.get(et, len(ERROR_TYPES))))
        winner_signals = [s for s in signals if s["error_type"] == winner]
        # 置信度 = 基础 0.35 + 相对票数 0.30 + 多条同向证据每条 +0.05，封顶 0.95
        confidence = min(0.95, round(
            0.35 + 0.30 * (score_by_type[winner] / total_weight)
            + 0.05 * (len(winner_signals) - 1), 2))
    else:
        # 没有任何规则信号：给出保守默认值，并在证据里写明"无显著信号"
        winner = "knowledge_gap"
        confidence = 0.30

    evidence = {
        "question_type": qtype,
        "wrong_count": wrong_count,
        "duration": duration,
        "user_answer": user_answer,
        "correct_answer": correct_answer,
        "mastery": mastery,
        "knowledge_point": knowledge_point,
        "legacy_error_type": row["legacy_error_type"],
        "signals": signals,
        "score_by_type": score_by_type,
        "rule_error_type": winner,
        "rule_confidence": confidence,
        "llm": None,
    }

    return {
        "question_id": row["question_id"],
        "question_type": qtype or "未分类",
        "knowledge_point": knowledge_point,
        "error_type": winner,
        "confidence": confidence,
        "root_cause": _root_cause(winner, knowledge_point, duration,
                                  wrong_count, mastery, signals),
        "remediation": _remediation(winner, knowledge_point),
        "wrong_count": wrong_count,
        "last_wrong_at": row["last_wrong_at"],
        "evidence": evidence,
        "source": "rules",
    }


# ==================== LLM 精修 ====================

ATTRIBUTION_SYSTEM_PROMPT = "你是四六级错题归因专家。只输出 JSON，不要输出解释性文字。"

ATTRIBUTION_PROMPT = """请根据每道错题的作答证据判断错误原因，并给出可执行的补救建议。

error_type 必须从以下枚举中选择：
- vocab：词汇欠缺（词义/固定搭配不认识）
- careless：粗心失误（秒选、审题/复核不足、选项相邻）
- concept：概念不清（规则/概念理解不到位、同题反复错）
- knowledge_gap：知识漏洞（长期掌握度低）
- inference：推理偏差（推断/主旨/态度题逻辑链断裂）
- grammar：语法薄弱（时态/从句/非谓语等规则错误）

错题数据：
{items}

只输出 JSON 数组，每个元素格式：
{{"question_id": "题目ID", "error_type": "枚举值", "knowledge_point": "知识点",
  "root_cause": "根因（40字内）", "remediation": "具体补救动作（60字内）",
  "confidence": 0.0到1.0之间的小数}}
"""


def _format_items_for_prompt(items: list[dict]) -> str:
    lines = []
    for index, item in enumerate(items, 1):
        evidence = item["evidence"]
        duration = evidence.get("duration")
        duration_text = f"{duration:.1f}s" if duration is not None else "未知"
        options = evidence.get("_options_text") or ""
        lines.append(
            f"{index}. question_id: {item['question_id']}\n"
            f"   题型: {item['question_type']}；知识点: {item['knowledge_point']}；"
            f"用户答案: {evidence.get('user_answer') or '未记录'}；"
            f"正确答案: {evidence.get('correct_answer') or '未记录'}；"
            f"答题时长: {duration_text}；重复错: {item['wrong_count']} 次；"
            f"掌握度: {evidence.get('mastery') if evidence.get('mastery') is not None else '未知'}；"
            f"题干: {(evidence.get('content') or '')[:200]}；"
            f"选项: {options[:300]}；"
            f"解析: {(evidence.get('explanation') or '')[:150]}"
        )
    return "\n".join(lines)


def _clean_str(value) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_llm_entries(parsed, allowed_qids: set[str]) -> dict[str, dict]:
    """服务端校验 LLM 输出：非法项直接丢弃，交由规则结果补齐"""
    if isinstance(parsed, dict):
        parsed = [parsed]  # 兼容只返回单个对象的模型
    if not isinstance(parsed, list):
        return {}

    entries: dict[str, dict] = {}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        question_id = _clean_str(row.get("question_id"))
        if question_id not in allowed_qids:
            continue  # 不在输入集合内的题目一律丢弃，防止模型臆造
        error_type = _clean_str(row.get("error_type"))
        if error_type not in ERROR_TYPES:
            continue  # 枚举外类型丢弃
        confidence = _to_float(row.get("confidence"), default=None)
        if confidence is None or not (0.0 <= confidence <= 1.0):
            continue  # 置信度必须落在 0-1
        entries[question_id] = {
            "error_type": error_type,
            "confidence": round(confidence, 2),
            "knowledge_point": _clean_str(row.get("knowledge_point")),
            "root_cause": _clean_str(row.get("root_cause")),
            "remediation": _clean_str(row.get("remediation")),
        }
    return entries


async def _refine_with_llm(items: list[dict]) -> tuple[dict[str, dict], dict]:
    """调用 LLM 精修；解析/结构非法时带修复提示重试一次，最终失败返回空"""
    if not items:
        return {}, {"attempted": False, "applied": False, "fallback": False,
                    "attempts": 0, "error": None, "prompt_version": PROMPT_VERSION,
                    **_agent_identity()}

    allowed_qids = {item["question_id"] for item in items}
    prompt = ATTRIBUTION_PROMPT.format(items=_format_items_for_prompt(items))
    messages = [
        {"role": "system", "content": ATTRIBUTION_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    meta = {
        "attempted": True, "applied": False, "fallback": False,
        "attempts": 0, "error": None, "prompt_version": PROMPT_VERSION,
        **_agent_identity(),
    }

    for attempt in range(2):
        meta["attempts"] = attempt + 1
        try:
            content = await llm_client.chat(
                messages, temperature=0.2, max_tokens=1200)
        except Exception as exc:  # 无 key / 超时 / 网络错误 → 直接降级规则
            meta["fallback"] = True
            meta["error"] = str(exc)
            logger.warning(f"[归因] LLM 调用失败，回退规则归因: {exc}")
            return {}, meta

        parsed = _loads_json_lenient(content)
        entries = _normalize_llm_entries(parsed, allowed_qids)
        if entries:
            meta["applied"] = True
            return entries, meta

        # 解析失败/结构非法：追加修复提示再试一次
        meta["error"] = "LLM 输出不是合法 JSON 数组或字段不合规"
        logger.warning(f"[归因] LLM 输出不可用（第 {attempt + 1} 次）")
        messages = messages + [
            {"role": "assistant", "content": str(content)[:1000]},
            {"role": "user",
             "content": "上面的输出不是合法 JSON 数组或字段不合规。"
                        "请严格只输出 JSON 数组，question_id 必须是给定集合内的值，"
                        "error_type 必须是枚举值，confidence 必须是 0-1 的小数，"
                        "不要输出任何解释文字。"},
        ]

    meta["fallback"] = True
    return {}, meta


def _merge_llm_entries(items: list[dict], entries: dict[str, dict]) -> None:
    """把校验通过的 LLM 结果合并进规则结果：LLM 缺的字段用规则补齐"""
    for item in items:
        entry = entries.get(item["question_id"])
        if not entry:
            item["source"] = "rules"
            item["evidence"]["llm"] = None
            continue

        fields_from_rules = 0
        knowledge_point = entry["knowledge_point"] or item["knowledge_point"]
        fields_from_rules += 0 if entry["knowledge_point"] else 1
        root_cause = entry["root_cause"] or item["root_cause"]
        fields_from_rules += 0 if entry["root_cause"] else 1
        remediation = entry["remediation"] or item["remediation"]
        fields_from_rules += 0 if entry["remediation"] else 1

        item["error_type"] = entry["error_type"]
        item["confidence"] = entry["confidence"]
        item["knowledge_point"] = knowledge_point
        item["root_cause"] = root_cause
        item["remediation"] = remediation
        # LLM 全字段齐全 → llm；依赖规则补齐 → hybrid
        item["source"] = "llm" if fields_from_rules == 0 else "hybrid"
        item["evidence"]["llm"] = {
            "error_type": entry["error_type"],
            "confidence": entry["confidence"],
            "fields_from_rules": fields_from_rules,
            "accepted": True,
        }


# ==================== 落库 ====================

async def _upsert_attributions(user_id: str, items: list[dict]) -> None:
    """同题重复归因走 UPDATE，保证幂等（UNIQUE(user_id, question_id)）"""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        for item in items:
            await db.execute(
                """
                INSERT INTO wrong_attributions
                (user_id, question_id, knowledge_point, error_type, confidence,
                 root_cause, remediation, evidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, question_id) DO UPDATE SET
                    knowledge_point=excluded.knowledge_point,
                    error_type=excluded.error_type,
                    confidence=excluded.confidence,
                    root_cause=excluded.root_cause,
                    remediation=excluded.remediation,
                    evidence=excluded.evidence,
                    source=excluded.source,
                    created_at=excluded.created_at
                """,
                (
                    user_id,
                    item["question_id"],
                    item["knowledge_point"],
                    item["error_type"],
                    item["confidence"],
                    item["root_cause"],
                    item["remediation"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                    item["source"],
                    now,
                ),
            )
        await db.commit()


# ==================== 对外入口 ====================

async def _load_wrong_question_rows(db, user_id: str, limit: int):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT w.question_id, w.wrong_count, w.error_type AS legacy_error_type,
               w.last_wrong_at, q.question_type, q.knowledge_tags, q.content,
               q.options, q.answer, q.explanation,
               ar.duration AS answer_duration,
               ar.user_answer AS latest_user_answer,
               ar.correct_answer AS latest_correct_answer
        FROM wrong_questions w
        JOIN questions q ON q.question_id = w.question_id
        LEFT JOIN answer_records ar ON ar.record_id = (
            SELECT MAX(ar2.record_id) FROM answer_records ar2
            WHERE ar2.user_id = w.user_id AND ar2.question_id = w.question_id
        )
        WHERE w.user_id = ? AND w.resolved = 0
        ORDER BY w.wrong_count DESC, w.last_wrong_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return await cursor.fetchall()


async def _load_mastery_map(db, user_id: str) -> dict[str, float]:
    cursor = await db.execute(
        "SELECT knowledge_point, mastery FROM knowledge_state WHERE user_id=?",
        (user_id,),
    )
    return {row[0]: row[1] for row in await cursor.fetchall() if row[0]}


async def attribute_wrong_questions(user_id: str, limit: int = 20,
                                    use_llm: bool = True) -> dict:
    """错题归因主入口：规则归因 + 可选 LLM 精修 → 落库 → 返回报告"""
    started = time.time()
    uid = _require_user_id(user_id)
    limit = max(1, min(50, int(limit or 20)))

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await _load_wrong_question_rows(db, uid, limit)
        mastery_map = await _load_mastery_map(db, uid)

    items = [_build_rule_item(row, mastery_map) for row in rows]
    # 为 prompt 补充原始题干/选项/解析（只用于 LLM，不落库）
    for row, item in zip(rows, items):
        item["evidence"]["content"] = row["content"] or ""
        item["evidence"]["explanation"] = row["explanation"] or ""
        item["evidence"]["_options_text"] = json.dumps(
            _parse_options(row["options"]), ensure_ascii=False)

    llm_meta = {"attempted": False, "applied": False, "fallback": False,
                "attempts": 0, "error": None, "prompt_version": PROMPT_VERSION,
                **_agent_identity()}
    if use_llm and items:
        entries, llm_meta = await _refine_with_llm(items[:LLM_BATCH_LIMIT])
        if entries:
            _merge_llm_entries(items, entries)
        else:
            for item in items:
                item["source"] = "rules"
                item["evidence"]["llm"] = None
    else:
        for item in items:
            item["source"] = "rules"
            item["evidence"]["llm"] = None

    # 落库前清理 prompt 专用字段，evidence 只保留可复核依据
    for item in items:
        item["evidence"].pop("_options_text", None)

    if items:
        await _upsert_attributions(uid, items)

    report = await get_attribution_report(uid)
    report["meta"].update({
        "llm": llm_meta,
        "elapsed_ms": int((time.time() - started) * 1000),
        "limit": limit,
        "use_llm": bool(use_llm),
    })
    return report


async def get_attribution_report(user_id: str) -> dict:
    """读取归因报告：按错误类型/题型聚合，输出 top 薄弱点与可执行建议"""
    uid = _require_user_id(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT w.question_id, w.wrong_count, w.last_wrong_at,
                   q.question_type, a.knowledge_point, a.error_type, a.confidence,
                   a.root_cause, a.remediation, a.evidence, a.source
            FROM wrong_questions w
            JOIN questions q ON q.question_id = w.question_id
            LEFT JOIN wrong_attributions a
                   ON a.user_id = w.user_id AND a.question_id = w.question_id
            WHERE w.user_id = ? AND w.resolved = 0
            ORDER BY w.wrong_count DESC, w.last_wrong_at DESC
            """,
            (uid,),
        )
        rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT knowledge_point, mastery FROM knowledge_state WHERE user_id=?",
            (uid,),
        )
        mastery_map = {r[0]: r[1] for r in await cursor.fetchall() if r[0]}

    items: list[dict] = []
    by_error_type: Counter = Counter()
    by_question_type: Counter = Counter()
    point_stats: dict[str, dict] = {}
    source_counts: Counter = Counter()
    total_wrong = len(rows)

    for row in rows:
        if not row["error_type"]:
            continue  # 该错题尚未归因
        evidence = _loads_json_lenient(row["evidence"])
        if not isinstance(evidence, dict):
            evidence = {}
        question_type = row["question_type"] or "未分类"
        item = {
            "question_id": row["question_id"],
            "question_type": question_type,
            "knowledge_point": row["knowledge_point"] or "未标注知识点",
            "error_type": row["error_type"],
            "error_type_cn": ERROR_TYPE_CN.get(row["error_type"], row["error_type"]),
            "confidence": row["confidence"],
            "root_cause": row["root_cause"],
            "remediation": row["remediation"],
            "wrong_count": int(row["wrong_count"] or 1),
            "last_wrong_at": row["last_wrong_at"],
            "source": row["source"] or "rules",
            "evidence": evidence,
        }
        items.append(item)
        by_error_type[item["error_type"]] += 1
        by_question_type[question_type] += 1
        source_counts[item["source"]] += 1

        stat = point_stats.setdefault(item["knowledge_point"], {
            "knowledge_point": item["knowledge_point"],
            "mastery": mastery_map.get(item["knowledge_point"]),
            "wrong_count": 0,
            "error_types": [],
        })
        stat["wrong_count"] += item["wrong_count"]
        if item["error_type"] not in stat["error_types"]:
            stat["error_types"].append(item["error_type"])

    # 薄弱点排序：错得越多越优先；同错次数下掌握度越低越优先
    top_weak_points = sorted(
        point_stats.values(),
        key=lambda p: (-p["wrong_count"],
                       p["mastery"] if p["mastery"] is not None else 1.0,
                       p["knowledge_point"]),
    )[:5]

    attributed = len(items)
    unattributed = max(0, total_wrong - attributed)
    summary = {
        "total_wrong": total_wrong,
        "attributed": attributed,
        "by_error_type": dict(by_error_type),
        "by_question_type": dict(by_question_type),
        "top_weak_points": top_weak_points,
    }

    note = None
    if total_wrong == 0:
        note = "暂无错题记录，先完成一次练习再来查看归因报告。"
    elif attributed == 0:
        note = (f"检测到 {total_wrong} 道错题但尚未归因，"
                f"请先调用 POST /api/wrong/{uid}/attribution 生成归因。")
    elif unattributed:
        note = f"还有 {unattributed} 道错题尚未归因，可重新运行归因以覆盖全部错题。"

    # 没有任何错题时返回空报告 + 一句提示，不给无数据支撑的"建议"
    recommendations = ([] if total_wrong == 0 else
                       _build_recommendations(top_weak_points,
                                              by_error_type,
                                              unattributed))

    return {
        "user_id": uid,
        "generated_at": time.time(),
        "summary": summary,
        "items": items,
        "recommendations": recommendations,
        "note": note,
        "meta": {
            "source_counts": dict(source_counts),
            "unattributed": unattributed,
        },
    }


_ACTION_BY_ERROR_TYPE = {
    "vocab": "建立「{kp}」高频词与固定搭配卡片，每天 15 分钟，每个生词造 1 个句子。",
    "careless": "针对「{kp}」做 3 道同型题，强制圈画题干关键词，"
                "答完留 10 秒复核选项。",
    "concept": "回看「{kp}」的定义与规则，用费曼法复述一遍，"
               "再做 5 道变式题并写出每步理由。",
    "knowledge_gap": "为「{kp}」排 7 天补漏清单：讲解→10 题练习→错题复盘，"
                     "掌握度回到 0.6 再推进。",
    "inference": "精读 2 篇真题，逐题标出定位句与选项的同义替换点，练习排除法。",
    "grammar": "整理「{kp}」语法卡（结构+例句+易错点），"
               "完成 10 道专项题并标注考点。",
}


def _build_recommendations(top_weak_points: list[dict],
                           by_error_type: Counter,
                           unattributed: int) -> list[str]:
    recommendations: list[str] = []
    for point in top_weak_points[:2]:
        mastery = (f"{point['mastery']:.2f}"
                   if point["mastery"] is not None else "未知")
        recommendations.append(
            f"优先补强「{point['knowledge_point']}」：掌握度 {mastery}，"
            f"累计错 {point['wrong_count']} 次。")

    if by_error_type:
        dominant_type, _ = max(by_error_type.items(),
                               key=lambda kv: (kv[1], -_TYPE_PRIORITY.get(kv[0], 99)))
        knowledge_point = (top_weak_points[0]["knowledge_point"]
                           if top_weak_points else "薄弱考点")
        recommendations.append(
            _ACTION_BY_ERROR_TYPE.get(dominant_type,
                                      "针对基础薄弱项做专项补强。")
            .format(kp=knowledge_point))

    if unattributed:
        recommendations.append(
            f"还有 {unattributed} 道错题未归因，先运行归因让建议覆盖完整错题集。")

    if not recommendations:
        recommendations.append("暂无足够错题数据，先完成 20 题练习再生成归因建议。")
    return recommendations[:5]
