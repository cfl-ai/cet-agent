import asyncio
import logging
import time
import json
import uuid
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File
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
from models.database import init_db, DB_PATH
from models.seed_data import seed_if_empty
from models.schemas import (TranslateRequest, ASRRequest, ContentGenRequest)
from services.llm_client import llm_client
from services.translate_service import translate_service
from services.asr_service import asr_service
from services.vector_service import vector_service
from services.tts_service import tts_service, AUDIO_CACHE_DIR
from core.monitor import metrics
from core.telemetry import setup_telemetry, is_enabled as telemetry_enabled

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ENABLE_TRACING:
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

    yield
    await llm_client.close()


app = FastAPI(title="CET AI Agent", lifespan=lifespan)

orchestrator = Orchestrator()
content_agent = ContentAgent()
research_agent = ResearchAgent()
study_plan_agent = StudyPlanAgent()


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
    target_score: int = 425
    days_left: int = 60
    level: str = "CET4"


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
        result = await orchestrator.evaluate_and_review(req.user_id, answers)
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"评估失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 学情预测与学习方案 ====================
@app.post("/api/study/predict-plan")
async def study_predict_plan(req: StudyPlanRequest):
    """基于学情数据生成个性化学习方案（LLM 生成，可能耗时 10-20 秒）"""
    try:
        result = await study_plan_agent.execute(
            req.model_dump(),
            task_id=f"study_plan_{req.user_id}_{int(time.time())}",
        )
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"学习方案生成失败: {e}")
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

        await db.execute("""
            UPDATE exam_sessions SET ended_at=?, status=?, answers=?, score=?
            WHERE session_id=?
        """, (time.time(), status,
              json.dumps(req.answers, ensure_ascii=False),
              0, req.session_id))
        await db.commit()

    return {"code": 0, "data": {
        "session_id": req.session_id, "status": status, "elapsed": elapsed,
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
async def upload_files(files: list[UploadFile] = File(...)):
    upload_dir = Path("data/raw/real_papers")
    upload_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        file_path = upload_dir / f.filename
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
    import subprocess
    subprocess.Popen([sys.executable, "scripts/import_real_papers.py"])
    return {"code": 0, "message": f"{len(files)} 个文件已上传"}


@app.get("/api/admin/unverified")
async def get_unverified():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT question_id, content, answer, source "
            "FROM questions WHERE verified=0 LIMIT 100"
        )
        rows = await cursor.fetchall()
    return {"items": [{"question_id": r[0], "content": r[1],
                        "answer": r[2], "source": r[3]} for r in rows]}


@app.post("/api/admin/update-answer")
async def update_answer(req: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET answer=? WHERE question_id=?",
            (req["answer"], req["question_id"]),
        )
        await db.commit()
    return {"code": 0}


@app.post("/api/admin/verify")
async def verify_question(req: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET verified=1 WHERE question_id=?",
            (req["question_id"],),
        )
        await db.commit()
    return {"code": 0}


# ==================== 静态文件 ====================
@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT,
                workers=1, limit_concurrency=10)