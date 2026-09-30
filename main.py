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
from models.database import init_db, DB_PATH
from models.seed_data import seed_if_empty
from models.schemas import (TranslateRequest, ASRRequest, ContentGenRequest)
from services.llm_client import llm_client
from services.translate_service import translate_service
from services.asr_service import asr_service
from core.monitor import metrics

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_if_empty()
    logger.info("数据库初始化完成")
    yield
    await llm_client.close()


app = FastAPI(title="CET AI Agent", lifespan=lifespan)

orchestrator = Orchestrator()
content_agent = ContentAgent()
research_agent = ResearchAgent()


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


# ==================== 基础接口 ====================
@app.get("/api/health")
async def health():
    return {"status": "ok", "max_concurrent": settings.MAX_CONCURRENT_AGENTS}


@app.post("/api/exam/generate")
async def generate_exam(req: ExamRequest):
    try:
        result = await orchestrator.generate_exam(req.model_dump())
        return {"code": 0, "data": result}
    except Exception as e:
        logger.error(f"出题失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/exam/generate-from-wrong")
async def generate_from_wrong(req: WrongExamRequest):
    try:
        result = await orchestrator.generate_from_wrong(req.user_id, req.num)
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/exam/evaluate")
async def evaluate(req: EvaluateRequest):
    try:
        answers = [a.model_dump() for a in req.answers]
        result = await orchestrator.evaluate_and_review(req.user_id, answers)
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 真题演练 ====================
@app.post("/api/exam/paper/start")
async def paper_start(req: PaperStartRequest):
    """开始一套真题演练"""
    session_id = str(uuid.uuid4())
    # 四级 125 分钟，六级 130 分钟
    duration = 125 * 60 if req.level == "cet4" else 130 * 60

    async with aiosqlite.connect(DB_PATH) as db:
        # 检查是否有未完成的会话
        cursor = await db.execute("""
            SELECT session_id FROM exam_sessions
            WHERE user_id=? AND status='in_progress'
        """, (req.user_id,))
        existing = await cursor.fetchone()
        if existing:
            # 把旧的未完成会话标记为超时
            await db.execute(
                "UPDATE exam_sessions SET status='timeout' WHERE session_id=?",
                (existing[0],)
            )

        await db.execute("""
            INSERT INTO exam_sessions
            (session_id, user_id, level, year, month, set_number,
             duration_seconds, started_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
        """, (
            session_id, req.user_id, req.level, req.year, req.month,
            req.set_number, duration, time.time(), time.time()
        ))
        await db.commit()

    return {
        "code": 0,
        "data": {
            "session_id": session_id,
            "duration_seconds": duration,
            "started_at": time.time(),
        }
    }


@app.post("/api/exam/paper/submit")
async def paper_submit(req: PaperSubmitRequest):
    """提交真题演练"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, started_at, duration_seconds FROM exam_sessions WHERE session_id=?",
            (req.session_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="会话不存在")

        user_id, started_at, duration = row
        elapsed = time.time() - started_at
        status = "timeout" if elapsed >= duration else "completed"

        await db.execute("""
            UPDATE exam_sessions SET
                ended_at=?, status=?, answers=?, score=?
            WHERE session_id=?
        """, (time.time(), status, json.dumps(req.answers, ensure_ascii=False),
              0, req.session_id))
        await db.commit()

    return {"code": 0, "data": {"session_id": req.session_id,
                                 "status": status, "elapsed": elapsed}}


@app.get("/api/exam/paper/session/{session_id}")
async def paper_session(session_id: str):
    """查询会话状态"""
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


# ==================== TTS / 语音识别 ====================
@app.post("/api/tts")
async def tts(req: TTSRequest):
    """返回TTS配置（前端调用浏览器API播放）"""
    return {
        "code": 0,
        "data": {
            "text": req.text, "lang": req.lang, "rate": req.rate,
            "method": "browser_tts"
        }
    }


# ==================== 单词接口 ====================
@app.get("/api/vocab/random")
async def random_vocab(level: str = "CET4", category: str = "",
                        count: int = 10):
    """随机获取词汇，支持按分类筛选"""
    sql = "SELECT word, phonetic, meaning, level, topic, category FROM vocabulary WHERE level=?"
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
    """获取词汇分类统计"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT category, COUNT(*) FROM vocabulary GROUP BY category"
        )
        rows = await cursor.fetchall()
    return {"categories": [
        {"name": r[0] or "高频", "count": r[1]} for r in rows
    ]}


# ==================== 翻译 / ASR / 内容生成 / 检索 ====================
@app.post("/api/translate")
async def translate(req: TranslateRequest):
    return await translate_service.translate(req.text, req.source_lang, req.target_lang)


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
            task_id=f"content_{int(time.time())}"
        )
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/research")
async def research(query: str):
    try:
        result = await research_agent.execute(
            {"query": query}, task_id=f"research_{int(time.time())}"
        )
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 监控 / 用户统计 / 错题 ====================
@app.get("/api/metrics")
async def get_metrics():
    return metrics.snapshot()


@app.get("/api/user/{user_id}/stats")
async def user_stats(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), SUM(is_correct) FROM answer_records WHERE user_id=?",
            (user_id,))
        total, correct = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM wrong_questions WHERE user_id=? AND resolved=0",
            (user_id,))
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
            "SELECT question_id, content, answer, source FROM questions WHERE verified=0 LIMIT 100"
        )
        rows = await cursor.fetchall()
    return {"items": [{"question_id": r[0], "content": r[1],
                        "answer": r[2], "source": r[3]} for r in rows]}


@app.post("/api/admin/update-answer")
async def update_answer(req: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET answer=? WHERE question_id=?",
            (req["answer"], req["question_id"]))
        await db.commit()
    return {"code": 0}


@app.post("/api/admin/verify")
async def verify_question(req: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE questions SET verified=1 WHERE question_id=?",
            (req["question_id"],))
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