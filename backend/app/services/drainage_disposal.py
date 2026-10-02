"""排水设施处置闭环：空态/边界分支、现场复测优先、三表同事务、中断续传与幂等。

契约要点：
- 设施编号缺失、无积水、泵站离线、复测超时各自走「可复位分支」，与正常排水严格分开；
- 监测与人工复测冲突时以现场复测为准，两侧观测按各自观测时点分别留档，互不覆盖；
- 处置结论同一事务写入：排水设施台账、设施整改待办、路段监测清单（另含观测历史、
  处置主记录），任一步失败整批回滚；
- 保存中断保留原因码与失败分支，重连后从失败分支继续；重复回传按
  「设施编号 + 观测时间」幂等，直接返回首次结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import (
    FAIL_STAGES,
    T_DISPOSAL,
    T_DRAINAGE,
    T_MONITOR,
    T_OBSERVATION,
    T_TODO,
    WriteFailure,
    store,
)

# ---- 分支定义：边界分支与正常排水不混用 ----------------------------------
BRANCH_NORMAL = "NORMAL_DRAINAGE"  # 正常排水
BRANCH_NO_WATER = "NO_WATER"  # 无积水
BRANCH_PUMP_OFFLINE = "PUMP_OFFLINE"  # 泵站离线
BRANCH_RECHECK_TIMEOUT = "RECHECK_TIMEOUT"  # 复测超时
BRANCH_FACILITY_MISSING = "FACILITY_MISSING"  # 设施编号缺失
RESETTABLE_BRANCHES = {
    BRANCH_NO_WATER,
    BRANCH_PUMP_OFFLINE,
    BRANCH_RECHECK_TIMEOUT,
    BRANCH_FACILITY_MISSING,
}

BRANCH_LABELS = {
    BRANCH_NORMAL: "正常排水",
    BRANCH_NO_WATER: "无积水",
    BRANCH_PUMP_OFFLINE: "泵站离线",
    BRANCH_RECHECK_TIMEOUT: "复测超时",
    BRANCH_FACILITY_MISSING: "设施编号缺失",
}

# 各分支落到三张表上的固定口径，禁止与正常排水混用
BRANCH_PROFILES: dict[str, dict[str, str]] = {
    BRANCH_NORMAL: {
        "结论": "正常排水",
        "台账状态": "正常",
        "待办类型": "留档观察",
        "清单状态": "正常留档",
        "message": "监测数据与现场复测一致，按正常排水留档",
    },
    BRANCH_NO_WATER: {
        "结论": "现场无积水",
        "台账状态": "无积水(可复位)",
        "待办类型": "复位核查",
        "清单状态": "边界留档(可复位)",
        "message": "现场复测无积水，进入可复位分支，不与正常排水混用",
    },
    BRANCH_PUMP_OFFLINE: {
        "结论": "泵站离线",
        "台账状态": "泵站离线(可复位)",
        "待办类型": "复位核查",
        "清单状态": "边界留档(可复位)",
        "message": "泵站离线无法排水，进入可复位分支，待泵站恢复后复位",
    },
    BRANCH_RECHECK_TIMEOUT: {
        "结论": "复测超时",
        "台账状态": "复测超时(可复位)",
        "待办类型": "复位核查",
        "清单状态": "边界留档(可复位)",
        "message": "人工复测超时未回，进入可复位分支，复测到位后复位",
    },
    BRANCH_FACILITY_MISSING: {
        "结论": "设施编号缺失",
        "台账状态": "设施编号缺失(可复位)",
        "待办类型": "补录编号",
        "清单状态": "边界留档(可复位)",
        "message": "设施编号缺失，进入可复位分支，补录编号后复位，不按正常排水处置",
    },
}

# ---- 中断原因码：保存到处置主记录，重连后续传 -----------------------------
REASON_CLIENT_OFFLINE = "CLIENT_OFFLINE"  # 客户端保存中断
REASON_STAGE = {
    "ledger": "DB_LEDGER_FAILED",
    "todo": "DB_TODO_FAILED",
    "monitor": "DB_MONITOR_FAILED",
    "observation": "DB_OBSERVATION_FAILED",
    "disposal": "DB_DISPOSAL_FAILED",
}
STAGE_LABELS = {
    "submit": "提交保存",
    "ledger": "排水设施台账",
    "todo": "设施整改待办",
    "monitor": "路段监测清单",
    "observation": "观测历史",
    "disposal": "处置主记录",
}

STATUS_DONE = "已完成"
STATUS_INTERRUPTED = "已中断"
STATUS_RESET = "已复位"

OFFLINE_TOKENS = {"离线", "停机", "故障", "断电", "offline", "down", "0"}
ONLINE_TOKENS = {"在线", "运行", "正常", "online", "up", "1"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _depth(value: Any) -> float | None:
    """积水深度（米）；空值/无法解析视为无读数，不当成 0 正常排水。"""
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pump_state(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    low = text.lower()
    if low in OFFLINE_TOKENS or text in OFFLINE_TOKENS:
        return "离线"
    if low in ONLINE_TOKENS or text in ONLINE_TOKENS:
        return "在线"
    return None


def _truthy(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on", "是", "超时"}


def _biz_id() -> str:
    return f"DRAD-{store.next_id(T_DISPOSAL):04d}"


def _source_payload(values: dict[str, Any], *, manual: bool) -> dict[str, Any]:
    if manual:
        return {
            "depth": _depth(values.get("人工积水深度")),
            "pump": _pump_state(values.get("人工泵站状态")),
            "time": _text(values.get("人工复测时间")),
            "present": bool(
                _text(values.get("人工积水深度")) or _text(values.get("人工泵站状态"))
            ),
            "raw_depth": _text(values.get("人工积水深度")),
            "raw_pump": _text(values.get("人工泵站状态")),
        }
    return {
        "depth": _depth(values.get("监测积水深度")),
        "pump": _pump_state(values.get("监测泵站状态")),
        "time": _text(values.get("监测时间")),
        "present": bool(
            _text(values.get("监测积水深度")) or _text(values.get("监测泵站状态"))
        ),
        "raw_depth": _text(values.get("监测积水深度")),
        "raw_pump": _text(values.get("监测泵站状态")),
    }


def _has_water(source: dict[str, Any]) -> bool | None:
    if source["depth"] is not None:
        return source["depth"] > 0
    if source["pump"] is not None:
        # 只有泵站读数时，在线视为正在排水（有水），离线无法判定有水
        return source["pump"] == "在线"
    return None


def _conflict(monitor: dict[str, Any], manual: dict[str, Any]) -> bool:
    """监测与人工复测是否冲突：有水判定相反、深度差超过 5cm、泵站在线性相反。"""
    if not (monitor["present"] and manual["present"]):
        return False
    mw, hw = _has_water(monitor), _has_water(manual)
    if mw is not None and hw is not None and mw != hw:
        return True
    if (
        monitor["depth"] is not None
        and manual["depth"] is not None
        and abs(monitor["depth"] - manual["depth"]) > 0.05
    ):
        return True
    if monitor["pump"] is not None and manual["pump"] is not None:
        if monitor["pump"] != manual["pump"]:
            return True
    return False


def classify(values: dict[str, Any]) -> dict[str, Any]:
    """判定处置分支。返回分支、冲突标记与最终采信的观测（冲突时以现场复测为准）。"""
    monitor = _source_payload(values, manual=False)
    manual = _source_payload(values, manual=True)
    timeout = _truthy(values.get("复测超时"))
    conflicted = _conflict(monitor, manual)
    # 现场复测优先：有人工读数就用人工；没有人工才退回监测
    chosen = manual if manual["present"] else monitor

    if not _text(values.get("设施编号")):
        branch = BRANCH_FACILITY_MISSING
    elif timeout and not manual["present"]:
        # 复测未在时限内返回，现场事实不明，优先挂超时分支等待复测
        branch = BRANCH_RECHECK_TIMEOUT
    elif chosen["pump"] == "离线":
        branch = BRANCH_PUMP_OFFLINE
    else:
        water = _has_water(chosen)
        branch = BRANCH_NORMAL if water else BRANCH_NO_WATER

    return {
        "branch": branch,
        "conflicted": conflicted,
        "monitor": monitor,
        "manual": manual,
        "chosen": chosen,
        "data_source": "现场人工复测" if manual["present"] else "在线监测",
    }


class DrainageDisposalService:
    """排水设施处置回传服务。"""

    # ---- 查询 ----------------------------------------------------------
    def list_disposals(
        self,
        *,
        branch: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(T_DISPOSAL)
        if branch:
            rows = [row for row in rows if row.get("分支编码") == branch]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_observations(self, facility_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_OBSERVATION)
        if facility_code:
            rows = [row for row in rows if row.get("设施编号") == facility_code]
        return rows

    def list_todos(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        rows = store.rows(T_TODO)
        if pending_only:
            rows = [row for row in rows if row.get("待办状态") not in {"留档观察", "已取消(复位)"}]
        return rows

    def list_monitor_items(self) -> list[dict[str, Any]]:
        return store.rows(T_MONITOR)

    # ---- 幂等 ----------------------------------------------------------
    def _find_idempotent(self, code: str, observed_at: str) -> dict[str, Any] | None:
        for row in reversed(store.rows(T_DISPOSAL)):
            if (
                row.get("设施编号") == code
                and row.get("观测时间") == observed_at
                and row.get("状态") != STATUS_RESET
            ):
                return row
        return None

    # ---- 回传提交 ------------------------------------------------------
    def submit(self, values: dict[str, Any]) -> dict[str, Any]:
        observed_at = _text(values.get("观测时间"))
        if not observed_at:
            return self._error("观测时间缺失：按「设施编号 + 观测时间」幂等，必须提供观测时间")
        code = _text(values.get("设施编号"))

        existed = self._find_idempotent(code, observed_at)
        if existed is not None:
            return {
                "ok": True,
                "幂等": True,
                "重复回传": True,
                "message": "同一设施编号与观测时间已处置，直接返回首次结论，不重复落库",
                "record": existed,
            }

        verdict = classify(values)
        # 客户端保存中断：不落任何业务表，只留中断主记录与原因码，等待重连续传
        if _truthy(values.get("模拟中断")) or _text(values.get("中断原因")) == REASON_CLIENT_OFFLINE:
            record = self._save_interrupted(
                values, verdict, reason=REASON_CLIENT_OFFLINE, stage="submit", note="客户端保存中断"
            )
            return {
                "ok": True,
                "中断": True,
                "record": record,
                "message": "保存中断已登记原因码 CLIENT_OFFLINE，重连后可从该分支继续",
            }

        fail_stage = _text(values.get("模拟落库失败阶段"))
        try:
            record = self._commit(verdict, values, fail_stage=fail_stage or None)
        except WriteFailure as exc:
            record = self._save_interrupted(
                values,
                verdict,
                reason=REASON_STAGE[exc.stage],
                stage=exc.stage,
                note=f"{STAGE_LABELS[exc.stage]}写入失败，三表已整批回滚",
            )
            return {
                "ok": False,
                "中断": True,
                "record": record,
                "message": f"{STAGE_LABELS[exc.stage]}写入失败，台账/待办/清单已整批回滚，"
                f"已保留原因码 {REASON_STAGE[exc.stage]}，可重连续传",
            }
        return {"ok": True, "record": record, "message": BRANCH_PROFILES[verdict["branch"]]["message"]}

    def resume(self, disposal_id: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """重连后从失败分支继续：合并原载荷与补传字段，重新走同一事务。"""
        record = store.find(T_DISPOSAL, disposal_id)
        if record is None:
            return self._error(f"处置单 {disposal_id} 不存在", status_code=404)
        if record.get("状态") != STATUS_INTERRUPTED:
            return self._error(f"处置单 {disposal_id} 当前状态为{record.get('状态')}，无需续传")

        values = dict(record.get("回传载荷") or {})
        values.update({k: v for k, v in (overrides or {}).items() if v is not None})
        # 续传时不再允许“客户端中断”短路；落库失败阶段可以重新指定用于演练
        values.pop("模拟中断", None)
        verdict = classify(values)
        # 以首次失败分支为准（从失败分支继续），分类结果只用于刷新采信数据
        verdict["branch"] = record["分支编码"]
        fail_stage = _text(values.get("模拟落库失败阶段"))

        try:
            self._commit(verdict, values, fail_stage=fail_stage or None, existing=record)
        except WriteFailure as exc:
            reason = REASON_STAGE[exc.stage]
            record.update({
                "状态": STATUS_INTERRUPTED,
                "原因码": reason,
                "失败阶段": exc.stage,
                "续传次数": int(record.get("续传次数", 0)) + 1,
                "最近中断时间": _now(),
                "中断说明": f"续传时{STAGE_LABELS[exc.stage]}仍失败，整批再次回滚",
                "回传载荷": values,
            })
            return {
                "ok": False,
                "中断": True,
                "record": record,
                "message": f"续传在{STAGE_LABELS[exc.stage]}再次失败并回滚，原因码 {reason} 已更新，可再次续传",
            }
        return {"ok": True, "record": record, "message": f"处置单 {disposal_id} 已从{BRANCH_LABELS[record['分支编码']]}分支续传完成"}

    def reset(self, disposal_id: int) -> dict[str, Any]:
        """复位可复位分支；正常排水与未完成的中断单不允许复位。"""
        record = store.find(T_DISPOSAL, disposal_id)
        if record is None:
            return self._error(f"处置单 {disposal_id} 不存在", status_code=404)
        if record.get("状态") == STATUS_INTERRUPTED:
            return self._error("中断单须先续传完成，不能直接复位")
        if record.get("状态") == STATUS_RESET:
            return self._error("该分支已复位，请勿重复操作")
        if record.get("分支编码") not in RESETTABLE_BRANCHES:
            return self._error("正常排水结论不进入复位流程")

        with store.transaction(touch=(T_DRAINAGE, T_TODO, T_MONITOR, T_DISPOSAL)):
            record["状态"] = STATUS_RESET
            record["复位时间"] = _now()
            for row in store.rows(T_TODO):
                if row.get("关联单号") == record["处置单号"]:
                    row["待办状态"] = "已取消(复位)"
                    row["办结时间"] = record["复位时间"]
            for row in store.rows(T_MONITOR):
                if row.get("关联单号") == record["处置单号"]:
                    row["清单状态"] = "已复位"
                    row["复位时间"] = record["复位时间"]
            ledger_row = self._find_ledger_row(record)
            if ledger_row is not None:
                ledger_row["设施状态"] = f"已复位（原：{record['处置结论']}）"
        return {"ok": True, "record": record, "message": f"处置单 {disposal_id} 已复位，关联待办与监测清单同步关闭"}

    # ---- 事务内落库：台账 / 待办 / 清单 / 观测 / 主记录 ------------------
    def _commit(
        self,
        verdict: dict[str, Any],
        values: dict[str, Any],
        *,
        fail_stage: str | None,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if fail_stage and fail_stage not in FAIL_STAGES:
            raise ValueError(f"未知失败阶段：{fail_stage}")
        branch = verdict["branch"]
        profile = BRANCH_PROFILES[branch]
        code = _text(values.get("设施编号"))
        observed_at = _text(values.get("观测时间"))
        # 事务内先定单号：此时主记录尚未 append，多次取号一致，回滚后号段随快照一起回退
        biz = existing["处置单号"] if existing else _peek_biz_id()

        with store.transaction(fail_stage=fail_stage):
            ledger = self._upsert_ledger(values, branch, profile, existing, biz)
            self._guard(fail_stage, "ledger")

            todo = self._upsert_todo(values, branch, profile, code, observed_at, existing, biz)
            self._guard(fail_stage, "todo")

            monitor = self._upsert_monitor(values, verdict, profile, code, observed_at, existing, biz)
            self._guard(fail_stage, "monitor")

            self._archive_observations(values, verdict, existing, biz)
            self._guard(fail_stage, "observation")

            record = self._finalize_disposal(
                values, verdict, profile, code, observed_at, existing
            )
            self._guard(fail_stage, "disposal")
        return record

    @staticmethod
    def _guard(fail_stage: str | None, stage: str) -> None:
        if fail_stage == stage:
            raise WriteFailure(stage, REASON_STAGE[stage])

    def _upsert_ledger(
        self,
        values: dict[str, Any],
        branch: str,
        profile: dict[str, str],
        existing: dict[str, Any] | None,
        biz: str,
    ) -> dict[str, Any]:
        code = _text(values.get("设施编号"))
        row = self._find_ledger_row(existing) if existing else None
        if row is None and code:
            row = next(
                (r for r in store.rows(T_DRAINAGE) if _text(r.get("设施编号")) == code),
                None,
            )
        if row is None:
            row = {"id": store.next_id(T_DRAINAGE), "关联单号": biz}
            store.rows(T_DRAINAGE).append(row)
        row.update({
            "设施编号": code,
            "设施类型": _text(values.get("设施类型")) or row.get("设施类型", ""),
            "所属路段": _text(values.get("所属路段")) or row.get("所属路段", ""),
            "桩号位置": _text(values.get("桩号位置")) or row.get("桩号位置", ""),
            "status": profile["台账状态"],
            "设施状态": profile["台账状态"],
            "pending": branch in RESETTABLE_BRANCHES,
            "abnormal": branch in {BRANCH_PUMP_OFFLINE, BRANCH_RECHECK_TIMEOUT, BRANCH_FACILITY_MISSING},
            "处置分支": branch,
            "最近处置时间": _now(),
        })
        return row

    def _upsert_todo(
        self,
        values: dict[str, Any],
        branch: str,
        profile: dict[str, str],
        code: str,
        observed_at: str,
        existing: dict[str, Any] | None,
        biz: str,
    ) -> dict[str, Any]:
        row = None
        if existing:
            row = next(
                (r for r in store.rows(T_TODO) if r.get("关联单号") == existing["处置单号"]),
                None,
            )
        if row is None:
            seq = store.next_id(T_TODO)
            row = {"id": seq, "待办编号": f"RECT-{seq:04d}"}
            store.rows(T_TODO).append(row)
        row.update({
            "设施编号": code,
            "所属路段": _text(values.get("所属路段")),
            "桩号位置": _text(values.get("桩号位置")),
            "观测时间": observed_at,
            "处置结论": profile["结论"],
            "待办类型": profile["待办类型"],
            "待办状态": "留档观察" if branch == BRANCH_NORMAL else "待办",
            "可复位": branch in RESETTABLE_BRANCHES,
            "关联单号": biz,
            "下发时间": _now(),
        })
        return row

    def _upsert_monitor(
        self,
        values: dict[str, Any],
        verdict: dict[str, Any],
        profile: dict[str, str],
        code: str,
        observed_at: str,
        existing: dict[str, Any] | None,
        biz: str,
    ) -> dict[str, Any]:
        row = None
        if existing:
            row = next(
                (r for r in store.rows(T_MONITOR) if r.get("关联单号") == existing["处置单号"]),
                None,
            )
        if row is None:
            seq = store.next_id(T_MONITOR)
            row = {"id": seq, "清单编号": f"RSEC-{seq:04d}"}
            store.rows(T_MONITOR).append(row)
        chosen = verdict["chosen"]
        row.update({
            "设施编号": code,
            "所属路段": _text(values.get("所属路段")),
            "桩号位置": _text(values.get("桩号位置")),
            "观测时间": observed_at,
            "数据来源": verdict["data_source"],
            "积水深度": chosen["raw_depth"],
            "泵站状态": chosen["raw_pump"],
            "监测复测冲突": "是（已按现场复测采信）" if verdict["conflicted"] else "否",
            "积水结论": profile["结论"],
            "清单状态": profile["清单状态"],
            "处置分支": verdict["branch"],
            "关联单号": biz,
            "留档时间": _now(),
        })
        return row

    def _archive_observations(
        self,
        values: dict[str, Any],
        verdict: dict[str, Any],
        existing: dict[str, Any] | None,
        biz: str,
    ) -> None:
        """监测与人工各按自己的观测时点留一条历史；冲突时谁也不覆盖谁。"""
        base_time = _text(values.get("观测时间"))
        for source_name, source in (("在线监测", verdict["monitor"]), ("现场人工复测", verdict["manual"])):
            if not source["present"]:
                continue
            seq = store.next_id(T_OBSERVATION)
            store.rows(T_OBSERVATION).append({
                "id": seq,
                "观测记录号": f"DOBS-{seq:04d}",
                "设施编号": _text(values.get("设施编号")),
                "所属路段": _text(values.get("所属路段")),
                "观测来源": source_name,
                "观测时点": source["time"] or base_time,
                "积水深度": source["raw_depth"],
                "泵站状态": source["raw_pump"],
                "是否采信": (source_name == verdict["data_source"]),
                "关联单号": biz,
                "留档时间": _now(),
            })

    def _finalize_disposal(
        self,
        values: dict[str, Any],
        verdict: dict[str, Any],
        profile: dict[str, str],
        code: str,
        observed_at: str,
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if existing is not None:
            existing.update({
                "状态": STATUS_DONE,
                "原因码": "",
                "失败阶段": "",
                "完成时间": _now(),
                "中断说明": "",
                "处置结论": profile["结论"],
                "数据来源": verdict["data_source"],
                "监测复测冲突": verdict["conflicted"],
                "回传载荷": values,
            })
            return existing

        seq = store.next_id(T_DISPOSAL)
        record = {
            "id": seq,
            "处置单号": f"DRAD-{seq:04d}",
            "设施编号": code,
            "所属路段": _text(values.get("所属路段")),
            "观测时间": observed_at,
            "分支编码": verdict["branch"],
            "分支名称": BRANCH_LABELS[verdict["branch"]],
            "可复位": verdict["branch"] in RESETTABLE_BRANCHES,
            "处置结论": profile["结论"],
            "数据来源": verdict["data_source"],
            "监测复测冲突": verdict["conflicted"],
            "状态": STATUS_DONE,
            "原因码": "",
            "失败阶段": "",
            "续传次数": 0,
            "提交时间": _now(),
            "完成时间": _now(),
            "回传载荷": values,
        }
        store.rows(T_DISPOSAL).append(record)
        return record

    # ---- 中断留痕（事务外，必须保住） -----------------------------------
    def _save_interrupted(
        self,
        values: dict[str, Any],
        verdict: dict[str, Any],
        *,
        reason: str,
        stage: str,
        note: str,
    ) -> dict[str, Any]:
        code = _text(values.get("设施编号"))
        observed_at = _text(values.get("观测时间"))
        seq = store.next_id(T_DISPOSAL)
        record = {
            "id": seq,
            "处置单号": f"DRAD-{seq:04d}",
            "设施编号": code,
            "所属路段": _text(values.get("所属路段")),
            "观测时间": observed_at,
            "分支编码": verdict["branch"],
            "分支名称": BRANCH_LABELS[verdict["branch"]],
            "可复位": verdict["branch"] in RESETTABLE_BRANCHES,
            "处置结论": "",
            "数据来源": verdict["data_source"],
            "监测复测冲突": verdict["conflicted"],
            "状态": STATUS_INTERRUPTED,
            "原因码": reason,
            "失败阶段": stage,
            "续传次数": 0,
            "提交时间": _now(),
            "最近中断时间": _now(),
            "中断说明": note,
            "回传载荷": values,
        }
        store.rows(T_DISPOSAL).append(record)
        return record

    @staticmethod
    def _find_ledger_row(record: dict[str, Any]) -> dict[str, Any] | None:
        code = record.get("设施编号")
        if code:
            return next(
                (r for r in store.rows(T_DRAINAGE) if _text(r.get("设施编号")) == code),
                None,
            )
        # 设施编号缺失时台账行只能靠处置单号关联
        return next(
            (r for r in store.rows(T_DRAINAGE) if r.get("关联单号") == record["处置单号"]),
            None,
        )

    @staticmethod
    def _error(message: str, *, status_code: int = 400) -> dict[str, Any]:
        return {"ok": False, "message": message, "status_code": status_code}


def _peek_biz_id() -> str:
    """事务内先生成主记录要占用的单号，供台账/待办/清单关联。"""
    return f"DRAD-{store.next_id(T_DISPOSAL):04d}"


disposal_service = DrainageDisposalService()
