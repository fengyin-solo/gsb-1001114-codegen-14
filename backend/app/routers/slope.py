"""边坡防护接口。

在原有台账列表/登记之外，提供风险结论单一版本状态机的操作面：
- POST /{id}/reviews          现场复核（携带版本锁 expected_version）
- POST /{id}/publish          评级发布（事务内三表投影）
- GET  /profile               稳定性剖面带（分段加载，风险点服务端去重统计）
- POST /{id}/survey-tasks     点选偏离坡段生成工程踏勘任务（事务内三表投影）
- POST /{id}/survey-complete  踏勘完成，顺序收口
- GET  /surveys、/risk-summary、/{id}/workflow
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.slope import (
    RATINGS,
    STAGE_ORDER,
    SlopeService,
    VersionConflict,
    WorkflowError,
)

router = APIRouter(prefix="/api/slope", tags=["边坡防护"])

service = SlopeService()

LIST_FIELDS = ["边坡编号", "所属路段", "边坡类型", "坡高", "防护形式", "稳定性评级", "最近巡检", "边坡状态"]
STATUSES = ["待复核", "待发布", "稳定", "局部变形", "失稳", "踏勘中"]


class ReviewPayload(BaseModel):
    """现场复核提交体：版本锁 + 现场评级（冲突时以最近一次现场复核为准）。"""

    expected_version: int = Field(..., description="调用方看到的结论版本号，过期则 409")
    复核人: str
    现场评级: str = Field(..., description="稳定 / 较稳定 / 较差 / 不稳定")
    建议评级: str | None = None
    复核日期: str | None = None
    结论说明: str | None = None


class PublishPayload(BaseModel):
    expected_version: int


class SurveyTaskPayload(BaseModel):
    expected_version: int
    point_keys: list[str] = Field(..., min_length=1, description="点选的偏离坡段测点主键")


class SurveyCompletePayload(BaseModel):
    expected_version: int
    结论说明: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按边坡编号检索"),
    status: str | None = Query(default=None),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按边坡编号与状态过滤边坡台账；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/risk-summary")
def risk_summary() -> dict[str, Any]:
    """风险总览数字：全部按测点主键去重，前端分页累加得到的数字不作数。"""
    return service.risk_summary()


@router.get("/profile")
def profile(
    slope_id: int | None = Query(default=None, description="按边坡过滤剖面带"),
    road: str | None = Query(default=None, description="按所属路段过滤"),
    page: int = 1,
    size: int = Query(default=20, le=500, description="分段加载，单段最多 500 个测点"),
) -> dict[str, Any]:
    """稳定性剖面带：沿里程给出坡高、防护形式、巡检间隔、相邻病害与偏离标记。"""
    items, total, risk_points = service.profile_points(
        slope_id=slope_id, road=road, page=page, size=size
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "risk_points": risk_points,
        "dedup_key": "point_key",
    }


@router.get("/surveys")
def list_surveys(
    slope_id: int | None = None,
    status: str | None = Query(default=None, description="待踏勘 / 踏勘中 / 已踏勘"),
) -> dict[str, Any]:
    """工程踏勘任务清单。"""
    items = service.list_surveys(slope_id=slope_id, status=status)
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出边坡防护清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "slope", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict[str, Any]:
    """读取单条边坡明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"边坡 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/workflow")
def workflow_detail(entry_id: int) -> dict[str, Any]:
    """单坡风险结论工作流全貌：当前阶段、版本历史、剖面测点、踏勘任务。"""
    try:
        return service.workflow_detail(entry_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条边坡，缺字段时说明原因而不是静默丢弃；同时生成 v1 待复核结论。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="边坡已登记，风险结论进入待复核", entry=entry)


@router.post("/{entry_id}/reviews", response_model=ActionResult)
def submit_review(entry_id: int, payload: ReviewPayload) -> ActionResult:
    """现场复核：状态机第一跳；已复核后再次提交视为改评，旧版作废、版本号递增。"""
    try:
        slope, review = service.submit_review(entry_id, payload.model_dump(), payload.expected_version)
    except VersionConflict as exc:
        return ActionResult(ok=False, message=str(exc), entry={"code": 409, "stage": "version_conflict"})
    except WorkflowError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=f"现场复核完成（v{slope['version']}），现场评级「{review['现场评级']}」，可评级发布",
        entry=slope,
    )


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish_rating(entry_id: int, payload: PublishPayload) -> ActionResult:
    """评级发布：结论同事务写入边坡台账、路面病害待办和工程清单。"""
    try:
        slope, review, projection = service.publish_rating(entry_id, payload.expected_version)
    except VersionConflict as exc:
        return ActionResult(ok=False, message=str(exc), entry={"code": 409, "stage": "version_conflict"})
    except WorkflowError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=(
            f"评级已发布（v{slope['version']}，{slope['稳定性评级']}），"
            "边坡台账、路面病害待办、工程清单已同事务更新"
        ),
        entry={"slope": slope, "review": review, "projection": projection},
    )


@router.post("/{entry_id}/survey-tasks", response_model=ActionResult)
def create_survey_tasks(entry_id: int, payload: SurveyTaskPayload) -> ActionResult:
    """点选偏离坡段生成工程踏勘任务，并把剖面结论刷入台账、病害清单、工程待办。"""
    try:
        slope, tasks, projection = service.create_survey_tasks(
            entry_id, payload.point_keys, payload.expected_version
        )
    except VersionConflict as exc:
        return ActionResult(ok=False, message=str(exc), entry={"code": 409, "stage": "version_conflict"})
    except WorkflowError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=(
            f"已为 {len(tasks)} 个偏离坡段生成/复用工程踏勘任务，"
            f"当前风险点数 {projection['risk_points']}（按测点去重）"
        ),
        entry={"slope": slope, "tasks": tasks, "projection": projection},
    )


@router.post("/{entry_id}/survey-complete", response_model=ActionResult)
def complete_survey(entry_id: int, payload: SurveyCompletePayload) -> ActionResult:
    """踏勘完成：该边坡全部待踏勘任务处理完后，状态机顺序收口到已踏勘。"""
    try:
        slope, review = service.complete_survey(
            entry_id, payload.expected_version, payload.结论说明
        )
    except VersionConflict as exc:
        return ActionResult(ok=False, message=str(exc), entry={"code": 409, "stage": "version_conflict"})
    except WorkflowError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=f"踏勘阶段结束（v{slope['version']}），结论已归档为「{review['stage']}」",
        entry=slope,
    )
