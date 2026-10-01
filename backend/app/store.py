"""内存数据仓库：统一列表读写、幂等键与事务边界。

示例项目仍使用进程内数据；这里显式实现快照事务，业务服务可以把台账、待办、
清单和历史放在同一事务里提交，任一写入失败都会回滚整批。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator

from app.seed import SEED_ROWS


class StorageWriteError(RuntimeError):
    """事务内写入失败：携带固定原因码，供中断后继续处理。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class Transaction:
    def __init__(self, store: "Store") -> None:
        self._store = store

    def next_id(self, table: str) -> int:
        rows = self._store.rows(table)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def find_idempotent(self, table: str, idempotency_key: str) -> dict[str, Any] | None:
        for row in self._store.rows(table):
            if row.get("idempotencyKey") == idempotency_key:
                return row
        return None

    def insert(
        self,
        table: str,
        row: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        fail_once: str | None = None,
        fail_reason: str = "STORAGE_WRITE_FAILED",
    ) -> dict[str, Any]:
        if fail_once:
            self._store._consume_failure(fail_once, fail_reason)

        rows = self._store.rows(table)
        if idempotency_key:
            existing = self.find_idempotent(table, idempotency_key)
            if existing is not None:
                return existing
            row["idempotencyKey"] = idempotency_key
        rows.append(row)
        return row

    def upsert_by_key(
        self,
        table: str,
        business_key: str,
        key_field: str,
        values: dict[str, Any],
        *,
        fail_once: str | None = None,
        fail_reason: str = "STORAGE_WRITE_FAILED",
    ) -> dict[str, Any]:
        if fail_once:
            self._store._consume_failure(fail_once, fail_reason)

        rows = self._store.rows(table)
        for row in rows:
            if str(row.get(key_field) or "") == business_key:
                row.update(values)
                return row

        row = dict(values)
        row.setdefault("id", self.next_id(table))
        row[key_field] = business_key
        rows.append(row)
        return row


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = RLock()
        self._pending_failures: dict[str, str] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_key(self, module: str, key_field: str, key_value: str) -> dict[str, Any] | None:
        for row in self.rows(module):
            if str(row.get(key_field) or "") == key_value:
                return row
        return None

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield Transaction(self)
            except Exception:
                self._tables = snapshot
                raise

    def arm_write_failure(self, fail_once: str, reason_code: str) -> None:
        """测试/故障演练入口：只让指定的下一次写入失败。"""
        self._pending_failures[fail_once] = reason_code

    def _consume_failure(self, fail_once: str, reason_code: str) -> None:
        configured = self._pending_failures.pop(fail_once, None)
        if configured is None:
            return
        raise StorageWriteError(configured, f"写入点 {fail_once} 失败：{configured}")

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
