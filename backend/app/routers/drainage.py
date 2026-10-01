"""排水设施接口：台账维护、处置回传、可复位分支与中断续传。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas import ActionResult, DisposalPayload, EntryPayload, PageResult
from app.services.drainage import WRITE_FAILURE_REASONS, DrainageService

router = APIRouter(prefix="/api/drainage", tags=["排水设施"])

service = DrainageService()

LIST_FIELDS = ["设施编号", "设施类型", "所属路段", "桩号位置", "清理日期", "淤积程度", "管养班组", "设施状态"]
STATUSES = ["正常", "淤积", "堵塞", "损坏"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按设施编号检索"),
    status: str | None = Query(default=None, description="正常、淤积、堵塞、损坏"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按设施编号与状态过滤排水设施列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/monitoring-list", response_model=PageResult[dict])
def list_monitoring_list(page: int = 1, size: int = 20) -> PageResult[dict]:
    """排水处置同步更新的路段监测清单。"""
    items, total = service.list_monitoring_list(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/todos", response_model=PageResult[dict])
def list_rectification_todos(page: int = 1, size: int = 20) -> PageResult[dict]:
    """排水设施整改待办；正常排水生成无需整改记录，异常生成待整改。"""
    items, total = service.list_rectification_todos(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/history", response_model=PageResult[dict])
def list_disposal_history(page: int = 1, size: int = 20) -> PageResult[dict]:
    """按观测时点留档的正式处置历史；不覆盖旧观测。"""
    items, total = service.list_disposal_history(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/reset-branches", response_model=PageResult[dict])
def list_reset_branches(page: int = 1, size: int = 20) -> PageResult[dict]:
    """设施编号缺失、无积水、泵站离线、复测超时统一进入可复位分支。"""
    items, total = service.list_reset_branches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/interruptions", response_model=PageResult[dict])
def list_interruptions(page: int = 1, size: int = 20) -> PageResult[dict]:
    """查看保存中断点；每个点保留原因码和原始回传报文。"""
    items, total = service.list_interruptions(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/disposals")
def submit_disposal(payload: DisposalPayload, response: Response) -> dict[str, Any]:
    """提交处置回传；正式结论同事务写入台账、待办和路段监测清单。"""
    values = payload.to_service_dict()
    fail_once = values.pop("fail_once", None)
    if fail_once is not None and fail_once not in WRITE_FAILURE_REASONS:
        raise HTTPException(status_code=400, detail=f"未知写入失败点：{fail_once}")
    try:
        result = service.submit_disposal(values, fail_once=fail_once)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("interrupted"):
        response.status_code = 503
    return result


@router.post("/disposals/interruptions/{interruption_id}/resume")
def resume_interruption(interruption_id: int) -> dict[str, Any]:
    """重连后从失败分支继续；同一设施编号和观测时点不重复落库。"""
    try:
        return service.resume_interruption(interruption_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


@router.post("/disposals/reset-branches/{branch_id}/reset")
def reset_branch(branch_id: int) -> dict[str, Any]:
    """复位空态/边界分支；它不会混写成正常排水结论。"""
    try:
        return service.reset_branch(branch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出排水设施清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drainage", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条排水设施明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"排水设施 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条排水设施，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="排水设施已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条排水设施执行安排清淤、安排疏通、登记损坏；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
