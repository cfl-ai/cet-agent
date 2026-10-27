"""错题归因 + 个性化提分方案测试。

覆盖：
  1. 规则归因：时长/题型/重复错/掌握度/选项相邻各自触发对应 error_type，
     并验证 evidence 证据链确实落库（不是"拍脑袋"结论）。
  2. LLM 精修：正常合并、脏 JSON 修复、不可解析重试后回退、非法条目丢弃、
     调用异常回退规则（绝不 500）。
  3. 落库与报告：upsert 幂等、top_weak_points 排序、空数据报告、未归因提示。
  4. 提分方案：真实考试分 / 仅掌握度启发式 / 全空三种 current_score 分支、
     周计划题量合理、days_left 与默认 horizon、LLM 叙述成功/失败/重试、
     latest_plan 读取。
  5. 路由：四个业务接口 + 管理接口鉴权 + 参数校验（400）与未预期异常（500）。

所有测试不访问真实 LLM：autouse fixture 把 llm_client.chat 替换为抛错桩，
需要 LLM 输出的用例再显式 monkeypatch 成假响应。
"""
import json
import time
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import aiosqlite

from models.database import DB_PATH, init_db
from services import attribution_service, improvement_plan
from services.llm_client import llm_client


# ==================== 公共 fixture / 辅助函数 ====================

@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """默认禁用真实 LLM：未显式替换时一律走降级分支，并记录调用次数"""
    calls = {"count": 0}

    async def _raise(*args, **kwargs):
        calls["count"] += 1
        raise RuntimeError("LLM 不可用（测试桩）")

    monkeypatch.setattr(llm_client, "chat", _raise)
    return calls


def _patch_llm(monkeypatch, responses):
    """把 llm_client.chat 替换成按顺序返回 responses 的假实现"""
    calls = {"count": 0, "messages": []}

    async def _fake(messages, **kwargs):
        index = min(calls["count"], len(responses) - 1)
        item = responses[index]
        calls["count"] += 1
        calls["messages"].append(messages)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm_client, "chat", _fake)
    return calls


async def _seed_user(db, user_id, level="CET4", target_score=425):
    now = time.time()
    await db.execute(
        "INSERT OR REPLACE INTO users "
        "(user_id, username, level, target_score, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, user_id, level, target_score, now, now),
    )


async def _seed_question(db, question_id, question_type="阅读-仔细阅读",
                         tags=None, answer="D", explanation="",
                         content="测试题干", options=None):
    if options is None:
        # 选项文本刻意做成低相似度，避免无关用例误触发 similar_option 规则
        options = ["A. apple", "B. banana", "C. cherry", "D. dragon"]
    await db.execute(
        "INSERT OR REPLACE INTO questions "
        "(question_id, question_type, topic, difficulty, content, options, "
        "answer, explanation, knowledge_tags, source, verified, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (question_id, question_type, "测试", 3, content,
         json.dumps(options, ensure_ascii=False), answer, explanation,
         json.dumps(list(tags or []), ensure_ascii=False), "test", 1, time.time()),
    )


async def _seed_wrong(db, user_id, question_id, wrong_count=1, resolved=0,
                      last_wrong_at=None):
    await db.execute(
        "INSERT OR REPLACE INTO wrong_questions "
        "(user_id, question_id, wrong_count, error_type, resolved, last_wrong_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, question_id, wrong_count, None, resolved,
         last_wrong_at if last_wrong_at is not None else time.time()),
    )


async def _seed_answer(db, user_id, question_id, duration,
                       user_answer="B", correct_answer="D"):
    await db.execute(
        "INSERT INTO answer_records "
        "(user_id, question_id, user_answer, correct_answer, is_correct, "
        "duration, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, question_id, user_answer, correct_answer, 0, duration,
         time.time()),
    )


async def _seed_mastery(db, user_id, knowledge_point, mastery=0.7,
                        attempts=10, correct=7):
    await db.execute(
        "INSERT OR REPLACE INTO knowledge_state "
        "(user_id, knowledge_point, mastery, attempts, correct, last_update) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, knowledge_point, mastery, attempts, correct, time.time()),
    )


async def _seed_wrong_item(db, user_id, question_id, *,
                           question_type="阅读-仔细阅读", tag="细节理解",
                           answer="D", user_answer="B", duration=30.0,
                           wrong_count=1, mastery=0.7, explanation="",
                           options=None, commit=True):
    """一条完整错题：用户 + 题目 + 错题本 + 最近答题记录 + 知识点掌握度"""
    await _seed_user(db, user_id)
    await _seed_question(db, question_id, question_type=question_type,
                         tags=[tag], answer=answer, explanation=explanation,
                         options=options)
    await _seed_wrong(db, user_id, question_id, wrong_count=wrong_count)
    if duration is not None:
        await _seed_answer(db, user_id, question_id, duration,
                           user_answer, answer)
    if mastery is not None:
        await _seed_mastery(db, user_id, tag, mastery=mastery)
    if commit:
        await db.commit()


def _items_by_id(report):
    return {item["question_id"]: item for item in report["items"]}


async def _make_client():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from routers.insight import router

    app = FastAPI()
    app.include_router(router)
    return AsyncClient(transport=ASGITransport(app=app),
                       base_url="http://test")


# ==================== 一、规则归因 ====================

class TestRuleAttribution:
    """规则归因必须"有据可依"：每条结论都能在 evidence.signals 里找到依据"""

    @pytest.mark.asyncio
    async def test_rush_duration_triggers_careless(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            # 3 秒秒选 → careless；其余信号都保持中性
            await _seed_wrong_item(db, "rule_rush", "q_rush", duration=3.0)

        report = await attribution_service.attribute_wrong_questions(
            "rule_rush", use_llm=False)
        item = _items_by_id(report)["q_rush"]

        assert item["error_type"] == "careless"
        assert item["error_type_cn"] == "粗心失误"
        signals = [s["signal"] for s in item["evidence"]["signals"]]
        assert "duration_rush" in signals
        assert 0.0 < item["confidence"] <= 1.0
        assert item["root_cause"] and item["remediation"]

    @pytest.mark.asyncio
    async def test_vocab_question_type_triggers_vocab(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(
                db, "rule_vocab", "q_vocab",
                question_type="阅读-选词填空", tag="词义辨析", duration=30.0)

        report = await attribution_service.attribute_wrong_questions(
            "rule_vocab", use_llm=False)
        item = _items_by_id(report)["q_vocab"]

        assert item["error_type"] == "vocab"
        assert any(s["signal"] == "question_type_vocab"
                   for s in item["evidence"]["signals"])

    @pytest.mark.asyncio
    async def test_repeat_wrong_triggers_concept(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "rule_repeat", "q_repeat",
                                   duration=30.0, wrong_count=3)

        report = await attribution_service.attribute_wrong_questions(
            "rule_repeat", use_llm=False)
        item = _items_by_id(report)["q_repeat"]

        assert item["error_type"] == "concept"
        assert any(s["signal"] == "repeat_wrong"
                   for s in item["evidence"]["signals"])

    @pytest.mark.asyncio
    async def test_low_mastery_triggers_knowledge_gap(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "rule_mastery", "q_mastery",
                                   duration=30.0, mastery=0.25)

        report = await attribution_service.attribute_wrong_questions(
            "rule_mastery", use_llm=False)
        item = _items_by_id(report)["q_mastery"]

        assert item["error_type"] == "knowledge_gap"
        assert item["evidence"]["mastery"] == 0.25
        assert any(s["signal"] == "low_mastery"
                   for s in item["evidence"]["signals"])

    @pytest.mark.asyncio
    async def test_hesitate_duration_triggers_concept(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "rule_hesitate", "q_hesitate",
                                   duration=95.0)

        report = await attribution_service.attribute_wrong_questions(
            "rule_hesitate", use_llm=False)
        item = _items_by_id(report)["q_hesitate"]

        assert item["error_type"] == "concept"
        assert any(s["signal"] == "duration_hesitate"
                   for s in item["evidence"]["signals"])

    @pytest.mark.asyncio
    async def test_adjacent_option_triggers_careless(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            # 选 C、正确 D：相邻选项 → 排除法不彻底
            await _seed_wrong_item(db, "rule_adjacent", "q_adjacent",
                                   duration=30.0, user_answer="C")

        report = await attribution_service.attribute_wrong_questions(
            "rule_adjacent", use_llm=False)
        item = _items_by_id(report)["q_adjacent"]

        assert item["error_type"] == "careless"
        assert any(s["signal"] == "adjacent_option"
                   for s in item["evidence"]["signals"])


# ==================== 二、LLM 精修与降级 ====================

class TestLlmRefinement:

    @pytest.mark.asyncio
    async def test_llm_success_merges_and_marks_source(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "llm_merge", "q_m1")
            await _seed_wrong_item(db, "llm_merge", "q_m2")
            await db.commit()

        responses = [
            json.dumps([
                {"question_id": "q_m1", "error_type": "careless",
                 "knowledge_point": "细节理解", "root_cause": "复核不足",
                 "remediation": "圈画关键词后复核", "confidence": 0.88},
                {"question_id": "q_m2", "error_type": "concept",
                 "confidence": 0.66},
            ], ensure_ascii=False),
        ]
        _patch_llm(monkeypatch, responses)

        report = await attribution_service.attribute_wrong_questions(
            "llm_merge", use_llm=True)
        items = _items_by_id(report)

        # 全字段由 LLM 提供 → source=llm
        assert items["q_m1"]["error_type"] == "careless"
        assert items["q_m1"]["confidence"] == 0.88
        assert items["q_m1"]["source"] == "llm"
        # 缺字段由规则补齐 → source=hybrid
        assert items["q_m2"]["error_type"] == "concept"
        assert items["q_m2"]["source"] == "hybrid"
        assert items["q_m2"]["root_cause"]
        assert report["meta"]["llm"]["applied"] is True
        assert report["meta"]["llm"]["fallback"] is False

    @pytest.mark.asyncio
    async def test_dirty_json_is_repaired(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "llm_dirty", "q_dirty")

        dirty = (
            "好的，分析结果如下：\n"
            "```json\n"
            "[{\"question_id\": \"q_dirty\", \"error_type\": \"concept\", "
            "\"knowledge_point\": \"细节理解\", \"root_cause\": \"规则边界不清\", "
            "\"remediation\": \"回看定义并做变式题\", \"confidence\": 0.8,},]\n"
            "```"
        )
        # 先确认这段文本确实不是合法 JSON，证明修复逻辑真的生效
        with pytest.raises(json.JSONDecodeError):
            json.loads(dirty)

        calls = _patch_llm(monkeypatch, [dirty])
        report = await attribution_service.attribute_wrong_questions(
            "llm_dirty", use_llm=True)
        item = _items_by_id(report)["q_dirty"]

        assert calls["count"] == 1
        assert item["error_type"] == "concept"
        assert item["confidence"] == 0.8
        assert report["meta"]["llm"]["fallback"] is False

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back_to_rules(self, no_real_llm):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "llm_error", "q_err", duration=3.0)

        report = await attribution_service.attribute_wrong_questions(
            "llm_error", use_llm=True)
        item = _items_by_id(report)["q_err"]

        assert item["error_type"] == "careless"  # 规则结果
        assert item["source"] == "rules"
        assert report["meta"]["llm"]["fallback"] is True
        assert report["meta"]["llm"]["error"]
        assert no_real_llm["count"] >= 1

    @pytest.mark.asyncio
    async def test_unparseable_output_retries_then_falls_back(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "llm_retry", "q_retry")

        calls = _patch_llm(monkeypatch, ["这不是 JSON", "仍然不是 JSON"])
        report = await attribution_service.attribute_wrong_questions(
            "llm_retry", use_llm=True)
        item = _items_by_id(report)["q_retry"]

        assert calls["count"] == 2, "解析失败应带修复提示重试一次"
        assert item["source"] == "rules"
        assert report["meta"]["llm"]["fallback"] is True

    @pytest.mark.asyncio
    async def test_invalid_llm_entries_are_dropped(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "llm_invalid", "q_i1")
            await _seed_wrong_item(db, "llm_invalid", "q_i2")
            await db.commit()

        responses = [
            json.dumps([
                # question_id 不在输入集合内 → 丢弃
                {"question_id": "q_made_up", "error_type": "careless",
                 "confidence": 0.9},
                # error_type 不在枚举内 → 丢弃
                {"question_id": "q_i1", "error_type": "banana",
                 "confidence": 0.9},
                # confidence 越界 → 丢弃
                {"question_id": "q_i1", "error_type": "careless",
                 "confidence": 2.0},
                # 合法条目
                {"question_id": "q_i2", "error_type": "grammar",
                 "knowledge_point": "语法", "root_cause": "从句规则不清",
                 "remediation": "整理语法卡", "confidence": 0.7},
            ], ensure_ascii=False),
        ]
        _patch_llm(monkeypatch, responses)

        report = await attribution_service.attribute_wrong_questions(
            "llm_invalid", use_llm=True)
        items = _items_by_id(report)

        assert items["q_i1"]["source"] == "rules"  # 非法项全部丢弃，保留规则结果
        assert items["q_i1"]["error_type"] == "knowledge_gap"
        assert items["q_i2"]["error_type"] == "grammar"
        assert items["q_i2"]["source"] == "llm"
        assert report["meta"]["llm"]["applied"] is True

    @pytest.mark.asyncio
    async def test_llm_batch_capped_at_five(self, monkeypatch):
        """单次 LLM 精修最多 5 题：控制成本，其余题目保留规则结果"""
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            for index in range(1, 7):
                await _seed_wrong_item(
                    db, "llm_cap", f"q_cap{index}", duration=30.0)
            await db.commit()

        entries = [
            {"question_id": f"q_cap{index}", "error_type": "careless",
             "confidence": 0.7}
            for index in range(1, 7)
        ]
        calls = _patch_llm(monkeypatch, [
            json.dumps(entries, ensure_ascii=False)])

        report = await attribution_service.attribute_wrong_questions(
            "llm_cap", use_llm=True)
        prompt = calls["messages"][0][1]["content"]
        assert prompt.count("question_id: q_cap") == 5, "LLM 一次最多处理 5 题"

        items = _items_by_id(report)
        sources = [items[f"q_cap{index}"]["source"]
                   for index in range(1, 7)]
        assert sources.count("rules") == 1, "超出 5 题批次的题目保留规则结果"
        assert sum(1 for source in sources
                   if source in {"llm", "hybrid"}) == 5


# ==================== 三、落库 / 报告聚合 ====================

class TestAttributionPersistence:

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "upsert_user", "q_u1")
            await _seed_wrong_item(db, "upsert_user", "q_u2")
            await db.commit()

        first = await attribution_service.attribute_wrong_questions(
            "upsert_user", use_llm=False)
        second = await attribution_service.attribute_wrong_questions(
            "upsert_user", use_llm=False)

        assert first["summary"]["attributed"] == 2
        assert second["summary"]["attributed"] == 2
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM wrong_attributions WHERE user_id=?",
                ("upsert_user",))
            (count,) = await cursor.fetchone()
        assert count == 2, "重复归因不应重复插入"

    @pytest.mark.asyncio
    async def test_limit_controls_scope(self):
        """limit 控制本次归因题量，未覆盖的错题在报告 note 中提示"""
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            for index in range(1, 4):
                await _seed_wrong_item(
                    db, "limit_user", f"q_limit{index}", duration=3.0)
            await db.commit()

        report = await attribution_service.attribute_wrong_questions(
            "limit_user", limit=2, use_llm=False)
        assert report["summary"]["total_wrong"] == 3
        assert report["summary"]["attributed"] == 2
        assert len(report["items"]) == 2
        assert "尚未归因" in (report["note"] or "")

    @pytest.mark.asyncio
    async def test_report_top_weak_points_sorted(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            # 词汇：掌握度低但只错 1 次
            await _seed_wrong_item(db, "report_user", "q_weak1",
                                   tag="词汇", mastery=0.3, wrong_count=1)
            # 阅读：掌握度中等但反复错（4 + 3 次）
            await _seed_wrong_item(db, "report_user", "q_weak2",
                                   tag="阅读", mastery=0.7, wrong_count=4)
            await _seed_wrong_item(db, "report_user", "q_weak3",
                                   tag="阅读", mastery=0.7, wrong_count=3)
            await db.commit()

        report = await attribution_service.attribute_wrong_questions(
            "report_user", use_llm=False)
        summary = report["summary"]

        assert summary["total_wrong"] == 3
        assert summary["attributed"] == 3
        assert summary["by_error_type"].get("concept") == 2
        assert summary["by_error_type"].get("knowledge_gap") == 1
        assert summary["by_question_type"].get("阅读-仔细阅读") == 3

        top = summary["top_weak_points"]
        assert top[0]["knowledge_point"] == "阅读"
        assert top[0]["wrong_count"] == 7
        assert top[0]["mastery"] == 0.7
        assert top[1]["knowledge_point"] == "词汇"
        assert report["recommendations"], "报告应给出可执行建议"

    @pytest.mark.asyncio
    async def test_empty_report(self, no_real_llm):
        await init_db()
        report = await attribution_service.attribute_wrong_questions(
            "nobody", use_llm=True)

        assert report["summary"]["total_wrong"] == 0
        assert report["summary"]["attributed"] == 0
        assert report["summary"]["by_error_type"] == {}
        assert report["summary"]["top_weak_points"] == []
        assert report["items"] == []
        assert report["note"]
        assert report["recommendations"] == []
        assert no_real_llm["count"] == 0, "没有错题时不应调用 LLM"

    @pytest.mark.asyncio
    async def test_report_before_attribution_has_note(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "note_user", "q_note")
            await db.commit()

        report = await attribution_service.get_attribution_report("note_user")
        assert report["summary"]["total_wrong"] == 1
        assert report["summary"]["attributed"] == 0
        assert report["items"] == []
        assert "尚未归因" in (report["note"] or "")

    @pytest.mark.asyncio
    async def test_empty_user_id_rejected(self):
        await init_db()
        with pytest.raises(ValueError):
            await attribution_service.attribute_wrong_questions("")
        with pytest.raises(ValueError):
            await attribution_service.get_attribution_report("   ")


# ==================== 四、提分方案：当前分三分支 ====================

class TestCurrentScore:

    @pytest.mark.asyncio
    async def test_real_exam_score_has_priority(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "score_real", target_score=500)
            await _seed_mastery(db, "score_real", "阅读", mastery=0.3,
                                attempts=10)
            await db.execute(
                "INSERT INTO user_exam_scores "
                "(user_id, level, score, exam_date, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("score_real", "CET4", 480, "2026-06-01", "real_exam",
                 time.time()))
            await db.commit()

        plan = await improvement_plan.generate_improvement_plan(
            "score_real", days_left=28)

        assert plan["current_score"] == 480.0
        assert plan["current_score_source"] == "real_exam"
        assert plan["gap"] == 20.0
        snapshot = plan["source_snapshot"]
        assert snapshot["current_score"]["method"] == "real_exam"
        assert snapshot["latest_exam_score"]["score"] == 480

    @pytest.mark.asyncio
    async def test_heuristic_score_from_mastery(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "score_heur", target_score=500)
            await _seed_mastery(db, "score_heur", "阅读", mastery=0.4,
                                attempts=10)
            await _seed_mastery(db, "score_heur", "听力", mastery=0.6,
                                attempts=10)
            await db.commit()

        plan = await improvement_plan.generate_improvement_plan(
            "score_heur", days_left=28)

        # avg_mastery = 0.5 → 290 + 0.5*420 = 500
        assert plan["current_score"] == 500.0
        assert plan["current_score_source"] == "heuristic"
        assert plan["gap"] == 0.0
        current = plan["source_snapshot"]["current_score"]
        assert current["method"] == "heuristic"
        assert current["formula"] == "290 + avg_mastery * 420"
        assert current["avg_mastery"] == 0.5

    @pytest.mark.asyncio
    async def test_empty_data_gives_none_and_advice(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "score_none")
            await db.commit()

        plan = await improvement_plan.generate_improvement_plan("score_none")

        assert plan["current_score"] is None
        assert plan["gap"] is None
        assert plan["current_score_source"] == "none"
        assert "20 题" in (plan["note"] or "")
        assert len(plan["focus_points"]) >= 3
        assert plan["weekly_schedule"], "无数据也要给出可执行的均衡计划"
        assert plan["actions"]

    @pytest.mark.asyncio
    async def test_unknown_user_uses_profile_defaults(self):
        await init_db()
        plan = await improvement_plan.generate_improvement_plan("ghost_user")

        assert plan["level"] == "CET4"
        assert plan["target_score"] == 425
        assert plan["horizon_days"] == 42
        assert await improvement_plan.get_latest_plan("ghost_user") is not None

    @pytest.mark.asyncio
    async def test_snapshot_includes_all_data_sources(self):
        """source_snapshot 固化写作/翻译/归因/学习统计，保证方案可复算"""
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "snapshot_user", "q_snapshot",
                                   duration=3.0)
            now = time.time()
            await db.execute(
                "INSERT INTO writing_grades "
                "(grade_id, user_id, level, topic, essay, word_count, score, "
                "score_710, level_band, feedback, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("g_write", "snapshot_user", "CET4", "环保", "essay", 120,
                 8.0, 56.8, "8分档", "结构完整", "approved", now))
            await db.execute(
                "INSERT INTO translation_grades "
                "(grade_id, user_id, level, source_text, user_translation, "
                "reference_translation, score, score_710, level_band, "
                "feedback, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("g_trans", "snapshot_user", "CET4", "原文", "译文", "参考",
                 7.0, 49.7, "7分档", "漏译一处", "approved", now))
            await db.execute(
                "INSERT INTO study_stats "
                "(user_id, stat_date, questions_done, correct_count, "
                "words_reviewed, study_minutes) VALUES (?, ?, ?, ?, ?, ?)",
                ("snapshot_user", time.strftime("%Y-%m-%d"),
                 20, 14, 30, 45))
            await db.commit()

        await attribution_service.attribute_wrong_questions(
            "snapshot_user", use_llm=False)
        plan = await improvement_plan.generate_improvement_plan(
            "snapshot_user", days_left=14)
        snapshot = plan["source_snapshot"]

        assert snapshot["writing_grade"]["score"] == 8.0
        assert snapshot["translation_grade"]["score"] == 7.0
        assert snapshot["study_stats_14d"]["questions_done"] == 20
        assert snapshot["attribution"]["total"] == 1
        assert snapshot["volume"]["weeks"] == 2

    @pytest.mark.asyncio
    async def test_empty_user_id_rejected(self):
        await init_db()
        with pytest.raises(ValueError):
            await improvement_plan.generate_improvement_plan("")
        with pytest.raises(ValueError):
            await improvement_plan.get_latest_plan("  ")


# ==================== 五、周计划题量与 horizon ====================

class TestWeeklySchedule:

    @pytest.mark.asyncio
    async def test_volume_is_reasonable(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_volume", target_score=550)
            await _seed_mastery(db, "plan_volume", "阅读", mastery=0.3,
                                attempts=20)
            await db.commit()

        plan = await improvement_plan.generate_improvement_plan(
            "plan_volume", days_left=28)
        schedule = plan["weekly_schedule"]
        volume = plan["source_snapshot"]["volume"]

        assert len(schedule) == 4, "28 天 = 4 周"
        assert 10 <= volume["daily_questions"] <= 20
        assert volume["practice_total"] == sum(
            task["count"]
            for week in schedule
            for task in week["tasks"]
            if task["type"] == "practice"
        )
        assert 280 <= volume["practice_total"] <= 560, "总题量应与 28 天匹配"

        for week in schedule:
            assert week["theme"] and week["milestone"]
            task_types = {task["type"] for task in week["tasks"]}
            assert task_types <= set(improvement_plan.TASK_TYPES)
            counts = {task["type"]: task["count"] for task in week["tasks"]}
            assert 70 <= counts["practice"] <= 140, "每天 10-20 题 → 每周 70-140"
            assert 1 <= counts["writing"] <= 2, "每周 1-2 篇写作批改"
            assert 1 <= counts["translation"] <= 2, "每周 1-2 篇翻译批改"
            assert counts["vocabulary"] > 0 and counts["review"] > 0

    @pytest.mark.asyncio
    async def test_days_left_controls_horizon(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_days")
            await db.commit()

        short = await improvement_plan.generate_improvement_plan(
            "plan_days", days_left=14)
        assert short["horizon_days"] == 14
        assert len(short["weekly_schedule"]) == 2

        urgent = await improvement_plan.generate_improvement_plan(
            "plan_days", days_left=1)
        assert urgent["horizon_days"] == 1
        assert len(urgent["weekly_schedule"]) == 1

        zero = await improvement_plan.generate_improvement_plan(
            "plan_days", days_left=0)
        assert zero["horizon_days"] >= 1, "days_left 必须收敛到合法区间"

    @pytest.mark.asyncio
    async def test_default_horizon_depends_on_level(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_cet4", level="CET4")
            await _seed_user(db, "plan_cet6", level="CET6")
            await db.commit()

        cet4 = await improvement_plan.generate_improvement_plan("plan_cet4")
        cet6 = await improvement_plan.generate_improvement_plan("plan_cet6")

        assert cet4["level"] == "CET4"
        assert cet4["horizon_days"] == 42
        assert len(cet4["weekly_schedule"]) == 6
        assert cet6["level"] == "CET6"
        assert cet6["horizon_days"] == 56
        assert len(cet6["weekly_schedule"]) == 8


# ==================== 六、LLM 叙述与 latest_plan ====================

class TestPlanNarrative:

    @pytest.mark.asyncio
    async def test_llm_narrative_and_actions(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_llm", target_score=500)
            await _seed_mastery(db, "plan_llm", "阅读", mastery=0.5,
                                attempts=20)
            await db.commit()

        narrative = (
            "你的基础不错，阅读和听力是主要提分点。接下来四周每天完成十五道专项题，"
            "重点突破仔细阅读和听力长对话，每两天复盘一次错题归因。"
            "写作和翻译每周各练两篇，先保证句子正确再追求表达多样。"
            "坚持记录正确率，两周后你会看到明显进步，考前一周进入全真模考节奏。"
        )
        actions = [
            "每天完成 15 道专项题并当天订正。",
            "每天复习 20 个高频词与固定搭配。",
            "每周完成 2 篇写作批改并重写。",
            "每周完成 2 篇翻译批改并对照参考译文。",
            "周末复盘错题归因 30 分钟。",
        ]
        _patch_llm(monkeypatch, [
            json.dumps({"narrative": narrative, "actions": actions},
                       ensure_ascii=False),
        ])

        plan = await improvement_plan.generate_improvement_plan(
            "plan_llm", days_left=28)

        assert plan["narrative"] == narrative
        assert plan["actions"] == actions
        meta = plan["model_meta"]
        assert meta["fallback"] is False
        assert meta["narrative_source"] == "llm"
        assert meta["actions_source"] == "llm"
        assert "provider" in meta and "model" in meta

        latest = await improvement_plan.get_latest_plan("plan_llm")
        assert latest is not None
        assert latest["plan_id"] == plan["plan_id"]
        assert latest["narrative"] == narrative

    @pytest.mark.asyncio
    async def test_llm_failure_uses_template(self, no_real_llm):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_fallback", target_score=500)
            await _seed_mastery(db, "plan_fallback", "听力", mastery=0.4,
                                attempts=20)
            await db.commit()

        plan = await improvement_plan.generate_improvement_plan(
            "plan_fallback", days_left=28)

        meta = plan["model_meta"]
        assert meta["fallback"] is True
        assert meta["error"]
        assert meta["narrative_source"] == "template"
        assert len(plan["narrative"]) >= 80, "模板叙述也要具体、可读"
        assert 3 <= len(plan["actions"]) <= 6
        assert no_real_llm["count"] >= 1
        assert await improvement_plan.get_latest_plan("plan_fallback") is not None

    @pytest.mark.asyncio
    async def test_partial_llm_output_fills_actions_with_template(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_partial")
            await db.commit()

        narrative = ("你的目标是四级 500 分，当前主要问题是阅读和听力基础不牢。"
                     "接下来六周每天保持十二道专项题，先补仔细阅读和长对话，"
                     "写作翻译每周各一篇，周末固定复盘错题。坚持记录每天正确率，"
                     "每两周做一次全真模考，把稳定得分段先拿满，再逐步冲高分。")
        _patch_llm(monkeypatch, [
            json.dumps({"narrative": narrative}, ensure_ascii=False),
        ])

        plan = await improvement_plan.generate_improvement_plan("plan_partial")

        assert plan["narrative"] == narrative
        assert plan["model_meta"]["narrative_source"] == "llm"
        assert plan["model_meta"]["actions_source"] == "template"
        assert plan["model_meta"]["fallback"] is True
        assert 3 <= len(plan["actions"]) <= 6

    @pytest.mark.asyncio
    async def test_dirty_plan_json_retries(self, monkeypatch):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "plan_retry")
            await db.commit()

        narrative = ("先完成 20 题测评建立基线，之后每天十到十五道专项题，"
                     "阅读和听力交替训练，写作翻译每周各一篇并提交批改。"
                     "每周日复盘错题归因，把概念不清的题重做一遍。"
                     "两周后再做一套全真模考，根据分数调整下一个阶段的重心，保持节奏最重要。")
        valid = json.dumps({
            "narrative": narrative,
            "actions": ["每天 12 道专项题并订正。", "每天复习 20 个高频词。",
                        "每周 1 篇写作批改。", "每周 1 篇翻译批改。"],
        }, ensure_ascii=False)
        calls = _patch_llm(monkeypatch, ["抱歉，我先想想。", valid])

        plan = await improvement_plan.generate_improvement_plan("plan_retry")

        assert calls["count"] == 2, "解析失败应重试一次"
        assert plan["narrative"] == narrative
        assert plan["model_meta"]["fallback"] is False
        assert plan["model_meta"]["attempts"] == 2

    @pytest.mark.asyncio
    async def test_latest_plan_none_then_latest(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_user(db, "latest_user", target_score=500)
            await db.commit()

        assert await improvement_plan.get_latest_plan("latest_user") is None

        first = await improvement_plan.generate_improvement_plan(
            "latest_user", days_left=14)
        time.sleep(0.01)
        second = await improvement_plan.generate_improvement_plan(
            "latest_user", days_left=28)

        latest = await improvement_plan.get_latest_plan("latest_user")
        assert latest is not None
        assert latest["plan_id"] == second["plan_id"]
        assert latest["plan_id"] != first["plan_id"]
        assert latest["horizon_days"] == 28
        assert latest["current_score_source"] in {"heuristic", "real_exam", "none"}


# ==================== 七、路由接口 ====================

class TestInsightRouter:

    @pytest.mark.asyncio
    async def test_attribution_and_plan_flow(self):
        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "router_user", "q_router",
                                   duration=3.0)

        async with await _make_client() as client:
            response = await client.post(
                "/api/wrong/router_user/attribution",
                json={"use_llm": False, "limit": 20},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["code"] == 0
            assert body["data"]["summary"]["total_wrong"] == 1
            assert body["data"]["items"][0]["error_type"] == "careless"

            response = await client.get("/api/wrong/router_user/attribution")
            assert response.status_code == 200
            assert response.json()["data"]["summary"]["attributed"] == 1

            response = await client.post(
                "/api/plan/improve",
                json={"user_id": "router_user", "target_score": 500,
                      "days_left": 14},
            )
            assert response.status_code == 200
            plan = response.json()["data"]
            assert plan["horizon_days"] == 14
            assert plan["user_id"] == "router_user"

            response = await client.get(
                "/api/plan/improve/router_user/latest")
            assert response.status_code == 200
            latest = response.json()["data"]
            assert latest is not None
            assert latest["plan_id"] == plan["plan_id"]

            # 从未生成方案的用户返回 null，而不是报错
            response = await client.get(
                "/api/plan/improve/fresh_user/latest")
            assert response.status_code == 200
            assert response.json()["data"] is None

    @pytest.mark.asyncio
    async def test_invalid_params_return_400(self):
        await init_db()
        async with await _make_client() as client:
            response = await client.post(
                "/api/plan/improve", json={"user_id": ""})
            assert response.status_code == 400

            response = await client.post(
                "/api/wrong/%20/attribution", json={"use_llm": False})
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_500(self, monkeypatch):
        await init_db()

        async def _boom(*args, **kwargs):
            raise RuntimeError("模拟未预期异常")

        monkeypatch.setattr(
            attribution_service, "attribute_wrong_questions", _boom)

        async with await _make_client() as client:
            response = await client.post(
                "/api/wrong/boom_user/attribution",
                json={"use_llm": False},
            )
            assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_admin_recompute_requires_token(self, monkeypatch):
        from config import settings

        await init_db()
        async with aiosqlite.connect(DB_PATH) as db:
            await _seed_wrong_item(db, "admin_user", "q_admin", duration=3.0)

        async with await _make_client() as client:
            # 服务端未配置 ADMIN_TOKEN → 503，接口不裸奔
            monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
            response = await client.post(
                "/api/admin/insight/attribution/recompute")
            assert response.status_code == 503

            monkeypatch.setattr(settings, "ADMIN_TOKEN", "test-admin-token")
            # 无令牌 / 错误令牌 → 401
            response = await client.post(
                "/api/admin/insight/attribution/recompute")
            assert response.status_code == 401
            response = await client.post(
                "/api/admin/insight/attribution/recompute",
                headers={"X-Admin-Token": "wrong-token"},
            )
            assert response.status_code == 401

            # 正确令牌 → 200，并返回重算结果
            response = await client.post(
                "/api/admin/insight/attribution/recompute",
                headers={"X-Admin-Token": "test-admin-token"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["code"] == 0
            assert body["data"]["user_count"] == 1
            assert body["data"]["results"][0]["attributed"] == 1
