"""写作 / 翻译真实判分服务。

为什么需要这一层（此前的问题）：
  1. 旧 GradingAgent 对主观题只打 "subjective" 标记跳过，没有任何分数；
  2. 即便调用 LLM 打分，也只是把模型返回的数字原样透传，缺服务端校验，
     模型给出 0-100 分、维度分与总分对不上、返回 markdown 包裹的脏 JSON
     时会直接 500 或落库脏数据；
  3. 模型不可用时没有兜底，用户拿不到任何反馈。

本模块的三条硬约束：
  * 分数可解释：每个分数都能落到维度分 + 逐句批注 / 逐条问题 + 修改建议，
    不允许"请对照参考答案自查"式空评分；
  * 服务端复核：LLM 只是"提议"，分数范围、维度分与总分一致性、JSON 结构、
    输入长度全部由服务端再校验一次；解析失败先修复重试，最终失败降级规则评分，
    绝不 500；
  * 每次落库：写入 writing_grades / translation_grades（列名严格对齐
    models/database.py），model_meta 记录 provider/model/prompt_version/
    是否 fallback/耗时，便于事后复核与成本核算。

评分口径：
  * 写作按四六级官方 15 分制：content / language / organization 各 0-5，
    总分 = 三维度之和（0-15）；
  * 翻译按 15 分制：accuracy / language 各 0-7.5，总分 0-15；
  * 写作、翻译各占试卷 15%（106.5 分），折算 score_710 = score / 15 * 106.5。
"""
import json
import logging
import re
import time
import uuid
from collections import Counter
from typing import Any

import aiosqlite

from models.database import DB_PATH
from services.llm_client import llm_client

logger = logging.getLogger(__name__)

# ==================== 常量 ====================

# prompt 版本号：改动评分 prompt 或 JSON 契约时必须递增，便于回溯历史评分的口径
PROMPT_VERSION = "grading-2026.10.v1"

# 正文类文本上限（字符），超过直接拒绝，避免超长文本烧 token + 拖垮接口
MAX_TEXT_LEN = 5000

# 15 分制满分、折算后的 106.5 分制满分
MAX_SCORE = 15.0
SCORE_710_FULL = 106.5

# 翻译两维度上限
TRANSLATION_DIM_MAX = 7.5

# LLM 温度：判分要稳定可复现，用低温度
LLM_TEMPERATURE = 0.2
LLM_MAX_TOKENS = 1800
MAX_LLM_ATTEMPTS = 2  # 首次 + 一次"修复重试"

# 四六级写作建议字数
DEFAULT_WORD_LIMIT = {"CET4": 120, "CET6": 150}

# 官方档位（15 分制）
LEVEL_BANDS = (
    (13.0, "14分档"),
    (10.0, "11分档"),
    (7.0, "8分档"),
    (4.0, "5分档"),
    (1.0, "2分档"),
)

_TABLE_OF = {"writing": "writing_grades", "translation": "translation_grades"}

# 写作衔接词（组织维度证据）
_CONNECTIVES = (
    "however", "therefore", "moreover", "furthermore", "in addition",
    "besides", "for example", "for instance", "on the other hand",
    "in contrast", "as a result", "consequently", "firstly", "first of all",
    "secondly", "finally", "in conclusion", "to sum up", "in short",
    "all in all", "what is more", "as far as", "in my opinion", "in a word",
)

# 从句/非谓语标记（语言维度证据）
_CLAUSE_MARKERS = {
    "although", "though", "because", "since", "while", "whereas", "unless",
    "if", "once", "when", "whenever", "where", "until", "which", "who",
    "whom", "whose", "before", "after",
}

# 常见拼写错误 → 正确写法（规则兜底的语言维度证据）
_MISSPELLINGS = {
    "becuase": "because",
    "recieve": "receive",
    "alot": "a lot",
    "dont": "don't",
    "cant": "can't",
    "wont": "won't",
    "isnt": "isn't",
    "arent": "aren't",
    "didnt": "didn't",
    "thier": "their",
    "teh": "the",
    "adn": "and",
    "definately": "definitely",
    "seperate": "separate",
    "occured": "occurred",
    "enviroment": "environment",
    "goverment": "government",
    "knowlege": "knowledge",
    "opinon": "opinion",
    "sucess": "success",
    "benifit": "benefit",
}

# 常见模板句（ai_traces 证据）
_TEMPLATE_PHRASES = (
    "with the development of", "with the rapid development of",
    "every coin has two sides", "last but not least",
    "as far as i am concerned", "it is widely believed that",
    "in conclusion", "all in all", "no pain no gain",
    "where there is a will there is a way",
)

# 常见中式英语 → 建议表达（翻译用词维度证据）
_CHINGLISH_MAP = {
    "according to me": "In my opinion",
    "learn knowledge": "gain / acquire knowledge",
    "open the light": "turn on the light",
    "close the light": "turn off the light",
    "in nowadays": "nowadays",
    "very like": "like ... very much",
    "more and more people": "a growing number of people",
    "with the development of society": "as society develops",
}

# 停止词：统计"重复用词"时排除
_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "have", "has", "had",
    "are", "was", "were", "will", "would", "can", "could", "should",
    "not", "but", "you", "your", "they", "their", "them", "our", "ours",
    "from", "about", "into", "than", "then", "when", "what", "which",
    "who", "whom", "whose", "there", "here", "also", "very", "more",
    "most", "some", "such", "only", "just", "because", "while", "however",
    "therefore", "although", "though", "still", "even", "much", "many",
}

# 翻译问题的类型映射：LLM 可能输出中文/近义英文，统一成前端约定值
_ISSUE_TYPE_ALIASES = {
    "omission": "omission", "missing": "omission", "untranslated": "omission",
    "漏译": "omission", "缺失": "omission", "少译": "omission",
    "mistranslation": "mistranslation", "wrong_translation": "mistranslation",
    "错译": "mistranslation", "误译": "mistranslation", "曲解": "mistranslation",
    "grammar": "grammar", "syntax": "grammar", "语法": "grammar",
    "tense": "grammar", "article": "grammar", "preposition": "grammar",
    "word_choice": "word_choice", "word choice": "word_choice",
    "vocabulary": "word_choice", "diction": "word_choice",
    "用词": "word_choice", "词汇": "word_choice", "选词": "word_choice",
    "spelling": "spelling", "拼写": "spelling", "capitalization": "grammar",
    "structure": "structure", "coherence": "structure", "cohesion": "structure",
    "logic": "structure", "organization": "structure", "transition": "structure",
    "结构": "structure", "衔接": "structure", "连贯": "structure",
    "content": "content", "内容": "content", "切题": "content",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# SQLite / 日志里避免直接写三反引号，用常量拼接，便于 grep
_FENCE = "\x60" * 3

_WRITING_JSON_SCHEMA = """{
  "content_score": 0-5 的数字（内容：切题、论据充分、观点明确）,
  "language_score": 0-5 的数字（语言：语法、词汇、句式多样性）,
  "organization_score": 0-5 的数字（组织：段落结构、衔接连贯、首尾完整）,
  "score": 上述三个维度之和（0-15，必须等于三者相加）,
  "feedback": "150 字内的总评，必须说明得分依据，不要写“请自行对照参考”",
  "strengths": ["具体优点，2-4 条"],
  "improvements": ["具体可执行的改进建议，2-4 条"],
  "sentence_notes": [
    {"index": 1, "sentence": "原文原句（可截断）", "issue": "具体问题", "type": "grammar|word_choice|structure|spelling|content", "suggestion": "改写建议"}
  ],
  "ai_traces": ["检测到的模板套句/疑似 AI 生成痕迹，没有就给空数组"]
}"""

_TRANSLATION_JSON_SCHEMA = """{
  "accuracy_score": 0-7.5 的数字（准确度：信息完整、无漏译/错译）,
  "language_score": 0-7.5 的数字（语言：语法正确、用词地道、表达流畅）,
  "dimension_scale": 7.5,
  "score": 两维度之和（0-15，必须等于两者相加）,
  "feedback": "150 字内总评，说明扣分依据",
  "issues": [
    {"segment": "对应的源文片段或译文片段", "type": "omission|mistranslation|grammar|word_choice", "detail": "具体问题", "suggestion": "修改建议"}
  ],
  "improved_translation": "你的润色改进版译文（完整即可，不必与参考译文完全一致）",
  "reference_translation": "可接受的参考译文"
}"""


# ==================== 基础工具 ====================

def _clamp(value: float, low: float, high: float) -> float:
    """数值截断到 [low, high]"""
    return max(low, min(high, value))


def _score_710(score: float) -> float:
    """15 分制原始分 → 106.5 分制（写作 / 翻译各占试卷 15%）"""
    return round(_clamp(float(score), 0.0, MAX_SCORE) / MAX_SCORE * SCORE_710_FULL, 1)


def _level_band(score: float) -> str:
    """15 分制档位"""
    score = _clamp(float(score), 0.0, MAX_SCORE)
    for threshold, name in LEVEL_BANDS:
        if score >= threshold:
            return name
    return "0分档"


def _require_text(value: Any, field: str) -> str:
    """必填文本校验：非空、字符串、不超长"""
    if value is None:
        raise ValueError(f"{field} 不能为空")
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(
            f"{field} 超过 {MAX_TEXT_LEN} 字符上限（当前 {len(text)} 字符）")
    return text


def _normalize_level(level: Any) -> str:
    """级别归一化：仅支持 CET4 / CET6"""
    text = str(level or "CET4").strip().upper().replace("-", "")
    if text not in DEFAULT_WORD_LIMIT:
        raise ValueError("level 仅支持 CET4 / CET6")
    return text


def _normalize_word_limit(word_limit: Any, level: str) -> int:
    """字数要求校验，缺省按级别默认值"""
    if word_limit is None:
        return DEFAULT_WORD_LIMIT[level]
    try:
        limit = int(word_limit)
    except (TypeError, ValueError):
        raise ValueError("word_limit 必须是整数")
    if not 20 <= limit <= 1000:
        raise ValueError("word_limit 需在 20-1000 之间")
    return limit


def _normalize_kind(kind: Any) -> str:
    """kind 归一化：writing / translation"""
    text = str(kind or "").strip().lower()
    if text in ("writing", "essay", "写作"):
        return "writing"
    if text in ("translation", "translate", "翻译"):
        return "translation"
    raise ValueError("kind 仅支持 writing / translation")


def _resolve_kinds(kind: Any) -> list[str]:
    """list_grades 的 kind 解析：None / all / both 表示两种都查"""
    if kind is None or str(kind).strip().lower() in ("", "all", "both"):
        return ["writing", "translation"]
    return [_normalize_kind(kind)]


def _normalize_limit(limit: Any, default: int, maximum: int) -> int:
    """条数校验 + 截断"""
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数")
    return min(max(1, value), maximum)


def _english_words(text: str) -> list[str]:
    """抽取英文单词（含连字符/撇号）"""
    return re.findall(r"[A-Za-z][A-Za-z'-]*", text or "")


def _split_sentences(text: str) -> list[str]:
    """英文句子切分：按 .!? 后空白或换行切"""
    parts = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    return [p.strip() for p in parts if p and p.strip()]


def _split_source_segments(text: str) -> list[str]:
    """中文源文切分：按句末标点或换行切"""
    parts = re.split(r"(?<=[。！？；!?;])\s*|\n+", (text or "").strip())
    return [p.strip() for p in parts if p and p.strip()]


def _paragraphs(text: str) -> list[str]:
    """段落切分：按非空行切（四六级作文通常一行一段）"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _json_load(value: Any, default: Any) -> Any:
    """数据库里的 JSON 字段解析，脏数据不抛异常"""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _str_list(raw: Any, limit: int = 8) -> list[str]:
    """把任意结构收敛成字符串列表"""
    if isinstance(raw, str):
        items = [raw.strip()] if raw.strip() else []
    elif isinstance(raw, (list, tuple)):
        items = []
        for item in raw:
            if isinstance(item, (str, int, float)) and str(item).strip():
                items.append(str(item).strip())
    else:
        items = []
    return items[:limit]


def _first_number(payload: dict, *keys: str) -> float | None:
    """从 payload 里按候选键取第一个数字（兼容字符串数字与嵌套 score）"""
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            continue
        number = None
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                number = None
        elif isinstance(value, dict) and "score" in value:
            nested = value["score"]
            try:
                number = float(nested)
            except (TypeError, ValueError):
                number = None
        if number is not None:
            return number
    return None


def _normalize_issue_type(raw: Any) -> str:
    """问题类型归一化（至少覆盖 omission/mistranslation/grammar/word_choice）"""
    key = re.sub(r"\s+", " ", str(raw or "").strip().lower()).replace("-", "_")
    return _ISSUE_TYPE_ALIASES.get(key, key or "other")


def _normalize_sentence_notes(raw: Any) -> list[dict]:
    """逐句批注结构化（写作）"""
    notes: list[dict] = []
    if not isinstance(raw, list):
        return notes
    for pos, item in enumerate(raw):
        if isinstance(item, str):
            text = item.strip()
            if text:
                notes.append({
                    "index": pos + 1, "sentence": "", "issue": text,
                    "type": "other", "suggestion": "",
                })
            continue
        if not isinstance(item, dict):
            continue
        sentence = str(item.get("sentence") or item.get("text") or "").strip()
        issue = str(item.get("issue") or item.get("problem") or "").strip()
        suggestion = str(item.get("suggestion") or item.get("advice") or "").strip()
        if not (sentence or issue or suggestion):
            continue
        try:
            index = int(item.get("index", pos + 1))
        except (TypeError, ValueError):
            index = pos + 1
        notes.append({
            "index": index,
            "sentence": sentence[:300],
            "issue": issue,
            "type": _normalize_issue_type(item.get("type")),
            "suggestion": suggestion,
        })
    return notes[:20]


def _normalize_issues(raw: Any) -> list[dict]:
    """逐条问题结构化（翻译）"""
    issues: list[dict] = []
    if not isinstance(raw, list):
        return issues
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                issues.append({
                    "segment": "", "type": "other", "detail": text,
                    "suggestion": "",
                })
            continue
        if not isinstance(item, dict):
            continue
        segment = str(
            item.get("segment") or item.get("sentence")
            or item.get("source") or item.get("text") or "").strip()
        detail = str(
            item.get("detail") or item.get("issue") or item.get("problem")
            or item.get("description") or "").strip()
        suggestion = str(
            item.get("suggestion") or item.get("advice")
            or item.get("revised") or "").strip()
        if not (segment or detail or suggestion):
            continue
        issues.append({
            "segment": segment[:300],
            "type": _normalize_issue_type(item.get("type")),
            "detail": detail,
            "suggestion": suggestion,
        })
    return issues[:20]


# ==================== JSON 解析与脏数据修复 ====================

class _LLMOutputError(Exception):
    """LLM 输出不可用（解析失败 / 结构不合法），内部触发修复重试或规则兜底"""


def _strip_code_fence(text: str) -> str:
    """去除 markdown 代码块围栏"""
    match = re.match(
        r"^\s*" + _FENCE + r"(?:json|JSON)?\s*\n?(.*?)\n?\s*" + _FENCE
        + r"\s*$", text, re.S)
    if match:
        return match.group(1).strip()
    # 只有开头围栏没有结尾时，也尝试去掉
    if text.lstrip().startswith(_FENCE):
        text = re.sub(r"^\s*" + _FENCE + r"(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*" + _FENCE + r"\s*$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """截取第一个 { 到最后一个 } 之间的内容（模型常带前后解释文字）"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start:end + 1]


def _loads_json(raw: str) -> tuple[dict, list[str]]:
    """宽松 JSON 解析，返回 (payload, 修复记录)"""
    if raw is None or not str(raw).strip():
        raise _LLMOutputError("模型返回为空")

    repairs: list[str] = []
    text = str(raw).strip()

    unfenced = _strip_code_fence(text)
    if unfenced != text:
        repairs.append("去除 markdown 代码块围栏")
        text = unfenced

    extracted = _extract_json_object(text)
    if extracted != text:
        repairs.append("截取 JSON 主体（模型输出夹带解释文字）")
        text = extracted

    try:
        payload = json.loads(text)
    except Exception:
        fixed = text
        fixed_no_trailing = re.sub(r",\s*([}\]])", r"\1", fixed)
        if fixed_no_trailing != fixed:
            repairs.append("删除 JSON 尾随逗号")
            fixed = fixed_no_trailing
        fixed_quotes = (fixed.replace("“", '"').replace("”", '"')
                        .replace("‘", "'").replace("’", "'"))
        if fixed_quotes != fixed:
            repairs.append("修正全角引号")
            fixed = fixed_quotes
        try:
            payload = json.loads(fixed)
        except Exception as exc:
            raise _LLMOutputError(f"JSON 解析失败：{exc}") from exc

    if not isinstance(payload, dict):
        raise _LLMOutputError("JSON 顶层不是对象")
    return payload, repairs


def _normalize_writing_payload(payload: dict) -> tuple[dict, list[str]]:
    """服务端复核写作 LLM 输出：维度分范围、总分一致性、结构完整性"""
    warnings: list[str] = []
    dimensions: dict[str, float] = {}
    alias_map = {
        "content": ("content_score", "content", "content_points"),
        "language": ("language_score", "language", "language_points"),
        "organization": ("organization_score", "organization",
                         "structure_score", "organization_points"),
    }
    for name, aliases in alias_map.items():
        raw = _first_number(payload, *aliases)
        if raw is None:
            raise _LLMOutputError(f"缺少维度分：{name}")
        value = _clamp(raw, 0.0, 5.0)
        if abs(value - raw) > 1e-9:
            warnings.append(
                f"维度 {name} 原始分 {raw:g} 超出 0-5，已截断为 {value:g}")
        dimensions[name] = value

    total = round(sum(dimensions.values()), 1)
    llm_score = _first_number(payload, "score", "total_score", "total")
    if llm_score is None:
        warnings.append("LLM 未给出总分，已按三维度之和计算")
    elif abs(llm_score - total) > 0.05:
        warnings.append(
            f"LLM 总分 {llm_score:g} 与维度分之和 {total:g} 不一致，"
            f"已以维度分为准")

    feedback = str(payload.get("feedback") or payload.get("comment")
                   or payload.get("comments") or "").strip()
    if len(feedback) < 8:
        warnings.append("LLM 评语过短/缺失，已按维度分自动生成得分依据")
        feedback = (
            f"各维度得分：内容 {dimensions['content']:g}/5、"
            f"语言 {dimensions['language']:g}/5、"
            f"组织 {dimensions['organization']:g}/5。")

    strengths = _str_list(payload.get("strengths") or payload.get("advantages"))
    improvements = _str_list(
        payload.get("improvements") or payload.get("suggestions")
        or payload.get("improvement"))
    sentence_notes = _normalize_sentence_notes(
        payload.get("sentence_notes") or payload.get("sentence_comments")
        or payload.get("annotations") or payload.get("notes"))
    ai_traces = _str_list(
        payload.get("ai_traces") or payload.get("traces")
        or payload.get("template_traces"))

    return {
        "score": round(_clamp(total, 0.0, MAX_SCORE), 1),
        "dimensions": dimensions,
        "feedback": feedback,
        "strengths": strengths,
        "improvements": improvements,
        "sentence_notes": sentence_notes,
        "ai_traces": ai_traces,
    }, warnings


def _normalize_translation_payload(payload: dict,
                                   reference_translation: str | None
                                   ) -> tuple[dict, list[str]]:
    """服务端复核翻译 LLM 输出：维度分范围/量纲、总分一致性、结构完整性"""
    warnings: list[str] = []
    accuracy_raw = _first_number(payload, "accuracy_score", "accuracy")
    language_raw = _first_number(payload, "language_score", "language")
    if accuracy_raw is None:
        raise _LLMOutputError("缺少维度分：accuracy")
    if language_raw is None:
        raise _LLMOutputError("缺少维度分：language")

    # 兼容模型用 0-5 制给维度分的情况：按 dimension_scale 折算到 0-7.5
    scale = _first_number(payload, "dimension_scale", "scale", "dim_max")
    factor = 1.0
    if scale is not None and 0 < scale < TRANSLATION_DIM_MAX:
        factor = TRANSLATION_DIM_MAX / scale
        warnings.append(
            f"检测到 {scale:g} 分制维度分，已折算到 0-7.5 制（×{factor:g}）")
    accuracy = _clamp(accuracy_raw * factor, 0.0, TRANSLATION_DIM_MAX)
    language = _clamp(language_raw * factor, 0.0, TRANSLATION_DIM_MAX)
    for name, raw, value in (
        ("accuracy", accuracy_raw * factor, accuracy),
        ("language", language_raw * factor, language),
    ):
        if abs(value - raw) > 1e-9:
            warnings.append(
                f"维度 {name} 原始分 {raw:g} 超出 0-7.5，已截断为 {value:g}")

    total = round(accuracy + language, 1)
    llm_score = _first_number(payload, "score", "total_score", "total")
    if llm_score is None:
        warnings.append("LLM 未给出总分，已按两维度之和计算")
    elif abs(llm_score - total) > 0.05:
        warnings.append(
            f"LLM 总分 {llm_score:g} 与维度分之和 {total:g} 不一致，"
            f"已以维度分为准")

    feedback = str(payload.get("feedback") or payload.get("comment")
                   or payload.get("comments") or "").strip()
    if len(feedback) < 8:
        warnings.append("LLM 评语过短/缺失，已按维度分自动生成得分依据")
        feedback = f"准确度 {accuracy:g}/7.5、语言 {language:g}/7.5。"

    issues = _normalize_issues(
        payload.get("issues") or payload.get("problems")
        or payload.get("errors"))
    improved = str(payload.get("improved_translation")
                   or payload.get("improved")
                   or payload.get("revised_translation") or "").strip()
    llm_reference = str(payload.get("reference_translation")
                        or payload.get("reference") or "").strip()

    return {
        "score": round(_clamp(total, 0.0, MAX_SCORE), 1),
        "dimensions": {"accuracy": accuracy, "language": language},
        "feedback": feedback,
        "issues": issues,
        "improved_translation": improved,
        "reference_translation": reference_translation or llm_reference,
    }, warnings


# ==================== LLM 调用（含修复重试） ====================

def _llm_identity() -> tuple[str, str]:
    """记录当前实际使用的 provider / model（成本核算与复核用）"""
    try:
        configured = [p for p in getattr(llm_client, "providers", [])
                      if p.get("api_key")]
    except Exception:
        configured = []
    if configured:
        first = configured[0]
        return str(first.get("name", "unknown")), str(first.get("model", ""))
    return "unconfigured", "unconfigured"


def _llm_meta(*, fallback: bool, attempts: int, duration_ms: int,
              warnings: list[str], errors: list[str]) -> dict:
    """统一的 model_meta，落库后可复核、可成本核算"""
    provider, model = _llm_identity()
    if fallback:
        provider, model = "rule_engine", "rule_engine"
    return {
        "provider": provider,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "fallback": fallback,
        "scored_by": "rules" if fallback else "llm",
        "attempts": attempts,
        "duration_ms": duration_ms,
        "temperature": LLM_TEMPERATURE,
        "warnings": warnings,
        "error": errors[-1] if errors else None,
        "error_history": errors[:3],
        "graded_at": time.time(),
    }


async def _call_llm_json(user_prompt: str, normalizer, schema_hint: str,
                         errors: list[str], warnings: list[str]
                         ) -> tuple[dict | None, int, int]:
    """调用 LLM 并解析 JSON。

    流程：首次输出 → 解析失败则把错误回灌给模型做一次"修复重试" →
    仍失败返回 None（由调用方降级规则评分）。LLM 调用本身抛错时直接返回 None，
    因为 providers 内部已做过故障转移，重试无意义。
    """
    system_prompt = (
        "你是资深的大学英语四六级（CET）阅卷老师，按官方评分标准给分。"
        "只输出严格合法的 JSON，不要解释文字，不要 markdown 代码块。")
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    started = time.perf_counter()
    attempts = 0
    while attempts < MAX_LLM_ATTEMPTS:
        attempts += 1
        try:
            raw = await llm_client.chat(
                messages, temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS)
        except Exception as exc:  # 无 key / 超时 / 全部 provider 失败
            errors.append(f"LLM 调用失败：{exc}")
            break

        try:
            payload, repairs = _loads_json(raw)
            if repairs:
                warnings.append("JSON 修复：" + "；".join(repairs))
            normalized, extra = normalizer(payload)
            warnings.extend(extra)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return normalized, attempts, duration_ms
        except _LLMOutputError as exc:
            errors.append(f"第 {attempts} 次输出解析失败：{exc}")
            if attempts >= MAX_LLM_ATTEMPTS:
                break
            # 修复重试：把上一次的错误输出与失败原因回灌，要求只输出 JSON
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": str(raw)[:1500]},
                {"role": "user", "content": (
                    "上面的输出不可用（原因：" + str(exc) + "）。"
                    "请严格只输出一个合法 JSON 对象，不要任何解释、"
                    "不要 markdown 代码块，字段与要求如下：\n" + schema_hint)},
            ]
        except Exception as exc:  # 归一化阶段的非预期异常也按输出不可用处理
            errors.append(f"第 {attempts} 次输出结构不合法：{exc}")
            if attempts >= MAX_LLM_ATTEMPTS:
                break

    duration_ms = int((time.perf_counter() - started) * 1000)
    return None, attempts, duration_ms


def _writing_user_prompt(topic: str, essay: str, level: str,
                         word_limit: int, word_count: int) -> str:
    return (
        "请按四六级官方写作评分标准为下面这篇作文打分。\n"
        f"级别：{level}；要求字数：{word_limit} 词；实际字数：{word_count} 词。\n\n"
        f"题目要求：\n{topic}\n\n"
        f"学生作文：\n{essay}\n\n"
        "评分要求：\n"
        "1. content/language/organization 三个维度各 0-5 分，总分等于三维度之和；\n"
        "2. sentence_notes 至少 3 条（全文不足 3 句则逐句给出），必须引用原句、"
        "指出具体问题并给出改写建议；\n"
        "3. 不要写“请对照参考答案自查”这类没有依据的评语。\n"
        + _WRITING_JSON_SCHEMA
    )


def _translation_user_prompt(source_text: str, user_translation: str,
                             level: str,
                             reference_translation: str | None) -> str:
    parts = [
        "请按四六级官方翻译评分标准批改下面的汉译英译文。\n",
        f"级别：{level}\n\n",
        f"中文原文：\n{source_text}\n\n",
        f"学生译文：\n{user_translation}\n\n",
    ]
    if reference_translation:
        parts.append(
            "参考译文（仅供你判断信息完整性，不要照抄评分）：\n"
            f"{reference_translation}\n\n")
    parts.append(
        "评分要求：\n"
        "1. accuracy（准确度：信息完整、无漏译/错译）与 language"
        "（语言：语法、用词、流畅度）各 0-7.5 分，总分等于两者之和；\n"
        "2. issues 必须逐条指出问题，type 取值范围："
        "omission（漏译）/ mistranslation（错译）/ grammar（语法）/ "
        "word_choice（用词），并给出 segment、detail、suggestion；\n"
        "3. 给出 improved_translation 与 reference_translation；\n"
        "4. 不要写“请对照参考答案自查”这类没有依据的评语。\n"
        + _TRANSLATION_JSON_SCHEMA
    )
    return "".join(parts)


# ==================== 规则兜底评分：写作 ====================

def _is_complex_sentence(sentence: str) -> bool:
    """判断句子是否含从句/非谓语等复杂结构（语言维度证据）"""
    tokens = {w.lower() for w in _english_words(sentence)}
    if tokens & _CLAUSE_MARKERS:
        return True
    if re.search(r"\b(to\s+[a-z]+|in\s+order\s+to|not\s+only|rather\s+than)\b",
                 sentence, re.I):
        return True
    # 逗号后接现在分词：..., facing / considering / given
    if re.search(r",\s*[A-Za-z]{4,}ing\b", sentence):
        return True
    return False


def _count_connectives(text: str) -> list[str]:
    """统计实际使用的衔接词（组织维度证据）"""
    lowered = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    return [word for word in _CONNECTIVES if word in lowered]


def _find_misspellings(text: str) -> list[tuple[str, str]]:
    """常见拼写错误 + 第一人称 i 未大写"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in _english_words(text):
        low = token.lower()
        if low in _MISSPELLINGS:
            if low not in seen:
                seen.add(low)
                found.append((token, _MISSPELLINGS[low]))
    if re.search(r"(?<![A-Za-z])i(?![A-Za-z'])", text or ""):
        found.append(("i", "I"))
    return found


def _consecutive_duplicates(words: list[str]) -> list[str]:
    """连续重复用词"""
    dups = []
    for prev, cur in zip(words, words[1:]):
        if prev.lower() == cur.lower() and len(cur) > 1:
            dups.append(cur)
    return dups


def _overused_words(words: list[str], min_count: int = 4) -> list[str]:
    """高频重复实词（占比过高说明用词贫乏）"""
    if not words:
        return []
    counter = Counter(w.lower() for w in words
                      if len(w) > 3 and w.lower() not in _STOPWORDS)
    threshold = max(min_count, int(len(words) * 0.06))
    return [word for word, count in counter.most_common(3)
            if count >= threshold]


def _paragraphs_balanced(paragraphs: list[str]) -> bool:
    """段落长度是否相对均衡"""
    lengths = [len(_english_words(p)) for p in paragraphs]
    if len(lengths) <= 1:
        return True
    return max(lengths) <= max(10, 3 * max(1, min(lengths)))


def _has_conclusion(paragraphs: list[str], sentences: list[str]) -> bool:
    """结尾是否有总结性表达"""
    last_text = ""
    if paragraphs:
        last_text = paragraphs[-1]
    elif sentences:
        last_text = sentences[-1]
    markers = ("in conclusion", "to sum up", "in short", "all in all",
               "to conclude", "in a word", "therefore", "as a result")
    return any(marker in last_text.lower() for marker in markers)


def _detect_template_phrases(text: str) -> list[str]:
    """模板套句与重复句式（ai_traces 证据）"""
    lowered = (text or "").lower()
    traces = [f"检测到模板句：{phrase}" for phrase in _TEMPLATE_PHRASES
              if phrase in lowered]
    sentences = _split_sentences(text)
    openings = []
    for sentence in sentences:
        words = _english_words(sentence)
        if words:
            openings.append(words[0].lower())
    if openings:
        counter = Counter(openings)
        word, count = counter.most_common(1)[0]
        if count >= 3:
            traces.append(f"{count} 个句子以相同词开头（{word}），句式较模板化")
    return traces[:5]


_SENTENCE_NOTE_SUGGESTIONS = {
    "structure": "拆成两个短句，或用从句/非谓语把逻辑关系写清楚",
    "grammar": "按语法规则改写该句（时态、主谓一致、句首大写、句末标点）",
    "spelling": "改正拼写或大小写错误",
    "word_choice": "换成更准确的词，避免重复使用同一表达",
    "content": "补充论据或例证，让观点更具体",
    "other": "结合评语重写该句",
}


def _rule_sentence_notes(essay: str) -> list[dict]:
    """规则版逐句批注：没有 LLM 时也能把扣分落到具体句子"""
    notes: list[dict] = []
    for index, sentence in enumerate(_split_sentences(essay), start=1):
        text = sentence.strip()
        if not text:
            continue
        words = _english_words(text)
        problems: list[tuple[str, str]] = []
        if len(words) > 35:
            problems.append(("structure", f"句子过长（{len(words)} 词），可读性差"))
        if text[0].isalpha() and not text[0].isupper():
            problems.append(("grammar", "句首字母未大写"))
        if text[-1] not in ".!?\"')":
            problems.append(("grammar", "句末缺少标点"))
        for token, correct in _find_misspellings(text):
            problems.append(("spelling", f"疑似拼写/大小写错误：{token} → {correct}"))
        dups = _consecutive_duplicates(words)
        if dups:
            problems.append(("word_choice", f"连续重复用词：{dups[0]}"))
        for issue_type, issue in problems[:2]:
            notes.append({
                "index": index,
                "sentence": text[:200],
                "issue": issue,
                "type": issue_type,
                "suggestion": _SENTENCE_NOTE_SUGGESTIONS.get(
                    issue_type, "重写该句"),
            })
        if len(notes) >= 8:
            break
    return notes


def _rule_grade_writing(topic: str, essay: str, word_limit: int,
                        reason: str) -> dict:
    """规则兜底写作评分。

    把"字数达标度 / 切题度 / 首尾段 / 复杂句式 / 平均句长 / 拼写 / 重复词 /
    连接词 / 段落均衡 / 总结句"这些可计算的指标映射到官方三维度，
    并逐条写进 feedback，保证分数有据可查。
    """
    words = _english_words(essay)
    word_count = len(words)
    paragraphs = _paragraphs(essay)
    sentences = _split_sentences(essay)
    sentence_lengths = [len(_english_words(s)) for s in sentences] or [0]
    avg_sentence_len = sum(sentence_lengths) / max(1, len(sentence_lengths))
    length_ratio = word_count / max(1, word_limit)

    # ---- 内容维度（0-5）：字数达标 2 + 切题 1.5 + 首尾段 1 + 三段结构 0.5 ----
    length_points = 2.0 * min(1.0, length_ratio)
    topic_words = {w.lower() for w in _english_words(topic) if len(w) > 3}
    essay_words = {w.lower() for w in words}
    topic_hits = topic_words & essay_words
    relevance_points = (1.5 * min(1.0, len(topic_hits) / 3)
                        if topic_words else 0.75)
    structure_points = (0.5 if paragraphs else 0.0)
    structure_points += 0.5 if len(paragraphs) >= 2 else 0.0
    paragraph_bonus = 0.5 if len(paragraphs) >= 3 else 0.0
    content = _clamp(length_points + relevance_points + structure_points
                     + paragraph_bonus, 0.0, 5.0)

    # ---- 语言维度（0-5）：复杂句式 1.5 + 句长 1 + 拼写 1.5 + 用词重复 0.5 + 基础 0.5 ----
    complex_sentences = sum(1 for s in sentences if _is_complex_sentence(s))
    complexity_ratio = complex_sentences / max(1, len(sentences))
    complexity_points = 1.5 * min(1.0, complexity_ratio / 0.5)
    if 12 <= avg_sentence_len <= 25:
        sentence_length_points = 1.0
    elif 8 <= avg_sentence_len < 12 or 25 < avg_sentence_len <= 35:
        sentence_length_points = 0.6
    else:
        sentence_length_points = 0.3
    misspellings = _find_misspellings(essay)
    spelling_points = max(0.0, 1.5 - 0.3 * len(misspellings))
    duplicates = _consecutive_duplicates(words)
    overused = _overused_words(words)
    repetition_points = (0.5 if not duplicates and not overused
                         else 0.25 if len(duplicates) + len(overused) <= 2
                         else 0.0)
    language = _clamp(0.5 + complexity_points + sentence_length_points
                      + spelling_points + repetition_points, 0.0, 5.0)

    # ---- 组织维度（0-5）：段落结构 2 + 连接词 1.5 + 段落均衡 1 + 总结句 0.5 ----
    connectives = _count_connectives(essay)
    paragraph_count = len(paragraphs)
    if paragraph_count >= 3:
        paragraph_points = 2.0
    elif paragraph_count == 2:
        paragraph_points = 1.2
    else:
        paragraph_points = 0.4
    connective_points = 1.5 * min(1.0, len(connectives) / 4)
    balance_points = 1.0 if _paragraphs_balanced(paragraphs) else 0.5
    closing_points = 0.5 if _has_conclusion(paragraphs, sentences) else 0.0
    organization = _clamp(paragraph_points + connective_points + balance_points
                          + closing_points, 0.0, 5.0)

    content = round(content, 1)
    language = round(language, 1)
    organization = round(organization, 1)
    total = round(_clamp(content + language + organization, 0.0, MAX_SCORE), 1)

    # ---- 可解释依据 ----
    feedback = (
        f"规则兜底评分（LLM 不可用：{reason}）。"
        f"字数 {word_count}/{word_limit} 词（达标度 {length_ratio:.0%}），"
        f"{paragraph_count} 段，平均句长 {avg_sentence_len:.1f} 词，"
        f"含从句/非谓语的句子 {complex_sentences}/{len(sentences)} 句，"
        f"衔接词 {len(connectives)} 处，拼写/大小写异常 {len(misspellings)} 处，"
        f"连续重复用词 {len(duplicates)} 处。"
        f"据此折算：内容 {content}/5、语言 {language}/5、组织 {organization}/5，"
        f"总分 {total}/15（{_level_band(total)}）。"
    )

    strengths: list[str] = []
    if length_ratio >= 0.95:
        strengths.append(f"字数达标（{word_count}/{word_limit} 词），篇幅完整")
    if paragraph_count >= 3:
        strengths.append(f"采用 {paragraph_count} 段结构，层次清晰")
    if complexity_ratio >= 0.3:
        strengths.append(
            f"{complex_sentences} 句使用从句或非谓语结构，句式有一定变化")
    if len(connectives) >= 3:
        strengths.append(
            f"使用了 {len(connectives)} 处衔接表达（"
            + "、".join(connectives[:3]) + "）")
    if not misspellings:
        strengths.append("未检测到常见拼写/大小写错误")
    if closing_points:
        strengths.append("结尾段有总结性表达，收束完整")

    improvements: list[str] = []
    if length_ratio < 0.95:
        improvements.append(
            f"字数不足：目前 {word_count} 词，建议写到 {word_limit} 词左右，"
            "补充论据或例证")
    if length_ratio > 1.6:
        improvements.append("篇幅明显超出要求，注意删减重复表达、精炼句子")
    if paragraph_count < 3:
        improvements.append("段落偏少，建议采用三段式：引入观点—论证—总结")
    if complexity_ratio < 0.3:
        improvements.append(
            "句式偏单一，建议加入定语从句、状语从句、非谓语等复杂结构")
    if len(connectives) < 3:
        improvements.append(
            "衔接词不足，建议加入 however / therefore / in addition 等显性衔接")
    if misspellings:
        samples = "、".join(f"{bad}→{good}" for bad, good in misspellings[:3])
        improvements.append(f"存在拼写/大小写问题：{samples}")
    if duplicates:
        improvements.append(f"存在连续重复用词：{duplicates[0]}，注意替换同义表达")
    if not closing_points:
        improvements.append("结尾缺少总结句，可加 In conclusion / To sum up")
    if not improvements:
        improvements.append("整体结构完整，可继续打磨用词准确性与句式多样性")

    return {
        "score": total,
        "dimensions": {
            "content": content,
            "language": language,
            "organization": organization,
        },
        "feedback": feedback,
        "strengths": strengths,
        "improvements": improvements,
        "sentence_notes": _rule_sentence_notes(essay),
        "ai_traces": _detect_template_phrases(essay),
    }


# ==================== 规则兜底评分：翻译 ====================

def _surface_fix_translation(text: str) -> str:
    """只修表面规范（句首大写、句末标点），用于没有参考译文时的兜底改进版"""
    fixed = []
    for segment in _split_sentences(text):
        sentence = segment.strip()
        if not sentence:
            continue
        if sentence[0].isalpha():
            sentence = sentence[0].upper() + sentence[1:]
        if sentence[-1] not in ".!?":
            sentence += "."
        fixed.append(sentence)
    return " ".join(fixed)


def _rule_grade_translation(source_text: str, user_translation: str,
                            reference_translation: str | None,
                            reason: str) -> dict:
    """规则兜底翻译评分。

    依据：源文/译文句数对齐（漏译）、长度比、中文残留、句首大写/句末标点、
    重复用词、常见中式表达、与参考译文的句数对齐。
    """
    source_segments = _split_source_segments(source_text)
    trans_segments = _split_sentences(user_translation)
    issues: list[dict] = []

    # ---- 漏译：句数对齐（有参考译文时取更长的作基准） ----
    ref_segments = (_split_sentences(reference_translation)
                    if reference_translation else [])
    expected_count = max(len(source_segments), len(ref_segments))
    missing_count = max(0, expected_count - len(trans_segments))
    if missing_count:
        pool = (source_segments if len(source_segments) >= len(ref_segments)
                else ref_segments)
        for segment in pool[len(trans_segments):][:5]:
            issues.append({
                "segment": segment[:200],
                "type": "omission",
                "detail": "该句在译文中没有对应内容（疑似漏译）",
                "suggestion": "补译该句，确保原文信息完整",
                "source": "rules",
            })

    # ---- 中文残留：说明这部分根本没翻译 ----
    residue_count = 0
    for segment in trans_segments:
        if _CJK_RE.search(segment):
            residue_count += 1
            issues.append({
                "segment": segment[:150],
                "type": "omission",
                "detail": "译文中残留中文，该部分实际未完成翻译",
                "suggestion": "把残留中文改写为英文表达",
                "source": "rules",
            })

    # ---- 长度比（英文/中文字符数，正常约 2-8 倍） ----
    source_len = len(re.sub(r"\s+", "", source_text))
    trans_len = len(re.sub(r"\s+", "", user_translation))
    length_ratio = trans_len / max(1, source_len)
    if length_ratio < 1.0:
        length_penalty = 1.5
        issues.append({
            "segment": user_translation[:150],
            "type": "omission",
            "detail": f"译文长度比仅 {length_ratio:.2f}（正常约 2-8），信息明显缺失",
            "suggestion": "对照原文逐句补全信息，不要只译大意",
            "source": "rules",
        })
    elif length_ratio < 1.8:
        length_penalty = 0.8
        issues.append({
            "segment": user_translation[:150],
            "type": "omission",
            "detail": f"译文长度比 {length_ratio:.2f} 偏低，可能概括过度",
            "suggestion": "补充修饰成分与细节，忠实还原原文信息",
            "source": "rules",
        })
    elif length_ratio > 10.0:
        length_penalty = 0.5
        issues.append({
            "segment": user_translation[:150],
            "type": "word_choice",
            "detail": f"译文长度比 {length_ratio:.2f} 偏高，表达可能啰嗦",
            "suggestion": "删减冗余表达，用更简洁的英文句式",
            "source": "rules",
        })
    else:
        length_penalty = 0.0

    # ---- 句子完整性：句首大写 / 句末标点 ----
    grammar_count = 0
    for segment in trans_segments:
        sentence = segment.strip()
        if not sentence:
            continue
        if sentence[0].isalpha() and not sentence[0].isupper():
            grammar_count += 1
            issues.append({
                "segment": sentence[:150],
                "type": "grammar",
                "detail": "句首字母未大写",
                "suggestion": "英文句子首字母应大写",
                "source": "rules",
            })
        if sentence[-1] not in ".!?\"')":
            grammar_count += 1
            issues.append({
                "segment": sentence[:150],
                "type": "grammar",
                "detail": "句末缺少标点",
                "suggestion": "补上句末标点，保证句子完整",
                "source": "rules",
            })

    # ---- 用词：重复词 / 常见中式表达 ----
    trans_words = _english_words(user_translation)
    duplicates = _consecutive_duplicates(trans_words)
    overused = _overused_words(trans_words)
    if duplicates:
        issues.append({
            "segment": duplicates[0],
            "type": "word_choice",
            "detail": f"连续重复用词：{duplicates[0]}",
            "suggestion": "换用同义表达，避免重复",
            "source": "rules",
        })
    if overused:
        issues.append({
            "segment": "、".join(overused[:3]),
            "type": "word_choice",
            "detail": "高频重复实词：" + "、".join(overused[:3]),
            "suggestion": "丰富词汇表达，避免同一实词反复出现",
            "source": "rules",
        })
    chinglish_hits = []
    lowered = (user_translation or "").lower()
    for bad, good in _CHINGLISH_MAP.items():
        if bad in lowered:
            chinglish_hits.append((bad, good))
            issues.append({
                "segment": bad,
                "type": "word_choice",
                "detail": f"中式表达：{bad}",
                "suggestion": f"更地道的说法：{good}",
                "source": "rules",
            })

    # ---- 映射到两维度 ----
    accuracy = 7.5
    accuracy -= min(3.5, missing_count * 1.0)
    accuracy -= min(1.6, residue_count * 0.8)
    accuracy -= length_penalty
    accuracy -= min(1.5, len(chinglish_hits) * 0.75)
    accuracy = round(_clamp(accuracy, 0.0, TRANSLATION_DIM_MAX), 1)

    language = 7.5
    language -= min(2.0, grammar_count * 0.25)
    language -= min(1.0, len(duplicates) * 0.5)
    language -= min(0.8, len(overused) * 0.2)
    language = round(_clamp(language, 0.0, TRANSLATION_DIM_MAX), 1)

    total = round(_clamp(accuracy + language, 0.0, MAX_SCORE), 1)

    feedback = (
        f"规则兜底评分（LLM 不可用：{reason}）。"
        f"源文 {len(source_segments)} 句 / 译文 {len(trans_segments)} 句"
        f"（对齐缺失 {missing_count} 句），长度比 {length_ratio:.2f}，"
        f"中文残留 {residue_count} 处，句首/句末规范问题 {grammar_count} 处，"
        f"重复用词 {len(duplicates) + len(overused)} 处，"
        f"中式表达 {len(chinglish_hits)} 处。"
        f"据此折算：准确度 {accuracy}/7.5、语言 {language}/7.5，"
        f"总分 {total}/15（{_level_band(total)}）。"
    )

    if reference_translation:
        improved = reference_translation
    else:
        improved = _surface_fix_translation(user_translation)

    return {
        "score": total,
        "dimensions": {"accuracy": accuracy, "language": language},
        "feedback": feedback,
        "issues": issues[:20],
        "improved_translation": improved,
        "reference_translation": reference_translation or "",
    }


# ==================== 落库与查询 ====================

_WRITING_INSERT = """
    INSERT INTO writing_grades (
        grade_id, user_id, level, topic, essay, word_count, score, score_710,
        level_band, content_score, language_score, organization_score,
        feedback, strengths, improvements, sentence_notes, ai_traces,
        status, reviewer_note, reviewed_at, model_meta, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_TRANSLATION_INSERT = """
    INSERT INTO translation_grades (
        grade_id, user_id, level, source_text, user_translation,
        reference_translation, score, score_710, level_band,
        accuracy_score, language_score, feedback, issues,
        improved_translation, status, reviewer_note, reviewed_at,
        model_meta, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


async def _insert_writing(record: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_WRITING_INSERT, (
            record["grade_id"], record["user_id"], record["level"],
            record["topic"], record["essay"], record["word_count"],
            record["score"], record["score_710"], record["level_band"],
            record["dimensions"]["content"],
            record["dimensions"]["language"],
            record["dimensions"]["organization"],
            record["feedback"],
            json.dumps(record["strengths"], ensure_ascii=False),
            json.dumps(record["improvements"], ensure_ascii=False),
            json.dumps(record["sentence_notes"], ensure_ascii=False),
            json.dumps(record["ai_traces"], ensure_ascii=False),
            record["status"], record["reviewer_note"], record["reviewed_at"],
            json.dumps(record["model_meta"], ensure_ascii=False),
            record["created_at"],
        ))
        await db.commit()


async def _insert_translation(record: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_TRANSLATION_INSERT, (
            record["grade_id"], record["user_id"], record["level"],
            record["source_text"], record["user_translation"],
            record["reference_translation"], record["score"],
            record["score_710"], record["level_band"],
            record["dimensions"]["accuracy"], record["dimensions"]["language"],
            record["feedback"],
            json.dumps(record["issues"], ensure_ascii=False),
            record["improved_translation"], record["status"],
            record["reviewer_note"], record["reviewed_at"],
            json.dumps(record["model_meta"], ensure_ascii=False),
            record["created_at"],
        ))
        await db.commit()


def _writing_from_row(data: dict) -> dict:
    """数据库行 → 前端约定的统一结构"""
    return {
        "grade_id": data["grade_id"],
        "user_id": data["user_id"],
        "level": data["level"],
        "topic": data["topic"],
        "essay": data["essay"],
        "word_count": data["word_count"],
        "score": data["score"],
        "score_710": data["score_710"],
        "level_band": data["level_band"],
        "dimensions": {
            "content": data["content_score"],
            "language": data["language_score"],
            "organization": data["organization_score"],
        },
        "feedback": data["feedback"],
        "strengths": _json_load(data["strengths"], []),
        "improvements": _json_load(data["improvements"], []),
        "sentence_notes": _json_load(data["sentence_notes"], []),
        "ai_traces": _json_load(data["ai_traces"], []),
        "status": data["status"],
        "model_meta": _json_load(data["model_meta"], {}),
        "created_at": data["created_at"],
        "reviewer_note": data["reviewer_note"],
        "reviewed_at": data["reviewed_at"],
    }


def _translation_from_row(data: dict) -> dict:
    """数据库行 → 前端约定的统一结构"""
    return {
        "grade_id": data["grade_id"],
        "user_id": data["user_id"],
        "level": data["level"],
        "source_text": data["source_text"],
        "user_translation": data["user_translation"],
        "reference_translation": data["reference_translation"],
        "score": data["score"],
        "score_710": data["score_710"],
        "level_band": data["level_band"],
        "dimensions": {
            "accuracy": data["accuracy_score"],
            "language": data["language_score"],
        },
        "feedback": data["feedback"],
        "issues": _json_load(data["issues"], []),
        "improved_translation": data["improved_translation"],
        "status": data["status"],
        "model_meta": _json_load(data["model_meta"], {}),
        "created_at": data["created_at"],
        "reviewer_note": data["reviewer_note"],
        "reviewed_at": data["reviewed_at"],
    }


async def _fetch_row(grade_id: str, kind: str) -> dict | None:
    """按主键取一行，返回列名→值字典"""
    table = _TABLE_OF[kind]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE grade_id = ?", (grade_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
    return dict(zip(columns, row))


def _summary(kind: str, data: dict) -> dict:
    """列表页摘要：不返回全文，避免一次拉几百篇作文"""
    if kind == "writing":
        source = data.get("topic") or data.get("essay") or ""
    else:
        source = data.get("source_text") or ""
    title = " ".join(str(source).split())[:60]
    return {
        "grade_id": data["grade_id"],
        "kind": kind,
        "user_id": data["user_id"],
        "level": data["level"],
        "score": data["score"],
        "score_710": data["score_710"],
        "level_band": data["level_band"],
        "status": data["status"],
        "title": title,
        "created_at": data["created_at"],
    }


# ==================== 对外接口 ====================

async def grade_writing(user_id: str, topic: str, essay: str,
                        level: str = "CET4",
                        word_limit: int | None = None) -> dict:
    """写作批改：LLM 真实判分 + 服务端复核，失败降级规则评分并落库。

    返回结构（前端契约）：
        grade_id / user_id / level / topic / essay / word_count / score /
        score_710 / level_band / dimensions{content,language,organization} /
        feedback / strengths / improvements / sentence_notes / ai_traces /
        status / model_meta / created_at
    """
    uid = _require_text(user_id, "user_id")
    topic_text = _require_text(topic, "topic")
    essay_text = _require_text(essay, "essay")
    level_text = _normalize_level(level)
    limit = _normalize_word_limit(word_limit, level_text)
    word_count = len(_english_words(essay_text))

    errors: list[str] = []
    warnings: list[str] = []
    payload, attempts, duration_ms = await _call_llm_json(
        _writing_user_prompt(topic_text, essay_text, level_text, limit,
                             word_count),
        _normalize_writing_payload,
        _WRITING_JSON_SCHEMA,
        errors, warnings,
    )

    if payload is None:
        reason = errors[-1] if errors else "LLM 不可用"
        graded = _rule_grade_writing(topic_text, essay_text, limit, reason)
        meta = _llm_meta(fallback=True, attempts=attempts,
                         duration_ms=duration_ms, warnings=warnings,
                         errors=errors)
        meta["warnings"].append("LLM 不可用，已降级为规则兜底评分")
    else:
        graded = payload
        meta = _llm_meta(fallback=False, attempts=attempts,
                         duration_ms=duration_ms, warnings=warnings,
                         errors=errors)
        # LLM 没给逐句证据时，用规则批注补上，保证"扣分有据"
        if not graded["sentence_notes"]:
            graded["sentence_notes"] = _rule_sentence_notes(essay_text)
            meta["warnings"].append("LLM 未给出逐句批注，已用规则检测补充")
        if not graded["ai_traces"]:
            graded["ai_traces"] = _detect_template_phrases(essay_text)

    score = round(_clamp(float(graded["score"]), 0.0, MAX_SCORE), 1)
    record = {
        "grade_id": f"wr_{uuid.uuid4().hex[:12]}",
        "user_id": uid,
        "level": level_text,
        "topic": topic_text,
        "essay": essay_text,
        "word_count": word_count,
        "score": score,
        "score_710": _score_710(score),
        "level_band": _level_band(score),
        "dimensions": graded["dimensions"],
        "feedback": graded["feedback"],
        "strengths": graded["strengths"],
        "improvements": graded["improvements"],
        "sentence_notes": graded["sentence_notes"],
        "ai_traces": graded["ai_traces"],
        "status": "pending_review",
        "model_meta": meta,
        "created_at": time.time(),
        "reviewer_note": None,
        "reviewed_at": None,
    }
    await _insert_writing(record)
    return record


async def grade_translation(user_id: str, source_text: str,
                            user_translation: str, level: str = "CET4",
                            reference_translation: str | None = None) -> dict:
    """翻译批改：LLM 真实判分 + 服务端复核，失败降级规则评分并落库。

    返回结构（前端契约）：
        grade_id / user_id / level / source_text / user_translation /
        reference_translation / score / score_710 / level_band /
        dimensions{accuracy,language} / feedback / issues /
        improved_translation / status / model_meta / created_at
    """
    uid = _require_text(user_id, "user_id")
    source = _require_text(source_text, "source_text")
    translated = _require_text(user_translation, "user_translation")
    level_text = _normalize_level(level)
    reference = None
    if reference_translation is not None and str(reference_translation).strip():
        reference = _require_text(reference_translation, "reference_translation")

    errors: list[str] = []
    warnings: list[str] = []
    payload, attempts, duration_ms = await _call_llm_json(
        _translation_user_prompt(source, translated, level_text, reference),
        lambda data: _normalize_translation_payload(data, reference),
        _TRANSLATION_JSON_SCHEMA,
        errors, warnings,
    )

    if payload is None:
        reason = errors[-1] if errors else "LLM 不可用"
        graded = _rule_grade_translation(source, translated, reference, reason)
        meta = _llm_meta(fallback=True, attempts=attempts,
                         duration_ms=duration_ms, warnings=warnings,
                         errors=errors)
        meta["warnings"].append("LLM 不可用，已降级为规则兜底评分")
    else:
        graded = payload
        meta = _llm_meta(fallback=False, attempts=attempts,
                         duration_ms=duration_ms, warnings=warnings,
                         errors=errors)
        if not graded["issues"]:
            # 模型说"没问题"时用规则再查一遍；仍为空才认为是高质量译文
            rule_check = _rule_grade_translation(source, translated,
                                                 reference, "复核")
            graded["issues"] = rule_check["issues"]
            if graded["issues"]:
                meta["warnings"].append("LLM 未指出具体问题，规则复核补充了证据")
        if not graded["improved_translation"]:
            graded["improved_translation"] = (
                reference or _surface_fix_translation(translated))

    score = round(_clamp(float(graded["score"]), 0.0, MAX_SCORE), 1)
    record = {
        "grade_id": f"tr_{uuid.uuid4().hex[:12]}",
        "user_id": uid,
        "level": level_text,
        "source_text": source,
        "user_translation": translated,
        "reference_translation": graded.get("reference_translation")
                                  or reference or "",
        "score": score,
        "score_710": _score_710(score),
        "level_band": _level_band(score),
        "dimensions": graded["dimensions"],
        "feedback": graded["feedback"],
        "issues": graded["issues"],
        "improved_translation": graded.get("improved_translation") or "",
        "status": "pending_review",
        "model_meta": meta,
        "created_at": time.time(),
        "reviewer_note": None,
        "reviewed_at": None,
    }
    await _insert_translation(record)
    return record


async def list_grades(user_id: str, kind: str | None = None,
                      limit: int = 20) -> list[dict]:
    """批改历史（摘要），kind=None 表示写作 + 翻译一起查"""
    uid = _require_text(user_id, "user_id")
    kinds = _resolve_kinds(kind)
    limit = _normalize_limit(limit, 20, 100)

    items: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        for current in kinds:
            table = _TABLE_OF[current]
            cursor = await db.execute(
                f"SELECT * FROM {table} WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (uid, limit),
            )
            rows = await cursor.fetchall()
            columns = [item[0] for item in cursor.description]
            for row in rows:
                items.append(_summary(current, dict(zip(columns, row))))
    items.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return items[:limit]


async def get_grade(grade_id: str, kind: str = "writing") -> dict | None:
    """单条批改详情，不存在返回 None"""
    gid = _require_text(grade_id, "grade_id")
    kind_text = _normalize_kind(kind)
    data = await _fetch_row(gid, kind_text)
    if data is None:
        return None
    return (_writing_from_row(data) if kind_text == "writing"
            else _translation_from_row(data))


async def list_review_queue(kind: str = "writing",
                            limit: int = 50) -> list[dict]:
    """人工复核队列：待复核（pending_review）的批改，先进先出"""
    kind_text = _normalize_kind(kind)
    limit = _normalize_limit(limit, 50, 200)
    table = _TABLE_OF[kind_text]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE status = 'pending_review' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        columns = [item[0] for item in cursor.description]
    return [(_writing_from_row(dict(zip(columns, row)))
             if kind_text == "writing"
             else _translation_from_row(dict(zip(columns, row))))
            for row in rows]


async def review_grade(grade_id: str, kind: str, action: str,
                       score: float | None = None,
                       note: str | None = None) -> dict:
    """人工复核：approve 保持机评分，override 用人工分覆盖（clamp 到 0-15）。

    复核结果落库：status 改为 approved / overridden，同时写入
    reviewer_note、reviewed_at；override 时同步更新 score_710 与档位。
    """
    gid = _require_text(grade_id, "grade_id")
    kind_text = _normalize_kind(kind)
    action_text = str(action or "").strip().lower()
    if action_text in ("approve", "approved", "pass"):
        status = "approved"
    elif action_text in ("override", "overridden", "override_score"):
        status = "overridden"
    else:
        raise ValueError("action 仅支持 approve / override")

    current = await get_grade(gid, kind_text)
    if current is None:
        raise LookupError(f"批改记录不存在: {gid}")

    if status == "overridden":
        if score is None:
            raise ValueError("override 必须提供人工分数 score")
        try:
            human_score = float(score)
        except (TypeError, ValueError):
            raise ValueError("score 必须是数字")
        if human_score != human_score:  # NaN
            raise ValueError("score 不能是 NaN")
        human_score = round(_clamp(human_score, 0.0, MAX_SCORE), 1)
        new_score = human_score
        new_score_710 = _score_710(human_score)
        new_band = _level_band(human_score)
    else:
        new_score = current["score"]
        new_score_710 = current["score_710"]
        new_band = current["level_band"]

    reviewer_note = str(note).strip()[:1000] if note is not None else None
    reviewed_at = time.time()

    table = _TABLE_OF[kind_text]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE {table} SET status = ?, reviewer_note = ?, "
            "reviewed_at = ?, score = ?, score_710 = ?, level_band = ? "
            "WHERE grade_id = ?",
            (status, reviewer_note, reviewed_at, new_score, new_score_710,
             new_band, gid),
        )
        await db.commit()

    updated = await get_grade(gid, kind_text)
    if updated is None:  # 理论上不可达（上面刚更新过）
        raise LookupError(f"批改记录不存在: {gid}")
    return updated
