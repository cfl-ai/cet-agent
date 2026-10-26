import asyncio
import logging
import time
import json
import uuid
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import shutil
import sys

from config import settings
from agents.orchestrator import Orchestrator
from agents.content_agent import ContentAgent
from agents.research_agent import ResearchAgent
from agents.graph_orchestrator import get_graph, is_enabled as graph_enabled
from agents.study_plan_agent import StudyPlanAgent
from agents.grading_agent import GradingAgent
from models.database import init_db, DB_PATH
from models.seed_data import seed_if_empty
from models.schemas import (TranslateRequest, ASRRequest, ContentGenRequest)
from services.llm_client import llm_client
from services.translate_service import translate_service
from services.asr_service import asr_service
from services.vector_service import vector_service
from services.tts_service import tts_service, AUDIO_CACHE_DIR
from services import user_service
from services import analytics_service
from services import learning_service
from core.monitor import metrics
from core.telemetry import setup_telemetry, is_enabled as telemetry_enabled

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★ 用 getattr 兜底，避免 config 缺字段导致服务崩溃
    if getattr(settings, "ENABLE_TRACING", True):
        try:
            setup_telemetry(app)
        except Exception as e:
            logger.warning(f"Telemetry 初始化失败: {e}")
    await init_db()
    await seed_if_empty()

    try:
        removed = tts_service.cleanup_cache()
        if removed:
            logger.info(f"[TTS] 清理缓存 {removed} 个文件")
    except Exception:
        pass

    logger.info(
        f"服务启动 | 追踪: {telemetry_enabled()} | "
        f"向量: {vector_service.enabled} | "
        f"智能规划: {graph_enabled()} | "
        f"服务端TTS: {tts_service.available}"
    )

    # 启动时检查一次：若已有足够真实分数样本而模型停留在旧版本，自动补训
    learning_service.schedule_training("startup")

    yield
    await llm_client.close()


app = FastAPI(title="CET AI Agent", lifespan=lifespan)

orchestrator = Orchestrator()
content_agent = ContentAgent()
research_agent = ResearchAgent()
study_plan_agent = StudyPlanAgent()


# ==================== 管理接口鉴权 ====================
async def require_admin(x_admin_token: str = Header(default=None)):
    """
    管理接口统一鉴权。
    - 服务端未配置 ADMIN_TOKEN 时直接拒绝，避免接口裸奔
    - 令牌不匹配返回 401
    """
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="服务端未配置 ADMIN_TOKEN，管理接口已禁用"
        )
    if x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="管理接口鉴权失败")


# 上传白名单：只接受文档与文本，防止上传可执行文件或脚本
ALLOWED_UPLOAD_EXT = {".docx", ".doc", ".txt", ".pdf", ".md"}


def _safe_filename(raw: str) -> str:
    """
    清洗上传文件名，防路径穿越。
    仅取 basename（剥离 ../、绝对路径等），并拒绝空名与保留名。
    """
    name = Path(raw).name.strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail=f"非法文件名: {raw!r}")
    if Path(name).suffix.lower() not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不允许的文件类型: {Path(name).suffix or '(无扩展名)'}"
        )
    return name


# ==================== 请求模型 ====================
class ExamRequest(BaseModel):
    num_questions: int = 1
    question_type: str = "reading_careful"
    knowledge_point: str = ""
    difficulty: int = 3
    topic: str = ""


class AnswerItem(BaseModel):
    question_id: str
    user_answer: str
    # 答题耗时（秒）。此前缺失该字段，Pydantic 会静默丢弃前端传入值，
    # 导致答题时长永远记为 0，无法分析答题速度与识别秒选猜答。
    duration: float = 0.0


class EvaluateRequest(BaseModel):
    user_id: str
    answers: list[AnswerItem]


class WrongExamRequest(BaseModel):
    user_id: str
    num: int = 3


class AgentRunRequest(BaseModel):
    user_request: str
    user_id: str = "anonymous"
    max_iterations: int = 3


class PaperStartRequest(BaseModel):
    user_id: str
    level: str = "cet4"
    year: int = 2024
    month: int = 6
    set_number: int = 1


class PaperSubmitRequest(BaseModel):
    session_id: str
    answers: dict


class TTSRequest(BaseModel):
    text: str
    lang: str = "en-US"
    rate: float = 0.9


class StudyPlanRequest(BaseModel):
    user_id: str
    # None 表示未指定，此时从用户档案读取（实现跨设备长期保存）
    target_score: int | None = None
    days_left: int = 60
    level: str | None = None


class VocabReviewRequest(BaseModel):
    """单词复习上报 — 此前 words_reviewed 从未被写入"""
    user_id: str
    count: int = 1


class ExamScoreRequest(BaseModel):
    """真实考试成绩录入 — 预测模型的监督信号（自举标签不会让模型变准）

    user_id 取自路径参数，body 中可不传（保持与其他 /api/user/{id}/* 接口一致）。
    """
    user_id: str | None = None
    score: int = 425
    level: str = "CET4"
    exam_date: str | None = None


# ==================== 模考折算 ====================
# 四六级各部分的标准分权重（听力/阅读各35%，写作/翻译各15%）
SECTION_WEIGHTS = {"听力": 248.5, "阅读": 248.5, "翻译": 106.5, "写作": 106.5}
# 主观题给分基准：写了答案按满分的65%折算（卷面中位水平），空白0分
SUBJECTIVE_BASELINE = 0.65
SCORE_FLOOR = 220


def _section_of(qtype: str) -> str | None:
    for key in SECTION_WEIGHTS:
        if key in (qtype or ""):
            return key
    return None


def _estimate_paper_score(answers: dict, qmap: dict,
                          subjective_types: set) -> int:
    """
    把模考作答折算成标准分。
    此前 paper/submit 恒写 score=0，等于白白丢掉一个真实监督信号源。
    未作答的部分不计分（如实反映发挥），总分保底 220（四六级报道分下限）。
    """
    stats: dict[str, list] = {}
    for qid, user_ans in (answers or {}).items():
        info = qmap.get(qid)
        if not info:
            continue
        qtype, correct_ans = info
        section = _section_of(qtype) or "阅读"
        answered = str(user_ans or "").strip()

        if qtype in subjective_types:
            correct = SUBJECTIVE_BASELINE if answered else 0.0
        elif not correct_ans:
            continue
        else:
            correct = (1.0 if answered.upper() == str(correct_ans).strip().upper()
                       else 0.0)

        total, got = stats.get(section, [0, 0.0])
        stats[section] = [total + 1, got + correct]

    score = sum((got / total) * SECTION_WEIGHTS[section]
                for section, (total, got) in stats.items() if total)
    return max(SCORE_FLOOR, min(710, int(round(score))))


# ==================== 工具函数 ====================
async def save_questions_to_db(questions: list[dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        for q in questions:
            qid = f"ai_{uuid.uuid4().hex[:12]}"
            q["question_id"] = qid

            options = q.get("options") or []
            await db.execute("""
                INSERT OR IGNORE INTO questions
                (question_id, question_type, topic, difficulty,
                 content, options, answer, explanation,
                 knowledge_tags, source, verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                qid,
                q.get("question_type", "综合训练"),
                q.get("topic", "通用"),
                q.get("difficulty", 3),
                q.get("question", ""),
                json.dumps(options, ensure_ascii=False),
                q.get("answer", "A"),
                q.get("explanation", ""),
                json.dumps(q.get("knowledge_tags", []), ensure_ascii=False),
                "ai_generated",
                1,
                time.time(),
            ))
        await db.commit()


# ==================== 基础接口 ====================
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "features": {
            "tracing": telemetry_enabled(),
            "vector": vector_service.enabled,
            "graph": graph_enabled(),
            "server_tts": tts_service.available,
        },
        "vector_count": vector_service.count(),
        "max_concurrent": settings.MAX_CONCURRENT_AGENTS,
    }


@app.post("/api/exam/generate")
async def generate_exam(req: ExamRequest):
    try:
        result = await orchestrator.generate_exam(req.model_dump())
        await save_questions_to_db(result["questions"])
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"出题失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/exam/generate-from-wrong")
async def generate_from_wrong(req: WrongExamRequest):
    try:
        result = await orchestrator.generate_from_wrong(req.user_id, req.num)
        await save_questions_to_db(result["questions"])
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"错题变式题生成失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/exam/evaluate")
async def evaluate(req: EvaluateRequest):
    try:
        answers = [a.model_dump() for a in req.answers]
        # 确保用户档案存在并刷新活跃时间，使学习数据有长期归属
        await user_service.touch(req.user_id)
        result = await orchestrator.evaluate_and_review(req.user_id, answers)
        # 每次答题都可能让样本达到触发阈值，交给后台判断是否该重训
        learning_service.schedule_training("answer_graded")
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"评估失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 学情预测与学习方案 ====================
@app.post("/api/study/predict-plan")
async def study_predict_plan(req: StudyPlanRequest):
    """基于学情数据生成个性化学习方案（LLM 生成，可能耗时 10-20 秒）"""
    try:
        # 未显式指定的 level / target_score，回退到用户档案中的长期设置
        profile = await user_service.get_profile(req.user_id)
        level = req.level or (profile or {}).get("level") or "CET4"
        target_score = (
            req.target_score
            or (profile or {}).get("target_score")
            or 425
        )

        task = {
            "user_id": req.user_id,
            "days_left": req.days_left,
            "level": level,
            "target_score": target_score,
        }
        result = await study_plan_agent.execute(
            task,
            task_id=f"study_plan_{req.user_id}_{int(time.time())}",
        )
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"学习方案生成失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 用户档案 ====================
@app.get("/api/user/{user_id}/profile")
async def get_user_profile(user_id: str):
    """
    读取个人档案与累计学习统计。
    首次访问自动建档（默认 CET4 / 425 分），保证后续学习数据有归属。
    """
    try:
        return {"code": 0, "data": await user_service.get_study_overview(user_id)}
    except Exception as e:
        logger.error(f"读取用户档案失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/user/{user_id}/profile")
async def update_user_profile(user_id: str, req: dict):
    """
    创建或更新个人档案。仅更新显式提供的字段。
    level 支持 CET4/CET6；target_score 会被收敛到 220-710 合理区间。
    """
    try:
        profile = await user_service.upsert_profile(
            user_id,
            username=req.get("username"),
            level=req.get("level"),
            target_score=req.get("target_score"),
        )
        return {"code": 0, "data": profile}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"更新用户档案失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 学习分析（趋势/薄弱点/打卡/模考历史） ====================
@app.get("/api/user/{user_id}/analytics")
async def user_analytics(user_id: str, days: int = 30):
    """
    学情仪表盘：档案总览 + 每日趋势 + 薄弱知识点 + 连续打卡 + 模考历史。
    days 为趋势回溯天数，收敛到 1-90。
    """
    try:
        return {"code": 0,
                "data": await analytics_service.get_dashboard(user_id, days)}
    except Exception as e:
        logger.error(f"学习分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/{user_id}/weak-points")
async def user_weak_points(user_id: str, limit: int = 5):
    """薄弱知识点排名：掌握度升序，附未解决错题数"""
    try:
        limit = max(1, min(20, limit))
        return {"code": 0,
                "data": await analytics_service.get_weak_points(user_id, limit)}
    except Exception as e:
        logger.error(f"薄弱点查询失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/{user_id}/streak")
async def user_streak(user_id: str):
    """连续打卡统计（当前连续 / 历史最长 / 累计学习天数）"""
    try:
        return {"code": 0, "data": await analytics_service.get_streak(user_id)}
    except Exception as e:
        logger.error(f"打卡统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/{user_id}/exams")
async def user_exams(user_id: str, limit: int = 20):
    """真题模考历史（含真实用时与状态）"""
    try:
        limit = max(1, min(100, limit))
        return {"code": 0,
                "data": await analytics_service.get_exam_history(user_id, limit)}
    except Exception as e:
        logger.error(f"模考历史查询失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/user/{user_id}/exam-score")
async def record_exam_score(user_id: str, req: ExamScoreRequest):
    """
    录入真实考试成绩 — 这是预测模型最有价值的监督信号。

    之前的训练标签是由掌握度自举推导的假标签，模型只是在复述公式；
    只有真实分数才能让预测真的越来越准。分数收敛到 0-710 后入库，
    并异步触发训练检查（够样本就会自动训练并择优晋升）。
    """
    try:
        data = await learning_service.record_true_score(
            user_id, req.score, level=req.level, exam_date=req.exam_date)
        learning_service.schedule_training("real_exam_score")
        return {"code": 0, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"考试成绩录入失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 智能规划入口 ====================
@app.post("/api/agent/run")
async def agent_run(req: AgentRunRequest):
    if not graph_enabled():
        raise HTTPException(
            status_code=503,
            detail="LangGraph 未安装。请执行: pip install langgraph langchain-core",
        )

    graph = get_graph(orchestrator, llm_client)
    if graph is None:
        raise HTTPException(status_code=503, detail="图初始化失败")

    try:
        initial_state = {
            "user_request": req.user_request,
            "params": {"user_id": req.user_id},
            "messages": [],
            "iterations": 0,
            "max_iterations": req.max_iterations,
            "result": {},
        }
        final = await graph.ainvoke(initial_state)

        return {
            "code": 0,
            "data": {
                "intent": final.get("intent"),
                "messages": final.get("messages", []),
                "result": final.get("result", {}),
                "iterations": final.get("iterations", 0),
            },
        }
    except Exception as e:
        logger.error(f"智能规划失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 向量库管理 ====================
@app.get("/api/vector/stats")
async def vector_stats():
    return {
        "code": 0,
        "data": {
            "enabled": vector_service.enabled,
            "count": vector_service.count(),
        },
    }


@app.post("/api/vector/similar")
async def vector_similar(req: dict):
    query = req.get("query", "")
    k = req.get("k", 5)
    if not query:
        raise HTTPException(status_code=400, detail="缺少 query 参数")
    results = vector_service.find_similar(query, k=k)
    return {"code": 0, "data": {"results": results}}


# ==================== 真题演练 ====================
@app.post("/api/exam/paper/start")
async def paper_start(req: PaperStartRequest):
    session_id = str(uuid.uuid4())
    duration = 125 * 60 if req.level == "cet4" else 130 * 60

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_id FROM exam_sessions "
            "WHERE user_id=? AND status='in_progress'",
            (req.user_id,),
        )
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                "UPDATE exam_sessions SET status='timeout' WHERE session_id=?",
                (existing[0],),
            )
        await db.execute("""
            INSERT INTO exam_sessions
            (session_id, user_id, level, year, month, set_number,
             duration_seconds, started_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
        """, (
            session_id, req.user_id, req.level, req.year, req.month,
            req.set_number, duration, time.time(), time.time(),
        ))
        await db.commit()

    return {"code": 0, "data": {
        "session_id": session_id, "duration_seconds": duration,
        "started_at": time.time(),
    }}


@app.post("/api/exam/paper/submit")
async def paper_submit(req: PaperSubmitRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, started_at, duration_seconds "
            "FROM exam_sessions WHERE session_id=?",
            (req.session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="会话不存在")

        user_id, started_at, duration = row
        elapsed = time.time() - started_at
        status = "timeout" if elapsed >= duration else "completed"

        # 折算真实得分。此前恒为 0，白白丢掉一个真实监督信号源
        qmap = {}
        qids = list((req.answers or {}).keys())
        if qids:
            placeholders = ",".join("?" * len(qids))
            cursor = await db.execute(
                f"SELECT question_id, question_type, answer FROM questions "
                f"WHERE question_id IN ({placeholders})", qids)
            qmap = {r[0]: (r[1] or "", r[2] or "") for r in await cursor.fetchall()}
        score = _estimate_paper_score(
            req.answers, qmap, GradingAgent.SUBJECTIVE_TYPES)

        await db.execute("""
            UPDATE exam_sessions SET ended_at=?, status=?, answers=?, score=?
            WHERE session_id=?
        """, (time.time(), status,
              json.dumps(req.answers, ensure_ascii=False),
              score, req.session_id))
        await db.commit()

    # 新增了一个真实分数标签，可能够触发下一轮在线训练
    learning_service.schedule_training("paper_submit")

    return {"code": 0, "data": {
        "session_id": req.session_id, "status": status, "elapsed": elapsed,
        "score": score,
    }}


@app.get("/api/exam/paper/session/{session_id}")
async def paper_session(session_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT session_id, user_id, level, year, month, set_number,
                   duration_seconds, started_at, ended_at, status
            FROM exam_sessions WHERE session_id=?
        """, (session_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"code": 0, "data": {
        "session_id": row[0], "user_id": row[1], "level": row[2],
        "year": row[3], "month": row[4], "set_number": row[5],
        "duration_seconds": row[6], "started_at": row[7],
        "ended_at": row[8], "status": row[9],
    }}


# ==================== TTS ====================
@app.post("/api/tts")
async def tts_config(req: TTSRequest):
    return {"code": 0, "data": {
        "text": req.text, "lang": req.lang, "rate": req.rate,
        "server_tts_available": tts_service.available,
    }}


@app.post("/api/tts/audio")
async def tts_audio(req: TTSRequest):
    result = await tts_service.generate(req.text, req.lang)
    return {
        "code": 0 if result.get("success") else 1,
        "data": result,
    }


@app.get("/api/tts/audio/{filename}")
async def get_audio_file(filename: str):
    import re
    if not re.match(r"^[a-zA-Z0-9_\-]+\.mp3$", filename):
        raise HTTPException(status_code=400, detail="非法文件名")

    file_path = AUDIO_CACHE_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="音频不存在")

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Accept-Ranges": "bytes",
        },
    )


# ==================== 单词 ====================
@app.get("/api/vocab/random")
async def random_vocab(level: str = "CET4", category: str = "",
                        count: int = 10):
    sql = ("SELECT word, phonetic, meaning, level, topic, category "
           "FROM vocabulary WHERE level=?")
    params = [level]
    if category:
        sql += " AND category=?"
        params.append(category)
    sql += " ORDER BY RANDOM() LIMIT ?"
    params.append(count)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
    return {"words": [
        {"word": r[0], "phonetic": r[1], "meaning": r[2],
         "level": r[3], "topic": r[4], "category": r[5] or "高频"}
        for r in rows
    ]}


@app.get("/api/vocab/categories")
async def vocab_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT category, COUNT(*) FROM vocabulary GROUP BY category"
        )
        rows = await cursor.fetchall()
    return {"categories": [{"name": r[0] or "高频", "count": r[1]} for r in rows]}


@app.post("/api/vocab/review")
async def vocab_review(req: VocabReviewRequest):
    """
    上报单词复习量，累加到当日 study_stats.words_reviewed。
    此前该字段从未被写入，词汇学习在统计上是黑洞。
    """
    try:
        data = await analytics_service.record_words_reviewed(
            req.user_id, req.count)
        return {"code": 0, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"词汇复习记录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 翻译/ASR/内容/检索 ====================
@app.post("/api/translate")
async def translate(req: TranslateRequest):
    return await translate_service.translate(
        req.text, req.source_lang, req.target_lang)


@app.post("/api/translate/cet")
async def translate_cet(req: TranslateRequest):
    return await translate_service.translate_cet(req.text)


@app.post("/api/asr")
async def asr(req: ASRRequest):
    return await asr_service.transcribe(req.audio_base64, req.language)


@app.post("/api/content/generate")
async def content_gen(req: ContentGenRequest):
    try:
        result = await content_agent.execute(
            {"prompt": req.prompt, "content_type": req.content_type},
            task_id=f"content_{int(time.time())}",
        )
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/research")
async def research(query: str):
    try:
        result = await research_agent.execute(
            {"query": query}, task_id=f"research_{int(time.time())}",
        )
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 监控/统计/错题 ====================
@app.get("/api/metrics")
async def get_metrics():
    return metrics.snapshot()


@app.get("/api/user/{user_id}/stats")
async def user_stats(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), SUM(is_correct) FROM answer_records WHERE user_id=?",
            (user_id,),
        )
        total, correct = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND resolved=0",
            (user_id,),
        )
        (wrong,) = await cursor.fetchone()
    return {
        "total_questions": total or 0,
        "correct": correct or 0,
        "accuracy": round((correct or 0) / total, 3) if total else 0,
        "unresolved_wrong": wrong or 0,
    }


@app.get("/api/wrong/{user_id}")
async def get_wrong(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT w.question_id, w.wrong_count, w.error_type, w.last_wrong_at,
                   q.content, q.options, q.answer, q.explanation, q.knowledge_tags
            FROM wrong_questions w
            JOIN questions q ON w.question_id = q.question_id
            WHERE w.user_id=? AND w.resolved=0
            ORDER BY w.last_wrong_at DESC LIMIT 50
        """, (user_id,))
        rows = await cursor.fetchall()
    items = [{
        "question_id": r[0], "wrong_count": r[1], "error_type": r[2],
        "last_wrong_at": r[3], "content": r[4],
        "options": json.loads(r[5]) if r[5] else [],
        "answer": r[6], "explanation": r[7],
        "knowledge_tags": json.loads(r[8]) if r[8] else [],
    } for r in rows]
    return {"items": items, "total": len(items)}


# ==================== 管理接口 ====================
@app.post("/api/admin/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    _: None = Depends(require_admin),
):
    upload_dir = Path("data/raw/real_papers")
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved, skipped = [], []
    for f in files:
        # 路径穿越防护：剥离目录成分，只保留纯文件名
        safe_name = _safe_filename(f.filename or "")
        file_path = upload_dir / safe_name
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
        saved.append(safe_name)

    # 受控触发导入：仅在鉴权通过后执行，且不继承请求上下文
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, "scripts/import_real_papers.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import_started = True
    except Exception as e:
        logger.warning(f"导入脚本启动失败: {e}")
        import_started = False

    return {
        "code": 0,
        "message": f"{len(saved)} 个文件已上传",
        "saved": saved,
        "import_started": import_started,
    }


@app.get("/api/admin/unverified")
async def get_unverified(_: None = Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT question_id, content, answer, source "
            "FROM questions WHERE verified=0 LIMIT 100"
        )
        rows = await cursor.fetchall()
    return {"items": [{"question_id": r[0], "content": r[1],
                        "answer": r[2], "source": r[3]} for r in rows]}


@app.post("/api/admin/update-answer")
async def update_answer(req: dict, _: None = Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET answer=? WHERE question_id=?",
            (req["answer"], req["question_id"]),
        )
        await db.commit()
    return {"code": 0}


@app.post("/api/admin/verify")
async def verify_question(req: dict, _: None = Depends(require_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET verified=1 WHERE question_id=?",
            (req["question_id"],),
        )
        await db.commit()
    return {"code": 0}


@app.get("/api/admin/model/status")
async def model_status(_: None = Depends(require_admin)):
    """在线学习状态：当前模型版本、样本数、误差、训练历史"""
    return {"code": 0, "data": await learning_service.get_model_status()}


@app.post("/api/admin/model/train")
async def model_train(_: None = Depends(require_admin)):
    """手动触发一次训练（忽略"样本是否增长"的判断，但仍走择优晋升）"""
    try:
        return {"code": 0, "data": await learning_service.train_if_needed(
            force=True)}
    except Exception as e:
        logger.error(f"手动训练失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 静态文件 ====================
@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT,
                workers=1, limit_concurrency=10)