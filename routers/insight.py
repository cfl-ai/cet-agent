"""错题归因与个性化提分方案路由。

挂载方式（由 main.py 装配，本模块不改 main.py）：
    from routers.insight import router as insight_router
    app.include_router(insight_router)

接口：
  POST /api/wrong/{user_id}/attribution       生成归因（body: use_llm, limit）
  GET  /api/wrong/{user_id}/attribution       读取归因报告
  POST /api/plan/improve                      生成个性化提分方案
  GET  /api/plan/improve/{user_id}/latest     读取最近一次方案（可能为 null）
  POST /api/admin/insight/attribution/recompute  管理接口：重算全部用户归因

异常约定：ValueError → 400（user_id 为空等参数问题）；未预期异常 → 500 + logger.error。
无数据不报错：空报告 / 带 note 的方案照常返回 code=0。
"""
import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_admin
from models.database import DB_PATH
from services import attribution_service, improvement_plan

logger = logging.getLogger(__name__)

router = APIRouter(tags=["insight"])


class AttributionRequest(BaseModel):
    """归因请求。use_llm=false 时只走规则，适合批量/离线重算"""
    use_llm: bool = True
    limit: int = 20


class ImprovePlanRequest(BaseModel):
    """提分方案请求。level/target_score/days_left 缺省时回退用户档案与默认值"""
    user_id: str
    level: str | None = None
    target_score: int | None = None
    days_left: int | None = None


# ==================== 错题归因 ====================

@router.post("/api/wrong/{user_id}/attribution")
async def attribute_wrong_questions(
    user_id: str,
    req: AttributionRequest | None = None,
):
    """对当前用户的未解决错题做归因（规则 + 可选 LLM 精修），并返回最新报告"""
    try:
        body = req or AttributionRequest()
        data = await attribution_service.attribute_wrong_questions(
            user_id, limit=body.limit, use_llm=body.use_llm)
        return {"code": 0, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"错题归因失败: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/wrong/{user_id}/attribution")
async def get_attribution_report(user_id: str):
    """读取错题归因报告（未归因时返回空报告 + note，不报错）"""
    try:
        data = await attribution_service.get_attribution_report(user_id)
        return {"code": 0, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"读取错题归因报告失败: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ==================== 个性化提分方案 ====================

@router.post("/api/plan/improve")
async def improve_plan(req: ImprovePlanRequest):
    """生成个性化提分方案（无历史数据时返回带 note 的方案，不报错）"""
    try:
        data = await improvement_plan.generate_improvement_plan(
            req.user_id,
            level=req.level,
            target_score=req.target_score,
            days_left=req.days_left,
        )
        return {"code": 0, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"生成提分方案失败: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/plan/improve/{user_id}/latest")
async def get_latest_plan(user_id: str):
    """读取最近一次提分方案；从未生成时 data 为 null"""
    try:
        data = await improvement_plan.get_latest_plan(user_id)
        return {"code": 0, "data": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"读取提分方案失败: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ==================== 管理接口（需 ADMIN_TOKEN） ====================

@router.post("/api/admin/insight/attribution/recompute")
async def recompute_all_attributions(
    limit: int = 20,
    _: None = Depends(require_admin),
):
    """批量重算所有有错题用户的归因（默认只走规则，避免批量 LLM 成本）"""
    try:
        limit = max(1, min(50, int(limit or 20)))
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT DISTINCT user_id FROM wrong_questions")
            user_ids = [row[0] for row in await cursor.fetchall() if row[0]]

        results = []
        for uid in user_ids[:100]:
            report = await attribution_service.attribute_wrong_questions(
                uid, limit=limit, use_llm=False)
            summary = report["summary"]
            results.append({
                "user_id": uid,
                "total_wrong": summary["total_wrong"],
                "attributed": summary["attributed"],
            })
        return {"code": 0, "data": {
            "user_count": len(results),
            "results": results,
        }}
    except Exception as exc:
        logger.error(f"批量重算归因失败: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
