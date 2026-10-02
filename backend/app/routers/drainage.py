"""排水设施接口：维护排水设施，覆盖安排清淤、安排疏通、登记损坏等动作。

另提供处置回传闭环接口：四类可复位分支、现场复测优先、三表同事务、
中断续传（按原因码从失败分支继续）与按「设施编号 + 观测时间」幂等。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drainage import DrainageService
from app.services.drainage_disposal import disposal_service

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


@router.get("/disposals", response_model=PageResult[dict])
def list_disposals(
    branch: str | None = Query(default=None, description="按处置分支过滤"),
    status: str | None = Query(default=None, description="已完成、已中断、已复位"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """处置主记录列表：含正常排水与四类可复位分支，中断单也会列出待续传。"""
    items, total = disposal_service.list_disposals(branch=branch, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/disposals/observations")
def list_observations(
    facility_code: str | None = Query(default=None, alias="facility", description="按设施编号过滤"),
) -> dict[str, Any]:
    """观测历史：监测与人工按各自观测时点分别留档，冲突时两侧都在。"""
    return {"items": disposal_service.list_observations(facility_code)}


@router.get("/disposals/todos")
def list_todos(pending_only: bool = False) -> dict[str, Any]:
    """设施整改待办：正常排水只留档观察，可复位分支待复位/补录。"""
    return {"items": disposal_service.list_todos(pending_only=pending_only)}


@router.get("/disposals/monitor-items")
def list_monitor_items() -> dict[str, Any]:
    """路段监测清单：每条处置结论都会在此留档。"""
    return {"items": disposal_service.list_monitor_items()}


@router.post("/disposals", response_model=ActionResult)
def submit_disposal(payload: EntryPayload) -> ActionResult:
    """回传一次处置结论。

    设施编号缺失/无积水/泵站离线/复测超时走可复位分支，不与正常排水混用；
    监测与人工冲突时按现场复测采信；三表同事务，失败整体回滚并保留原因码。
    可用 ``模拟中断=true`` 或 ``模拟落库失败阶段=ledger|todo|monitor`` 演练断点续传。
    """
    result = disposal_service.submit(payload.values)
    return ActionResult(
        ok=bool(result.get("ok")),
        message=result.get("message", ""),
        entry=result.get("record"),
    )


@router.post("/disposals/{disposal_id}/resume", response_model=ActionResult)
def resume_disposal(disposal_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """重连后续传：从失败分支继续，补传字段覆盖原载荷；重复续传安全。"""
    overrides = payload.values if payload is not None else {}
    result = disposal_service.resume(disposal_id, overrides)
    if result.get("status_code"):
        raise HTTPException(status_code=int(result["status_code"]), detail=result["message"])
    return ActionResult(ok=bool(result.get("ok")), message=result.get("message", ""), entry=result.get("record"))


@router.post("/disposals/{disposal_id}/reset", response_model=ActionResult)
def reset_disposal(disposal_id: int) -> ActionResult:
    """复位可复位分支：同步关闭待办、更新清单与台账；正常排水不可复位。"""
    result = disposal_service.reset(disposal_id)
    if result.get("status_code"):
        raise HTTPException(status_code=int(result["status_code"]), detail=result["message"])
    return ActionResult(ok=True, message=result["message"], entry=result.get("record"))


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


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出排水设施清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drainage", "total": total, "items": items}
