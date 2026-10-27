"""写作 / 翻译真实判分模块测试。

覆盖重点（对应产品要求"判分机制要真实"）：
  1. 正常评分：LLM 返回维度分 + 逐句批注，服务端复核总分，落库可查；
  2. 降级：LLM 抛错 / 连续输出脏数据时，规则兜底仍给出可解释分数，绝不 500；
  3. 脏 JSON：markdown 围栏、尾随逗号、缺字段、维度分越界，修复 + 截断；
  4. 服务端校验：空文本、超长文本、非法 level/word_limit、非法 kind/action；
  5. 折算：score / 15 * 106.5，人工 override 同步更新；
  6. 权限：管理接口无 token 401、服务端未配置 token 503；
  7. 落库与查询：writing_grades / translation_grades 写入、list/get 读取。

LLM 一律 monkeypatch，不发起真实网络请求。
"""
import json
import sys
from pathlib import Path

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from models.database import DB_PATH, init_db  # noqa: E402
from routers.grading import router as grading_router  # noqa: E402
from services import essay_grading  # noqa: E402
from services import llm_client as llm_mod  # noqa: E402

FENCE = chr(96) * 3  # 测试文件里避免直接写三反引号

TOPIC = "Online Learning: Benefits and Challenges"

ESSAY = (
    "With the development of the Internet, online learning has become popular "
    "among college students. However, some students cannot control themselves "
    "and waste much time on games.\n\n"
    "In my opinion, we should make a clear plan before learning online. "
    "For example, we can set a goal and finish it on time. Moreover, we should "
    "turn off our phones when we study.\n\n"
    "In conclusion, online learning is useful if we use it in a right way. "
    "Only in this way can we improve our efficiency."
)

BAD_ESSAY = (
    "i think online learning is good and i think it is good becuase it is good"
)

SOURCE_TEXT = (
    "随着互联网的发展，在线学习在大学生中越来越流行。"
    "然而，一些学生无法控制自己，把大量时间浪费在游戏上。"
    "因此，我们应该制定清晰的学习计划。"
)

USER_TRANSLATION = (
    "With the development of the Internet, online learning is becoming more "
    "and more popular among college students. However, some students cannot "
    "control themselves and waste a lot of time on games. Therefore, we should "
    "make a clear study plan."
)

REFERENCE_TRANSLATION = (
    "With the development of the Internet, online learning has become "
    "increasingly popular among college students. However, some students "
    "cannot control themselves and waste a lot of time on games. Therefore, "
    "we should make clear study plans."
)

GOOD_WRITING = {
    "content_score": 4.0,
    "language_score": 4.0,
    "organization_score": 3.5,
    "score": 11.5,
    "feedback": "内容切题，三段结构清晰；部分衔接词使用生硬，个别句子可更精炼。",
    "strengths": ["观点明确", "段落结构完整", "使用了连接词"],
    "improvements": ["减少模板化开头", "补充具体例证"],
    "sentence_notes": [
        {"index": 1,
         "sentence": "With the development of the Internet, online learning...",
         "issue": "模板化开头，缺乏具体场景",
         "type": "structure",
         "suggestion": "用具体现象或数据开篇"},
        {"index": 2,
         "sentence": "Only in this way can we improve our efficiency.",
         "issue": "only 倒装使用正确但与前句衔接生硬",
         "type": "structure",
         "suggestion": "加一个过渡词使逻辑更连贯"},
    ],
    "ai_traces": ["检测到模板句：with the development of"],
}

GOOD_TRANSLATION = {
    "accuracy_score": 6.5,
    "language_score": 6.0,
    "dimension_scale": 7.5,
    "score": 12.5,
    "feedback": "信息基本完整，第三句语气副词处理略生硬；个别搭配可更地道。",
    "issues": [
        {"segment": "越来越流行", "type": "word_choice",
         "detail": "more and more popular 略显口语化",
         "suggestion": "increasingly popular"},
        {"segment": "因此", "type": "grammar",
         "detail": "Therefore 后缺少逗号",
         "suggestion": "Therefore, we should ..."},
    ],
    "improved_translation": REFERENCE_TRANSLATION,
    "reference_translation": REFERENCE_TRANSLATION,
}


# ==================== 测试工具 ====================

def _fake_chat(payload):
    """返回固定 JSON 的假 chat"""
    calls = []

    async def chat(messages, **kwargs):
        calls.append(messages)
        return json.dumps(payload, ensure_ascii=False)

    chat.calls = calls
    return chat


def _fake_chat_sequence(outputs):
    """按调用顺序返回不同输出，模拟"第一次脏、修复重试成功" """
    calls = []

    async def chat(messages, **kwargs):
        index = min(len(calls), len(outputs) - 1)
        calls.append(messages)
        return outputs[index]

    chat.calls = calls
    return chat


def _raising_chat(message="所有LLM提供商均不可用"):
    async def chat(messages, **kwargs):
        raise RuntimeError(message)

    return chat


async def _prepare_db():
    await init_db()


async def _db_count(table: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
        return (await cursor.fetchone())[0]


async def _db_row(table: str, grade_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE grade_id = ?", (grade_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
    return dict(zip(columns, row))


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(grading_router)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")


# ==================== 写作：正常评分与落库 ====================

class TestWritingGrade:

    @pytest.mark.asyncio
    async def test_normal_grading_uses_llm_and_persists(self, monkeypatch):
        """正常评分：维度分 + 逐句批注 + 总分一致，且完整落库"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))

        result = await essay_grading.grade_writing("u1", TOPIC, ESSAY)

        assert result["score"] == 11.5
        assert result["dimensions"] == {
            "content": 4.0, "language": 4.0, "organization": 3.5}
        assert result["score"] == round(sum(result["dimensions"].values()), 1)
        assert result["score_710"] == round(11.5 / 15 * 106.5, 1)
        assert result["level_band"] == "11分档"
        assert result["status"] == "pending_review"
        assert result["word_count"] > 0
        assert result["sentence_notes"], "必须有逐句批注，不能空评分"
        note = result["sentence_notes"][0]
        assert {"index", "sentence", "issue", "type", "suggestion"} <= set(note)
        assert result["strengths"] and result["improvements"]
        assert result["model_meta"]["fallback"] is False
        assert result["model_meta"]["scored_by"] == "llm"
        assert result["model_meta"]["prompt_version"] == essay_grading.PROMPT_VERSION
        assert result["model_meta"]["duration_ms"] >= 0

        # 落库
        assert await _db_count("writing_grades") == 1
        row = await _db_row("writing_grades", result["grade_id"])
        assert row is not None
        assert row["score"] == 11.5
        assert row["score_710"] == result["score_710"]
        assert row["status"] == "pending_review"
        assert json.loads(row["sentence_notes"])
        assert json.loads(row["model_meta"])["fallback"] is False

    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_rules(self, monkeypatch):
        """LLM 抛错时规则兜底仍出分，并明确标记 fallback"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _raising_chat())

        result = await essay_grading.grade_writing("u2", TOPIC, BAD_ESSAY)

        meta = result["model_meta"]
        assert meta["fallback"] is True
        assert meta["scored_by"] == "rules"
        assert meta["provider"] == "rule_engine"
        assert "规则兜底评分" in result["feedback"]
        assert "字数" in result["feedback"], "兜底评语必须说明评分依据"
        assert 0 <= result["score"] <= 15
        assert result["score"] == round(sum(result["dimensions"].values()), 1)
        assert result["sentence_notes"], "规则兜底也要能指出具体句子问题"
        assert result["improvements"]
        assert result["score_710"] == round(result["score"] / 15 * 106.5, 1)

        row = await _db_row("writing_grades", result["grade_id"])
        assert row is not None
        stored_meta = json.loads(row["model_meta"])
        assert stored_meta["fallback"] is True
        assert stored_meta["error"]

    @pytest.mark.asyncio
    async def test_unparsable_output_retried_then_fallback(self, monkeypatch):
        """连续两次输出不可解析：重试一次后降级规则评分，不抛异常"""
        await _prepare_db()
        monkeypatch.setattr(
            llm_mod.llm_client, "chat",
            _fake_chat_sequence(["抱歉，我无法批改这篇作文。", "还是无法输出 JSON"]))

        result = await essay_grading.grade_writing("u3", TOPIC, ESSAY)

        meta = result["model_meta"]
        assert meta["fallback"] is True
        assert meta["attempts"] == 2, "解析失败必须有一次修复重试"
        assert "解析失败" in (meta["error"] or "")
        assert result["score"] > 0

    @pytest.mark.asyncio
    async def test_repair_retry_succeeds_on_second_attempt(self, monkeypatch):
        """第一次输出脏数据、第二次修复成功：不降级，记录 attempts=2"""
        await _prepare_db()
        monkeypatch.setattr(
            llm_mod.llm_client, "chat",
            _fake_chat_sequence([
                "这不是 JSON，抱歉。",
                json.dumps(GOOD_WRITING, ensure_ascii=False),
            ]))

        result = await essay_grading.grade_writing("u4", TOPIC, ESSAY)

        assert result["model_meta"]["fallback"] is False
        assert result["model_meta"]["attempts"] == 2
        assert result["score"] == 11.5

    @pytest.mark.asyncio
    async def test_dirty_json_repaired_and_scores_clamped(self, monkeypatch):
        """markdown 围栏 + 尾随逗号 + 维度越界 + 缺字段：修复、截断、补默认值"""
        await _prepare_db()
        dirty = (
            "这是评分结果：\n" + FENCE + "json\n"
            + '{"content_score": 7.5, "language_score": -1, '
              '"organization_score": 3.2, "score": 12, '
              '"feedback": "内容基本切题，但语法错误较多，需要加强从句练习。", '
              '"sentence_notes": [], "ai_traces": [], }\n'
            + FENCE + "\n希望对你有帮助"
        )
        monkeypatch.setattr(
            llm_mod.llm_client, "chat", _fake_chat_sequence([dirty]))

        result = await essay_grading.grade_writing("u5", TOPIC, ESSAY)
        meta = result["model_meta"]

        assert meta["fallback"] is False
        # 维度分截断
        assert result["dimensions"]["content"] == 5.0
        assert result["dimensions"]["language"] == 0.0
        assert result["dimensions"]["organization"] == 3.2
        # 总分以维度分为准（LLM 给的 12 不一致）
        assert result["score"] == round(5.0 + 0.0 + 3.2, 1)
        joined = " ".join(meta["warnings"])
        assert "截断" in joined
        assert "不一致" in joined
        assert "围栏" in joined or "尾随逗号" in joined
        # 缺失字段补默认值
        assert result["strengths"] == []
        assert result["improvements"] == []
        assert isinstance(result["sentence_notes"], list)

    @pytest.mark.asyncio
    async def test_dimension_overflow_clamped_to_full_score(self, monkeypatch):
        """三维度都超界：各自截断到 5，总分封顶 15，score_710 = 106.5"""
        await _prepare_db()
        payload = dict(GOOD_WRITING)
        payload.update({"content_score": 9, "language_score": 9,
                        "organization_score": 9, "score": 27})
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(payload))

        result = await essay_grading.grade_writing("u6", TOPIC, ESSAY)

        assert result["dimensions"] == {
            "content": 5.0, "language": 5.0, "organization": 5.0}
        assert result["score"] == 15.0
        assert result["score_710"] == 106.5
        assert result["level_band"] == "14分档"

    @pytest.mark.asyncio
    async def test_input_validation_rejects_empty_and_oversized(self):
        """空文本/空 user_id/超长文本/非法 level/非法 word_limit → ValueError"""
        with pytest.raises(ValueError):
            await essay_grading.grade_writing("u1", TOPIC, "   ")
        with pytest.raises(ValueError):
            await essay_grading.grade_writing("", TOPIC, ESSAY)
        with pytest.raises(ValueError):
            await essay_grading.grade_writing("u1", "", ESSAY)
        with pytest.raises(ValueError):
            await essay_grading.grade_writing("u1", TOPIC, "a " * 3000)
        with pytest.raises(ValueError):
            await essay_grading.grade_writing("u1", TOPIC, ESSAY, level="GRE")
        with pytest.raises(ValueError):
            await essay_grading.grade_writing(
                "u1", TOPIC, ESSAY, word_limit=5)


# ==================== 翻译：正常评分、漏译、脏 JSON ====================

class TestTranslationGrade:

    @pytest.mark.asyncio
    async def test_normal_grading_structure_and_persist(self, monkeypatch):
        """正常翻译批改：两维度、逐条问题、改进译文、折算、落库"""
        await _prepare_db()
        monkeypatch.setattr(
            llm_mod.llm_client, "chat", _fake_chat(GOOD_TRANSLATION))

        result = await essay_grading.grade_translation(
            "u1", SOURCE_TEXT, USER_TRANSLATION,
            reference_translation=REFERENCE_TRANSLATION)

        assert result["score"] == 12.5
        assert result["dimensions"] == {"accuracy": 6.5, "language": 6.0}
        assert result["score"] == round(sum(result["dimensions"].values()), 1)
        assert result["score_710"] == round(12.5 / 15 * 106.5, 1)
        assert result["status"] == "pending_review"
        assert isinstance(result["issues"], list) and result["issues"]
        for issue in result["issues"]:
            assert {"segment", "type", "detail", "suggestion"} <= set(issue)
        assert result["improved_translation"]
        assert result["reference_translation"] == REFERENCE_TRANSLATION
        assert result["model_meta"]["fallback"] is False

        assert await _db_count("translation_grades") == 1
        row = await _db_row("translation_grades", result["grade_id"])
        assert row is not None
        assert row["score"] == 12.5
        assert json.loads(row["issues"])

    @pytest.mark.asyncio
    async def test_fallback_detects_omission_in_issues(self, monkeypatch):
        """LLM 不可用 + 译文只译了 1/3：issues 必须出现 omission 漏译证据"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _raising_chat())
        partial_translation = (
            "With the development of the Internet, online learning is becoming "
            "more and more popular among college students.")

        result = await essay_grading.grade_translation(
            "u2", SOURCE_TEXT, partial_translation)

        assert result["model_meta"]["fallback"] is True
        assert "规则兜底评分" in result["feedback"]
        issue_types = {issue["type"] for issue in result["issues"]}
        assert "omission" in issue_types, "漏译必须出现在 issues 里"
        assert any("漏译" in issue["detail"] for issue in result["issues"])
        # 漏译应扣准确度，但不至于为 0
        assert 0 < result["dimensions"]["accuracy"] < 7.5
        assert result["score"] == round(
            result["dimensions"]["accuracy"] + result["dimensions"]["language"], 1)
        assert result["improved_translation"], "必须给出改进译文"
        assert result["score_710"] == round(result["score"] / 15 * 106.5, 1)

    @pytest.mark.asyncio
    async def test_dirty_json_scale_and_issue_type_normalized(self, monkeypatch):
        """围栏 + 尾随逗号 + 0-5 制维度分 + 中文问题类型：修复、折算、归一化"""
        await _prepare_db()
        payload = (
            "评分如下：\n" + FENCE + "json\n"
            + '{"accuracy_score": 5, "language_score": 4.5, '
              '"dimension_scale": 5, "score": 9.5, '
              '"feedback": "信息基本完整，个别搭配不地道，语序略生硬。", '
              '"issues": [{"segment": "越来越流行", "type": "用词", '
              '"detail": "表达口语化", "suggestion": "increasingly popular"}, '
              '{"segment": "因此", "type": "语法", "detail": "连接词后缺逗号", '
              '"suggestion": "Therefore,"}, ], '
              '"improved_translation": "", "reference_translation": "" }\n'
            + FENCE
        )
        monkeypatch.setattr(
            llm_mod.llm_client, "chat", _fake_chat_sequence([payload]))

        result = await essay_grading.grade_translation(
            "u3", SOURCE_TEXT, USER_TRANSLATION,
            reference_translation=REFERENCE_TRANSLATION)

        assert result["model_meta"]["fallback"] is False
        # 0-5 制 → 0-7.5 制
        assert result["dimensions"]["accuracy"] == 7.5
        assert result["dimensions"]["language"] == 6.75
        assert result["score"] == round(7.5 + 6.75, 1)
        joined = " ".join(result["model_meta"]["warnings"])
        assert "折算" in joined
        # 问题类型归一化到约定值
        issue_types = [issue["type"] for issue in result["issues"]]
        assert "word_choice" in issue_types
        assert "grammar" in issue_types
        # 空改进译文 + 有参考译文 → 用参考译文兜底
        assert result["improved_translation"] == REFERENCE_TRANSLATION

    @pytest.mark.asyncio
    async def test_translation_input_validation(self):
        """翻译输入校验：空源文/空译文/超长/非法 level → ValueError"""
        with pytest.raises(ValueError):
            await essay_grading.grade_translation("u1", "", USER_TRANSLATION)
        with pytest.raises(ValueError):
            await essay_grading.grade_translation("u1", SOURCE_TEXT, "  ")
        with pytest.raises(ValueError):
            await essay_grading.grade_translation(
                "u1", SOURCE_TEXT, "b " * 3000)
        with pytest.raises(ValueError):
            await essay_grading.grade_translation(
                "u1", SOURCE_TEXT, USER_TRANSLATION, level="TOEFL")


# ==================== 查询 / 人工复核 ====================

class TestQueryAndReview:

    @pytest.mark.asyncio
    async def test_list_and_get_grades(self, monkeypatch):
        """list_grades 支持 kind 过滤与 limit；get_grade 缺失返回 None"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))
        writing = await essay_grading.grade_writing("u1", TOPIC, ESSAY)
        await essay_grading.grade_writing("u1", TOPIC, ESSAY)

        monkeypatch.setattr(
            llm_mod.llm_client, "chat", _fake_chat(GOOD_TRANSLATION))
        translation = await essay_grading.grade_translation(
            "u1", SOURCE_TEXT, USER_TRANSLATION)
        # 其他用户的数据不应出现在 u1 的列表里
        await essay_grading.grade_writing("other", TOPIC, ESSAY)

        all_items = await essay_grading.list_grades("u1")
        assert len(all_items) == 3
        assert {item["kind"] for item in all_items} == {"writing", "translation"}
        assert all({"grade_id", "score", "score_710", "status",
                    "level_band", "created_at"} <= set(item)
                   for item in all_items)
        # 按时间倒序
        times = [item["created_at"] for item in all_items]
        assert times == sorted(times, reverse=True)

        writing_items = await essay_grading.list_grades("u1", kind="writing")
        assert len(writing_items) == 2
        translation_items = await essay_grading.list_grades(
            "u1", kind="translation")
        assert len(translation_items) == 1
        assert translation_items[0]["grade_id"] == translation["grade_id"]

        limited = await essay_grading.list_grades("u1", limit=1)
        assert len(limited) == 1

        detail = await essay_grading.get_grade(writing["grade_id"], "writing")
        assert detail is not None
        assert detail["essay"] == ESSAY
        assert detail["sentence_notes"]
        translation_detail = await essay_grading.get_grade(
            translation["grade_id"], "translation")
        assert translation_detail is not None
        assert translation_detail["issues"]

        assert await essay_grading.get_grade("not-exist", "writing") is None
        with pytest.raises(ValueError):
            await essay_grading.list_grades("u1", kind="listening")
        with pytest.raises(ValueError):
            await essay_grading.get_grade("x", "listening")

    @pytest.mark.asyncio
    async def test_review_approve_keeps_machine_score(self, monkeypatch):
        """approve：状态 approved，记 reviewer_note/reviewed_at，机评分不变"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))
        created = await essay_grading.grade_writing("u1", TOPIC, ESSAY)

        reviewed = await essay_grading.review_grade(
            created["grade_id"], "writing", "approve", note="机评合理")

        assert reviewed["status"] == "approved"
        assert reviewed["reviewer_note"] == "机评合理"
        assert reviewed["reviewed_at"] is not None
        assert reviewed["score"] == created["score"]
        assert reviewed["score_710"] == created["score_710"]

        row = await _db_row("writing_grades", created["grade_id"])
        assert row["status"] == "approved"
        assert row["reviewer_note"] == "机评合理"
        assert row["reviewed_at"] is not None

    @pytest.mark.asyncio
    async def test_review_override_updates_score_and_710(self, monkeypatch):
        """override：人工分覆盖机评分，同步更新 score_710/档位，并 clamp 到 0-15"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))
        created = await essay_grading.grade_writing("u1", TOPIC, ESSAY)

        overridden = await essay_grading.review_grade(
            created["grade_id"], "writing", "override", score=12,
            note="内容分给低了，人工上调")
        assert overridden["status"] == "overridden"
        assert overridden["score"] == 12.0
        assert overridden["score_710"] == round(12 / 15 * 106.5, 1)
        assert overridden["level_band"] == "11分档"
        assert overridden["reviewer_note"] == "内容分给低了，人工上调"

        # 上限 clamp：99 → 15，折算 106.5
        capped = await essay_grading.review_grade(
            created["grade_id"], "writing", "override", score=99)
        assert capped["score"] == 15.0
        assert capped["score_710"] == 106.5
        # 下限 clamp：-3 → 0
        floored = await essay_grading.review_grade(
            created["grade_id"], "writing", "override", score=-3)
        assert floored["score"] == 0.0
        assert floored["score_710"] == 0.0

        with pytest.raises(ValueError):
            await essay_grading.review_grade(
                created["grade_id"], "writing", "override")  # 缺 score
        with pytest.raises(ValueError):
            await essay_grading.review_grade(
                created["grade_id"], "writing", "reject")
        with pytest.raises(LookupError):
            await essay_grading.review_grade(
                "not-exist", "writing", "approve")


# ==================== HTTP 接口 ====================

class TestGradingAPI:

    @pytest.mark.asyncio
    async def test_writing_api_returns_400_for_bad_input(self):
        """接口层：空作文/超长作文/非法 level → 400 而非 500"""
        await _prepare_db()
        app = _make_app()
        async with _client(app) as client:
            empty = await client.post("/api/grade/writing", json={
                "user_id": "u1", "topic": TOPIC, "essay": "   "})
            assert empty.status_code == 400
            assert "essay" in empty.json()["detail"]

            too_long = await client.post("/api/grade/writing", json={
                "user_id": "u1", "topic": TOPIC, "essay": "a " * 3000})
            assert too_long.status_code == 400

            bad_level = await client.post("/api/grade/writing", json={
                "user_id": "u1", "topic": TOPIC, "essay": ESSAY,
                "level": "GRE"})
            assert bad_level.status_code == 400

    @pytest.mark.asyncio
    async def test_api_flow_writing_translation_records_detail(self, monkeypatch):
        """完整链路：写作/翻译批改 → 历史列表 → 详情 → 不存在 404"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))
        app = _make_app()
        async with _client(app) as client:
            writing_resp = await client.post("/api/grade/writing", json={
                "user_id": "u1", "topic": TOPIC, "essay": ESSAY})
            assert writing_resp.status_code == 200
            writing_data = writing_resp.json()
            assert writing_data["code"] == 0
            grade_id = writing_data["data"]["grade_id"]
            assert writing_data["data"]["score"] == 11.5

            monkeypatch.setattr(
                llm_mod.llm_client, "chat", _fake_chat(GOOD_TRANSLATION))
            translation_resp = await client.post("/api/grade/translation", json={
                "user_id": "u1", "source_text": SOURCE_TEXT,
                "user_translation": USER_TRANSLATION})
            assert translation_resp.status_code == 200
            translation_data = translation_resp.json()["data"]
            assert translation_data["score"] == 12.5

            records_resp = await client.get(
                "/api/grade/records", params={"user_id": "u1"})
            assert records_resp.status_code == 200
            records = records_resp.json()["data"]
            assert len(records) == 2

            detail_resp = await client.get(
                f"/api/grade/{grade_id}", params={"kind": "writing"})
            assert detail_resp.status_code == 200
            assert detail_resp.json()["data"]["essay"] == ESSAY

            missing_resp = await client.get(
                "/api/grade/not-exist", params={"kind": "writing"})
            assert missing_resp.status_code == 404

            bad_kind_resp = await client.get(
                f"/api/grade/{grade_id}", params={"kind": "listening"})
            assert bad_kind_resp.status_code == 400

    @pytest.mark.asyncio
    async def test_admin_endpoints_require_token(self, monkeypatch):
        """管理接口鉴权：无 token 401 / 错 token 401 / 未配置 token 503 / 正确 200"""
        await _prepare_db()
        monkeypatch.setattr(llm_mod.llm_client, "chat", _fake_chat(GOOD_WRITING))
        created = await essay_grading.grade_writing("u1", TOPIC, ESSAY)

        monkeypatch.setattr(settings, "ADMIN_TOKEN", "secret-admin-token")
        app = _make_app()
        async with _client(app) as client:
            # 未带 token
            assert (await client.get("/api/admin/grade/queue")).status_code == 401
            assert (await client.post(
                "/api/admin/grade/review",
                json={"grade_id": created["grade_id"], "kind": "writing",
                      "action": "approve"})).status_code == 401
            # token 错误
            assert (await client.get(
                "/api/admin/grade/queue",
                headers={"X-Admin-Token": "wrong"})).status_code == 401

            # 正确 token：队列能看到 pending_review
            ok_headers = {"X-Admin-Token": "secret-admin-token"}
            queue_resp = await client.get(
                "/api/admin/grade/queue", headers=ok_headers)
            assert queue_resp.status_code == 200
            queue = queue_resp.json()["data"]
            assert [item["grade_id"] for item in queue] == [created["grade_id"]]

            review_resp = await client.post(
                "/api/admin/grade/review",
                headers=ok_headers,
                json={"grade_id": created["grade_id"], "kind": "writing",
                      "action": "override", "score": 13.5,
                      "note": "人工复核上调"})
            assert review_resp.status_code == 200
            reviewed = review_resp.json()["data"]
            assert reviewed["status"] == "overridden"
            assert reviewed["score"] == 13.5
            assert reviewed["score_710"] == round(13.5 / 15 * 106.5, 1)

        # 服务端未配置 ADMIN_TOKEN → 503，避免接口裸奔
        monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
        async with _client(app) as client:
            assert (await client.get(
                "/api/admin/grade/queue",
                headers={"X-Admin-Token": "anything"})).status_code == 503

    @pytest.mark.asyncio
    async def test_api_unexpected_error_returns_500(self, monkeypatch):
        """未预期异常 → 500（而不是抛到 FastAPI 默认的 500 页面/崩溃）"""
        await _prepare_db()

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated internal error")

        monkeypatch.setattr(essay_grading, "grade_writing", boom)
        app = _make_app()
        async with _client(app) as client:
            resp = await client.post("/api/grade/writing", json={
                "user_id": "u1", "topic": TOPIC, "essay": ESSAY})
            assert resp.status_code == 500
            assert "失败" in resp.json()["detail"]
