"""排水设施业务规则：状态流转、空态边界、现场复测与处置落库。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import StorageWriteError, store

MODULE = "drainage"
HISTORY_MODULE = "drainage_disposal_history"
RESET_MODULE = "drainage_reset_branch"
TODO_MODULE = "facility_rectification_todo"
MONITOR_MODULE = "road_monitoring_list"
INTERRUPTION_MODULE = "drainage_disposal_interruption"

REQUIRED_FIELDS = ["设施编号", "设施类型", "所属路段"]
STATUS_ORDER = ["正常", "淤积", "堵塞", "损坏"]
ACTION_RULES = {"安排清淤": "淤积", "安排疏通": "堵塞", "登记损坏": "损坏"}
NEGATIVE_ACTIONS = []

RESET_FACILITY_CODE = "FACILITY_CODE_MISSING"
RESET_NO_WATER = "NO_STANDING_WATER"
RESET_PUMP_OFFLINE = "PUMP_OFFLINE"
RESET_RETEST_TIMEOUT = "RETEST_TIMEOUT"
NORMAL_DRAINAGE = "NORMAL_DRAINAGE"
RESET_REASON_CODES = {
    RESET_FACILITY_CODE,
    RESET_NO_WATER,
    RESET_PUMP_OFFLINE,
    RESET_RETEST_TIMEOUT,
}

BRANCH_STATUS = {
    RESET_FACILITY_CODE: "设施编号缺失",
    RESET_NO_WATER: "无积水",
    RESET_PUMP_OFFLINE: "泵站离线",
    RESET_RETEST_TIMEOUT: "复测超时",
    NORMAL_DRAINAGE: "正常排水",
    "SILTED": "淤积",
    "BLOCKED": "堵塞",
    "DAMAGED": "损坏",
}
FORMAL_STATUS = {
    NORMAL_DRAINAGE: "正常",
    "SILTED": "淤积",
    "BLOCKED": "堵塞",
    "DAMAGED": "损坏",
}
CONCLUSION_CODES = {
    "无积水": RESET_NO_WATER,
    "正常排水": NORMAL_DRAINAGE,
    "排水正常": NORMAL_DRAINAGE,
    "正常": NORMAL_DRAINAGE,
    "淤积": "SILTED",
    "堵塞": "BLOCKED",
    "损坏": "DAMAGED",
}
OFFLINE_VALUES = {"离线", "offline", "off", "0", "false"}
WRITE_FAILURE_REASONS = {
    "history": "HISTORY_WRITE_FAILED",
    "ledger": "LEDGER_WRITE_FAILED",
    "todo": "TODO_WRITE_FAILED",
    "monitor": "MONITORING_LIST_WRITE_FAILED",
    "reset": "RESET_BRANCH_WRITE_FAILED",
}


class DrainageService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("设施编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"排水设施 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于排水设施可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"排水设施已{action}"

    def submit_disposal(
        self,
        payload: dict[str, Any],
        *,
        fail_once: str | None = None,
        from_interruption: bool = False,
        existing_interruption_id: int | None = None,
    ) -> dict[str, Any]:
        """提交一次排水处置回传；同一设施编号 + 观测时点重复回传直接返回原结果。"""
        branch = self._classify(payload)
        if fail_once:
            if fail_once not in WRITE_FAILURE_REASONS:
                raise ValueError(f"未知写入失败点：{fail_once}")
            store.arm_write_failure(fail_once, WRITE_FAILURE_REASONS[fail_once])
        idempotency_key = self._idempotency_key(branch["facilityCode"], branch["observedAt"], branch["reasonCode"])

        active_interruption = None if from_interruption else self._find_active_interruption(idempotency_key)
        if active_interruption is not None:
            return active_interruption

        table = RESET_MODULE if branch["reasonCode"] in RESET_REASON_CODES else HISTORY_MODULE
        existing = self._find_idempotent(table, idempotency_key)
        if existing is not None:
            return existing.get("result") or existing

        try:
            if branch["reasonCode"] in RESET_REASON_CODES:
                result = self._save_reset_branch(branch, idempotency_key, fail_once=fail_once)
            else:
                result = self._save_formal_disposal(branch, idempotency_key, fail_once=fail_once)
        except StorageWriteError as exc:
            return self._save_interruption(
                payload=payload,
                branch=branch,
                idempotency_key=idempotency_key,
                reason_code=exc.reason_code,
                message=exc.message,
                failed_write_point=fail_once or "unknown",
                existing_interruption_id=existing_interruption_id,
            )
        return result

    def resume_interruption(self, interruption_id: int) -> dict[str, Any]:
        interruption = store.find(INTERRUPTION_MODULE, interruption_id)
        if interruption is None:
            raise KeyError(f"中断记录 {interruption_id} 不存在")
        if interruption.get("status") == "已继续":
            return interruption.get("result") or interruption

        try:
            result = self.submit_disposal(
                interruption["payload"],
                from_interruption=True,
                existing_interruption_id=int(interruption["id"]),
            )
        except StorageWriteError:
            refreshed = store.find(INTERRUPTION_MODULE, interruption_id)
            return refreshed or interruption

        interruption["status"] = "已继续"
        interruption["resumedAt"] = self._now()
        interruption["result"] = result
        interruption["message"] = "重连后已从失败分支继续，整批处置写入完成"
        return {
            **result,
            "interruptionId": interruption["id"],
            "resumed": True,
        }

    def reset_branch(self, branch_id: int) -> dict[str, Any]:
        row = store.find(RESET_MODULE, branch_id)
        if row is None:
            raise KeyError(f"复位分支 {branch_id} 不存在")
        with store.transaction() as tx:
            row = tx.upsert_by_key(
                RESET_MODULE,
                str(branch_id),
                "id",
                {"resetStatus": "已复位", "resetAt": self._now()},
            )
        return row

    def list_rectification_todos(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        return self._page_table(TODO_MODULE, page, size)

    def list_monitoring_list(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        return self._page_table(MONITOR_MODULE, page, size)

    def list_disposal_history(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        return self._page_table(HISTORY_MODULE, page, size)

    def list_reset_branches(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        return self._page_table(RESET_MODULE, page, size)

    def list_interruptions(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        return self._page_table(INTERRUPTION_MODULE, page, size)

    def _save_formal_disposal(
        self,
        branch: dict[str, Any],
        idempotency_key: str,
        *,
        fail_once: str | None,
    ) -> dict[str, Any]:
        now = self._now()
        with store.transaction() as tx:
            history_id = tx.next_id(HISTORY_MODULE)
            todo_id = tx.next_id(TODO_MODULE)
            monitor_id = tx.next_id(MONITOR_MODULE)
            facility_code = branch["facilityCode"]
            status = FORMAL_STATUS[branch["reasonCode"]]
            abnormal = branch["reasonCode"] != NORMAL_DRAINAGE

            history = {
                "id": history_id,
                "设施编号": facility_code,
                "观测时点": branch["observedAt"],
                "权威来源": branch["effectiveSource"],
                "监测结论": branch["monitoringConclusion"],
                "人工复测结论": branch["manualConclusion"],
                "处置结论": branch["conclusion"],
                "结论原因码": branch["reasonCode"],
                "冲突": branch["conflict"],
                "观测明细": branch["observations"],
                "留档时间": now,
            }
            tx.insert(
                HISTORY_MODULE,
                history,
                idempotency_key=idempotency_key,
                fail_once=fail_once if fail_once == "history" else None,
                fail_reason=WRITE_FAILURE_REASONS["history"],
            )

            ledger = {
                "设施编号": facility_code,
                "设施类型": branch.get("facilityType") or "未填报",
                "所属路段": branch.get("roadName") or "未填报",
                "桩号位置": branch.get("station") or "未填报",
                "设施状态": status,
                "status": status,
                "pending": abnormal,
                "abnormal": abnormal,
                "处置结论": branch["conclusion"],
                "结论原因码": branch["reasonCode"],
                "权威来源": branch["effectiveSource"],
                "监测结论": branch["monitoringConclusion"],
                "人工复测结论": branch["manualConclusion"],
                "冲突": branch["conflict"],
                "最近观测时间": branch["observedAt"],
                "最近处置时间": now,
            }
            tx.upsert_by_key(
                MODULE,
                facility_code,
                "设施编号",
                ledger,
                fail_once=fail_once if fail_once == "ledger" else None,
                fail_reason=WRITE_FAILURE_REASONS["ledger"],
            )

            todo = {
                "id": todo_id,
                "待办编号": f"DRTODO-{todo_id:04d}",
                "设施编号": facility_code,
                "所属路段": branch.get("roadName") or "未填报",
                "桩号位置": branch.get("station") or "未填报",
                "处置结论": branch["conclusion"],
                "原因码": branch["reasonCode"],
                "待办状态": "待整改" if abnormal else "无需整改",
                "pending": abnormal,
                "权威来源": branch["effectiveSource"],
                "观测时间": branch["observedAt"],
                "创建时间": now,
                "更新时间": now,
            }
            tx.insert(
                TODO_MODULE,
                todo,
                idempotency_key=idempotency_key,
                fail_once=fail_once if fail_once == "todo" else None,
                fail_reason=WRITE_FAILURE_REASONS["todo"],
            )

            monitoring = {
                "id": monitor_id,
                "清单编号": f"RMON-{monitor_id:04d}",
                "设施编号": facility_code,
                "所属路段": branch.get("roadName") or "未填报",
                "桩号位置": branch.get("station") or "未填报",
                "监测状态": "持续监测" if abnormal else "正常",
                "处置结论": branch["conclusion"],
                "原因码": branch["reasonCode"],
                "权威来源": branch["effectiveSource"],
                "观测时间": branch["observedAt"],
                "更新时间": now,
            }
            tx.insert(
                MONITOR_MODULE,
                monitoring,
                idempotency_key=idempotency_key,
                fail_once=fail_once if fail_once == "monitor" else None,
                fail_reason=WRITE_FAILURE_REASONS["monitor"],
            )

            result = {
                "ok": True,
                "idempotencyKey": idempotency_key,
                "facilityCode": facility_code,
                "observedAt": branch["observedAt"],
                "branchStatus": BRANCH_STATUS[branch["reasonCode"]],
                "reasonCode": branch["reasonCode"],
                "resettable": False,
                "conclusion": branch["conclusion"],
                "effectiveSource": branch["effectiveSource"],
                "conflict": branch["conflict"],
                "written": {
                    "ledger": facility_code,
                    "todo": todo["待办编号"],
                    "monitoringList": monitoring["清单编号"],
                    "history": history_id,
                },
                "message": "处置结论已同事务写入台账、整改待办和路段监测清单",
            }
            history["result"] = result

        return result

    def _save_reset_branch(
        self,
        branch: dict[str, Any],
        idempotency_key: str,
        *,
        fail_once: str | None,
    ) -> dict[str, Any]:
        now = self._now()
        with store.transaction() as tx:
            row = {
                "id": tx.next_id(RESET_MODULE),
                "设施编号": branch["facilityCode"] or "—",
                "观测时点": branch["observedAt"],
                "分支状态": BRANCH_STATUS[branch["reasonCode"]],
                "原因码": branch["reasonCode"],
                "监测结论": branch["monitoringConclusion"],
                "人工复测结论": branch["manualConclusion"],
                "权威来源": branch["effectiveSource"],
                "处置结论": branch["conclusion"],
                "观测明细": branch["observations"],
                "resetStatus": "待复位",
                "receivedAt": now,
            }
            tx.insert(
                RESET_MODULE,
                row,
                idempotency_key=idempotency_key,
                fail_once=fail_once if fail_once == "reset" else None,
                fail_reason=WRITE_FAILURE_REASONS["reset"],
            )

        return {
            "ok": True,
            "idempotencyKey": idempotency_key,
            "facilityCode": branch["facilityCode"],
            "observedAt": branch["observedAt"],
            "branchStatus": BRANCH_STATUS[branch["reasonCode"]],
            "reasonCode": branch["reasonCode"],
            "resettable": True,
            "conclusion": branch["conclusion"],
            "effectiveSource": branch["effectiveSource"],
            "conflict": branch["conflict"],
            "written": {"resetBranch": row["id"]},
            "message": f"已进入{BRANCH_STATUS[branch['reasonCode']]}可复位分支，未按正常排水处置",
            "result": row,
        }

    def _save_interruption(
        self,
        *,
        payload: dict[str, Any],
        branch: dict[str, Any],
        idempotency_key: str,
        reason_code: str,
        message: str,
        failed_write_point: str,
        existing_interruption_id: int | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with store.transaction() as tx:
            if existing_interruption_id is not None:
                interruption = store.find(INTERRUPTION_MODULE, existing_interruption_id)
                if interruption is None:
                    raise KeyError(f"中断记录 {existing_interruption_id} 不存在")
                interruption.update({
                    "reasonCode": reason_code,
                    "failedWritePoint": failed_write_point,
                    "message": message,
                    "updatedAt": now,
                })
            else:
                interruption = {
                    "id": tx.next_id(INTERRUPTION_MODULE),
                    "idempotencyKey": idempotency_key,
                    "设施编号": branch["facilityCode"],
                    "观测时点": branch["observedAt"],
                    "status": "待继续",
                    "reasonCode": reason_code,
                    "failedWritePoint": failed_write_point,
                    "payload": payload,
                    "message": message,
                    "createdAt": now,
                    "updatedAt": now,
                }
                tx.insert(INTERRUPTION_MODULE, interruption, idempotency_key=f"INTERRUPTION::{idempotency_key}")
                interruption["idempotencyKey"] = idempotency_key
        return self._interruption_response(interruption)

    def _classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        observations = payload.get("observations")
        if not isinstance(observations, dict):
            observations = {}
        monitoring = self._as_dict(observations.get("monitoring"))
        manual = self._as_dict(observations.get("manual"))

        observed_at = self._first_time(
            payload.get("observed_at"),
            manual.get("observed_at"),
            monitoring.get("observed_at"),
        )
        if not observed_at:
            raise ValueError("观测时点不能为空，重复回传需要按设施编号和观测时点幂等")

        facility_code = str(payload.get("facility_code") or manual.get("facility_code") or monitoring.get("facility_code") or "").strip()
        if not facility_code:
            return self._branch(
                payload,
                observations,
                monitoring,
                manual,
                observed_at,
                facility_code="",
                reason_code=RESET_FACILITY_CODE,
                conclusion=BRANCH_STATUS[RESET_FACILITY_CODE],
                effective_source="人工复核",
            )

        if self._is_retest_timeout(payload, manual):
            return self._branch(
                payload,
                observations,
                monitoring,
                manual,
                observed_at,
                facility_code,
                RESET_RETEST_TIMEOUT,
                BRANCH_STATUS[RESET_RETEST_TIMEOUT],
                effective_source="人工复测",
            )

        if self._is_pump_offline(payload, monitoring, manual):
            return self._branch(
                payload,
                observations,
                monitoring,
                manual,
                observed_at,
                facility_code,
                RESET_PUMP_OFFLINE,
                BRANCH_STATUS[RESET_PUMP_OFFLINE],
                effective_source=self._source(monitoring, manual),
            )

        monitoring_text = str(monitoring.get("conclusion") or "").strip()
        manual_text = str(manual.get("conclusion") or "").strip()
        effective_text = manual_text if manual_text else monitoring_text
        normalized = CONCLUSION_CODES.get(effective_text)
        if normalized is None:
            raise ValueError(f"处置结论「{effective_text or '空'}」不在允许范围内")

        return self._branch(
            payload,
            observations,
            monitoring,
            manual,
            observed_at,
            facility_code,
            normalized,
            effective_text,
            effective_source="人工复测" if manual_text else "监测",
        )

    def _branch(
        self,
        payload: dict[str, Any],
        observations: dict[str, Any],
        monitoring: dict[str, Any],
        manual: dict[str, Any],
        observed_at: str,
        facility_code: str,
        reason_code: str,
        conclusion: str,
        *,
        effective_source: str,
    ) -> dict[str, Any]:
        monitoring_text = str(monitoring.get("conclusion") or "").strip()
        manual_text = str(manual.get("conclusion") or "").strip()
        conflict = bool(monitoring_text and manual_text and monitoring_text != manual_text)
        return {
            "facilityCode": facility_code,
            "facilityType": str(payload.get("facility_type") or monitoring.get("facility_type") or "").strip(),
            "roadName": str(payload.get("road_name") or monitoring.get("road_name") or manual.get("road_name") or "").strip(),
            "station": str(payload.get("station") or monitoring.get("station") or manual.get("station") or "").strip(),
            "observedAt": observed_at,
            "reasonCode": reason_code,
            "conclusion": conclusion,
            "monitoringConclusion": monitoring_text or None,
            "manualConclusion": manual_text or None,
            "effectiveSource": effective_source,
            "conflict": conflict,
            "observations": observations,
        }

    def _is_retest_timeout(self, payload: dict[str, Any], manual: dict[str, Any]) -> bool:
        candidates = [payload.get("retest_status"), manual.get("retest_status"), manual.get("status")]
        return any(str(value or "").strip() in {"超时", "复测超时", "timeout", "TIMEOUT"} for value in candidates) or bool(
            payload.get("retest_timeout") or manual.get("timeout")
        )

    def _is_pump_offline(
        self,
        payload: dict[str, Any],
        monitoring: dict[str, Any],
        manual: dict[str, Any],
    ) -> bool:
        candidates = [
            payload.get("pump_status"),
            manual.get("pump_status"),
            monitoring.get("pump_status"),
        ]
        if any(str(value or "").strip().lower() in OFFLINE_VALUES for value in candidates):
            return True
        online_values = [
            payload.get("pump_online"),
            manual.get("pump_online"),
            monitoring.get("pump_online"),
        ]
        return any(value is False for value in online_values)

    def _source(self, monitoring: dict[str, Any], manual: dict[str, Any]) -> str:
        if manual:
            return "人工复测"
        if monitoring:
            return "监测"
        return "人工复核"

    def _find_active_interruption(self, idempotency_key: str) -> dict[str, Any] | None:
        for row in store.rows(INTERRUPTION_MODULE):
            if row.get("idempotencyKey") == idempotency_key and row.get("status") != "已继续":
                return self._interruption_response(row)
        return None

    def _interruption_response(self, interruption: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "interrupted": True,
            "resumable": True,
            "interruptionId": interruption["id"],
            "idempotencyKey": interruption.get("idempotencyKey"),
            "facilityCode": interruption.get("设施编号"),
            "observedAt": interruption.get("观测时点"),
            "reasonCode": interruption.get("reasonCode"),
            "failedWritePoint": interruption.get("failedWritePoint"),
            "message": interruption.get("message"),
        }

    def _find_idempotent(self, table: str, idempotency_key: str) -> dict[str, Any] | None:
        for row in store.rows(table):
            if row.get("idempotencyKey") == idempotency_key:
                return row
        return None

    def _idempotency_key(self, facility_code: str, observed_at: str, reason_code: str) -> str:
        code = facility_code or RESET_FACILITY_CODE
        return f"{code}@{observed_at}" if reason_code != RESET_FACILITY_CODE else f"{RESET_FACILITY_CODE}@{observed_at}"

    def _page_table(self, table: str, page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(table))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _first_time(self, *values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            if isinstance(value, datetime):
                return value.isoformat()
            text = str(value).strip()
            if text:
                return text
        return None

    def _as_dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
