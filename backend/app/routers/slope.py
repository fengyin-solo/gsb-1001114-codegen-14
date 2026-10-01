"""边坡防护接口：结论版本状态机、稳定性剖面带与工程踏勘任务。

状态类动作（现场复核、评级发布、踏勘任务生成/完成）全部只做参数收集，
业务顺序、跳级倒序拦截、跨模块写入都在 SlopeService 内完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.slope import RATINGS, Conflict, SlopeService

router = APIRouter(prefix="/api/slope", tags=["边坡防护"])

service = SlopeService()

LIST_FIELDS = ["边坡编号", "所属路段", "边坡类型", "坡高", "防护形式", "稳定性评级", "最近巡检", "边坡状态"]
PHASES = ["待复核", "待发布", "待生成踏勘", "踏勘中"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按边坡编号或所属路段检索"),
    status: str | None = Query(default=None, description="结论阶段：待复核、待发布、待生成踏勘、踏勘中"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按边坡编号/路段与结论阶段过滤边坡台账；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/risk-summary")
def risk_summary() -> dict[str, Any]:
    """风险概览：风险点数（与运营概览卡片同源去重）、待踏勘任务与评级分布。"""
    return service.risk_summary()


@router.get("/segments", response_model=PageResult[dict])
def list_segments(
    road: str | None = Query(default=None, description="按所属路段过滤剖面带分段"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """稳定性剖面带分段（大量测点）分页加载，相邻病害实时拼装，不产生写入、不计数。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="分段每页最多 500 条，请缩小分页范围")
    items, total = service.list_segments(road=road, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/survey-tasks")
def list_survey_tasks(
    slope: int | None = Query(default=None, description="按边坡 id 过滤"),
    open_only: bool = Query(default=False, description="只看待踏勘任务"),
) -> dict[str, Any]:
    """工程踏勘任务清单。"""
    items = service.list_survey_tasks(entry_id=slope, open_only=open_only)
    return {"items": items, "total": len(items)}


@router.get("/meta/options")
def options() -> dict[str, Any]:
    """供前端渲染状态机动作按钮与评级下拉。"""
    return {"phases": PHASES, "ratings": RATINGS}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出边坡防护台账全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "slope", "total": total, "items": items}


@router.get("/{entry_id}/profile")
def slope_profile(entry_id: int) -> dict[str, Any]:
    """单边坡稳定性剖面带：坡高、防护形式、巡检间隔、相邻病害与偏离坡段。"""
    profile = service.slope_profile(entry_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"边坡 {entry_id} 不存在或已归档")
    return profile


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条边坡明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"边坡 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条边坡（含起止桩号），缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少或无效：{'、'.join(missing)}")
    return ActionResult(ok=True, message="边坡已登记，进入待复核阶段", entry=entry)


@router.post("/{entry_id}/segments", response_model=ActionResult)
def add_segment(entry_id: int, payload: EntryPayload) -> ActionResult:
    """为边坡补登一个剖面带分段；历史分段保留原防护前提，新分段继承当前防护前提。"""
    entry, message = service.add_segment(entry_id, payload.values)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/reviews", response_model=ActionResult)
def review(entry_id: int, payload: EntryPayload) -> ActionResult:
    """现场复核：状态机第一步，记录评级与复核时的防护前提，进入待发布。"""
    try:
        entry, message = service.review(entry_id, payload.values)
    except Conflict as conflict:
        raise HTTPException(status_code=409, detail=conflict.message)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish(entry_id: int, payload: EntryPayload) -> ActionResult:
    """评级发布：状态机第二步，事务内同步边坡台账、路面病害待办与工程清单。"""
    try:
        entry, message = service.publish(entry_id, payload.values)
    except Conflict as conflict:
        raise HTTPException(status_code=409, detail=conflict.message)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/survey-tasks", response_model=ActionResult)
def generate_survey_tasks(entry_id: int, payload: EntryPayload) -> ActionResult:
    """点选偏离坡段（或指定分段）生成工程踏勘任务；测点已有待办时去重不重复生成。"""
    try:
        entry, message, detail = service.generate_survey_tasks(entry_id, payload.values)
    except Conflict as conflict:
        raise HTTPException(status_code=409, detail=conflict.message)
    return ActionResult(ok=entry is not None, message=message, entry=entry if entry is not None else detail)


@router.post("/survey-tasks/{task_id}/complete", response_model=ActionResult)
def complete_survey_task(task_id: int) -> ActionResult:
    """完成单个踏勘任务；该边坡任务全部完成后状态机回到待复核，允许新一轮现场复核。"""
    task, message = service.close_survey_task(task_id)
    return ActionResult(ok=task is not None, message=message, entry=task)
