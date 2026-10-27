"""写作 / 翻译批改路由。

接口清单：
  POST /api/grade/writing             写作批改（LLM 真实判分，失败规则兜底）
  POST /api/grade/translation         翻译批改
  GET  /api/grade/records             批改历史（摘要）
  GET  /api/grade/{grade_id}          单条批改详情
  GET  /api/admin/grade/queue         人工复核队列（管理员）
  POST /api/admin/grade/review        人工复核 approve / override（管理员）

错误映射：
  ValueError    → 400（参数/输入非法）
  LookupError   → 404（记录不存在）
  其他异常      → 500，并 logger.error 记录堆栈
LLM 不可用已在 services.essay_grading 内部降级为规则评分，不会走到 500。

注意：本模块不设 prefix，路由路径写全；由 main.py include_router 挂载。
"""
import logging
import traceback

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import require_admin
from services import essay_grading

logger = logging.getLogger(__name__)

router = APIRouter(tags=["grading"])


# ==================== 请求模型（pydantic v2） ====================

class WritingGradeRequest(BaseModel):
    """写作批改请求"""
    user_id: str = Field(..., description="用户ID")
    topic: str = Field(..., description="作文题目/要求")
    essay: str = Field(..., description="学生作文正文")
    level: str = Field("CET4", description="CET4 / CET6")
    word_limit: int | None = Field(None, description="字数要求，缺省按级别默认值")


class TranslationGradeRequest(BaseModel):
    """翻译批改请求"""
    user_id: str = Field(..., description="用户ID")
    source_text: str = Field(..., description="中文原文")
    user_translation: str = Field(..., description="学生译文")
    level: str = Field("CET4", description="CET4 / CET6")
    reference_translation: str | None = Field(
        None, description="参考译文（可选，传入时用于核对信息完整性）")


class ReviewRequest(BaseModel):
    """人工复核请求：approve 保持机评分，override 用人工分覆盖"""
    grade_id: str = Field(..., description="批改记录ID")
    kind: str = Field(..., description="writing / translation")
    action: str = Field(..., description="approve / override")
    score: float | None = Field(None, description="人工分（0-15，override 时必填）")
    note: str | None = Field(None, description="复核意见")


# ==================== 统一异常映射 ====================

async def _execute(action: str, coro):
    """统一返回 {"code": 0, "data": ...} 并做异常映射"""
    try:
        data = await coro
    except HTTPException:
        raise
    except ValueError as exc:
        # 输入/参数非法（空文本、超长、分数越界等）
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        logger.error(f"{action} 未预期异常\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"{action}失败，请稍后重试")
    return {"code": 0, "data": data}


# ==================== 批改接口 ====================

@router.post("/api/grade/writing")
async def grade_writing_api(req: WritingGradeRequest):
    """写作批改：四六级 15 分制三维度评分 + 逐句批注，真实落库"""
    return await _execute(
        "写作批改",
        essay_grading.grade_writing(
            req.user_id, req.topic, req.essay, req.level, req.word_limit),
    )


@router.post("/api/grade/translation")
async def grade_translation_api(req: TranslationGradeRequest):
    """翻译批改：准确度/语言两维度 + 逐条问题 + 改进译文，真实落库"""
    return await _execute(
        "翻译批改",
        essay_grading.grade_translation(
            req.user_id, req.source_text, req.user_translation, req.level,
            req.reference_translation),
    )


# 注意：/records 必须在 /{grade_id} 之前声明，否则会被路径参数吞掉
@router.get("/api/grade/records")
async def list_records(
    user_id: str = Query(..., description="用户ID"),
    kind: str | None = Query(None, description="writing / translation，缺省两种都查"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
):
    """批改历史列表（摘要，不返回作文全文）"""
    return await _execute(
        "查询批改记录",
        essay_grading.list_grades(user_id, kind, limit),
    )


@router.get("/api/grade/{grade_id}")
async def get_record(
    grade_id: str,
    kind: str = Query("writing", description="writing / translation"),
):
    """单条批改详情"""
    async def _job():
        data = await essay_grading.get_grade(grade_id, kind)
        if data is None:
            raise LookupError(f"批改记录不存在: {grade_id}")
        return data

    return await _execute("查询批改详情", _job())


# ==================== 管理接口（需 X-Admin-Token） ====================

@router.get("/api/admin/grade/queue")
async def review_queue(
    kind: str = Query("writing", description="writing / translation"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    _: None = Depends(require_admin),
):
    """人工复核队列：待复核批改（pending_review），先进先出"""
    return await _execute(
        "查询复核队列",
        essay_grading.list_review_queue(kind, limit),
    )


@router.post("/api/admin/grade/review")
async def review(
    req: ReviewRequest,
    _: None = Depends(require_admin),
):
    """人工复核：approve 保持机评分；override 用人工分覆盖并同步折算 score_710"""
    return await _execute(
        "人工复核",
        essay_grading.review_grade(
            req.grade_id, req.kind, req.action, req.score, req.note),
    )
